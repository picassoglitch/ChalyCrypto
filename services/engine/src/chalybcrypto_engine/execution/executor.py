"""ExecutionEngine — turns an APPROVED TradeDecision into a venue order.

This is the only module that places an order on an exchange. It sits at the very end of
the hot path:

    snapshot → strategy.evaluate → RiskEngine.authorize_new_entry (binding) → ExecutionEngine

CLAUDE.md compliance (these are not optional):
  * No LLM anywhere in this path (rule 2).
  * The Risk Engine has already run and is the final authority (rule 3). The executor
    only carries out an `approved=True` decision; anything else is refused here too.
  * `full_auto` is refused in the MVP, and only `semi_auto` may open a live entry
    (rule 5). `paper`/`backtest` must never reach a venue — they have their own fill
    simulators.
  * Every exchange write goes through the idempotency store (rule 8). A key that is
    already claimed is dropped as a duplicate, never double-sent.
  * Any connector failure fails SAFE: we return a `failed` report and never raise into
    the caller — the order is treated as not-placed (rule 3: fail safe, not open).
  * The caller persists the returned report to orders/trades/audit_logs (rule 9).
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from chalybcrypto_connectors.base import (
    ConnectorError,
    ExchangeConnector,
    OrderRequest,
)
from chalybcrypto_shared import (
    Mode,
    OrderType,
    Side,
    TradeDecision,
    dedup_hash,
)

from ..risk.idempotency import IdempotencyStore

# Matches the risk engine's idempotency window so a decision and its execution share the
# same dedup horizon (CLAUDE.md rule 8).
EXECUTION_IDEMPOTENCY_TTL_SECONDS = 60


class ExecutionStatus(StrEnum):
    FILLED = "filled"        # order accepted by the venue
    FAILED = "failed"        # venue/transport error — fail safe, treated as not placed
    DUPLICATE = "duplicate"  # idempotency key already claimed — not re-sent
    REJECTED = "rejected"    # refused before any venue call (mode/guard/qty)


class ExecutionReport(BaseModel):
    """Outcome of one execution attempt. The caller writes this to orders/trades/audit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ExecutionStatus
    pair: str
    side: Side
    qty: str  # stringified Decimal — audit-friendly, no float drift
    order_type: OrderType
    reduce_only: bool
    idempotency_key: str
    reason: str
    exchange_order_id: str | None = None
    client_id: str | None = None
    submitted_at: datetime | None = None

    @property
    def placed(self) -> bool:
        return self.status is ExecutionStatus.FILLED


def order_request_from_decision(
    decision: TradeDecision,
    *,
    pair: str,
    side: Side,
) -> OrderRequest:
    """Build a venue order from an approved decision. Pure; raises if not executable so
    callers don't silently send a malformed order. `pair`/`side` come from the originating
    Signal (the decision only carries a signal_id)."""
    if not decision.approved or decision.intended_qty is None:
        raise ValueError("order_request_from_decision: decision is not an approved entry")
    return OrderRequest(
        pair=pair,
        side=side,
        order_type=decision.intended_order_type or OrderType.MARKET,
        qty=decision.intended_qty,
        reduce_only=False,
        leverage=decision.intended_leverage,
        idempotency_key=decision.idempotency_key,
        stop_loss_price=decision.intended_stop_loss,
        take_profit_price=(
            decision.intended_take_profits[0]
            if decision.intended_take_profits
            else None
        ),
    )


class ExecutionEngine:
    """Stateless. All state (connector, idempotency store, mode) arrives per call."""

    async def open_position(
        self,
        req: OrderRequest,
        *,
        connector: ExchangeConnector,
        idempotency_store: IdempotencyStore,
        mode: Mode,
        now: datetime | None = None,
    ) -> ExecutionReport:
        ts = now or datetime.now(timezone.utc)

        # rule 5: full_auto is forbidden in the MVP; only semi_auto opens a live entry.
        # paper/backtest must never touch a venue — they have their own fill sims.
        if mode is not Mode.SEMI_AUTO:
            reason = (
                "full_auto_disabled_in_mvp"
                if mode is Mode.FULL_AUTO
                else f"mode_not_executable:{mode.value}"
            )
            return self._rejected(req, reason)

        if req.qty <= 0:
            return self._rejected(req, "non_positive_qty")

        # rule 8: claim the idempotency key BEFORE the venue write. A second attempt with
        # the same key (double-click, retry, replayed approval) is dropped, not re-sent.
        acquired = await idempotency_store.try_acquire(
            req.idempotency_key, EXECUTION_IDEMPOTENCY_TTL_SECONDS
        )
        if not acquired:
            return self._report(req, ExecutionStatus.DUPLICATE, "idempotency_key_already_claimed")

        return await self._send(req, connector, ts)

    async def close_position(
        self,
        *,
        pair: str,
        position_side: Side,
        qty,
        connector: ExchangeConnector,
        idempotency_store: IdempotencyStore,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> ExecutionReport:
        """Reduce-only market close of an existing position. Allowed in any mode — closing
        reduces risk and must never be blocked by entry guards (ARCHITECTURE §4). The
        closing order is the OPPOSITE side and reduce_only so it can only shrink, never
        flip, the position."""
        ts = now or datetime.now(timezone.utc)
        closing_side = Side.SHORT if position_side is Side.LONG else Side.LONG
        key = idempotency_key or dedup_hash("close", pair, position_side.value, ts.isoformat())
        req = OrderRequest(
            pair=pair,
            side=closing_side,
            order_type=OrderType.REDUCE_ONLY_MARKET,
            qty=qty,
            reduce_only=True,
            idempotency_key=key,
        )
        if req.qty <= 0:
            return self._rejected(req, "non_positive_qty")
        acquired = await idempotency_store.try_acquire(key, EXECUTION_IDEMPOTENCY_TTL_SECONDS)
        if not acquired:
            return self._report(req, ExecutionStatus.DUPLICATE, "idempotency_key_already_claimed")
        return await self._send(req, connector, ts)

    # ──────────────────────────────────────────────────────────────────────

    async def _send(
        self, req: OrderRequest, connector: ExchangeConnector, ts: datetime
    ) -> ExecutionReport:
        try:
            result = await connector.place_order(req)
        except ConnectorError as e:
            # Fail safe: a venue/transport error means the order is NOT considered placed.
            return self._report(req, ExecutionStatus.FAILED, f"connector_error:{e}")
        except Exception as e:  # noqa: BLE001 — last-resort safety net; never fail open
            return self._report(req, ExecutionStatus.FAILED, f"unexpected_error:{e!r}")
        return self._report(
            req,
            ExecutionStatus.FILLED,
            "ok",
            exchange_order_id=result.exchange_order_id,
            client_id=result.client_id,
            submitted_at=result.submitted_at,
        )

    @staticmethod
    def _report(
        req: OrderRequest,
        status: ExecutionStatus,
        reason: str,
        *,
        exchange_order_id: str | None = None,
        client_id: str | None = None,
        submitted_at: datetime | None = None,
    ) -> ExecutionReport:
        return ExecutionReport(
            status=status,
            pair=req.pair,
            side=req.side,
            qty=format(req.qty.normalize(), "f"),
            order_type=req.order_type,
            reduce_only=req.reduce_only,
            idempotency_key=req.idempotency_key,
            reason=reason,
            exchange_order_id=exchange_order_id,
            client_id=client_id,
            submitted_at=submitted_at,
        )

    @classmethod
    def _rejected(cls, req: OrderRequest, reason: str) -> ExecutionReport:
        return cls._report(req, ExecutionStatus.REJECTED, reason)
