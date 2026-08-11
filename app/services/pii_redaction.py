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
    `<uuid>`, `Bearer <token>`, `Basic <credentials>`, `<aws-key-id>`,
    `<private-key>`, `<redacted>`, `<hex:N>`). Re-running
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
from typing import List, Optional, Pattern, Tuple

# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

# Order matters: more-specific patterns first. e.g. JWT (eyJ...) before the
# long-hex catch-all; bearer-prefixed value before the key=value catcher; etc.

# PEM private-key block (RSA/EC/DSA/OPENSSH/generic "PRIVATE KEY"). Matched
# FIRST and across newlines (DOTALL) because the base64 body would otherwise
# be shredded into <hex:N> / <email> / <ip> fragments by later patterns,
# leaving a recognisable (and partially-leaked) key carcass. We collapse the
# whole armoured block to a single marker.
_PEM_PRIVATE_KEY: Pattern[str] = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)

# Log line truncated mid-key: BEGIN marker present, END marker lost. Without
# this rule the armoured body sailed through (the paired pattern above needs a
# matching END), shipping half a private key to Discord / kg_log_observations.
# Applied AFTER the paired pattern, so it only sees unpaired leftovers; matches
# to end-of-text (everything after a dangling BEGIN is key material anyway).
_PEM_PRIVATE_KEY_TRUNCATED: Pattern[str] = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*$",
    re.DOTALL,
)

# AWS Access Key ID. `AKIA` = long-term IAM key, `ASIA` = temporary/STS key.
# Always exactly the 4-char prefix + 16 uppercase-alnum chars. NOT hex, so the
# long-hex catch-all never sees it — must be an explicit pattern. Placed before
# the kv-rule so `aws_access_key_id=AKIA...` still collapses the id cleanly.
_AWS_ACCESS_KEY_ID: Pattern[str] = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")

# Креды внутри URI: `scheme://user:password@host`. Самый частый секрет в
# pod-логах (connection string к PG/Redis/NATS в сообщении об ошибке) — и он
# не ловится ни kv-правилом (нет `password=`), ни long-hex. Требуем `://`
# перед и `@` после, поэтому ложных срабатываний на обычном тексте нет.
_URI_CREDENTIALS: Pattern[str] = re.compile(
    r"(?<=://)([^\s:/@]+):([^\s:/@]+)(?=@)"
)

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

# `Basic <base64>` (HTTP Basic Authorization header). The base64 blob encodes
# `user:password`, so it must be scrubbed just like Bearer. Case-insensitive on
# `Basic`; we keep the literal "Basic" so logs stay readable.
_BASIC_AUTH: Pattern[str] = re.compile(
    r"\bBasic\s+[A-Za-z0-9+/]{8,}={0,2}",
    re.IGNORECASE,
)

# Вендорные API-токены с фиксированным префиксом. «Голый» токен (вне key=value
# и вне Authorization-заголовка) не ловится ни kv-правилом, ни bearer/basic-
# правилами, ни long-hex catch-all-ом (алфавит base64url, не hex) — прогон по
# реальным строкам это подтвердил. Каждому вендору — явный префиксный паттерн:
# префиксы достаточно уникальны, чтобы не давать ложных срабатываний на
# обычном тексте. Агрессивные «похоже-на-base64» эвристики намеренно НЕ
# используем — они бы порезали обычные слова/идентификаторы в логах.
_VENDOR_TOKENS: List[Tuple[Pattern[str], str]] = [
    # Anthropic: sk-ant-api03-... (base64url + дефисы). Case-sensitive: реальный
    # префикс всегда lowercase, а `\b` не даёт матчиться внутри слов ("risk-ant…").
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}"), "<anthropic-key>"),
    # Slack: xox[baprs]-… (bot/app/legacy/refresh/session). Тело — цифры и
    # base64url-сегменты через дефис.
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"), "<slack-token>"),
    # GitHub classic tokens: ghp_ (PAT), gho_/ghu_/ghs_/ghr_ (OAuth/app) —
    # один формат: префикс + 36+ alnum. Порог 20 отсекает случайные слова.
    (re.compile(r"\bgh[opsur]_[A-Za-z0-9]{20,}\b"), "<github-token>"),
    # GitHub fine-grained PAT: github_pat_<22>_<59>, алфавит с underscore.
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "<github-token>"),
    # Google API key: AIza + 35 символов [0-9A-Za-z_\-] (полный ключ = 39).
    # `{35,}` — чтобы дожевать хвост, если ключ длиннее канонического.
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35,}"), "<google-api-key>"),
    # Stripe: sk_live_/sk_test_ (secret) и rk_live_/rk_test_ (restricted).
    (re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{10,}\b"), "<stripe-key>"),
]

# UUID v1-v5 (8-4-4-4-12 hex). Must come BEFORE long-hex catch-all.
_UUID: Pattern[str] = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)

# Sensitive key=value or key: value. value is anything until the next
# whitespace, ampersand, semicolon, comma, or quote. Case-insensitive name.
# Note: applied AFTER bearer so `Authorization: Bearer <x>` is handled by
# _BEARER first.
#
# The key may carry a word-char prefix (`aws_secret_access_key`, `api_secret`,
# `client_secret`, `refresh_token`, ...) — a bare `\b` before the keyword can
# never match after `_` (it's a word char), which silently skipped every
# underscore-prefixed secret key. The keyword must sit at the END of the key,
# right before the separator, so `max_tokens=1024` stays untouched.
_KV_SECRET: Pattern[str] = re.compile(
    r"(?<![A-Za-z0-9])"
    r"([A-Za-z0-9_\-]*"
    r"(?:password|passwd|pwd|token|secret|api[_\-]?key|access[_\-]?key|auth[_\-]?token)"
    r")"
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


def redact_pii(text: str, max_len: Optional[int] = _MAX_LEN) -> str:
    """Redact PII / credentials from `text` and truncate to `max_len`.

    Returns a redacted, possibly truncated string. Empty / None-equivalent
    inputs return empty string.

    `max_len` — граница усечения результата (по умолчанию `_MAX_LEN`, как
    исторически для Discord/KG-пути). `None` = без усечения: нужен для
    pod-логов, уходящих в LLM-контекст, где 500 символов срезали бы весь
    диагностический хвост (redact — да, truncate — нет).

    Order of operations:
        1.  PEM key      → `<private-key>` (whole armoured block, DOTALL;
                           then a dangling BEGIN-without-END tail — a log
                           line truncated mid-key must not leak the body)
        1b. URI creds    → `user:<redacted>` (`scheme://user:password@host`)
        2.  Emails       → `<email>`
        3.  JWT          → `<jwt>`
        4.  AWS key id   → `<aws-key-id>` (AKIA.../ASIA...)
        5.  Bearer ...   → `Bearer <token>`
        6.  Basic ...    → `Basic <credentials>`
        7.  Vendor keys  → `<anthropic-key>` / `<slack-token>` /
                           `<github-token>` / `<google-api-key>` /
                           `<stripe-key>` (голые префиксные токены)
        8.  UUIDs        → `<uuid>`
        9.  key=value    → `key=<redacted>` (password/token/secret/api_key/...
                           incl. underscore-prefixed keys like
                           aws_secret_access_key / client_secret / refresh_token)
        10. Long hex     → `<hex:N>`
        11. IPv6         → `<ip>`
        12. IPv4         → `<ip>`
        13. Truncate     → first `max_len` chars + marker if longer

    PEM blocks are scrubbed first so the base64 body isn't shredded into
    `<hex:N>` / `<email>` fragments (which would leave a partial key carcass).
    JWT is processed before long-hex even though base64 chars aren't hex —
    the explicit `eyJ` prefix is the strongest signal. AWS key ids are
    explicit because they're uppercase-alnum (not hex) and would otherwise
    slip past the long-hex catch-all. UUID is processed before long-hex
    because UUIDs share the hex alphabet.
    """
    if not text:
        return ""

    out = text

    out = _PEM_PRIVATE_KEY.sub("<private-key>", out)
    out = _PEM_PRIVATE_KEY_TRUNCATED.sub("<private-key>", out)
    # До email/ip: в `postgres://svc:pw@host` пароль иначе доживёт до вывода.
    out = _URI_CREDENTIALS.sub(r"\1:<redacted>", out)
    out = _EMAIL.sub("<email>", out)
    out = _JWT.sub("<jwt>", out)
    out = _AWS_ACCESS_KEY_ID.sub("<aws-key-id>", out)
    out = _BEARER.sub("Bearer <token>", out)
    out = _BASIC_AUTH.sub("Basic <credentials>", out)
    # Вендорные токены — до kv-правила: `token=sk-ant-...` тоже схлопнется
    # (kv-правило потом заменит уже-обезличенный placeholder на <redacted>),
    # а «голый» токен без key= иначе не поймал бы никто.
    for pattern, placeholder in _VENDOR_TOKENS:
        out = pattern.sub(placeholder, out)
    out = _UUID.sub("<uuid>", out)
    out = _KV_SECRET.sub(_kv_replace, out)
    out = _LONG_HEX.sub(lambda m: f"<hex:{len(m.group(0))}>", out)
    out = _IPV6.sub("<ip>", out)
    out = _IPV4.sub("<ip>", out)

    if max_len is not None and len(out) > max_len:
        # Reserve room for the marker so the final string is `max_len` chars.
        head = out[: max_len - len(_TRUNCATE_MARKER)]
        out = f"{head}{_TRUNCATE_MARKER}"
    return out
