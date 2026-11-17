"""Pluggable signal connectors.

Built-ins register themselves on import; third-party connectors register through the
``opsmith.connectors`` entry-point group, so new sources need no pipeline changes.
"""

from __future__ import annotations

from opsmith.connectors.base import (
    ConnectorError,
    FetchResult,
    LogConnector,
    MetricConnector,
    available_types,
    build_connector,
    register,
)
from opsmith.connectors.files import FileLogConnector, FileMetricConnector
from opsmith.connectors.loki import LokiConnector
from opsmith.connectors.prometheus import PrometheusConnector
from opsmith.connectors.push import PushLogConnector, PushMetricConnector
from opsmith.connectors.victorialogs import VictoriaLogsConnector

__all__ = [
    "ConnectorError",
    "FetchResult",
    "FileLogConnector",
    "FileMetricConnector",
    "LogConnector",
    "LokiConnector",
    "MetricConnector",
    "PrometheusConnector",
    "PushLogConnector",
    "PushMetricConnector",
    "VictoriaLogsConnector",
    "available_types",
    "build_connector",
    "register",
]
