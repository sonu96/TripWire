"""Optional OpenTelemetry distributed tracing for TripWire.

All OTel imports are guarded by try/except ImportError so the application
works identically whether or not the ``otel`` extras are installed.
When OTel is not available, a lightweight ``NullTracer`` provides the same
``start_as_current_span`` interface as the real tracer, returning a no-op
context manager.  This means call-sites never need to check availability.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# StatusCode (real OTel enum when available, lightweight fallback otherwise)
# ---------------------------------------------------------------------------

try:
    from opentelemetry.trace import StatusCode
except ImportError:
    import enum

    class StatusCode(enum.Enum):  # type: ignore[no-redef]
        UNSET = 0
        OK = 1
        ERROR = 2

# ---------------------------------------------------------------------------
# Null / no-op fallback (used when OTel is not installed)
# ---------------------------------------------------------------------------


class _NullSpan:
    """Minimal stand-in for ``opentelemetry.trace.Span``."""

    def set_attribute(self, key: str, value: Any) -> None:  # noqa: ARG002
        pass

    def set_status(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
        pass

    def record_exception(self, exception: BaseException, **kwargs: Any) -> None:  # noqa: ARG002
        pass

    def __enter__(self) -> "_NullSpan":
        return self

    def __exit__(self, *args: Any) -> None:
        pass


class NullTracer:
    """Drop-in replacement for ``opentelemetry.trace.Tracer``.

    Every method is a harmless no-op so instrumented code runs without
    branching on OTel availability.
    """

    @contextlib.contextmanager
    def start_as_current_span(
        self, name: str, **kwargs: Any  # noqa: ARG002
    ) -> Iterator[_NullSpan]:
        yield _NullSpan()


# ---------------------------------------------------------------------------
# Module-level tracer (default: NullTracer)
# ---------------------------------------------------------------------------

tracer: Any = NullTracer()

# ---------------------------------------------------------------------------
# OTel setup / teardown (only effective when the SDK is installed)
# ---------------------------------------------------------------------------

_provider: Any = None  # holds TracerProvider for shutdown


def setup_tracing(
    service_name: str = "tripwire",
    version: str = "0.0.0",
    environment: str = "development",
    otlp_endpoint: str = "",
) -> None:
    """Configure the OpenTelemetry TracerProvider.

    If the OTLP endpoint is empty or the OTel SDK is not installed the
    function returns silently and the module-level ``tracer`` stays as a
    ``NullTracer``.
    """
    global tracer, _provider  # noqa: PLW0603

    if not otlp_endpoint:
        logger.info("OTel tracing disabled (no OTLP endpoint configured)")
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning(
            "OTel tracing requested but opentelemetry packages are not installed. "
            "Install with: pip install tripwire[otel]"
        )
        return

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": version,
            "deployment.environment": environment,
        }
    )

    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
    provider.add_span_processor(BatchSpanProcessor(exporter))

    trace.set_tracer_provider(provider)
    _provider = provider
    tracer = trace.get_tracer(service_name, version)

    logger.info(
        "OTel tracing enabled (endpoint=%s, service=%s)",
        otlp_endpoint,
        service_name,
    )


def shutdown_tracing() -> None:
    """Flush pending spans and shut down the TracerProvider.

    Safe to call even when OTel was never initialised (no-op in that case).
    """
    global tracer, _provider  # noqa: PLW0603

    if _provider is None:
        return

    try:
        _provider.force_flush()
        _provider.shutdown()
        logger.info("OTel tracing shut down")
    except Exception:
        logger.exception("Error shutting down OTel tracing")
    finally:
        _provider = None
        tracer = NullTracer()
