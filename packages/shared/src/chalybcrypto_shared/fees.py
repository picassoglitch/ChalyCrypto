from __future__ import annotations

from datetime import datetime, timezone

from .config import Settings
from .models import FeeSchedule


def seed_fee_schedules(settings: Settings, now: datetime | None = None) -> list[FeeSchedule]:
    """Convert seed rows from settings into FeeSchedule records.

    These are inserted into the fee_schedules table on bootstrap; the table remains the runtime
    source of truth (CLAUDE.md rule 6). Pass `now` for deterministic tests.
    """
    stamp = now or datetime.now(timezone.utc)
    return [
        FeeSchedule(
            exchange=row.exchange,
            symbol=None,
            vip_level=row.vip_level,
            maker_bps=row.maker_bps,
            taker_bps=row.taker_bps,
            effective_at=stamp,
            source="config_seed",
        )
        for row in settings.fee_schedule_seed()
    ]
