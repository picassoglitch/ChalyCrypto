"""In-memory store + Protocol — Phase 6 placeholder.

Real Supabase-backed store lands once the persistence layer is wired (Phase 1 schema is
already there). Tests, dashboard, and the API surface all sit behind the Protocol so the
swap is mechanical.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID, uuid4

from chalybcrypto_shared import (
    FeeSchedule,
    Mode,
    RiskProfile,
    Signal,
    TradeDecision,
)


class ApiStore(Protocol):
    # ── signals ───────────────────────────────────────────
    async def list_signals(self, *, user_id: UUID, status: str | None = None) -> list[dict]: ...
    async def add_parsed_signal(self, *, user_id: UUID, signal: Signal, raw_text: str | None = None) -> dict: ...
    async def add_validated_signal(self, *, user_id: UUID, decision: TradeDecision) -> dict: ...

    # ── approvals (semi-auto queue) ───────────────────────
    async def list_approvals(self, *, user_id: UUID) -> list[dict]: ...
    async def add_approval(self, *, user_id: UUID, signal: Signal, decision: TradeDecision) -> dict: ...
    async def get_approval(self, *, user_id: UUID, approval_id: UUID) -> dict | None: ...
    async def resolve_approval(self, *, user_id: UUID, approval_id: UUID, action: str, reason: str | None = None) -> dict | None: ...

    # ── execution ─────────────────────────────────────────
    async def list_positions(self, *, user_id: UUID) -> list[dict]: ...
    async def list_trades(self, *, user_id: UUID) -> list[dict]: ...
    async def add_order(self, *, user_id: UUID, order: dict) -> dict: ...
    async def add_trade(self, *, user_id: UUID, trade: dict) -> dict: ...
    async def add_audit_log(self, *, user_id: UUID, actor: str, action: str, reason: str, details: dict | None = None) -> dict: ...
    async def list_audit_logs(self, *, user_id: UUID) -> list[dict]: ...
    # Internal: returns the ENCRYPTED key blobs for one venue so the caller can decrypt
    # them just-in-time. Never exposed over the API (CLAUDE.md rule 7).
    async def get_exchange_connection_enc(self, *, user_id: UUID, exchange: str) -> dict | None: ...

    # ── mode + risk ───────────────────────────────────────
    async def get_mode(self, *, user_id: UUID) -> dict: ...
    async def set_mode(self, *, user_id: UUID, mode: Mode) -> dict: ...
    async def get_risk_profile(self, *, user_id: UUID) -> RiskProfile | None: ...
    async def put_risk_profile(self, *, user_id: UUID, profile: RiskProfile) -> RiskProfile: ...
    async def list_fee_schedules(self) -> list[FeeSchedule]: ...
    async def put_fee_schedules(self, *, schedules: list[FeeSchedule]) -> list[FeeSchedule]: ...

    # ── strategies ────────────────────────────────────────
    async def list_strategies(self) -> list[dict]: ...

    # ── tenants (Chalyb integration) ─────────────────────────────────
    async def provision_tenant(
        self,
        *,
        external_user_id: str,
        email: str,
        display_name: str | None,
        tier: str,
    ) -> tuple[dict, bool]: ...
    async def get_tenant_by_id(self, *, tenant_id: UUID) -> dict | None: ...
    async def set_tenant_status(self, *, tenant_id: UUID, status: str) -> dict | None: ...

    # ── connections (encrypted at rest; secrets never returned) ────────
    async def add_exchange_connection(
        self,
        *,
        user_id: UUID,
        exchange: str,
        api_key_enc: bytes,
        api_secret_enc: bytes,
        ip_allowlist: list[str] | None = None,
    ) -> dict: ...
    async def list_exchange_connections(self, *, user_id: UUID) -> list[dict]: ...


class InMemoryStore:
    """Process-local store for tests + the API skeleton. Not multi-process safe; will be
    swapped for a Supabase-backed impl in a follow-on. Methods match ApiStore."""

    def __init__(self) -> None:
        self._signals: list[dict] = []
        self._approvals: list[dict] = []
        self._positions: dict[UUID, list[dict]] = {}
        self._trades: dict[UUID, list[dict]] = {}
        self._orders: dict[UUID, list[dict]] = {}
        self._audit: dict[UUID, list[dict]] = {}
        self._mode: dict[UUID, dict] = {}
        self._risk_profiles: dict[UUID, RiskProfile] = {}
        self._fee_schedules: list[FeeSchedule] = []
        self._strategies: list[dict] = [
            {"key": "ema_adx_trend", "name": "EMA/ADX trend", "enabled": True},
            {"key": "vwap_rsi_meanrev", "name": "VWAP/RSI mean-reversion", "enabled": True},
            {"key": "fvg_ob", "name": "FVG + Order Block", "enabled": True},
        ]
        self._connections: list[dict] = []
        self._tenants: list[dict] = []

    async def list_signals(self, *, user_id: UUID, status: str | None = None) -> list[dict]:
        out = [s for s in self._signals if s["user_id"] == user_id]
        if status is not None:
            out = [s for s in out if s.get("status") == status]
        return out

    async def add_parsed_signal(self, *, user_id: UUID, signal: Signal, raw_text: str | None = None) -> dict:
        record = {
            "id": uuid4(),
            "user_id": user_id,
            "status": "parsed",
            "pair": signal.pair,
            "side": signal.side.value,
            "entry": str(signal.entry) if signal.entry is not None else None,
            "stop_loss": str(signal.stop_loss),
            "take_profits": [str(t) for t in signal.take_profits],
            "leverage": str(signal.leverage),
            "strategy": signal.strategy,
            "dedup_hash": signal.dedup_hash,
            "raw_text": raw_text,
        }
        self._signals.append(record)
        return record

    async def add_validated_signal(self, *, user_id: UUID, decision: TradeDecision) -> dict:
        record = {
            "id": uuid4(),
            "user_id": user_id,
            "status": "validated" if decision.approved else "rejected",
            "signal_id": decision.signal_id,
            "decision": "approved" if decision.approved else "rejected",
            "reject_reason": decision.reason if not decision.approved else None,
            "ev_net_bps": str(decision.ev_net_bps) if decision.ev_net_bps is not None else None,
        }
        self._signals.append(record)
        return record

    async def list_approvals(self, *, user_id: UUID) -> list[dict]:
        return [a for a in self._approvals if a["user_id"] == user_id and a["status"] == "pending"]

    async def add_approval(self, *, user_id: UUID, signal: Signal, decision: TradeDecision) -> dict:
        record = {
            "id": uuid4(),
            "user_id": user_id,
            "status": "pending",
            "signal_id": signal.id,
            "pair": signal.pair,
            "side": signal.side.value,
            "entry": str(signal.entry),
            "stop_loss": str(signal.stop_loss),
            "take_profits": [str(t) for t in signal.take_profits],
            "leverage": str(decision.intended_leverage) if decision.intended_leverage else str(signal.leverage),
            "qty": str(decision.intended_qty) if decision.intended_qty else None,
            "ev_net_bps": str(decision.ev_net_bps) if decision.ev_net_bps else None,
            "idempotency_key": decision.idempotency_key,
        }
        self._approvals.append(record)
        return record

    async def get_approval(self, *, user_id: UUID, approval_id: UUID) -> dict | None:
        for a in self._approvals:
            if a["id"] == approval_id and a["user_id"] == user_id:
                return a
        return None

    async def resolve_approval(self, *, user_id: UUID, approval_id: UUID, action: str, reason: str | None = None) -> dict | None:
        for a in self._approvals:
            if a["id"] == approval_id and a["user_id"] == user_id:
                a["status"] = action
                if reason:
                    a["resolution_reason"] = reason
                return a
        return None

    async def list_positions(self, *, user_id: UUID) -> list[dict]:
        return list(self._positions.get(user_id, []))

    async def list_trades(self, *, user_id: UUID) -> list[dict]:
        return list(self._trades.get(user_id, []))

    async def add_order(self, *, user_id: UUID, order: dict) -> dict:
        record = {"id": uuid4(), "user_id": user_id, **order}
        self._orders.setdefault(user_id, []).append(record)
        return record

    async def add_trade(self, *, user_id: UUID, trade: dict) -> dict:
        record = {"id": uuid4(), "user_id": user_id, **trade}
        self._trades.setdefault(user_id, []).append(record)
        return record

    async def add_audit_log(
        self, *, user_id: UUID, actor: str, action: str, reason: str, details: dict | None = None
    ) -> dict:
        record = {
            "id": uuid4(),
            "user_id": user_id,
            "actor": actor,
            "action": action,
            "reason": reason,
            "details": details or {},
        }
        self._audit.setdefault(user_id, []).append(record)
        return record

    async def list_audit_logs(self, *, user_id: UUID) -> list[dict]:
        return list(self._audit.get(user_id, []))

    async def get_exchange_connection_enc(self, *, user_id: UUID, exchange: str) -> dict | None:
        for c in self._connections:
            if c["user_id"] == user_id and c["exchange"] == exchange:
                return c  # includes api_key_enc / api_secret_enc — internal use only
        return None

    async def get_mode(self, *, user_id: UUID) -> dict:
        return self._mode.get(user_id, {
            "user_id": user_id,
            "mode": Mode.PAPER.value,
            "paper_trades_count": 0,
            "live_unlocked": False,
            "account_protection_lock": False,
        })

    async def set_mode(self, *, user_id: UUID, mode: Mode) -> dict:
        current = await self.get_mode(user_id=user_id)
        # Paper-gate enforcement (CLAUDE.md rule 5): live modes require live_unlocked=True.
        if mode in (Mode.SEMI_AUTO, Mode.FULL_AUTO) and not current.get("live_unlocked"):
            raise PermissionError("paper_gate_unmet")
        # full_auto is disabled in MVP (CLAUDE.md rule 5).
        if mode == Mode.FULL_AUTO:
            raise PermissionError("full_auto_disabled_in_mvp")
        current["mode"] = mode.value
        self._mode[user_id] = current
        return current

    async def unlock_live_for_test(self, *, user_id: UUID) -> None:
        """Test helper only — bypasses the paper-trade-count gate."""
        m = await self.get_mode(user_id=user_id)
        m["live_unlocked"] = True
        self._mode[user_id] = m

    async def get_risk_profile(self, *, user_id: UUID) -> RiskProfile | None:
        return self._risk_profiles.get(user_id)

    async def put_risk_profile(self, *, user_id: UUID, profile: RiskProfile) -> RiskProfile:
        self._risk_profiles[user_id] = profile
        return profile

    async def list_fee_schedules(self) -> list[FeeSchedule]:
        return list(self._fee_schedules)

    async def put_fee_schedules(self, *, schedules: list[FeeSchedule]) -> list[FeeSchedule]:
        self._fee_schedules = list(schedules)
        return self._fee_schedules

    async def list_strategies(self) -> list[dict]:
        return list(self._strategies)

    async def provision_tenant(
        self,
        *,
        external_user_id: str,
        email: str,
        display_name: str | None,
        tier: str,
    ) -> tuple[dict, bool]:
        """Return (tenant, created). created=False means we found an existing row."""
        import secrets
        for t in self._tenants:
            if t["external_user_id"] == external_user_id:
                return (dict(t), False)
        record = {
            "id": uuid4(),
            "external_user_id": external_user_id,
            "email": email,
            "display_name": display_name,
            "tier": tier,
            "status": "active",
            "api_token": secrets.token_urlsafe(32),
        }
        self._tenants.append(record)
        return (dict(record), True)

    async def get_tenant_by_id(self, *, tenant_id: UUID) -> dict | None:
        for t in self._tenants:
            if t["id"] == tenant_id:
                return dict(t)
        return None

    async def set_tenant_status(self, *, tenant_id: UUID, status: str) -> dict | None:
        for t in self._tenants:
            if t["id"] == tenant_id:
                t["status"] = status
                return dict(t)
        return None

    async def add_exchange_connection(
        self,
        *,
        user_id: UUID,
        exchange: str,
        api_key_enc: bytes,
        api_secret_enc: bytes,
        ip_allowlist: list[str] | None = None,
    ) -> dict:
        record = {
            "id": uuid4(),
            "user_id": user_id,
            "exchange": exchange,
            "api_key_enc": bytes(api_key_enc),
            "api_secret_enc": bytes(api_secret_enc),
            "ip_allowlist": list(ip_allowlist) if ip_allowlist else None,
            "status": "untested",
            "last_tested_at": None,
        }
        self._connections.append(record)
        # NEVER include the _enc fields in the return — caller turns this into the
        # response shape, and the encrypted blobs only live in this store.
        return {
            "id": record["id"],
            "user_id": record["user_id"],
            "exchange": record["exchange"],
            "status": record["status"],
            "ip_allowlist": record["ip_allowlist"],
        }

    async def list_exchange_connections(self, *, user_id: UUID) -> list[dict]:
        return [
            {
                "id": c["id"],
                "user_id": c["user_id"],
                "exchange": c["exchange"],
                "status": c["status"],
                "ip_allowlist": c["ip_allowlist"],
                "last_tested_at": c["last_tested_at"],
            }
            for c in self._connections
            if c["user_id"] == user_id
        ]
