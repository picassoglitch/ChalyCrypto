"""Approval → execution coordinator.

Glues a human "approve" click to a real venue order, WITHOUT weakening any safety rule:

  * Execution only fires in `semi_auto` with the paper gate satisfied (`live_unlocked`)
    and no account-protection lock (CLAUDE.md rules 3 & 5). In every other mode an
    approval is just recorded — it never reaches a venue.
  * The Risk Engine already authorized the entry when the scanner queued it; this code
    does not re-open that decision, it carries it out. The deterministic ExecutionEngine
    still refuses anything non-`semi_auto` and is idempotent (rules 2, 8).
  * Exchange keys are decrypted just-in-time via the SecretsVault and never logged or
    returned to the client (rule 7).
  * Every outcome — placed, failed, or blocked — writes an audit_logs row (rule 9).
  * Any connector failure fails safe: the approval stays pending so the operator can fix
    the cause and retry, rather than being silently marked done.

`connector_factory` and `idempotency_store` are injectable so tests drive the whole loop
with a fake venue and never touch real keys.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Callable

from chalybcrypto_connectors.base import ExchangeConnector, OrderRequest
from chalybcrypto_engine.execution import ExecutionEngine
from chalybcrypto_engine.risk import InMemoryIdempotencyStore
from chalybcrypto_shared import Mode, OrderType, Side, vault_from_env

# Process-local idempotency store (CLAUDE.md rule 8). A double-clicked approve within the
# TTL window is dropped, not double-sent. Multi-process deployments swap this for the
# Redis-backed store in Phase 6 — same Protocol.
_IDEMPOTENCY = InMemoryIdempotencyStore()


ConnectorFactory = Callable[[str, dict], ExchangeConnector]


def default_connector_factory(exchange: str, conn_enc: dict) -> ExchangeConnector:
    """Decrypt the stored keys and build the venue connector. Bitunix only for the MVP."""
    vault = vault_from_env()
    api_key = vault.decrypt(conn_enc["api_key_enc"])
    api_secret = vault.decrypt(conn_enc["api_secret_enc"])
    if exchange == "bitunix":
        from chalybcrypto_connectors.bitunix import BitunixConnector

        return BitunixConnector(api_key=api_key, api_secret=api_secret)
    raise ValueError(f"no live connector for venue {exchange!r}")


def _dec(value) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


async def handle_approval_approve(
    *,
    store,
    user_id,
    approval: dict,
    connector_factory: ConnectorFactory = default_connector_factory,
    idempotency_store=_IDEMPOTENCY,
    now=None,
) -> dict:
    """Resolve an 'approve' decision, executing a live order when (and only when) the
    mode/paper-gate/connection conditions are all met. Returns a dict the route returns
    verbatim."""
    approval_id = approval["id"]
    mode_state = await store.get_mode(user_id=user_id)
    mode = Mode(mode_state.get("mode", Mode.PAPER.value))
    locked = bool(mode_state.get("account_protection_lock"))
    unlocked = bool(mode_state.get("live_unlocked"))
    should_execute = (mode is Mode.SEMI_AUTO) and unlocked and not locked

    # Non-live path: record the approval, no venue write. Keeps paper/manual modes inert
    # and preserves the existing API contract (returns the resolved approval row).
    if not should_execute:
        resolved = await store.resolve_approval(
            user_id=user_id, approval_id=approval_id, action="approve"
        )
        await store.add_audit_log(
            user_id=user_id,
            actor="human",
            action="approval_approved_no_exec",
            reason=f"no live execution: mode={mode.value} live_unlocked={unlocked} locked={locked}",
            details={"approval_id": str(approval_id)},
        )
        return resolved

    # Live path. Need a Bitunix connection; without it we DON'T mark the approval done.
    conn_enc = await store.get_exchange_connection_enc(user_id=user_id, exchange="bitunix")
    if conn_enc is None:
        await store.add_audit_log(
            user_id=user_id,
            actor="execution_engine",
            action="open_order_blocked",
            reason="no_bitunix_connection",
            details={"approval_id": str(approval_id)},
        )
        return {
            "executed": False,
            "status": "blocked",
            "reason": "no_bitunix_connection",
            "approval_id": str(approval_id),
        }

    qty = _dec(approval.get("qty"))
    if qty is None or qty <= 0:
        await store.add_audit_log(
            user_id=user_id,
            actor="execution_engine",
            action="open_order_blocked",
            reason="approval_missing_qty",
            details={"approval_id": str(approval_id)},
        )
        return {
            "executed": False,
            "status": "blocked",
            "reason": "approval_missing_qty",
            "approval_id": str(approval_id),
        }

    side = Side(approval["side"])
    tps = approval.get("take_profits") or []
    req = OrderRequest(
        pair=approval["pair"],
        side=side,
        order_type=OrderType.MARKET,
        qty=qty,
        reduce_only=False,
        leverage=_dec(approval.get("leverage")),
        idempotency_key=approval["idempotency_key"],
        stop_loss_price=_dec(approval.get("stop_loss")),
        take_profit_price=_dec(tps[0]) if tps else None,
    )

    connector = connector_factory("bitunix", conn_enc)
    try:
        report = await ExecutionEngine().open_position(
            req,
            connector=connector,
            idempotency_store=idempotency_store,
            mode=mode,
            now=now,
        )
    finally:
        try:
            await connector.aclose()
        except Exception:  # noqa: BLE001 — closing must never mask the execution result
            pass

    if report.placed:
        trade = await store.add_trade(
            user_id=user_id,
            trade={
                "exchange": "bitunix",
                "pair": req.pair,
                "side": side.value,
                "strategy": None,
                "mode": mode.value,
                "entry_price": str(_dec(approval.get("entry")) or ""),
                "qty": str(qty),
                "leverage": str(req.leverage) if req.leverage is not None else None,
                "status": "open",
            },
        )
        order = await store.add_order(
            user_id=user_id,
            order={
                "trade_id": trade["id"],
                "exchange_order_id": report.exchange_order_id,
                "type": "market",
                "side": side.value,
                "price": None,
                "qty": str(qty),
                "reduce_only": False,
                "status": report.status.value,
                "idempotency_key": report.idempotency_key,
            },
        )
        await store.resolve_approval(
            user_id=user_id, approval_id=approval_id, action="approve"
        )
        await store.add_audit_log(
            user_id=user_id,
            actor="execution_engine",
            action="open_order",
            reason=report.reason,
            details={
                "approval_id": str(approval_id),
                "exchange_order_id": report.exchange_order_id,
                "idempotency_key": report.idempotency_key,
                "trade_id": str(trade["id"]),
            },
        )
        return {
            "executed": True,
            "status": report.status.value,
            "exchange_order_id": report.exchange_order_id,
            "trade_id": str(trade["id"]),
            "order_id": str(order["id"]),
            "pair": req.pair,
            "side": side.value,
            "qty": str(qty),
        }

    # Failed / duplicate / rejected: audit and leave the approval pending for retry.
    await store.add_audit_log(
        user_id=user_id,
        actor="execution_engine",
        action="open_order_failed",
        reason=report.reason,
        details={
            "approval_id": str(approval_id),
            "status": report.status.value,
            "idempotency_key": report.idempotency_key,
        },
    )
    return {
        "executed": False,
        "status": report.status.value,
        "reason": report.reason,
        "approval_id": str(approval_id),
    }
