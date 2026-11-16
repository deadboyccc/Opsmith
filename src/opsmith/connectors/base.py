"""Connector interface + registry.

Connector configuration is written by a tenant admin and runs on a worker shared by every
tenant, so this module also holds the limits that keep one connector from reaching things it
should not or from exhausting the worker: base-URL validation, a response size cap and a
timeout ceiling. File connectors are confined separately (see ``opsmith.localfs``).
"""

from __future__ import annotations

import inspect
import ipaddress
import json
import logging
import math
import re
import socket
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx

from opsmith.schemas import LogEvent, MetricPoint

log = logging.getLogger(__name__)

MAX_RESPONSE_BYTES = 64 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_TIMEOUT_SECONDS = 120.0

# Maps a host name to the addresses it resolves to. Injectable so tests never touch DNS.
Resolver = Callable[[str], Iterable[str]]

# scheme://host[:port][/path] and nothing else: no userinfo, query, fragment, whitespace or
# backslashes, so the URL means the same thing to this check and to the HTTP client.
_BASE_URL = re.compile(r"https?://[A-Za-z0-9._\-\[\]:]+(?:/[A-Za-z0-9._~%/\-]*)?", re.IGNORECASE)
# Cloud metadata endpoints outside the link-local ranges.
_METADATA_ADDRESSES = frozenset({ipaddress.ip_address("fd00:ec2::254")})


class ConnectorError(RuntimeError):
    pass


@dataclass
class FetchResult:
    logs: list[LogEvent] = field(default_factory=list)
    metrics: list[MetricPoint] = field(default_factory=list)
    cursor: dict[str, Any] = field(default_factory=dict)
    # Records the source returned that could not be parsed. They are left out, not fatal.
    skipped: int = 0


def _system_resolver(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise ConnectorError(f"cannot resolve connector host {host!r}") from exc
    return sorted({str(info[4][0]) for info in infos})


def _refused(address: str) -> bool:
    """Link-local and metadata addresses (169.254.0.0/16, fe80::/10) are never reachable.

    Loopback and private ranges stay allowed: self-hosted Loki/Prometheus live there.
    """
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return True  # a resolver answer that is not an address cannot be vetted
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_link_local or ip in _METADATA_ADDRESSES


def check_host_reachable(host: str, resolver: Resolver | None = None) -> None:
    """Raise ``ConnectorError`` if ``host`` is, or resolves to, an address Opsmith refuses to
    call (link-local and cloud metadata). Shared by connectors and webhook delivery."""
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        addresses = list((resolver or _system_resolver)(host))
    if not addresses:
        raise ConnectorError(f"cannot resolve host {host!r}")
    if any(_refused(address) for address in addresses):
        raise ConnectorError(f"host {host!r} is a link-local or metadata address")


def validate_base_url(url: Any, resolver: Resolver | None = None) -> str:
    """Return the connector base URL without a trailing slash, or raise ``ConnectorError``."""
    if not isinstance(url, str) or not url:
        raise ConnectorError("connector config needs a 'url'")
    if not _BASE_URL.fullmatch(url):
        raise ConnectorError(
            "connector url must be http(s)://host[:port][/path] without credentials, "
            "query or fragment"
        )
    try:
        parts = urlsplit(url)
        host = parts.hostname
        _ = parts.port  # raises on a malformed port
    except ValueError as exc:
        raise ConnectorError(f"connector url is not valid: {exc}") from None
    if not host:
        raise ConnectorError("connector url has no host")
    check_host_reachable(host, resolver)
    return url.rstrip("/")


def supported_keywords(target: Callable[..., Any], optional: Mapping[str, Any]) -> dict[str, Any]:
    """The subset of ``optional`` keyword arguments that ``target`` accepts.

    Lets newer optional arguments reach the connectors that understand them without breaking
    out-of-tree connector classes (or factories) written against the older signature.
    """
    try:
        params = inspect.signature(target).parameters.values()
    except (TypeError, ValueError):
        return {}
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
        return dict(optional)
    names = {p.name for p in params if p.kind is not inspect.Parameter.POSITIONAL_ONLY}
    return {key: value for key, value in optional.items() if key in names}


class BaseConnector(ABC):
    """A read-only source of signals. Connectors never write to the systems they read."""

    type_name: ClassVar[str]
    kind: ClassVar[str]  # logs | metrics
    # True for connectors whose config carries a base ``url``; it is validated on construction.
    uses_url: ClassVar[bool] = False

    def __init__(
        self,
        config: dict[str, Any],
        *,
        default_service: str = "unknown",
        credential: str | None = None,
        http: httpx.Client | None = None,
        local_root: Path | None = None,
        local_deny: tuple[Path, ...] = (),
        resolver: Resolver | None = None,
    ) -> None:
        self.config = config
        self.default_service = default_service
        self.credential = credential
        self._http = http
        self._owns_http = http is None  # a client handed in belongs to the caller
        self.local_root = local_root
        self.local_deny = tuple(local_deny)
        self._resolver = resolver
        if self.uses_url:
            self.base_url()

    @property
    def timeout(self) -> float:
        raw = self.config.get("timeout", DEFAULT_TIMEOUT_SECONDS)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ConnectorError("connector 'timeout' must be a number of seconds") from None
        if not math.isfinite(value) or value <= 0:
            raise ConnectorError("connector 'timeout' must be a positive number of seconds")
        return min(value, MAX_TIMEOUT_SECONDS)

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            headers = {"Authorization": f"Bearer {self.credential}"} if self.credential else {}
            self._http = httpx.Client(timeout=self.timeout, headers=headers)
        return self._http

    def close(self) -> None:
        """Release the HTTP client this connector opened. Called after every run."""
        # getattr: a connector whose own ``__init__`` does not call this class's has neither.
        client = getattr(self, "_http", None)
        if getattr(self, "_owns_http", False) and client is not None:
            client.close()
            self._http = None

    @property
    def environment(self) -> str:
        """Environment for records that carry none: the connector's ``environment`` setting
        (the linked service's environment when the connector does not set one)."""
        return str(self.config.get("environment") or "production")

    def seconds(self, key: str, default: float, maximum: float = 30 * 86_400.0) -> float:
        """A duration from the configuration, or ``default`` when the value is not a number
        between zero and ``maximum`` (a larger one overflows a ``timedelta``)."""
        try:
            value = float(self.config.get(key, default))
        except (TypeError, ValueError):
            return default
        return value if 0 <= value <= maximum else default  # False for NaN too

    def base_url(self) -> str:
        """The validated base URL. Called on construction and again before every fetch, so
        a host that starts resolving to a refused address is caught."""
        return validate_base_url(self.config.get("url"), self._resolver)

    def get_json(self, url: str, params: Mapping[str, str]) -> dict[str, Any]:
        """GET a JSON object with a hard size cap.

        Errors carry the status code or the kind of failure only, never the response body:
        the text ends up in job errors a tenant can read.
        """
        chunks: list[bytes] = []
        size = 0
        try:
            with self.http.stream("GET", url, params=dict(params)) as resp:
                if not resp.is_success:
                    raise ConnectorError(f"{self.type_name} returned HTTP {resp.status_code}")
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise ConnectorError(
                            f"{self.type_name} response is larger than {MAX_RESPONSE_BYTES} bytes"
                        )
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise ConnectorError(f"{self.type_name} request failed: {type(exc).__name__}") from exc
        try:
            body = json.loads(b"".join(chunks))
        except ValueError:
            raise ConnectorError(f"{self.type_name} response is not valid JSON") from None
        if not isinstance(body, dict):
            raise ConnectorError(f"{self.type_name} response is not a JSON object")
        return body

    @abstractmethod
    def fetch(self, start: datetime, end: datetime, cursor: dict[str, Any]) -> FetchResult:
        """Return signals in ``[start, end)``. Metric connectors may include a baseline
        look-back before ``start`` (points earlier than ``start`` are treated as baseline).

        A connector that could not read the whole window returns ``cursor["last_end"]`` set
        to where it stopped; the next run continues from there."""


class LogConnector(BaseConnector, ABC):
    kind = "logs"


class MetricConnector(BaseConnector, ABC):
    kind = "metrics"


_REGISTRY: dict[str, type[BaseConnector]] = {}
_ENTRYPOINTS_LOADED = False


def register(type_name: str) -> Callable[[type[BaseConnector]], type[BaseConnector]]:
    def deco(cls: type[BaseConnector]) -> type[BaseConnector]:
        cls.type_name = type_name
        _REGISTRY[type_name] = cls
        return cls

    return deco


def _load_entrypoints() -> None:
    global _ENTRYPOINTS_LOADED  # noqa: PLW0603
    if _ENTRYPOINTS_LOADED:
        return
    _ENTRYPOINTS_LOADED = True
    for ep in entry_points(group="opsmith.connectors"):
        try:
            cls = ep.load()
            if not getattr(cls, "type_name", None):
                cls.type_name = ep.name  # what ``@register`` sets; error messages use it
            _REGISTRY.setdefault(ep.name, cls)
        except Exception:  # a broken plugin must not take the platform down
            log.exception("failed to load connector plugin", extra={"plugin": ep.name})


def available_types() -> dict[str, str]:
    _load_entrypoints()
    return {name: cls.kind for name, cls in sorted(_REGISTRY.items())}


def build_connector(
    type_name: str,
    config: dict[str, Any],
    *,
    default_service: str = "unknown",
    credential: str | None = None,
    http: httpx.Client | None = None,
    local_root: Path | None = None,
    local_deny: tuple[Path, ...] = (),
    resolver: Resolver | None = None,
) -> BaseConnector:
    _load_entrypoints()
    cls = _REGISTRY.get(type_name)
    if cls is None:
        raise ConnectorError(f"unknown connector type {type_name!r}")
    optional = supported_keywords(
        cls, {"local_root": local_root, "local_deny": local_deny, "resolver": resolver}
    )
    return cls(
        config, default_service=default_service, credential=credential, http=http, **optional
    )
