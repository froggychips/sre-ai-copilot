"""Tests for app.services.pii_redaction.redact_pii.

Covers each pattern catalogued in the module docstring:
    - emails
    - IPv4 / IPv6
    - JWT (eyJ... three base64 segments)
    - Bearer <opaque>
    - UUIDs
    - long hex (>= 32) → <hex:N>
    - password=/token=/secret=/api_key= key/value pairs
    - truncation at 500 chars
    - idempotency (run twice → same output)
    - empty / falsy input
"""
from __future__ import annotations

from app.services.pii_redaction import redact_pii


# ---------------------------------------------------------------------------
# Emails
# ---------------------------------------------------------------------------

def test_redact_email_simple():
    assert redact_pii("contact yar.shulgin@gmail.com for details") == \
        "contact <email> for details"


def test_redact_multiple_emails():
    out = redact_pii("from a@b.co to user.name+tag@sub.example.org")
    assert "a@b.co" not in out
    assert "user.name+tag@sub.example.org" not in out
    assert out.count("<email>") == 2


def test_email_not_eating_spaces():
    # Adjacent text after email should remain intact (no greedy match across whitespace).
    out = redact_pii("user@example.com sent payload")
    assert out == "<email> sent payload"


# ---------------------------------------------------------------------------
# IPv4
# ---------------------------------------------------------------------------

def test_redact_ipv4():
    assert redact_pii("connect to 10.42.13.7:5432") == "connect to <ip>:5432"


def test_redact_ipv4_public_and_private():
    out = redact_pii("public=8.8.8.8 private=192.168.1.1 loopback=127.0.0.1")
    assert "8.8.8.8" not in out
    assert "192.168.1.1" not in out
    assert "127.0.0.1" not in out
    assert out.count("<ip>") == 3


def test_ipv4_does_not_eat_version_numbers():
    # 4-segment version like "1.2.3" should NOT trigger; "1.2.3.4" should.
    out = redact_pii("version 1.2.3 and host 1.2.3.4")
    assert "1.2.3 " in out
    assert "1.2.3.4" not in out


# ---------------------------------------------------------------------------
# IPv6
# ---------------------------------------------------------------------------

def test_redact_ipv6_full():
    out = redact_pii("addr 2001:0db8:85a3:0000:0000:8a2e:0370:7334 end")
    assert "2001:0db8" not in out
    assert "<ip>" in out


def test_redact_ipv6_short():
    out = redact_pii("loopback ::1 listening")
    # ::1 is matched by the trailing-:: / leading-:: pattern.
    assert "::1" not in out
    assert "<ip>" in out


def test_redact_ipv6_v4_mapped():
    out = redact_pii("mapped ::ffff:192.168.1.1 here")
    assert "192.168.1.1" not in out
    assert "<ip>" in out


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------

def test_redact_jwt():
    jwt = (
        "eyJhbGciOiJIUzI1NiJ9"
        ".eyJzdWIiOiIxMjM0NSIsIm5hbWUiOiJqb2huIn0"
        ".dozjgNryP4J3jVmNHl0w5N_XgL0iZqzCQ4Mb7sQ"
    )
    out = redact_pii(f"Authorization=Bearer {jwt}; ok")
    # JWT (eyJ...) gets <jwt>. The "Bearer eyJ..." substring is matched as
    # bearer pattern first — that's fine, either way the JWT body is gone.
    assert jwt not in out
    assert "eyJ" not in out


def test_redact_jwt_standalone():
    jwt = "eyJabc.eyJdef.signaturePart_123-XYZ"
    # Use a non-sensitive key prefix so the kv-rule doesn't shadow the JWT
    # pattern. ("token:" matches the kv-rule first and yields <redacted>,
    # which is also acceptable security-wise but the test wants <jwt>.)
    out = redact_pii(f"saw {jwt} expires soon")
    assert "<jwt>" in out
    assert jwt not in out


def test_redact_jwt_inside_sensitive_key_value():
    """When JWT is the value of a `token:` field, kv-rule wins → <redacted>.

    Either outcome scrubs the secret; both are acceptable.
    """
    jwt = "eyJabc.eyJdef.signaturePart_123-XYZ"
    out = redact_pii(f"token: {jwt} expires soon")
    assert jwt not in out
    assert "<redacted>" in out or "<jwt>" in out


# ---------------------------------------------------------------------------
# Bearer
# ---------------------------------------------------------------------------

def test_redact_bearer_opaque():
    out = redact_pii("Authorization: Bearer abcdefghij1234567890XYZ_opaque-token")
    assert "Bearer <token>" in out
    assert "opaque-token" not in out


def test_redact_bearer_case_insensitive():
    out = redact_pii("BEARER xyz123abc456")
    assert "Bearer <token>" in out


def test_redact_bearer_keeps_subsequent_text():
    # Make sure subsequent content after the token (separated by whitespace)
    # is preserved.
    out = redact_pii("Bearer abcdef1234567890 ; charset=utf-8")
    assert "Bearer <token>" in out
    assert "charset=utf-8" in out  # the kv catcher does not include charset


# ---------------------------------------------------------------------------
# Basic auth
# ---------------------------------------------------------------------------

def test_redact_basic_auth():
    # base64("admin:hunter2") = "YWRtaW46aHVudGVyMg=="
    out = redact_pii("Authorization: Basic YWRtaW46aHVudGVyMg==")
    assert "Basic <credentials>" in out
    assert "YWRtaW46aHVudGVyMg" not in out


def test_redact_basic_auth_case_insensitive():
    out = redact_pii("BASIC dXNlcjpwYXNzd29yZA==")
    assert "Basic <credentials>" in out
    assert "dXNlcjpwYXNzd29yZA" not in out


def test_redact_basic_auth_keeps_subsequent_text():
    out = redact_pii("Basic YWRtaW46c2VjcmV0Cg== ; charset=utf-8")
    assert "Basic <credentials>" in out
    assert "charset=utf-8" in out


def test_basic_auth_does_not_eat_plain_word():
    # The bare English word "Basic" with non-base64 short token must stay put.
    out = redact_pii("this is Basic stuff")
    assert out == "this is Basic stuff"


# ---------------------------------------------------------------------------
# AWS Access Key ID
# ---------------------------------------------------------------------------

def test_redact_aws_access_key_id():
    out = redact_pii("creds AKIAIOSFODNN7EXAMPLE leaked")
    assert "<aws-key-id>" in out
    assert "AKIAIOSFODNN7EXAMPLE" not in out


def test_redact_aws_temporary_key_id():
    # Собираем из частей, чтобы непрерывный ASIA-литерал не попадал в исходник
    # и не триггерил GitHub secret-scanning (это фейковая фикстура, не ключ).
    fake_temp_key = "ASIA" + "Y34FZKBOKMUTVV7A"
    out = redact_pii(f"sts {fake_temp_key} active")
    assert "<aws-key-id>" in out
    assert fake_temp_key not in out


def test_redact_aws_key_in_kv_pair():
    out = redact_pii("aws_access_key_id=AKIAIOSFODNN7EXAMPLE region=eu")
    # AWS pattern runs before the kv-rule; the id itself is gone either way.
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "region=eu" in out


def test_aws_key_does_not_eat_lowercase_or_short():
    # Lowercase "akia..." or wrong length must NOT be treated as a key id.
    out = redact_pii("word akiaIOSFODNN7example and AKIA12345 short")
    assert "<aws-key-id>" not in out
    assert "akiaIOSFODNN7example" in out
    assert "AKIA12345" in out


# ---------------------------------------------------------------------------
# PEM private-key block
# ---------------------------------------------------------------------------

def test_redact_pem_private_key_block():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA1234567890abcdefABCDEF/+xyz\n"
        "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MGFiY2RlZg==\n"
        "-----END RSA PRIVATE KEY-----"
    )
    out = redact_pii(f"loaded key:\n{pem}\ndone")
    assert "<private-key>" in out
    assert "BEGIN RSA PRIVATE KEY" not in out
    assert "MIIEpAIBAAKCAQEA" not in out
    assert "loaded key:" in out
    assert "done" in out


def test_redact_pem_openssh_and_ec_variants():
    for label in ("OPENSSH", "EC", ""):
        head = f"{label} " if label else ""
        pem = (
            f"-----BEGIN {head}PRIVATE KEY-----\n"
            "c2VjcmV0a2V5bWF0ZXJpYWxoZXJlMTIzNDU2Nzg5MA==\n"
            f"-----END {head}PRIVATE KEY-----"
        )
        out = redact_pii(pem)
        assert out == "<private-key>", f"variant {label!r} not collapsed: {out!r}"


def test_pem_body_not_partially_leaked():
    # The base64 body must not survive as <hex:N> / <email> fragments.
    pem = (
        "-----BEGIN PRIVATE KEY-----\n"
        "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n"
        "-----END PRIVATE KEY-----"
    )
    out = redact_pii(pem)
    assert "<hex:" not in out
    assert "deadbeef" not in out
    assert out == "<private-key>"


def test_redact_truncated_pem_without_end_marker():
    # Log line cut mid-key: BEGIN present, END lost. The paired pattern
    # requires a matching END, so the body used to sail through.
    truncated = (
        "loading key\n"
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA1234567890abcdefABCDEF/+xyz\n"
        "Zm9vYmFyYmF6cXV4"
    )
    out = redact_pii(truncated)
    assert "<private-key>" in out
    assert "MIIEpAIBAAKCAQEA" not in out
    assert "Zm9vYmFy" not in out
    assert "loading key" in out


def test_truncated_pem_idempotent():
    truncated = "-----BEGIN PRIVATE KEY-----\nc2VjcmV0a2V5"
    once = redact_pii(truncated)
    assert once == redact_pii(once) == "<private-key>"


# ---------------------------------------------------------------------------
# UUID
# ---------------------------------------------------------------------------

def test_redact_uuid():
    out = redact_pii("request_id=550e8400-e29b-41d4-a716-446655440000 ok")
    # The "request_id=<value>" key is not in our sensitive-keys list,
    # so the value isn't redacted by the kv-rule. The UUID rule catches it.
    assert "<uuid>" in out
    assert "550e8400" not in out


def test_redact_uuid_uppercase():
    out = redact_pii("id 550E8400-E29B-41D4-A716-446655440000 done")
    assert "<uuid>" in out
    assert "550E8400" not in out


# ---------------------------------------------------------------------------
# Long hex (>= 32)
# ---------------------------------------------------------------------------

def test_redact_long_hex_sha256():
    sha256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    out = redact_pii(f"hash={sha256} size=0")
    # `hash=...` key is not in the sensitive-keys list so kv-rule skips it.
    # Long-hex catches the 64-char string.
    assert "<hex:64>" in out
    assert sha256 not in out


def test_redact_long_hex_minimum_threshold():
    # Exactly 32 chars hex → matched. 31 chars → NOT matched (preserve commits etc.).
    hex32 = "a" * 32
    hex31 = "b" * 31
    out = redact_pii(f"h32={hex32} h31={hex31}")
    assert "<hex:32>" in out
    assert hex31 in out  # short hex preserved (looks like a git short-sha range)


# ---------------------------------------------------------------------------
# password / token / secret / api_key kv
# ---------------------------------------------------------------------------

def test_redact_password_equals():
    out = redact_pii("login user password=hunter2 success")
    assert "password=<redacted>" in out
    assert "hunter2" not in out


def test_redact_token_equals():
    out = redact_pii("token=abc.123_XYZ-987 sent")
    assert "token=<redacted>" in out
    assert "abc.123_XYZ-987" not in out


def test_redact_secret_colon_form():
    out = redact_pii("config: secret: supersecretvalue123 done")
    assert "secret:<redacted>" in out or "secret: <redacted>" in out
    # implementation re-emits `key` + sep + redacted; whitespace after `:` is
    # consumed by `\s*` — accept either rendering.
    assert "supersecretvalue123" not in out


def test_redact_api_key_quoted():
    out = redact_pii('config api_key="abcDEF12345" loaded')
    assert "<redacted>" in out
    assert "abcDEF12345" not in out


def test_redact_case_insensitive_keys():
    out = redact_pii("PASSWORD=foo TOKEN=bar Secret=baz API_KEY=qux")
    assert "foo" not in out
    assert "bar" not in out
    assert "baz" not in out
    assert "qux" not in out


# ---------------------------------------------------------------------------
# Underscore-prefixed secret keys (regression: `\b` can't match after `_`)
# ---------------------------------------------------------------------------

def test_redact_aws_secret_access_key():
    # The AWS secret half is base64-alphabet (not hex), so _LONG_HEX never
    # catches it — the kv-rule is the only line of defence.
    out = redact_pii("aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
    assert "wJalrXUtnFEMI" not in out
    assert "aws_secret_access_key=<redacted>" in out


def test_redact_secret_access_key():
    out = redact_pii("secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY end")
    assert "wJalrXUtnFEMI" not in out
    assert "secret_access_key=<redacted>" in out
    assert "end" in out


def test_redact_api_secret():
    out = redact_pii("api_secret=sk_live_abcdef123456 sent")
    assert "sk_live_abcdef123456" not in out
    assert "api_secret=<redacted>" in out


def test_redact_client_secret_colon_form():
    out = redact_pii("oauth client_secret: GOCSPX-abc123def456 loaded")
    assert "GOCSPX-abc123def456" not in out
    assert "client_secret" in out
    assert "<redacted>" in out


def test_redact_refresh_token():
    out = redact_pii("refresh_token=1//0eXyZzy-refresh-value done")
    assert "0eXyZzy-refresh-value" not in out
    assert "refresh_token=<redacted>" in out


def test_redact_underscore_keys_uppercase():
    out = redact_pii("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY")
    assert "wJalrXUtnFEMIK7MDENG" not in out
    assert "<redacted>" in out


def test_kv_rule_does_not_eat_max_tokens():
    # Keyword must sit at the END of the key: `max_tokens=` is a plain
    # config knob, not a secret.
    out = redact_pii("llm call max_tokens=1024 ok")
    assert "max_tokens=1024" in out


# ---------------------------------------------------------------------------
# «Голые» вендорные токены (без key= и без Authorization-заголовка)
#
# Регрессия: прогон по реальным строкам показал, что такой токен не ловился
# НИЧЕМ — kv-правилу нужен `key=`, bearer/basic нужен заголовок, long-hex не
# видит base64url-алфавит. Токен уезжал в pod-логах в LLM-контекст и в Discord.
# Литералы собираются из частей, чтобы фикстуры не триггерили secret-scanning.
# ---------------------------------------------------------------------------

def test_redact_bare_anthropic_key():
    key = "sk-ant-" + "api03-AbCdEf1234567890_-XyZaBcDeF"
    out = redact_pii(f"llm call failed with {key} at retry 2")
    assert "<anthropic-key>" in out
    assert key not in out
    assert "sk-ant-" not in out
    assert "retry 2" in out


def test_redact_bare_slack_tokens_all_prefixes():
    for prefix in ("xoxb", "xoxa", "xoxp", "xoxr", "xoxs"):
        token = f"{prefix}-" + "1234567890-9876543210-AbCdEfGhIjKlMnOp"
        out = redact_pii(f"slack post rejected: {token}")
        assert "<slack-token>" in out, prefix
        assert token not in out


def test_redact_bare_github_classic_pat():
    token = "ghp" + "_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    out = redact_pii(f"git push failed, token {token} expired")
    assert "<github-token>" in out
    assert token not in out


def test_redact_bare_github_fine_grained_pat():
    token = "github" + "_pat_" + "11ABCDEFG0abcdefghij_" + "K" * 40
    out = redact_pii(f"gh api 401 for {token}")
    assert "<github-token>" in out
    assert token not in out


def test_redact_bare_google_api_key():
    key = "AIza" + "SyA" + "b" * 32
    out = redact_pii(f"maps request key={key} denied")
    # kv-правило (`key=`) тоже сработало бы; главное — ключа в выводе нет.
    assert key not in out
    assert "<google-api-key>" in out or "<redacted>" in out


def test_redact_bare_google_api_key_without_kv_prefix():
    key = "AIza" + "SyD" + "z" * 32
    out = redact_pii(f"quota exceeded for {key} in project foo")
    assert "<google-api-key>" in out
    assert key not in out
    assert "project foo" in out


def test_redact_bare_stripe_live_and_restricted_keys():
    for prefix in ("sk_live_", "sk_test_", "rk_live_"):
        token = prefix + "51AbCdEfGhIjKlMnOpQrSt"
        out = redact_pii(f"charge failed with {token}")
        assert "<stripe-key>" in out, prefix
        assert token not in out


def test_vendor_tokens_are_idempotent():
    raw = (
        "sk-ant-" + "api03-AbCdEf1234567890 "
        "xoxb-" + "1234567890-abcdefghijkl "
        "ghp" + "_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    )
    once = redact_pii(raw)
    assert once == redact_pii(once)
    assert "<anthropic-key>" in once
    assert "<slack-token>" in once
    assert "<github-token>" in once


# ---- анти-FP: обычные строки, похожие на префиксы, не режем ---------------

def test_vendor_patterns_do_not_eat_plain_words():
    text = (
        "risk-antenna calibration ok; ghost_pipeline restarted; "
        "AIzawa-san reviewed; task_live_migration done; sk_liver enzyme"
    )
    out = redact_pii(text)
    assert out == text


def test_vendor_patterns_do_not_eat_short_lookalikes():
    # Короткие хвосты (< порога) — это не токены: имена контейнеров, метки.
    text = "images ghp_v2 and sk-ant-1 and xoxb-1 remain readable"
    out = redact_pii(text)
    assert out == text


def test_vendor_patterns_do_not_eat_k8s_names_and_shas():
    text = (
        "pod town-service-7d9f4-abcde restarted; image "
        "docker.lastoasisgame.com/wo/town-service:1.42.0-rc3 pulled; "
        "commit a1b2c3d4e5f"
    )
    out = redact_pii(text)
    assert out == text


def test_google_pattern_requires_full_length():
    # AIza + 20 символов — не ключ (полный = AIza + 35).
    short = "AIza" + "b" * 20
    out = redact_pii(f"value {short} ignored")
    assert short in out
    assert "<google-api-key>" not in out


# ---------------------------------------------------------------------------
# Креды внутри URI (`scheme://user:password@host`)
# ---------------------------------------------------------------------------

def test_redact_uri_credentials():
    out = redact_pii("conn failed: postgres://svc_user:hunter2@db-host:5432/town")
    assert "hunter2" not in out
    assert "svc_user:<redacted>" in out
    assert "db-host" in out  # хост остаётся — он нужен для диагностики


def test_uri_credentials_do_not_eat_plain_urls():
    text = "GET https://api.lastoasisgame.com/v1/status returned 503"
    assert redact_pii(text) == text


def test_uri_credentials_idempotent():
    once = redact_pii("redis://user:p@ss-less@cache:6379")
    assert once == redact_pii(once)
    assert "<redacted>" in once


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------

def test_truncate_long_input():
    # Use non-hex character to avoid the long-hex rule swallowing it into
    # `<hex:1000>` before truncation kicks in. Truncation is the final step.
    text = "z" * 1000
    out = redact_pii(text)
    assert len(out) == 500
    assert out.endswith("[truncated]")


def test_truncate_marker_appended_only_when_over_limit():
    """Truncate marker is only added past _MAX_LEN, not on exact-fit content."""
    fit = "z" * 500
    assert redact_pii(fit) == fit  # exactly 500 chars, no marker added
    just_over = "z" * 501
    out = redact_pii(just_over)
    assert out.endswith("[truncated]")
    assert len(out) == 500


def test_no_truncation_under_limit():
    text = "short message"
    out = redact_pii(text)
    assert out == "short message"
    assert "[truncated]" not in out


def test_max_len_none_disables_truncation():
    """pod-логи в LLM-контекст редактируем, но НЕ режем до 500 символов.

    Иначе `logs.py` / `k8s_facts.py` отдали бы модели только голову блоба, а
    диагностика (stacktrace, exit reason) живёт в хвосте.
    """
    text = "z" * 2000
    out = redact_pii(text, max_len=None)
    assert out == text
    assert "[truncated]" not in out


def test_max_len_custom_value():
    out = redact_pii("y" * 300, max_len=100)
    assert len(out) == 100
    assert out.endswith("[truncated]")


def test_max_len_none_still_redacts():
    out = redact_pii("user a@b.co from 10.0.0.1 " + "z" * 900, max_len=None)
    assert "a@b.co" not in out
    assert "10.0.0.1" not in out
    assert len(out) > 500


# ---------------------------------------------------------------------------
# Idempotency & empty input
# ---------------------------------------------------------------------------

def test_empty_input():
    assert redact_pii("") == ""
    assert redact_pii(None) == ""  # type: ignore[arg-type]


def test_idempotent_on_redacted_output():
    """Running redact_pii twice must give the same output (placeholders don't
    match source patterns)."""
    raw = (
        "user a@b.co at 10.0.0.1 with token=xyz123 and "
        "uuid 550e8400-e29b-41d4-a716-446655440000 key AKIAIOSFODNN7EXAMPLE "
        "Authorization: Basic YWRtaW46aHVudGVyMg=="
    )
    once = redact_pii(raw)
    twice = redact_pii(once)
    assert once == twice


def test_idempotent_on_pem_block():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA1234567890abcdef\n"
        "-----END RSA PRIVATE KEY-----"
    )
    once = redact_pii(pem)
    assert once == redact_pii(once) == "<private-key>"


# ---------------------------------------------------------------------------
# Combined / realistic stack-trace shape
# ---------------------------------------------------------------------------

def test_realistic_stack_trace():
    sample = (
        "AuthError: user a@b.co from 10.42.0.13 sent "
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.sig "
        "with api_key=abcDEF; request_id=550e8400-e29b-41d4-a716-446655440000 "
        "sha256=e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    out = redact_pii(sample)
    # All sensitive substrings gone.
    for leak in ("a@b.co", "10.42.0.13", "eyJ", "abcDEF",
                 "550e8400", "e3b0c44298fc"):
        assert leak not in out, f"leaked: {leak!r} in {out!r}"
    # And we kept the readable scaffolding.
    assert "AuthError" in out
    assert "<email>" in out
    assert "<ip>" in out
    assert "<uuid>" in out
