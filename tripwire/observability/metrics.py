"""Prometheus application-level metrics for TripWire.

Supabase provides database/infra metrics; this module covers the
application layer: pipeline throughput, webhook delivery, error rates,
request latency, and operational gauges.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, Info

# ── Counters ──────────────────────────────────────────────────

tripwire_events_processed_total = Counter(
    "tripwire_events_processed_total",
    "Total events processed through the pipeline",
    ["chain_id", "status"],
)

tripwire_webhooks_sent_total = Counter(
    "tripwire_webhooks_sent_total",
    "Total webhook delivery attempts",
    ["status", "mode"],
)

tripwire_errors_total = Counter(
    "tripwire_errors_total",
    "Total application errors by type",
    ["error_type"],
)

tripwire_auth_requests_total = Counter(
    "tripwire_auth_requests_total",
    "Total authentication requests",
    ["result"],
)

tripwire_nonce_dedup_total = Counter(
    "tripwire_nonce_dedup_total",
    "Total nonce deduplication lookups",
    ["result"],
)

# ── Histograms ────────────────────────────────────────────────

tripwire_pipeline_duration_seconds = Histogram(
    "tripwire_pipeline_duration_seconds",
    "Duration of pipeline stages in seconds",
    ["stage"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

tripwire_request_duration_seconds = Histogram(
    "tripwire_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "path_template", "status_code"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

tripwire_webhook_delivery_duration_seconds = Histogram(
    "tripwire_webhook_delivery_duration_seconds",
    "Webhook delivery round-trip duration in seconds",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

# ── Gauges ────────────────────────────────────────────────────

tripwire_dlq_backlog = Gauge(
    "tripwire_dlq_backlog",
    "Number of failed deliveries in the dead-letter queue",
)

# ── Info ──────────────────────────────────────────────────────

tripwire_build_info = Info(
    "tripwire_build",
    "TripWire build/deployment information",
)

# ── Helpers ───────────────────────────────────────────────────


def record_pipeline_timing(
    timings: dict[str, float],
    chain_id: int,
    status: str,
) -> None:
    """Observe all stage histograms from a pipeline timing dict and increment
    the event counter.

    *timings* is a dict like ``{"decode_ms": 1.2, "dedup_ms": 0.5, ...}``
    where each key ends in ``_ms``.  Values are converted to seconds before
    being observed on :data:`tripwire_pipeline_duration_seconds`.

    *chain_id* and *status* are used to increment
    :data:`tripwire_events_processed_total`.
    """
    for key, value_ms in timings.items():
        if key.endswith("_ms"):
            stage = key[:-3]  # strip "_ms" suffix
            tripwire_pipeline_duration_seconds.labels(stage=stage).observe(
                value_ms / 1000.0
            )

    tripwire_events_processed_total.labels(
        chain_id=str(chain_id), status=status
    ).inc()
