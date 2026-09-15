from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Any


def _normalize(value: Any) -> str:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, float):
        return format(Decimal(str(value)).normalize(), "f")
    if isinstance(value, str):
        return value.strip().lower()
    if value is None:
        return ""
    return str(value)


def dedup_hash(*parts: Any) -> str:
    """Stable SHA-256 dedup hash for idempotency keys and signal dedup.

    Order-sensitive; pass fields in a fixed canonical order at the call site.
    Decimals are normalized so 1.50 and 1.5 hash equal.
    """
    joined = "|".join(_normalize(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()
