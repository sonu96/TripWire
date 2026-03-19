"""Product-specific event handlers for the ingestion pipeline.

Handlers implement the ``EventHandler`` protocol and are registered
with the ``EventProcessor`` orchestrator.
"""

from tripwire.ingestion.handlers.base import EventHandler
from tripwire.ingestion.handlers.payment import PaymentHandler
from tripwire.ingestion.handlers.trigger import TriggerHandler

__all__ = ["EventHandler", "PaymentHandler", "TriggerHandler"]
