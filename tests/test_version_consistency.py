"""Версия объявлена один раз и нигде не разъезжается.

Повод: к 13.08.2026 версия жила в четырёх местах и все они говорили разное —
FastAPI отдавал `1.0.0-rc.3` (двенадцать релизов назад), Helm-чарт
`1.0.0-rc.8`, README-бейдж и CHANGELOG `1.0.0-rc.15`. Каждое место
обновлялось руками и по отдельности, поэтому расхождение накапливалось молча
и обнаруживалось случайно.

Тест намеренно проверяет ИМЕННО те поверхности, которые видит человек снаружи:
`/docs` и OpenAPI-схему, установленный чарт, README и CHANGELOG.
"""
import json
import re
import subprocess
from pathlib import Path

import pytest

from app import __version__

REPO_ROOT = Path(__file__).parent.parent

# Формат версии: SemVer с опциональным pre-release (-rc.N). Отдельная
# проверка, потому что «1.0.0rc15» или «v1.0.0» ломают и тег, и тег образа.
_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?$")


def test_version_is_semver():
    assert _SEMVER.match(__version__), f"нестандартная версия: {__version__!r}"


def test_fastapi_reports_the_same_version():
    """То, что отдаёт OpenAPI-схема, — самая заметная снаружи копия версии."""
    from app.main import app

    assert app.version == __version__


def test_helm_chart_appversion_matches():
    """appVersion чарта описывает, что реально крутится в кластере.

    Расхождение уже случалось: в чарте стояло 0.5.0, когда в ns sre-ai
    работал 1.0.0-rc.7 — репозиторий врал о собственном проде.
    """
    chart = (REPO_ROOT / "helm" / "sre-ai-copilot" / "Chart.yaml").read_text(encoding="utf-8")
    m = re.search(r'^appVersion:\s*"([^"]+)"', chart, re.MULTILINE)
    assert m, "в Chart.yaml не найден appVersion"
    assert m.group(1) == __version__, (
        f"Chart.yaml appVersion={m.group(1)}, а app.__version__={__version__}"
    )


def test_readme_badge_matches():
    """Бейдж в шапке README — первое, что видит человек, открывший репозиторий."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    m = re.search(r"badge/release-v([0-9A-Za-z.\-]+?)-blue", readme)
    assert m, "в README не найден бейдж release"
    # В shields.io дефис экранируется удвоением: 1.0.0--rc.15 → 1.0.0-rc.15.
    badge = m.group(1).replace("--", "-")
    assert badge == __version__, f"бейдж README={badge}, а app.__version__={__version__}"


def test_changelog_has_section_for_current_version():
    """У текущей версии есть своя секция CHANGELOG — иначе релиз без описания."""
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{__version__}]" in changelog, (
        f"в CHANGELOG нет секции ## [{__version__}]"
    )


def test_changelog_latest_release_is_current_version():
    """Верхняя релизная секция CHANGELOG — это и есть текущая версия.

    Ловит обратную ошибку: секцию для нового релиза добавили, а `__version__`
    поднять забыли.
    """
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    sections = re.findall(r"^## \[([^\]]+)\]", changelog, re.MULTILINE)
    releases = [s for s in sections if s.lower() != "unreleased"]
    assert releases, "в CHANGELOG нет ни одной релизной секции"
    assert releases[0] == __version__, (
        f"верхняя секция CHANGELOG={releases[0]}, а app.__version__={__version__}"
    )


@pytest.mark.skipif(
    not (REPO_ROOT / ".git").exists(), reason="не git-репозиторий"
)
def test_released_versions_have_tags():
    """Каждый релиз эпохи 1.0.0 закрыт git-тегом.

    Из шести rc-релизов тегами были закрыты только rc.1 и rc.15 — остальные
    существовали лишь секцией CHANGELOG, и откатиться на «то, что работало в
    rc.13» было не на что. Пропущенные теги восстановлены 13.08.2026 по
    заголовкам секций (это записано в аннотации каждого тега).

    Релизы 0.x намеренно не проверяются: их секции CHANGELOG написаны задним
    числом пачкой (0.1.0–0.4.0 внесены одним коммитом 904bbcc), поэтому
    достоверного «коммита релиза» у них не существует, а тег наугад врал бы
    убедительнее, чем его отсутствие.
    """
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    sections = re.findall(r"^## \[([^\]]+)\]", changelog, re.MULTILINE)
    releases = [
        s for s in sections
        if s.lower() != "unreleased" and s.startswith("1.")
    ]

    try:
        out = subprocess.run(
            ["git", "tag"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError) as e:  # pragma: no cover
        pytest.skip(f"git недоступен: {e}")
    if out.returncode != 0:  # pragma: no cover
        pytest.skip("git tag вернул ошибку")
    tags = set(out.stdout.split())
    if not tags:
        # Ни одного тега вообще — это не «релизы не тегированы», а клон без
        # тегов: actions/checkout по умолчанию их не выкачивает. Проверять
        # тут нечего, и падать на этом значит врать о состоянии репозитория.
        # В CI теги подтягиваются явно (fetch-tags в .github/workflows/ci.yml),
        # так что до skip доходят только чужие shallow-клоны.
        pytest.skip("в клоне нет тегов (shallow checkout) — проверять нечего")

    missing = [r for r in releases if f"v{r}" not in tags]
    assert not missing, (
        "релизы без git-тега: " + ", ".join(missing) +
        " — теги ставятся на коммит, которым релиз уехал"
    )


def test_version_json_is_serializable():
    """Версия уходит в JSON-ответы и в футер embed-а — она обязана быть строкой."""
    assert isinstance(__version__, str)
    assert json.dumps({"version": __version__})
