"""Structured JSON logging to Cloud Logging.

Cloud Run picks up JSON on stdout and parses it into structured LogEntry fields, so
there is no logging agent or client library to configure — the contract is just the
field names below.
https://docs.cloud.google.com/run/docs/logging#using-json

Every line carries target_id and thread_id where we have them, so a whole
conversation can be pulled out of Cloud Logging with one filter.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Any

from pythonjsonlogger import json as jsonlogger

# Request-scoped context. Set once at the top of an inbound/tick handler and every
# log line beneath it inherits the ids without threading them through call signatures.
_target_id: ContextVar[str | None] = ContextVar("target_id", default=None)
_thread_id: ContextVar[str | None] = ContextVar("thread_id", default=None)
_job_id: ContextVar[str | None] = ContextVar("job_id", default=None)

# Cloud Logging expects these exact severity strings.
_SEVERITY = {
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "WARNING": "WARNING",
    "ERROR": "ERROR",
    "CRITICAL": "CRITICAL",
}


def set_log_context(
    *, target_id: str | None = None, thread_id: str | None = None, job_id: str | None = None
) -> None:
    """Attach ids to every subsequent log line in this async context."""
    if target_id is not None:
        _target_id.set(target_id)
    if thread_id is not None:
        _thread_id.set(thread_id)
    if job_id is not None:
        _job_id.set(job_id)


class CloudLoggingFormatter(jsonlogger.JsonFormatter):
    def add_fields(
        self, log_record: dict[str, Any], record: logging.LogRecord, message_dict: dict[str, Any]
    ) -> None:
        super().add_fields(log_record, record, message_dict)
        # Cloud Logging reads `severity`, not `levelname`.
        log_record["severity"] = _SEVERITY.get(record.levelname, record.levelname)
        log_record.pop("levelname", None)
        log_record["logger"] = record.name

        for key, var in (("target_id", _target_id), ("thread_id", _thread_id), ("job_id", _job_id)):
            value = var.get()
            if value:
                log_record[key] = value

        # Correlate a log line with its Cloud Trace span when one is active.
        span = _current_span_ids()
        if span:
            log_record["logging.googleapis.com/trace"] = span[0]
            log_record["logging.googleapis.com/spanId"] = span[1]


def _current_span_ids() -> tuple[str, str] | None:
    """(trace, spanId) for the active OTel span, in the format Cloud Logging wants."""
    try:
        from opentelemetry import trace as otel_trace

        from greenroom.settings import get_settings

        ctx = otel_trace.get_current_span().get_span_context()
        if not ctx.is_valid:
            return None
        project = get_settings().google_cloud_project
        if not project:
            return None
        return (
            f"projects/{project}/traces/{ctx.trace_id:032x}",
            f"{ctx.span_id:016x}",
        )
    except Exception:  # observability must never break the request path
        return None


def configure_logging(level: int = logging.INFO) -> None:
    """Install the JSON formatter on the root logger. Idempotent."""
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(CloudLoggingFormatter("%(message)s %(name)s %(levelname)s"))
    root.addHandler(handler)

    # These are chatty and tell us nothing we do not already log ourselves.
    for noisy in ("google.auth", "google.api_core", "urllib3", "httpx", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# Attributes the stdlib puts on every LogRecord. Passing any of these in `extra=`
# raises KeyError inside Logger.makeRecord — which means a log line can 500 a request.
# We hit exactly that in production with extra={"created": n}: `created` is the record's
# own timestamp. Observability must never break the request path, so collisions are
# renamed rather than raised.
_RESERVED_LOG_KEYS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class SafeExtraLogger(logging.Logger):
    """A Logger that renames reserved keys in `extra` instead of raising on them."""

    def makeRecord(
        self, name, level, fn, lno, msg, args, exc_info, func=None, extra=None, sinfo=None
    ):
        if extra:
            extra = {(f"x_{k}" if k in _RESERVED_LOG_KEYS else k): v for k, v in extra.items()}
        return super().makeRecord(name, level, fn, lno, msg, args, exc_info, func, extra, sinfo)


logging.setLoggerClass(SafeExtraLogger)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
