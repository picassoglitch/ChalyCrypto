"""Execution — the only code path that writes an order to a venue.

Runs AFTER the Risk Engine has authorized an entry (CLAUDE.md rule 3: risk is the final
authority and its APPROVE/REJECT is binding). The executor never re-opens that decision;
it carries out an already-approved one, refuses anything not explicitly approved, and
fails safe on any venue error.
"""

from .executor import (
    EXECUTION_IDEMPOTENCY_TTL_SECONDS,
    ExecutionEngine,
    ExecutionReport,
    ExecutionStatus,
    order_request_from_decision,
)

__all__ = [
    "EXECUTION_IDEMPOTENCY_TTL_SECONDS",
    "ExecutionEngine",
    "ExecutionReport",
    "ExecutionStatus",
    "order_request_from_decision",
]
