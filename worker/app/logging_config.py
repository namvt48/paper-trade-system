"""Structured logging configuration for paper-trade-system.

Emits JSON to stdout only (for Promtail/Loki collection).
No file handler — logs are collected centrally via Docker container logs.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from pythonjsonlogger import jsonlogger


class ServiceJsonFormatter(jsonlogger.JsonFormatter):
    """JSON formatter that injects service name and ISO timestamp."""

    def __init__(self, service_name: str, *args, **kwargs):
        self._service_name = service_name
        super().__init__(*args, **kwargs)

    def add_fields(self, log_data, record, message_dict):
        super().add_fields(log_data, record, message_dict)
        log_data["service"] = self._service_name
        log_data["timestamp"] = datetime.now(timezone.utc).isoformat()
        log_data["level"] = record.levelname
        log_data["logger"] = record.name


def configure_structured_logging(
    service_name: str,
    log_level: str,
) -> None:
    """Configure root logger with JSON stdout handler only.

    Args:
        service_name: Service identifier (e.g. "paper-trade").
        log_level: Log level string (e.g. "INFO", "DEBUG").
    """
    app_level = getattr(logging, log_level.upper(), logging.INFO)

    json_formatter = ServiceJsonFormatter(
        service_name,
        "%(timestamp)s %(level)s %(logger)s %(message)s",
    )
    stdout_handler = logging.StreamHandler()
    stdout_handler.setFormatter(json_formatter)

    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    root.handlers = [stdout_handler]

    for name in ("app", "__main__", "worker"):
        logging.getLogger(name).setLevel(app_level)
