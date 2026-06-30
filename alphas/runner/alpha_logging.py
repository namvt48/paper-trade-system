from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


_SAFE_ALPHA_ID = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_alpha_log_name(alpha_id: Any) -> str:
    value = str(alpha_id or "").strip()
    value = _SAFE_ALPHA_ID.sub("_", value)
    return value[:160] or "unknown"


class AlphaFileLogHandler(logging.Handler):
    """Routes records carrying extra={"alpha_id": "..."} into per-alpha files."""

    def __init__(
        self,
        log_dir: str | Path,
        *,
        max_bytes: int = 20_000_000,
        backup_count: int = 5,
    ):
        super().__init__()
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.max_bytes = int(max_bytes)
        self.backup_count = int(backup_count)
        self._handlers: dict[str, RotatingFileHandler] = {}

    def emit(self, record: logging.LogRecord) -> None:
        alpha_id = getattr(record, "alpha_id", None)
        if not alpha_id:
            return
        try:
            handler = self._handler_for(alpha_id)
            handler.emit(record)
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        for handler in self._handlers.values():
            handler.close()
        self._handlers.clear()
        super().close()

    def _handler_for(self, alpha_id: Any) -> RotatingFileHandler:
        name = safe_alpha_log_name(alpha_id)
        handler = self._handlers.get(name)
        if handler is not None:
            return handler
        handler = RotatingFileHandler(
            self.log_dir / f"{name}.log",
            maxBytes=self.max_bytes,
            backupCount=self.backup_count,
            encoding="utf-8",
        )
        handler.setLevel(self.level)
        if self.formatter is not None:
            handler.setFormatter(self.formatter)
        self._handlers[name] = handler
        return handler
