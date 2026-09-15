"""Bitunix signing — canonical ordering + double SHA256.

Per https://www.bitunix.com/api-docs/futures/common/sign.html.
"""

from __future__ import annotations

import hashlib

from chalybcrypto_connectors.bitunix.auth import (
    build_signed_headers,
    canonical_body,
    canonical_query,
    sign_request,
)


def _h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def test_canonical_query_ascii_sorted_concatenated():
    # The official example: queryParams = "id1uid200" comes from {"uid":"200", "id":"1"}.
    assert canonical_query({"uid": "200", "id": "1"}) == "id1uid200"
    # Empty cases.
    assert canonical_query(None) == ""
    assert canonical_query({}) == ""


def test_canonical_body_no_spaces():
    body = {"uid": "2899", "arr": [{"id": 1, "name": "maple"}, {"id": 2, "name": "lily"}]}
    out = canonical_body(body)
    assert " " not in out
    assert '"uid":"2899"' in out


def test_canonical_body_empty():
    assert canonical_body(None) == ""
    assert canonical_body({}) == ""


def test_sign_request_matches_double_sha256_definition():
    nonce = "123456"
    timestamp = "20241120123045"
    api_key = "yourApiKey"
    query = "id1uid200"
    body = '{"uid":"2899","arr":[{"id":1,"name":"maple"},{"id":2,"name":"lily"}]}'
    secret = "yourSecret"

    expected_digest = _h(nonce + timestamp + api_key + query + body)
    expected_sign = _h(expected_digest + secret)

    actual = sign_request(
        api_key=api_key,
        secret_key=secret,
        nonce=nonce,
        timestamp=timestamp,
        query_str=query,
        body_str=body,
    )
    assert actual == expected_sign


def test_build_signed_headers_carries_through_all_fields():
    h = build_signed_headers(
        api_key="k",
        secret_key="s",
        params={"symbol": "BTCUSDT"},
        body=None,
        nonce="n1",
        timestamp_ms=1764979200000,
    )
    d = h.as_dict()
    assert d["api-key"] == "k"
    assert d["nonce"] == "n1"
    assert d["timestamp"] == "1764979200000"
    assert d["Content-Type"] == "application/json"
    assert "sign" in d and len(d["sign"]) == 64


def test_secret_never_appears_in_signed_headers():
    # CLAUDE.md rule 7: secrets never logged / returned.
    h = build_signed_headers(api_key="k", secret_key="SUPER_SECRET", body=None)
    d = h.as_dict()
    assert "SUPER_SECRET" not in repr(d)
    assert "SUPER_SECRET" not in d["sign"]  # output is a SHA256 digest, not the secret
