"""Connector interface + registry."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from importlib.metadata import entry_points
from typing import Any, ClassVar

import httpx

from opsmith.schemas import LogEvent, MetricPoint

log = logging.getLogger(__name__)


class ConnectorError(RuntimeError):
    pass


@dataclass
class FetchResult:
    logs: list[LogEvent] = field(default_factory=list)
    metrics: list[MetricPoint] = field(default_factory=list)
    cursor: dict[str, Any] = field(default_factory=dict)


class BaseConnector(ABC):
    """A read-only source of signals. Connectors never write to the systems they read."""

    type_name: ClassVar[str]
    kind: ClassVar[str]  # logs | metrics

    def __init__(
        self,
        config: dict[str, Any],
        *,
        default_service: str = "unknown",
        credential: str | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        self.config = config
        self.default_service = default_service
        self.credential = credential
        self._http = http

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            headers = {"Authorization": f"Bearer {self.credential}"} if self.credential else {}
            self._http = httpx.Client(
                timeout=float(self.config.get("timeout", 30)), headers=headers
            )
        return self._http

    @abstractmethod
    def fetch(self, start: datetime, end: datetime, cursor: dict[str, Any]) -> FetchResult:
        """Return signals in ``[start, end)``. Metric connectors may include a baseline
        look-back before ``start`` (points earlier than ``start`` are treated as baseline)."""


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
) -> BaseConnector:
    _load_entrypoints()
    cls = _REGISTRY.get(type_name)
    if cls is None:
        raise ConnectorError(f"unknown connector type {type_name!r}")
    return cls(config, default_service=default_service, credential=credential, http=http)
