"""The person's language for the engine's visible text and links back to the hub.

The hub passes `lang` (en | es) on the SSO launch URL (hub PR #174). Spanish is
the default and keeps every URL and text exactly as before; English only when
something explicitly says `en`.
"""

from __future__ import annotations

from typing import Literal

Lang = Literal["es", "en"]

DEFAULT_LANG: Lang = "es"
# Remembers the SSO `lang` on the engine's own domain so later redirects to the
# hub (/, /dashboard) keep it.
LANG_COOKIE = "chalyb_lang"
LANG_COOKIE_MAX_AGE = 60 * 60 * 24 * 365


def normalize_lang(raw: str | None) -> Lang | None:
    """'en', 'en-US', 'EN' → 'en'; 'es', 'es-MX' → 'es'; anything else → None."""
    if not raw:
        return None
    base = raw.strip().lower().replace("_", "-").split("-", 1)[0]
    if base == "en":
        return "en"
    if base == "es":
        return "es"
    return None


def lang_from_accept_language(header: str | None) -> Lang | None:
    """The first en/es language in an Accept-Language header, in the header's order."""
    if not header:
        return None
    for part in header.split(","):
        lang = normalize_lang(part.split(";", 1)[0])
        if lang:
            return lang
    return None


def hub_path(path: str, lang: str | None) -> str:
    """A hub path in `lang`: the hub's English pages live under /en, Spanish has no prefix."""
    return f"/en{path}" if normalize_lang(lang) == "en" else path
