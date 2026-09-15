"""LLM analyst — async, explanation-only.

CLAUDE.md rule 2: no LLM in the execution path. This package writes theses,
continue-or-exit briefings, and daily digests AFTER decisions are made by the
deterministic engine. If the LLM service is down, trading is unaffected;
only the human-readable explanations are delayed or absent.
"""

from .analyst import (
    AnalystResponse,
    ClaudeAnalyst,
    ContinueBriefing,
    DailyDigest,
    TradeThesis,
)

__all__ = [
    "AnalystResponse",
    "ClaudeAnalyst",
    "ContinueBriefing",
    "DailyDigest",
    "TradeThesis",
]
