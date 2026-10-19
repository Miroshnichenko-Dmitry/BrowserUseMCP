"""Single logging configuration for the whole process (ours + browser-use + uvicorn).

Logs go to stderr: required for stdio transport and the Docker convention for HTTP.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime

# uvicorn attaches `color_message` (the message with ANSI codes) to its records; never print it.
_STD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "color_message",
}


def prepare_environment() -> None:
    """Must run before `browser_use` is imported anywhere."""
    # browser-use reconfigures the root logger on import unless told not to.
    os.environ.setdefault("BROWSER_USE_SETUP_LOGGING", "false")
    # No anonymous telemetry / cloud sync from a self-hosted server unless explicitly enabled.
    os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
    os.environ.setdefault("BROWSER_USE_CLOUD_SYNC", "false")
    # FastMCP installs its own Rich handler unless disabled; we want one format for everything.
    os.environ.setdefault("FASTMCP_LOG_ENABLED", "false")
    os.environ.setdefault("FASTMCP_CHECK_FOR_UPDATES", "off")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s [%(name)s] %(message)s", "%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {k: v for k, v in record.__dict__.items() if k not in _STD_ATTRS and not k.startswith("_")}
        if extras:
            base += " " + " ".join(f"{k}={v}" for k, v in extras.items())
        return base


def configure_logging(level: str = "INFO", fmt: str = "text") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    for name in ("browser_use", "bubus", "cdp_use", "fastmcp", "uvicorn", "uvicorn.error", "uvicorn.access"):
        lib_logger = logging.getLogger(name)
        lib_logger.handlers = []
        lib_logger.propagate = True
    # Chatty internals: keep them at WARNING unless we are debugging.
    if level != "DEBUG":
        for name in ("bubus", "cdp_use", "websockets", "httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.WARNING)
