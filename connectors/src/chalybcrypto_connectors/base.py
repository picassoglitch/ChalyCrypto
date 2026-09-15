from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from chalybcrypto_shared import Kline, MarginType, OrderBookSnapshot, OrderType, Side


_MODEL_CFG = ConfigDict(extra="forbid", frozen=True)


class ConnectorError(RuntimeError):
    """Raised when an exchange call fails. Risk engine treats these as fail-safe (reject)."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class PositionSide(StrEnum):
    """Per-position side; differs from Side because hedge-mode venues track LONG/SHORT separately."""

    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class Balance(BaseModel):
    model_config = _MODEL_CFG

    margin_coin: str
    available: Decimal
    frozen: Decimal
    margin_locked: Decimal
    cross_unrealized_pnl: Decimal = Decimal("0")
    isolation_unrealized_pnl: Decimal = Decimal("0")


class PositionInfo(BaseModel):
    model_config = _MODEL_CFG

    exchange: str
    pair: str
    side: PositionSide
    qty: Decimal
    avg_entry_price: Decimal
    leverage: Decimal
    margin_type: MarginType
    liquidation_price: Decimal | None = None
    unrealized_pnl: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    margin: Decimal | None = None
    funding_paid: Decimal = Decimal("0")
    opened_at: datetime | None = None


class FundingInfo(BaseModel):
    model_config = _MODEL_CFG

    pair: str
    funding_rate: Decimal
    funding_interval_hours: int
    next_funding_time: datetime
    mark_price: Decimal
    last_price: Decimal
    max_funding_rate: Decimal | None = None
    min_funding_rate: Decimal | None = None


class OrderRequest(BaseModel):
    """Venue-agnostic order request. Connectors translate to venue-specific bodies."""

    model_config = _MODEL_CFG

    pair: str
    side: Side
    order_type: OrderType
    qty: Decimal
    price: Decimal | None = None
    reduce_only: bool = False
    margin_type: MarginType = MarginType.ISOLATED
    leverage: Decimal | None = None
    idempotency_key: str = Field(
        ...,
        description="Required. Risk engine builds via dedup_hash; venue echoes it back as clientId. CLAUDE.md rule 8.",
    )
    take_profit_price: Decimal | None = None
    stop_loss_price: Decimal | None = None


class OrderResult(BaseModel):
    model_config = _MODEL_CFG

    exchange_order_id: str
    client_id: str
    status: str
    submitted_at: datetime


class ExchangeConnector(ABC):
    """Abstract base for trading venues (Bitunix, LBank).

    Implementations MUST be deterministic, return shared models, and surface failures as
    ConnectorError so the risk engine fails safe. None of these methods may call an LLM.
    """

    exchange: str

    @abstractmethod
    async def klines(
        self,
        pair: str,
        interval: str,
        *,
        limit: int = 100,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Kline]: ...

    @abstractmethod
    async def order_book(self, pair: str, *, limit: int = 50) -> OrderBookSnapshot: ...

    @abstractmethod
    async def funding(self, pair: str) -> FundingInfo: ...

    @abstractmethod
    async def balances(self, margin_coin: str) -> list[Balance]: ...

    @abstractmethod
    async def positions(self, *, pair: str | None = None) -> list[PositionInfo]: ...

    @abstractmethod
    async def place_order(self, req: OrderRequest) -> OrderResult: ...

    @abstractmethod
    async def cancel_order(
        self, pair: str, *, order_id: str | None = None, client_id: str | None = None
    ) -> bool: ...

    async def aclose(self) -> None:
        """Override to release HTTP/WS resources."""
