"""Structured logging: one JSON object per line on stdout.

Log with an event name as the message and the facts as `extra` fields, e.g.

    logger.info("model_call", extra={"purpose": "ocr", "status_code": 429})

Never put request bodies, model prompts or responses, document text, API keys
or Authorization headers into a log record.
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

# Set by the HTTP middleware so every record written during a request carries it.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

# Attributes every LogRecord has; anything else on a record came from `extra`.
_STANDARD_ATTRIBUTES = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None) or request_id_var.get()
        if request_id:
            payload["request_id"] = request_id
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRIBUTES and key not in payload:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Send all logging, including uvicorn's and celery's, to stdout as JSON."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "celery"):
        logger = logging.getLogger(name)
        logger.handlers[:] = []
        logger.propagate = True
    # uvicorn's access log duplicates the http_request records written by the app.
    logging.getLogger("uvicorn.access").disabled = True
    # httpx logs every request URL at INFO; model_call already records these.
    logging.getLogger("httpx").setLevel(logging.WARNING)
