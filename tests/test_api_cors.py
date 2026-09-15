"""CORS middleware — allowed origins + preflight handling."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def test_cors_allows_default_localhost(monkeypatch):
    monkeypatch.delenv("CHALYBCRYPTO_CORS_ORIGINS", raising=False)
    # main is module-level configured; reload it so the env change takes effect
    import importlib
    from chalybcrypto_api import main as main_mod

    importlib.reload(main_mod)
    with TestClient(main_mod.app) as c:
        r = c.options(
            "/api/signals",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )
    # FastAPI/Starlette CORS responds 200 to allowed preflights with the right headers
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_cors_blocks_unconfigured_origin(monkeypatch):
    monkeypatch.delenv("CHALYBCRYPTO_CORS_ORIGINS", raising=False)
    import importlib
    from chalybcrypto_api import main as main_mod

    importlib.reload(main_mod)
    with TestClient(main_mod.app) as c:
        r = c.options(
            "/api/signals",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "GET",
            },
        )
    # Starlette returns 400 for disallowed preflight + no allow-origin header
    assert r.headers.get("access-control-allow-origin") is None


def test_cors_allows_configured_production_origin(monkeypatch):
    monkeypatch.setenv("CHALYBCRYPTO_CORS_ORIGINS", "https://chalyb.com")
    import importlib
    from chalybcrypto_api import main as main_mod

    importlib.reload(main_mod)
    with TestClient(main_mod.app) as c:
        r = c.options(
            "/api/signals",
            headers={
                "Origin": "https://chalyb.com",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "https://chalyb.com"
