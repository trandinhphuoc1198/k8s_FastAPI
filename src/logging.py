import json
import logging
import sys
import time
import uuid
from contextvars import ContextVar

# Per-request context: store a unique request ID so all log lines
# emitted during a single request share the same ID.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class JSONFormatter(logging.Formatter):
    """Emit every log record as a single-line JSON object."""

    RESERVED = {"message", "asctime", "levelname", "name", "request_id"}

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "timestamp": self.formatTime(record, self.datefmt or "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": request_id_var.get("-"),
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in {
                        "name", "msg", "args", "levelname", "levelno",
                        "pathname", "filename", "module", "exc_info",
                        "exc_text", "stack_info", "lineno", "funcName",
                        "created", "msecs", "relativeCreated", "thread",
                        "threadName", "processName", "process"
                        }:
                continue  # skip standard noisy fields
            if key.startswith("_"):
                continue

            payload[key] = value

        # Attach exception info when present
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def get_logger(name: str) -> logging.Logger:
    """Return a logger that writes structured JSON to stdout."""
    logger = logging.getLogger(name)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JSONFormatter())
        logger.addHandler(handler)
        logger.propagate = False

    return logger


def configure_root_logging(level: str = "INFO") -> None:
    """
    Call once at startup (e.g. in main.py) to:
    - Set the root logger level
    - Replace uvicorn's default formatter with the JSON formatter
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.root.setLevel(numeric_level)

    json_formatter = JSONFormatter()

    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        uv_logger = logging.getLogger(name)
        for handler in uv_logger.handlers:
            handler.setFormatter(json_formatter)