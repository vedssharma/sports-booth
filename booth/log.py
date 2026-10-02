"""
Logging for the booth: one `booth` logger tree, human-readable by default, JSON lines on request.

Context binding: wrap work in `with bind(event_id=..., game_id=...)` and every record logged inside
it — including from tasks it spawns, since asyncio copies the context — carries those fields, so a
single event can be followed through scheduling, the policy decision, three agent runs and the
broadcast with one grep / one JSON query.
"""
import contextlib
import contextvars
import json
import logging
import sys
from datetime import datetime, timezone

_context: contextvars.ContextVar[dict] = contextvars.ContextVar("booth_log_context", default={})

# Attributes every LogRecord has; anything else on a record came from `extra=` or bind()
_STANDARD = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime", "taskName"}


@contextlib.contextmanager
def bind(**fields):
    """Attach fields to every log record emitted in this (async) context."""
    token = _context.set({**_context.get(), **{k: v for k, v in fields.items() if v is not None}})
    try:
        yield
    finally:
        _context.reset(token)


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in _context.get().items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


def _extras(record: logging.LogRecord) -> dict:
    return {k: v for k, v in vars(record).items() if k not in _STANDARD and not k.startswith("_")}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
            **_extras(record),
        }
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


class TextFormatter(logging.Formatter):
    _ICONS = {"WARNING": "⚠️  ", "ERROR": "✗  "}

    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        extras = " ".join(f"{k}={v}" for k, v in _extras(record).items())
        line = f"{stamp} {self._ICONS.get(record.levelname, '')}{record.getMessage()}"
        if extras:
            line += f"  [{extras}]"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def setup_logging(level: str = "INFO", fmt: str = "text", stream=None) -> None:
    """Configure the `booth` logger. Safe to call more than once (replaces its handler)."""
    logger = logging.getLogger("booth")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    handler.addFilter(_ContextFilter())
    logger.addHandler(handler)
    logger.setLevel(level.upper())
    logger.propagate = False


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"booth.{name}")
