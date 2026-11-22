"""OTLP/HTTP log exporter with JSON encoding, set by `[otel] exporter` in the user config.

Each flush sends the queued events as OTLP log records in one `POST <endpoint>`
with an `ExportLogsServiceRequest` JSON body.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping
from urllib.parse import urlsplit

import httpx

from meltzer import __version__
from meltzer.config.loader import ConfigError

if TYPE_CHECKING:
    from meltzer.telemetry.events import TelemetryEvent

EXPORTER_NONE = "none"
OTLP_HTTP = "otlp-http"
OTLP_HTTP_KEYS = {"endpoint", "headers", "protocol"}
REQUEST_TIMEOUT = 10.0
SEVERITY_INFO = 9


class OtlpExportError(Exception):
    """The collector could not be reached or rejected the events."""


@dataclass(frozen=True)
class OtlpHttpSettings:
    endpoint: str
    headers: dict[str, str]


def parse_exporter(otel: Mapping[str, Any]) -> OtlpHttpSettings | None:
    """The exporter set by the `[otel]` table, or None for `exporter = "none"` (the default)."""
    exporter = otel.get("exporter", EXPORTER_NONE)
    if exporter == EXPORTER_NONE:
        return None
    if not isinstance(exporter, dict) or set(exporter) != {OTLP_HTTP} or not isinstance(exporter[OTLP_HTTP], dict):
        raise ConfigError('otel.exporter must be "none" or { otlp-http = { endpoint = "..." } }')
    entry = exporter[OTLP_HTTP]
    unknown = sorted(set(entry) - OTLP_HTTP_KEYS)
    if unknown:
        raise ConfigError(f"unknown otel.exporter.otlp-http keys: {', '.join(unknown)}")
    endpoint = entry.get("endpoint")
    try:
        parts = urlsplit(endpoint) if isinstance(endpoint, str) else None
    except ValueError:
        parts = None
    if parts is None or parts.scheme not in ("http", "https") or not parts.netloc:
        raise ConfigError(f"otel.exporter.otlp-http.endpoint must be an http or https URL, got {endpoint!r}")
    headers = entry.get("headers", {})
    if not isinstance(headers, dict) or not all(isinstance(v, str) for v in headers.values()):
        raise ConfigError("otel.exporter.otlp-http.headers must be a table of strings")
    protocol = entry.get("protocol", "json")
    if protocol != "json":
        raise ConfigError(f'otel.exporter.otlp-http.protocol {protocol!r} is not supported; use "json"')
    return OtlpHttpSettings(endpoint=endpoint, headers=dict(headers))


def _value(value: str | int | bool) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        # OTLP JSON encodes 64-bit integers as strings.
        return {"intValue": str(value)}
    return {"stringValue": str(value)}


def _attributes(values: Mapping[str, str | int | bool]) -> list[dict[str, Any]]:
    return [{"key": key, "value": _value(value)} for key, value in values.items()]


def export_request(events: list[TelemetryEvent]) -> dict[str, Any]:
    """An OTLP `ExportLogsServiceRequest` with one log record per event."""
    records = [
        {
            "timeUnixNano": str(event.time_unix_nano),
            "severityNumber": SEVERITY_INFO,
            "severityText": "INFO",
            "body": {"stringValue": event.name},
            "attributes": _attributes({"event.name": event.name, **event.attributes}),
        }
        for event in events
    ]
    resource = {"attributes": _attributes({"service.name": "meltzer", "service.version": __version__})}
    scope = {"name": "meltzer", "version": __version__}
    return {"resourceLogs": [{"resource": resource, "scopeLogs": [{"scope": scope, "logRecords": records}]}]}


class OtlpLogExporter:
    def __init__(self, settings: OtlpHttpSettings, *, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self._pending: list[TelemetryEvent] = []
        self._lock = threading.Lock()
        self._http = httpx.Client(
            timeout=REQUEST_TIMEOUT,
            transport=transport,
            headers={"User-Agent": f"meltzer/{__version__}", **settings.headers},
        )

    def add(self, event: TelemetryEvent) -> None:
        with self._lock:
            self._pending.append(event)

    def flush(self) -> None:
        """Send the queued events. Raises OtlpExportError; the events are not sent again."""
        with self._lock:
            events, self._pending = self._pending, []
        if not events:
            return
        try:
            response = self._http.post(self.settings.endpoint, json=export_request(events))
        except httpx.HTTPError as exc:
            raise OtlpExportError(f"{self.settings.endpoint}: {exc}") from exc
        if response.status_code >= 400:
            raise OtlpExportError(f"{self.settings.endpoint} returned HTTP {response.status_code}")

    def close(self) -> None:
        self._http.close()
