"""Background task health tracking for TripWire.

Provides a registry that background tasks (finality poller, WS subscriber,
DLQ handler) use to report liveness.  The ``/health/detailed`` endpoint
reads from this registry to include background-task status in its response.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace


@dataclass
class BackgroundTaskHealth:
    """Health snapshot for a single background task."""

    name: str
    running: bool = False
    last_run_at: float | None = None
    error_count: int = 0
    last_error: str | None = None


class HealthRegistry:
    """Registry of background task health status.

    Only accessed from the async event loop thread; CPython's GIL ensures
    dict read/write atomicity for simple operations so no lock is needed.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, BackgroundTaskHealth] = {}

    def register(self, name: str) -> None:
        """Register a background task as running."""
        self._tasks[name] = BackgroundTaskHealth(name=name, running=True)

    def record_run(self, name: str) -> None:
        """Record a successful poll/run cycle for *name*."""
        task = self._tasks.get(name)
        if task is not None:
            task.last_run_at = time.time()

    def record_error(self, name: str, error: str) -> None:
        """Record an error for *name*."""
        task = self._tasks.get(name)
        if task is not None:
            task.error_count += 1
            task.last_error = error

    def get_all(self) -> dict[str, BackgroundTaskHealth]:
        """Return a snapshot (deep copy) of all registered tasks."""
        return {
            name: replace(task)
            for name, task in self._tasks.items()
        }


# Module-level singleton
health_registry = HealthRegistry()
