from enum import StrEnum


class Mode(StrEnum):
    BACKTEST = "backtest"
    PAPER = "paper"
    SEMI_AUTO = "semi_auto"
    FULL_AUTO = "full_auto"
    BREAKEVEN_PROTECTION = "breakeven_protection"
    MANUAL_OVERRIDE = "manual_override"


class Side(StrEnum):
    LONG = "long"
    SHORT = "short"


class MarginType(StrEnum):
    ISOLATED = "isolated"
    CROSS = "cross"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP_MARKET = "stop_market"
    STOP_LIMIT = "stop_limit"
    REDUCE_ONLY_MARKET = "reduce_only_market"
    REDUCE_ONLY_LIMIT = "reduce_only_limit"
