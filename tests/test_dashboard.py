"""Dashboard route smoke tests."""

from __future__ import annotations

from fastapi.testclient import TestClient

from chalybcrypto_api.main import app


def test_root_redirects_to_dashboard():
    with TestClient(app) as c:
        r = c.get("/", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/dashboard"


def test_dashboard_serves_html_with_expected_content():
    with TestClient(app) as c:
        r = c.get("/dashboard")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    body = r.text
    # Spanish-first label
    assert "Panel" in body
    # Pulls real API endpoints
    assert "/api/health" in body
    assert "/api/strategies" in body
    assert "/api/signals" in body
    # OPTIMISTA label travels per CLAUDE.md
    assert "OPTIMISTA" in body
