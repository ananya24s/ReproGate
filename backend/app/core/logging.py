"""Structured logging configuration.

Logging is configured once, at application startup, via :func:`configure_logging`.
Every module obtains its logger with :func:`get_logger` and never configures
handlers of its own.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

_RESERVED_RECORD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
) | {"asctime", "message", "taskName"}

_CONSOLE_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key not in _RESERVED_RECORD_ATTRS:
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", log_format: str = "json") -> None:
    """Install a single stdout handler on the root logger.

    Args:
        level: Minimum level emitted by the application.
        log_format: ``"json"`` for machine-readable output, ``"console"`` for
            human-readable local development output.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter()
        if log_format == "json"
        else logging.Formatter(_CONSOLE_FORMAT)
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Uvicorn ships its own handlers; drop them so all output flows through ours.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True


def get_logger(name: str) -> logging.Logger:
    """Return the named application logger."""
    return logging.getLogger(name)
