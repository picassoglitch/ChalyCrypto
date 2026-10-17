"""Hub admission gate for heavy/costly routes (consumption contract).

Usage in a route:

    async with admitted(hub, user_id=user_id, operation="backtests.run",
                        external_job_id=job_id, est_tokens=...) as adm:
        ...do the work...

  * admit (class "job") runs before any work; a refusal maps to 402/429/413
    with the hub's `reason` so the UI can show it (CLAUDE.md: keep the UI honest).
  * settle always runs in `finally`: succeeded on clean exit, failed on error,
    cancelled on cancellation.
  * Fail closed: hub unreachable / erroring → 503 and no work. The only bypass is
    CHALYB_BASE_URL unset (local dev / tests), where get_hub_client() is None.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status

from chalybcrypto_hub import AdmitResult, HubClient, HubError, HubRejected

log = logging.getLogger(__name__)


# reason → HTTP status. 402: the user must pay/top up; 429: a rate/count cap that
# frees up with time; 413: the job itself is too big for the tier.
REASON_STATUS: dict[str, int] = {
    "no_tokens": status.HTTP_402_PAYMENT_REQUIRED,
    "boost_unavailable": status.HTTP_402_PAYMENT_REQUIRED,
    "concurrency": status.HTTP_429_TOO_MANY_REQUESTS,
    "jobs_cap": status.HTTP_429_TOO_MANY_REQUESTS,
    "minutes_cap": status.HTTP_429_TOO_MANY_REQUESTS,
    "streams_cap": status.HTTP_429_TOO_MANY_REQUESTS,
    "upload_too_large": 413,
    "video_too_long": 413,
    "storage_full": 413,
}


def status_for_reason(reason: str | None) -> int:
    # Unknown/new reasons are still refusals; 402 is the conservative default.
    return REASON_STATUS.get(reason or "", status.HTTP_402_PAYMENT_REQUIRED)


_UNSET: Any = object()
_HUB: HubClient | None = _UNSET


def get_hub_client() -> HubClient | None:
    """FastAPI dependency. None = CHALYB_BASE_URL unset (dev): admission skipped."""
    global _HUB
    if _HUB is _UNSET:
        _HUB = HubClient.from_env()
    return _HUB


def set_hub_client_for_tests(client: HubClient | None) -> None:
    global _HUB
    _HUB = client


@dataclass
class Admission:
    reservation_id: str | None
    lane: str = "standard"
    bypassed: bool = False
    result: AdmitResult | None = None
    outcome: str | None = None  # route may set "cancelled"/"failed" explicitly


@asynccontextmanager
async def admitted(
    hub: HubClient | None,
    *,
    user_id: UUID | str,
    operation: str,
    external_job_id: str,
    est_tokens: int = 0,
    upload_mb: float = 0,
    source_minutes: float = 0,
    boost: bool | None = None,
) -> AsyncIterator[Admission]:
    if hub is None:
        yield Admission(reservation_id=None, bypassed=True)
        return

    try:
        res = await hub.admit(
            external_user_id=str(user_id),
            external_job_id=external_job_id,
            operation=operation,
            job_class="job",
            est_tokens=est_tokens,
            upload_mb=upload_mb,
            source_minutes=source_minutes,
            boost=boost,
        )
    except HubRejected as e:
        # The hub answers real refusals with 200 + allowed=false; a 4xx that still
        # carries a refusal `reason` is shown the same way.
        reason = e.json.get("reason")
        if isinstance(reason, str) and reason:
            raise HTTPException(
                status_code=status_for_reason(reason),
                detail={"error": "usage_refused", "reason": reason, "operation": operation},
            ) from e
        log.error("hub admit rejected for %s (%s): %s", operation, external_job_id, e)
        if e.status_code == 404 and e.error == "unknown user_id":
            # The account, not the hub: retrying in a few minutes won't help.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "unknown_user", "operation": operation},
            ) from e
        # 401/403 bearer, 404 unknown engine, 400 a request we built wrong: our
        # config. Still fail closed as hub_unavailable.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "hub_unavailable", "operation": operation},
        ) from e
    except HubError as e:
        log.error("hub admit failed for %s (%s): %s", operation, external_job_id, e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "hub_unavailable", "operation": operation},
        ) from e

    if not res.allowed:
        raise HTTPException(
            status_code=status_for_reason(res.reason),
            detail={
                "error": "usage_refused",
                "reason": res.reason,
                "operation": operation,
                "limits": res.limits,
                "balance": res.balance,
            },
        )

    adm = Admission(reservation_id=res.reservation_id, lane=res.lane or "standard", result=res)
    outcome = "failed"
    try:
        yield adm
        outcome = adm.outcome or "succeeded"
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    finally:
        if res.reservation_id:
            try:
                await hub.settle(reservation_id=res.reservation_id, outcome=outcome)
            except HubError as e:
                # The reservation expires on its own (ttl); never mask the route result.
                log.error("hub settle(%s) failed for %s: %s", outcome, res.reservation_id, e)
