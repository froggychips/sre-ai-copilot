"""PII / secret redaction for log samples shipped to Discord / KG.

Why:
    Seq stack-traces in Error/Fatal events regularly contain emails,
    IPs, JWTs, bearer tokens, request payloads, etc. We persist these
    raw into `kg_log_observations.sample_message` and then echo them
    back into Discord embeds via `discord_service._build_log_error_rate_field`.
    Neither path was sanitising before this module.

Applied:
    - WRITE-time in `app.knowledge_graph.seq_logs_sync` before upsert.
    - Defense-in-depth READ-time in `app.services.discord_service`
      when rendering the embed (in case a future source forgets to
      redact on write).

Idempotency:
    Patterns replace with fixed tokens (`<email>`, `<ip>`, `<jwt>`,
    `<uuid>`, `Bearer <token>`, `<redacted>`, `<hex:N>`). Re-running
    redact on already-redacted text is a no-op (the placeholders do
    not match the source patterns), so apply-once-on-write is enough,
    but defense-in-depth is cheap.

Limits:
    Result is truncated to `_MAX_LEN` chars with a marker. Discord
    embed field-value limit is 1024; we keep 500 as a safer cap that
    matches the existing `[:200]` slice in discord_service but leaves
    headroom.
"""
from __future__ import annotations

import re
from typing import Pattern

# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

# Order matters: more-specific patterns first. e.g. JWT (eyJ...) before the
# long-hex catch-all; bearer-prefixed value before the key=value catcher; etc.

# RFC-light email. We deliberately don't try to follow RFC 5322 — too greedy
# regexes have caused CPU bombs in the wild. `\S` excluded so we don't eat
# spaces, and we cap the local-part at 64 / domain-part at 253.
_EMAIL: Pattern[str] = re.compile(
    r"\b[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,253}\.[A-Za-z]{2,24}\b"
)

# JWT (three base64url segments separated by dots, starts with eyJ).
# Must come BEFORE long-hex / uuid because the segments are base64, not hex.
_JWT: Pattern[str] = re.compile(
    r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b"
)

# `Bearer <token>` (Authorization header). Case-insensitive on `Bearer`.
# We keep the literal "Bearer" so logs remain readable but strip the secret.
_BEARER: Pattern[str] = re.compile(
    r"\bBearer\s+[A-Za-z0-9_\-\.=\+/]{8,}",
    re.IGNORECASE,
)

# UUID v1-v5 (8-4-4-4-12 hex). Must come BEFORE long-hex catch-all.
_UUID: Pattern[str] = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)

# Sensitive key=value or key: value. value is anything until the next
# whitespace, ampersand, semicolon, comma, or quote. Case-insensitive name.
# Note: applied AFTER bearer so `Authorization: Bearer <x>` is handled by
# _BEARER first.
_KV_SECRET: Pattern[str] = re.compile(
    r"\b(password|passwd|pwd|token|secret|api[_\-]?key|access[_\-]?key|auth[_\-]?token)"
    r"\s*[:=]\s*"
    r"(['\"]?)([^\s&;,'\"]+)\2",
    re.IGNORECASE,
)

# Long hex string (>= 32 chars). Catches md5/sha1/sha256/sha512 and most
# opaque secret blobs that aren't JWT/UUID. Word-boundary anchored.
_LONG_HEX: Pattern[str] = re.compile(r"\b[0-9a-fA-F]{32,}\b")

# IPv4 (basic 0-255 quad). Includes loopback and RFC1918 — we redact those
# too because pod-IPs leak topology even though they're not public.
_IPV4: Pattern[str] = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b"
)

# IPv6 — basic recognition. Covers full form (8x4 hex), `::` short form,
# and IPv4-mapped (::ffff:1.2.3.4). Not RFC-perfect; good enough for
# scrub-purposes (any false-positive simply gets `<ip>`).
_IPV6: Pattern[str] = re.compile(
    r"(?<![:.\w])"  # not preceded by hex/dot/colon (avoid mid-word)
    r"(?:"
    r"[0-9a-fA-F]{1,4}(?::[0-9a-fA-F]{1,4}){7}"  # full
    r"|(?:[0-9a-fA-F]{1,4}:){1,7}:"              # trailing ::
    r"|:(?::[0-9a-fA-F]{1,4}){1,7}"              # leading ::
    r"|(?:[0-9a-fA-F]{1,4}:){1,6}:[0-9a-fA-F]{1,4}"  # mid ::
    r"|::ffff:(?:\d{1,3}\.){3}\d{1,3}"           # v4-mapped
    r")"
    r"(?![:.\w])"
)


_MAX_LEN = 500
_TRUNCATE_MARKER = "… [truncated]"


def _kv_replace(match: "re.Match[str]") -> str:
    """Keep the `key=` part visible, replace the value with `<redacted>`."""
    key = match.group(1)
    quote = match.group(2) or ""
    sep_match = re.search(r"[:=]", match.group(0))
    sep = sep_match.group(0) if sep_match else "="
    return f"{key}{sep}{quote}<redacted>{quote}"


def redact_pii(text: str) -> str:
    """Redact PII / credentials from `text` and truncate to `_MAX_LEN`.

    Returns a redacted, possibly truncated string. Empty / None-equivalent
    inputs return empty string.

    Order of operations:
        1. Emails       → `<email>`
        2. JWT          → `<jwt>`
        3. Bearer ...   → `Bearer <token>`
        4. UUIDs        → `<uuid>`
        5. key=value    → `key=<redacted>` (password/token/secret/api_key/...)
        6. Long hex     → `<hex:N>`
        7. IPv6         → `<ip>`
        8. IPv4         → `<ip>`
        9. Truncate     → first `_MAX_LEN` chars + marker if longer

    JWT is processed before long-hex even though base64 chars aren't hex —
    the explicit `eyJ` prefix is the strongest signal. UUID is processed
    before long-hex because UUIDs share the hex alphabet.
    """
    if not text:
        return ""

    out = text

    out = _EMAIL.sub("<email>", out)
    out = _JWT.sub("<jwt>", out)
    out = _BEARER.sub("Bearer <token>", out)
    out = _UUID.sub("<uuid>", out)
    out = _KV_SECRET.sub(_kv_replace, out)
    out = _LONG_HEX.sub(lambda m: f"<hex:{len(m.group(0))}>", out)
    out = _IPV6.sub("<ip>", out)
    out = _IPV4.sub("<ip>", out)

    if len(out) > _MAX_LEN:
        # Reserve room for the marker so the final string is `_MAX_LEN` chars.
        head = out[: _MAX_LEN - len(_TRUNCATE_MARKER)]
        out = f"{head}{_TRUNCATE_MARKER}"
    return out
