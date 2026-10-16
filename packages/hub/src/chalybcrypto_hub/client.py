"""HTTP client for the Chalyb hub consumption contract.

    POST {CHALYB_BASE_URL}/api/engines/{slug}/usage/admit
    POST {CHALYB_BASE_URL}/api/engines/{slug}/usage/settle
    POST {CHALYB_BASE_URL}/api/engines/{slug}/usage

Auth: the engine's bearer token (the hub's CHALYBCRYPTO_ADMIN_TOKEN; on this side
CHALYB_HUB_TOKEN, falling back to CHALYB_ADMIN_TOKEN, which holds the same value).

`HubClient.from_env()` returns None when CHALYB_BASE_URL is unset — that is the
dev/test mode where admission is skipped and the outbox is never drained.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_SLUG = "chalybcrypto"
MAX_EVENTS_PER_REQUEST = 100


class HubError(Exception):
    """Base for hub call failures."""


class HubUnavailable(HubError):
    """Transport error, timeout, 408/429 or 5xx — worth retrying later."""


class HubRejected(HubError):
    """Permanent 4xx (other than 408/429). Do not retry the same payload."""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"hub rejected request: {status_code} {body[:500]}")
        self.status_code = status_code
        self.body = body


def is_retryable_status(code: int) -> bool:
    return code in (408, 429) or code >= 500


@dataclass(frozen=True)
class AdmitResult:
    allowed: bool
    reservation_id: str | None = None
    lane: str | None = None
    reason: str | None = None
    boost_fee_tokens: int = 0
    limits: dict[str, Any] = field(default_factory=dict)
    balance: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


class HubClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        slug: str = DEFAULT_SLUG,
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._base = f"{base_url.rstrip('/')}/api/engines/{slug}"
        self._token = token
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    @classmethod
    def from_env(cls, *, client: httpx.AsyncClient | None = None) -> HubClient | None:
        base = (os.environ.get("CHALYB_BASE_URL") or "").strip()
        if not base:
            return None
        token = os.environ.get("CHALYB_HUB_TOKEN") or os.environ.get("CHALYB_ADMIN_TOKEN") or ""
        slug = os.environ.get("CHALYB_ENGINE_SLUG") or DEFAULT_SLUG
        return cls(base_url=base, token=token, slug=slug, client=client)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            r = await self._client.post(
                f"{self._base}{path}",
                json=body,
                headers={"Authorization": f"Bearer {self._token}"},
            )
        except httpx.HTTPError as e:
            raise HubUnavailable(f"{type(e).__name__}: {e}") from e
        if r.status_code >= 400:
            if is_retryable_status(r.status_code):
                raise HubUnavailable(f"hub {path} returned {r.status_code}")
            raise HubRejected(r.status_code, r.text)
        try:
            return r.json()
        except ValueError as e:
            raise HubUnavailable(f"hub {path} returned non-JSON body") from e

    async def admit(
        self,
        *,
        external_user_id: str,
        external_job_id: str,
        operation: str,
        job_class: str = "job",
        est_tokens: int = 0,
        upload_mb: float = 0,
        source_minutes: float = 0,
        storage_mb_after: float | None = None,
        boost: bool | None = None,
        ttl_seconds: int | None = None,
    ) -> AdmitResult:
        body: dict[str, Any] = {
            "external_user_id": external_user_id,
            "external_job_id": external_job_id,
            "class": job_class,
            "operation": operation,
            "est_tokens": int(est_tokens),
            "upload_mb": upload_mb,
            "source_minutes": source_minutes,
            "boost": boost,
        }
        if storage_mb_after is not None:
            body["storage_mb_after"] = storage_mb_after
        if ttl_seconds is not None:
            body["ttl_seconds"] = ttl_seconds
        data = await self._post("/usage/admit", body)
        return AdmitResult(
            allowed=bool(data.get("allowed")),
            reservation_id=data.get("reservation_id"),
            lane=data.get("lane"),
            reason=data.get("reason"),
            boost_fee_tokens=int(data.get("boost_fee_tokens") or 0),
            limits=data.get("limits") or {},
            balance=data.get("balance") or {},
            raw=data,
        )

    async def settle(self, *, reservation_id: str, outcome: str) -> dict[str, Any]:
        if outcome not in ("succeeded", "failed", "cancelled", "heartbeat"):
            raise ValueError(f"invalid settle outcome {outcome!r}")
        return await self._post(
            "/usage/settle", {"reservation_id": reservation_id, "outcome": outcome}
        )

    async def post_usage(
        self, *, external_user_id: str, events: list[dict[str, Any]]
    ) -> dict[str, Any]:
        if len(events) > MAX_EVENTS_PER_REQUEST:
            raise ValueError(f"at most {MAX_EVENTS_PER_REQUEST} events per request")
        return await self._post(
            "/usage", {"external_user_id": external_user_id, "events": events}
        )
