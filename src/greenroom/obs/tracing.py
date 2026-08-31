"""OpenTelemetry → Cloud Trace.

https://docs.cloud.google.com/trace/docs/setup/python-ot
https://opentelemetry.io/docs/languages/python/instrumentation/

One trace per inbound message or tick, one span per agent, one span per tool call.
Each span carries a short input/output summary, so the reasoning chain is legible in
Cloud Trace without having to reconstruct it from logs.

Two deliberate constraints:

  * **Observability must never break the request path.** Every function here swallows
    its own failures. A tracing outage should cost visibility, not a booking.
  * **Span attributes are summaries, never payloads.** Inbound email is untrusted and
    a pitch is a customer's data; neither belongs in a trace backend. Text is truncated
    hard, and the raw body of an inbound message is never attached at all.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from greenroom.obs.logging import get_logger
from greenroom.settings import get_settings

log = get_logger(__name__)

_configured = False

# Long enough to be useful in a trace viewer, short enough that nobody is tempted to
# treat Cloud Trace as a data store.
MAX_ATTR_CHARS = 400


def configure_tracing(app: Any = None) -> bool:
    """Install the Cloud Trace exporter. Returns whether tracing is live.

    Idempotent, and a no-op without a project id — which keeps unit tests and local
    dry-runs from opening an exporter that has nowhere to send.
    """
    global _configured
    if _configured:
        return True

    settings = get_settings()
    if not settings.google_cloud_project:
        log.info("tracing disabled: no GOOGLE_CLOUD_PROJECT")
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(
            resource=Resource.create(
                {
                    "service.name": "greenroom",
                    "service.version": __import__("greenroom").__version__,
                }
            )
        )
        provider.add_span_processor(
            BatchSpanProcessor(CloudTraceSpanExporter(project_id=settings.google_cloud_project))
        )
        trace.set_tracer_provider(provider)

        if app is not None:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            # /health is hit constantly by Cloud Run and would drown the real traces.
            FastAPIInstrumentor.instrument_app(app, excluded_urls="health,readyz")

        _configured = True
        log.info("tracing configured", extra={"project": settings.google_cloud_project})
        return True
    except Exception as exc:
        log.warning("tracing unavailable", extra={"error": str(exc)[:200]})
        return False


def _tracer():
    from opentelemetry import trace

    return trace.get_tracer("greenroom")


def _clean(value: Any) -> str:
    return " ".join(str(value).split())[:MAX_ATTR_CHARS]


@contextmanager
def span(name: str, *, kind: str = "step", **attributes: Any):
    """Open a span. Never raises on a tracing failure; always re-raises the body's."""
    try:
        tracer = _tracer()
    except Exception:
        yield _NullSpan()
        return

    with tracer.start_as_current_span(name) as otel_span:
        wrapper = _Span(otel_span)
        wrapper.set("greenroom.kind", kind)
        for key, value in attributes.items():
            if value is not None:
                wrapper.set(f"greenroom.{key}", value)
        try:
            yield wrapper
        except Exception as exc:
            wrapper.set("greenroom.error", f"{type(exc).__name__}: {exc}")
            try:
                otel_span.record_exception(exc)
            except Exception:
                pass
            raise


def agent_span(agent: str, **attributes: Any):
    """One span per agent invocation — Researcher, Writer, Gatekeeper, Negotiator."""
    return span(f"agent.{agent}", kind="agent", agent=agent, **attributes)


def tool_span(tool: str, **attributes: Any):
    """One span per tool call — Gmail send, Calendar create, image generation."""
    return span(f"tool.{tool}", kind="tool", tool=tool, **attributes)


def current_trace_id() -> str:
    """The active trace id as hex, for storing alongside a Firestore record."""
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        return f"{ctx.trace_id:032x}" if ctx.is_valid else ""
    except Exception:
        return ""


def trace_url(trace_id: str) -> str:
    """A link a human can actually click, from a trace id."""
    project = get_settings().google_cloud_project
    if not (trace_id and project):
        return ""
    return f"https://console.cloud.google.com/traces/list?project={project}&tid={trace_id}"


class _Span:
    def __init__(self, otel_span: Any) -> None:
        self._span = otel_span

    def set(self, key: str, value: Any) -> None:
        try:
            self._span.set_attribute(key, _clean(value))
        except Exception:
            pass

    def summarise(self, **attributes: Any) -> None:
        """Record the outputs of a step. Summaries only — never a raw payload."""
        for key, value in attributes.items():
            if value is not None:
                self.set(f"greenroom.{key}", value)


class _NullSpan:
    def set(self, *_args: Any, **_kwargs: Any) -> None: ...
    def summarise(self, **_kwargs: Any) -> None: ...
