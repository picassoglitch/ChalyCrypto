"""Strategy contract.

Per ARCHITECTURE §2 the same evaluate() path runs in backtest, paper, and live — only the
fill source differs. evaluate() is therefore a PURE function over MarketSnapshot + params.
No network I/O, no LLM, no global state.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from chalybcrypto_shared import MarketSnapshot, Signal


_FROZEN = ConfigDict(extra="forbid", frozen=True)


class StrategyParams(BaseModel):
    """Base class — concrete strategies subclass with their own knobs."""

    model_config = _FROZEN


@dataclass(frozen=True)
class StrategyContext:
    """Per-call deterministic context. `now` is provided by the caller so backtests are
    reproducible from kline timestamps and live runs use wall-clock."""

    now: datetime


class Strategy(ABC):
    key: str  # stable identifier matching chalybcrypto.strategies.key

    @abstractmethod
    def evaluate(
        self,
        snapshot: MarketSnapshot,
        params: StrategyParams,
        context: StrategyContext,
    ) -> Signal | None: ...
