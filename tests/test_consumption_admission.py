"""Hub admission on heavy routes: refusal → 402/429/413, settle in finally, fail closed."""

from __future__ import annotations

import json
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from chalybcrypto_api.consumption import admitted, get_hub_client, status_for_reason
from chalybcrypto_api.deps import set_store_for_tests
from chalybcrypto_api.main import app
from chalybcrypto_api.store import InMemoryStore
from chalybcrypto_hub import HubClient

USER = UUID("11111111-1111-1111-1111-111111111111")
AUTH = {"X-User-Id": str(USER)}
BT = {"strategy": "ema_adx_trend", "pair": "BTCUSDT"}


class FakeHub:
    """Mock-transport hub recording every call."""

    def __init__(self, admit_response=None, *, admit_status=200, raise_on=None):
        self.calls: list[tuple[str, dict]] = []
        self.admit_response = admit_response or {
            "ok": True,
            "allowed": True,
            "reservation_id": "res-1",
            "lane": "standard",
        }
        self.admit_status = admit_status
        self.raise_on = raise_on or set()

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path.rsplit("/usage", 1)[1] or "/"
            body = json.loads(request.content)
            self.calls.append((path, body))
            if path in self.raise_on:
                raise httpx.ConnectError("hub down")
            if path == "/admit":
                return httpx.Response(self.admit_status, json=self.admit_response)
            return httpx.Response(200, json={"ok": True})

        self.client = HubClient(
            base_url="https://hub.test",
            token="tok",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    def paths(self) -> list[str]:
        return [p for p, _ in self.calls]


@pytest.fixture
def api():
    set_store_for_tests(InMemoryStore())

    def _use(hub: FakeHub | None):
        app.dependency_overrides[get_hub_client] = lambda: hub.client if hub else None

    with TestClient(app) as c:
        yield c, _use
    app.dependency_overrides.pop(get_hub_client, None)


@pytest.mark.parametrize(
    ("reason", "code"),
    [
        ("no_tokens", 402),
        ("boost_unavailable", 402),
        ("concurrency", 429),
        ("jobs_cap", 429),
        ("minutes_cap", 429),
        ("streams_cap", 429),
        ("upload_too_large", 413),
        ("video_too_long", 413),
        ("storage_full", 413),
        ("something_new", 402),
        (None, 402),
    ],
)
def test_reason_mapping(reason, code):
    assert status_for_reason(reason) == code


def test_backtest_admitted_and_settled_succeeded(api):
    c, use = api
    hub = FakeHub()
    use(hub)
    r = c.post("/api/backtests", headers=AUTH, json=BT)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["reservation_id"] == "res-1"
    assert body["optimistic"] is True
    assert hub.paths() == ["/admit", "/settle"]
    admit = hub.calls[0][1]
    assert admit["class"] == "job"
    assert admit["external_user_id"] == str(USER)
    assert admit["operation"] == "backtests.run"
    assert admit["external_job_id"] == body["job_id"]
    assert admit["est_tokens"] > 0
    assert hub.calls[1][1] == {"reservation_id": "res-1", "outcome": "succeeded"}


@pytest.mark.parametrize(
    ("reason", "code"), [("no_tokens", 402), ("concurrency", 429), ("storage_full", 413)]
)
def test_backtest_refused_maps_reason_and_does_not_settle(api, reason, code):
    c, use = api
    hub = FakeHub({"ok": True, "allowed": False, "reason": reason, "limits": {"x": 1}})
    use(hub)
    r = c.post("/api/backtests", headers=AUTH, json=BT)
    assert r.status_code == code
    detail = r.json()["detail"]
    assert detail["reason"] == reason
    assert detail["error"] == "usage_refused"
    assert hub.paths() == ["/admit"]


def test_hub_unreachable_fails_closed(api):
    c, use = api
    hub = FakeHub(raise_on={"/admit"})
    use(hub)
    r = c.post("/api/backtests", headers=AUTH, json=BT)
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "hub_unavailable"


def test_hub_5xx_fails_closed(api):
    c, use = api
    use(FakeHub(admit_status=502))
    assert c.post("/api/backtests", headers=AUTH, json=BT).status_code == 503


def test_hub_permanent_4xx_fails_closed(api):
    c, use = api
    use(FakeHub({"error": "unknown user_id"}, admit_status=404))
    assert c.post("/api/backtests", headers=AUTH, json=BT).status_code == 503


def test_no_hub_configured_bypasses_admission(api):
    c, use = api
    use(None)
    r = c.post("/api/backtests", headers=AUTH, json=BT)
    assert r.status_code == 202
    assert r.json()["reservation_id"] is None


def test_from_env_unset_means_dev(monkeypatch):
    monkeypatch.delenv("CHALYB_BASE_URL", raising=False)
    assert HubClient.from_env() is None
    monkeypatch.setenv("CHALYB_BASE_URL", "https://hub.test")
    assert HubClient.from_env() is not None


async def test_settle_failed_when_work_raises():
    hub = FakeHub()
    with pytest.raises(RuntimeError):
        async with admitted(hub.client, user_id=USER, operation="x", external_job_id="j"):
            raise RuntimeError("work blew up")
    assert hub.calls[-1] == ("/settle", {"reservation_id": "res-1", "outcome": "failed"})


async def test_settle_failure_does_not_mask_result():
    hub = FakeHub(raise_on={"/settle"})
    async with admitted(hub.client, user_id=USER, operation="x", external_job_id="j") as adm:
        result = adm.reservation_id
    assert result == "res-1"
    assert hub.paths() == ["/admit", "/settle"]


async def test_refusal_raises_http_exception_before_work():
    hub = FakeHub({"ok": True, "allowed": False, "reason": "jobs_cap"})
    ran = False
    with pytest.raises(HTTPException) as ei:
        async with admitted(hub.client, user_id=USER, operation="x", external_job_id="j"):
            ran = True
    assert ei.value.status_code == 429
    assert ran is False
