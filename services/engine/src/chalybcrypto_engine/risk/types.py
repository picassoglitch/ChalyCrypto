"""Shared types for the risk engine.

All money math is Decimal. All time-based fields are timezone-aware datetimes (UTC).
All risk percentages are in basis points (1 bp = 0.01%).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt

from chalybcrypto_shared import MarginType, Side


_FROZEN = ConfigDict(extra="forbid", frozen=True)


class RejectReason(StrEnum):
    """Every rejection writes one of these to TradeDecision.reason (CLAUDE.md rule 9)."""

    OK = "ok"
    ACCOUNT_PROTECTION_LOCK = "account_protection_lock"
    PAPER_GATE_UNMET = "paper_gate_unmet"
    STALE_PRICE = "stale_price"
    CONNECTOR_FAILURE = "connector_failure"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    WEEKLY_LOSS_LIMIT = "weekly_loss_limit"
    DRAWDOWN_LIMIT = "drawdown_limit"
    MAX_OPEN_POSITIONS = "max_open_positions"
    MAX_TRADES_PER_HOUR = "max_trades_per_hour"
    MAX_EXPOSURE_PER_ASSET = "max_exposure_per_asset"
    MAX_TOTAL_EXPOSURE = "max_total_exposure"
    COOLDOWN_AFTER_LOSS = "cooldown_after_loss"
    COOLDOWN_AFTER_VOLATILITY = "cooldown_after_volatility"
    SIZE_BELOW_MIN = "size_below_min"
    SIZE_RISK_UNREACHABLE = "size_risk_unreachable"
    MAX_LEVERAGE_EXCEEDED = "max_leverage_exceeded"
    LIQUIDATION_TOO_CLOSE = "liquidation_too_close"
    EV_NEGATIVE_AFTER_COSTS = "ev_negative_after_costs"
    EV_STATS_UNKNOWN = "ev_stats_unknown"
    MIN_RR_NOT_MET = "min_rr_not_met"
    MIN_LIQUIDITY_NOT_MET = "min_liquidity_not_met"
    DUPLICATE_SIGNAL = "duplicate_signal"
    INVALID_SIGNAL = "invalid_signal"


class StrategyStats(BaseModel):
    """Per-strategy realized performance, used by the EV gate.

    sample_size lives here so the engine can reject for live when stats are unproven
    (ARCHITECTURE §5: unknown EV → reject for live/semi-auto).
    """

    model_config = _FROZEN

    strategy: str
    sample_size: NonNegativeInt
    win_rate: Decimal = Field(..., description="0..1")
    avg_win_bps: Decimal = Field(..., description="bps of notional, positive")
    avg_loss_bps: Decimal = Field(..., description="bps of notional, positive (magnitude)")
    min_sample_for_live: NonNegativeInt = 50


class AccountState(BaseModel):
    """Snapshot of account-level facts the risk engine needs to authorize a new entry."""

    model_config = _FROZEN

    equity: Decimal
    balance: Decimal
    daily_realized_pnl: Decimal = Decimal("0")
    weekly_realized_pnl: Decimal = Decimal("0")
    peak_equity: Decimal
    open_positions_count: NonNegativeInt = 0
    exposure_per_asset_bps: dict[str, Decimal] = Field(default_factory=dict)
    total_exposure_bps: Decimal = Decimal("0")
    trades_last_hour: NonNegativeInt = 0
    last_loss_at: datetime | None = None
    last_volatility_spike_at: datetime | None = None
    account_protection_lock: bool = False
    paper_gate_unlocked: bool = False
    last_tick_at: datetime | None = None


class PositionState(BaseModel):
    """In-flight position state — needed for the protected-profit manager (ARCHITECTURE §6)."""

    model_config = _FROZEN

    pair: str
    side: Side
    qty: Decimal
    entry_price: Decimal
    leverage: Decimal
    margin_type: MarginType = MarginType.ISOLATED
    fees_paid: Decimal = Decimal("0")
    funding_paid: Decimal = Decimal("0")
    continue_flag: bool = False
    peak_gain_net: Decimal = Decimal("0")
    protected_floor_net: Decimal | None = None
    opened_at: datetime | None = None
