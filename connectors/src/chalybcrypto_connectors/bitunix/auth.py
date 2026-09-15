"""Bitunix request signing.

Per https://www.bitunix.com/api-docs/futures/common/sign.html the scheme is:

    queryStr = "".join(f"{k}{v}" for k, v in sorted(params.items()))   # ASCII-ascending
    bodyStr  = json.dumps(body, separators=(",", ":"))                 # no spaces
    digest   = SHA256(nonce + timestamp + apiKey + queryStr + bodyStr)
    sign     = SHA256(digest + secretKey)

Notes:
  * It's plain SHA256, not HMAC.
  * `timestamp` is milliseconds since epoch as a string (per the API introduction).
  * `nonce` is a per-request random string.
  * `body` is the raw POST JSON, whitespace-removed. For GETs, bodyStr is "".
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Mapping


def _hex_sha256(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def canonical_query(params: Mapping[str, object] | None) -> str:
    """Join params as key+value in ASCII-ascending key order. Empty dict → ""."""
    if not params:
        return ""
    return "".join(f"{k}{v}" for k, v in sorted(params.items()))


def canonical_body(body: object | None) -> str:
    """JSON-encode with no whitespace. None or empty → ""."""
    if body is None or body == {} or body == []:
        return ""
    if isinstance(body, str):
        # caller already serialized
        return body
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False)


def sign_request(
    *,
    api_key: str,
    secret_key: str,
    nonce: str,
    timestamp: str,
    query_str: str,
    body_str: str,
) -> str:
    digest = _hex_sha256(nonce + timestamp + api_key + query_str + body_str)
    return _hex_sha256(digest + secret_key)


@dataclass(frozen=True)
class SignedHeaders:
    api_key: str
    nonce: str
    timestamp: str
    sign: str

    def as_dict(self) -> dict[str, str]:
        return {
            "api-key": self.api_key,
            "nonce": self.nonce,
            "timestamp": self.timestamp,
            "sign": self.sign,
            "Content-Type": "application/json",
            "language": "en-US",
        }


def build_signed_headers(
    *,
    api_key: str,
    secret_key: str,
    params: Mapping[str, object] | None = None,
    body: object | None = None,
    nonce: str | None = None,
    timestamp_ms: int | None = None,
) -> SignedHeaders:
    """Convenience: derive nonce + timestamp if not supplied, return the four signed headers."""
    n = nonce or secrets.token_hex(16)
    ts = str(timestamp_ms) if timestamp_ms is not None else str(int(time.time() * 1000))
    sig = sign_request(
        api_key=api_key,
        secret_key=secret_key,
        nonce=n,
        timestamp=ts,
        query_str=canonical_query(params),
        body_str=canonical_body(body),
    )
    return SignedHeaders(api_key=api_key, nonce=n, timestamp=ts, sign=sig)
