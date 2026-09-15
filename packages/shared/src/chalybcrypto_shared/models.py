from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, PositiveInt

from .enums import MarginType, Mode, OrderType, Side


_MODEL_CFG = ConfigDict(extra="forbid", frozen=True)


class FeeSchedule(BaseModel):
    model_config = _MODEL_CFG

    exchange: str
    symbol: str | None = None
    vip_level: str = "regular"
    maker_bps: Decimal
    taker_bps: Decimal
    effective_at: datetime
    source: str | None = None


class Kline(BaseModel):
    model_config = _MODEL_CFG

    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    close_time: datetime


class OrderBookLevel(BaseModel):
    model_config = _MODEL_CFG

    price: Decimal
    size: Decimal


class OrderBookSnapshot(BaseModel):
    model_config = _MODEL_CFG

    exchange: str
    pair: str
    taken_at: datetime
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]
    is_native: bool = Field(
        default=True,
        description="True only for native venue books (LBank/Bitunix). Coinglass books are context-only.",
    )


class MarketSnapshot(BaseModel):
    model_config = _MODEL_CFG

    pair: str
    exchange: str
    taken_at: datetime
    klines: list[Kline] = Field(default_factory=list)
    order_book: OrderBookSnapshot | None = None
    mark_price: Decimal | None = None
    funding_rate: Decimal | None = None
    open_interest: Decimal | None = None
    spread_bps: Decimal | None = None
    coinglass_context: dict[str, Any] | None = None
    cmc_context: dict[str, Any] | None = None
    indicators: dict[str, Any] = Field(default_factory=dict)


class Signal(BaseModel):
    model_config = _MODEL_CFG

    id: UUID = Field(default_factory=uuid4)
    pair: str
    side: Side
    strategy: str
    entry: Decimal
    stop_loss: Decimal
    take_profits: list[Decimal] = Field(default_factory=list)
    leverage: Decimal
    margin_type: MarginType = MarginType.ISOLATED
    timeframe: str
    thesis_tags: list[str] = Field(default_factory=list)
    source: str = "scanner"
    dedup_hash: str
    created_at: datetime


class RiskProfile(BaseModel):
    """Configurable risk rules per ARCHITECTURE §4. All bounds are inclusive maxes unless noted."""

    model_config = _MODEL_CFG

    name: str
    max_risk_per_trade_bps: Decimal
    max_daily_loss_bps: Decimal
    max_weekly_loss_bps: Decimal
    max_drawdown_bps: Decimal
    max_open_positions: PositiveInt
    max_leverage: Decimal
    max_exposure_per_asset_bps: Decimal
    max_total_exposure_bps: Decimal
    max_trades_per_hour: PositiveInt
    min_rr: Decimal
    min_adx: Decimal
    min_liquidity_usd: Decimal
    min_volume_usd: Decimal
    min_expected_profit_after_fees_bps: Decimal
    min_liquidation_distance_bps: Decimal
    stale_price_max_seconds: NonNegativeInt
    cooldown_after_loss_seconds: NonNegativeInt
    cooldown_after_volatility_spike_seconds: NonNegativeInt
    breakeven_trigger_bps: Decimal
    trailing_trigger_bps: Decimal
    partial_tp_trigger_bps: Decimal
    protected_profit_giveback: Decimal = Field(
        default=Decimal("0.30"),
        description="Fraction of peak gain that may be given back; floor ratchets up only (§6).",
    )
    min_paper_trades_for_live: PositiveInt = 50
    min_paper_profit_factor_for_live: Decimal = Decimal("1.2")
    max_paper_drawdown_for_live_bps: Decimal = Decimal("1500")


class TradeDecision(BaseModel):
    """Final risk-engine output; APPROVE/REJECT is binding. Always audit-logged."""

    model_config = _MODEL_CFG

    signal_id: UUID
    mode: Mode
    approved: bool
    reason: str
    intended_order_type: OrderType | None = None
    intended_qty: Decimal | None = None
    intended_entry: Decimal | None = None
    intended_stop_loss: Decimal | None = None
    intended_take_profits: list[Decimal] = Field(default_factory=list)
    intended_leverage: Decimal | None = None
    ev_net_bps: Decimal | None = None
    liquidation_price: Decimal | None = None
    liquidation_distance_bps: Decimal | None = None
    fees_round_trip_bps: Decimal | None = None
    idempotency_key: str
    decided_at: datetime
    actor: str = "risk_engine"
