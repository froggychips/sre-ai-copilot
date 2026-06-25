from __future__ import annotations

import platform
import sys
from hashlib import sha256
from typing import Any, Dict
from urllib.parse import urlparse

# Snapshot'ы — наши собственные immutable-артефакты, лежат только в object
# storage (s3://) или на локальной FS воркера (file://). Никаких http(s):
# replay-инпут НЕ должен уметь указать на произвольный хост (SSRF defense
# даже если потребитель когда-нибудь начнёт по uri ходить — сейчас это echo).
ALLOWED_SNAPSHOT_URI_SCHEMES = ("s3", "file")
# Грубый потолок чтобы не принимать мусор/гигантские строки в URI.
_MAX_SNAPSHOT_URI_LEN = 2048


def build_environment_fingerprint(config: Dict[str, Any]) -> str:
    material = f"py={sys.version};platform={platform.platform()};config={sorted(config.items())}"
    return sha256(material.encode("utf-8")).hexdigest()


def validate_snapshot_uri(snapshot_uri: str) -> str:
    """Strict-валидация snapshot_uri: только allowlisted-схемы (s3/file).

    Отвергает http(s)/ftp/gopher/любые произвольные хосты (SSRF-поверхность),
    пустые/мусорные значения и схемо-релятивные `//host/...`. Возвращает
    URI без обрамляющих пробелов при успехе, иначе ValueError.
    """
    uri = (snapshot_uri or "").strip()
    if not uri:
        raise ValueError("snapshot_uri must be a non-empty string")
    if len(uri) > _MAX_SNAPSHOT_URI_LEN:
        raise ValueError("snapshot_uri exceeds maximum allowed length")

    parsed = urlparse(uri)
    scheme = parsed.scheme.lower()
    if not scheme:
        raise ValueError(
            "snapshot_uri must include an explicit scheme "
            f"(allowed: {', '.join(ALLOWED_SNAPSHOT_URI_SCHEMES)})"
        )
    if scheme not in ALLOWED_SNAPSHOT_URI_SCHEMES:
        raise ValueError(
            f"snapshot_uri scheme '{scheme}' is not allowed "
            f"(allowed: {', '.join(ALLOWED_SNAPSHOT_URI_SCHEMES)})"
        )
    # s3:// обязан указывать bucket (netloc). file:// допускает file:///path
    # (netloc пуст, путь абсолютный), но не file://remote-host/... .
    if scheme == "s3" and not parsed.netloc:
        raise ValueError("s3:// snapshot_uri must include a bucket")
    if scheme == "file" and parsed.netloc not in ("", "localhost"):
        raise ValueError("file:// snapshot_uri must not reference a remote host")
    if not (parsed.netloc or parsed.path):
        raise ValueError("snapshot_uri must include a path")
    return uri


def assert_replay_inputs(
    snapshot_id: str | None = None, snapshot_uri: str | None = None
) -> None:
    if not snapshot_id and not snapshot_uri:
        raise ValueError("Replay requires snapshot_id or snapshot_uri")
    # Если URI задан — он обязан пройти strict-валидацию (defense-in-depth).
    if snapshot_uri:
        validate_snapshot_uri(snapshot_uri)


def assert_replay_isolated_runtime(
    allow_network_egress: bool, allow_k8s_api: bool, allow_external_tools: bool
) -> None:
    if allow_network_egress or allow_k8s_api or allow_external_tools:
        raise ValueError(
            "Replay runtime must be isolated: no egress, no k8s API, no external tool calls"
        )
