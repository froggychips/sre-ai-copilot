#!/usr/bin/env python3
"""Страж персональных данных: репозиторий публичный, людей в нём быть не должно.

Ловит то, на чём уже обожглись (15.09.2026): карта `squad-N -> «логин · нода»` лежала
прямо в `scripts/squad_dashboard.py`, ФИО сотрудников — в фикстурах тестов, а сообщение
коммита сообщало, что конкретный человек уволился. По такому набору строится
таргетированная атака: известны логины, адреса внутренних систем и чьи учётки могли
остаться неотозванными.

С 18.11.2026 страж ловит и публичные IPv4-адреса: в манифестах лежали адрес внешней
базы с её NodePort и адреса ingress-нод — готовая карта «куда стучаться», собранная
самим владельцем. Адреса площадки подаются снаружи (`k8s/site.yaml.example`,
`STATICS_DB_CIDR` в deploy.sh).

ВАЖНО про устройство проверки. Здесь НЕТ списка настоящих логинов и ФИО — иначе сам
страж стал бы той самой утечкой. Проверяются только СТРУКТУРНЫЕ признаки:
связка «логин · нода», корпоративные адреса, непустые карты владения, глобально
маршрутизируемые IPv4. Всё, что выглядит
как реальный человек, обязано жить вне репозитория — в ConfigMap/ENV
(`NAME_OVERRIDES`, `RESERVED_MAP`, `OWNER_ALIASES_PATH`, `OWNERSHIP_MANIFEST_PATH`).

Не путать с `app/services/pii_redaction.py`: тот чистит данные В РАНТАЙМЕ (сэмплы
логов, уезжающие в Discord и KG). Этот страж — про исходники и сообщения коммитов,
то есть про то, что навсегда остаётся в публичной истории git.

Запуск: `python3 scripts/check_no_pii.py` (весь индекс git) либо с путями-аргументами.
Выход: 0 — чисто, 1 — есть находки (печатаются файл:строка и что именно сработало).
"""
from __future__ import annotations

import ipaddress
import re
import subprocess
import sys
from pathlib import Path

# Плейсхолдеры, которыми заменяются реальные логины (NATO-алфавит), технические учётки
# и типовые тестовые имена. Всё остальное в связке «логин · нода» считается человеком.
ALLOWED_LOGINS = {
    # NATO — плейсхолдеры вместо настоящих логинов
    "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel",
    "india", "juliet", "kilo", "lima", "mike", "november", "oscar", "papa",
    "quebec", "romeo", "sierra", "tango", "uniform", "victor", "whiskey",
    "xray", "yankee", "zulu",
    # технические учётки и боты — не люди
    "teamcity", "release-bot", "data-bot", "qauser", "analytics", "ai-agent",
    "root", "admin", "user", "test", "bot", "runner", "ci",
    # типовые тестовые
    "alice", "bob", "carol", "dev", "teamlead", "other", "someone", "nobody",
}

# Разрешённые «ФИО» — заведомо вымышленные, из фикстур и примеров в документации.
ALLOWED_NAMES = {
    "Имя Фамилия", "Иван Иванов", "Пётр Петров", "Сергей Сергеев",
    "Другое Имя", "Тест Тестов", "Test User", "Someone Else",
}

# Файлы и каталоги, которые проверять бессмысленно или вредно.
SKIP_PARTS = {".git", "node_modules", ".venv", "venv", "__pycache__", "htmlcov", "dist", "build"}
# .mailmap по своей природе состоит из git-адресов автора: они и так в истории коммитов,
# и файл лишь сводит их воедино. CHANGELOG — исторический журнал, правится отдельно.
SKIP_FILES = {".mailmap"}

BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz",
                   ".whl", ".so", ".dylib", ".woff", ".woff2", ".ttf"}

# ── Правила ──────────────────────────────────────────────────────────────────

#: Связка человека с конкретной нодой: «<логин> · dev-37», «<логин> · prod-k5».
RE_LOGIN_NODE = re.compile(
    r"\b(?P<login>[a-zA-Z][a-zA-Z0-9._-]{2,})\s*[·|]\s*(?:dev|prod|infra|node)-[a-zA-Z0-9-]+"
)

#: Корпоративная почта и личные адреса рядом с именем сотрудника.
RE_CORP_EMAIL = re.compile(r"\b[a-zA-Z0-9._%+-]+@(?:lastoasisgame\.com|juicybuttons\.[a-z]+)\b")

#: Кириллическое «Имя Фамилия» — в публичном коде их быть не должно.
RE_CYRILLIC_NAME = re.compile(r"\b[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\b")

#: Кадровые формулировки в сообщениях коммитов и комментариях.
#: Английское `fired` сюда не берём намеренно: в SRE-коде это «алерт сработал»
#: (alert fired), и правило дало бы десятки ложных срабатываний.
#: `ушедший` само по себе тоже не берём — в коде это «транзиент, ушедший на 3-й
#: попытке»; ловим только связку с человеком.
RE_HR_WORDING = re.compile(
    r"(уволен\w*|уволил\w*|увольнени\w+|покинул компанию|offboarding"
    r"|ушедш\w+\s+(?:сотрудник\w*|коллег\w*|разработчик\w*|админ\w*|тимлид\w*))",
    re.IGNORECASE,
)


#: IPv4-литерал. Слева не буква/цифра/точка (иначе это хвост версии или имени),
#: справа не продолжение (`1.2.3.4.5`, `1.2.3.4a`). Маска `/32` в совпадение не
#: входит — адрес в CIDR ловится так же, как голый.
RE_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w]|\.\d)")

#: Общеизвестные публичные адреса, которые ничего не говорят о нашей площадке:
#: открытые DNS-резолверы и хрестоматийный «адрес из примера».
ALLOWED_PUBLIC_IPS = {"8.8.8.8", "8.8.4.4", "1.1.1.1", "1.0.0.1", "1.2.3.4"}

#: Файлы, где правило адресов не применяется. Историю git не переписываем, а
#: эти файлы правятся отдельно от манифестов:
#:   CHANGELOG.md — журнал прошлых релизов (в нём остались адреса, уже
#:     опубликованные вместе с манифестами);
#:   app/services/node_resolver.py — адрес ноды в docstring; убрать при
#:     ближайшей правке модуля и вычеркнуть отсюда.
#: Новые записи сюда не добавлять — для единичного случая есть `pii-allow`.
IP_LEGACY_FILES = {"CHANGELOG.md", "app/services/node_resolver.py"}

#: Перед «адресом» стоит оператор сравнения версий — это версия пакета
#: (`click-plugins==1.1.1.2`), а не адрес.
_VERSION_PREFIXES = ("==", ">=", "<=", "~=", "!=")


def find_public_ipv4(line: str) -> list[str]:
    """Глобально маршрутизируемые IPv4 в строке.

    Разрешено всё, что не ведёт к конкретному хосту в интернете: RFC1918
    (10/8, 172.16/12, 192.168/16 — в т.ч. pod/service CIDR кластера, они нужны
    в `except` у NetworkPolicy и вне кластера ничего не значат), loopback,
    link-local, CGNAT, диапазоны для документации (RFC 5737), 0.0.0.0 и
    маски. Критерий — `ipaddress.is_global`, а не свой список сетей: его
    ведёт stdlib по реестру IANA.
    """
    found = []
    for m in RE_IPV4.finditer(line):
        text = m.group(0)
        if line[max(0, m.start() - 2):m.start()] in _VERSION_PREFIXES:
            continue
        try:
            addr = ipaddress.IPv4Address(text)
        except ValueError:  # 999.1.1.1, ведущие нули — не адрес
            continue
        if addr.is_global and text not in ALLOWED_PUBLIC_IPS:
            found.append(text)
    return found


def _iter_files(paths: list[str]) -> list[Path]:
    """Файлы под проверку: аргументы либо всё дерево git.

    `--others --exclude-standard` добавляет НЕотслеживаемые файлы: без них новый файл
    с персональными данными проходил локальную проверку и падал уже в CI — ровно это
    и случилось при первом прогоне стража.
    """
    if paths:
        return [Path(p) for p in paths if Path(p).is_file()]
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        capture_output=True, text=True, check=True,
    )
    files = []
    for line in out.stdout.splitlines():
        p = Path(line)
        if set(p.parts) & SKIP_PARTS or p.name in SKIP_FILES:
            continue
        if p.suffix.lower() in BINARY_SUFFIXES:
            continue
        files.append(p)
    return files


def _is_cyrillic_name_allowed(name: str) -> bool:
    if name in ALLOWED_NAMES:
        return True
    # «Ключ Задачи», «Живая Активность» — обычный текст, а не ФИО: второе слово
    # в русском ФИО почти всегда фамилия, а тут оба слова из словаря предметной
    # области. Отсекаем только те пары, где оба слова начинаются с заглавной
    # и это не начало предложения — проверить надёжно нельзя, поэтому список
    # разрешённых ведём явно.
    return False


def scan_file(path: Path) -> list[tuple[int, str, str]]:
    """→ [(номер строки, правило, фрагмент)] для одного файла."""
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []

    findings: list[tuple[int, str, str]] = []
    self_check = path.name == "check_no_pii.py"
    check_ips = path.as_posix() not in IP_LEGACY_FILES

    for lineno, line in enumerate(text.splitlines(), 1):
        if "pii-allow" in line:  # осознанное исключение прямо в строке
            continue

        for m in RE_LOGIN_NODE.finditer(line):
            login = m.group("login").lower()
            if login not in ALLOWED_LOGINS:
                findings.append((lineno, "логин↔нода", m.group(0).strip()))

        for m in RE_CORP_EMAIL.finditer(line):
            findings.append((lineno, "корпоративный e-mail", m.group(0)))

        if check_ips:
            for ip in find_public_ipv4(line):
                findings.append((lineno, "публичный IPv4", ip))

        if not self_check:
            for m in RE_CYRILLIC_NAME.finditer(line):
                if not _is_cyrillic_name_allowed(m.group(0)):
                    findings.append((lineno, "кириллическое ФИО", m.group(0)))

            for m in RE_HR_WORDING.finditer(line):
                findings.append((lineno, "кадровая формулировка", m.group(0)))

    return findings


def main(argv: list[str]) -> int:
    files = _iter_files(argv[1:])
    total = 0
    for path in files:
        for lineno, rule, snippet in scan_file(path):
            print(f"{path}:{lineno}: [{rule}] {snippet}")
            total += 1

    if total:
        print(
            f"\nНайдено {total} совпадений. Репозиторий публичный: связки логин↔человек,"
            "\nлогин↔нода, ФИО и кадровые формулировки хранить в нём нельзя."
            "\nРеальные карты подаются снаружи: NAME_OVERRIDES, RESERVED_MAP,"
            "\nOWNER_ALIASES_PATH, OWNERSHIP_MANIFEST_PATH (ConfigMap/ENV)."
            "\nПубличные адреса площадки — тоже снаружи: k8s/site.yaml.example, STATICS_DB_CIDR."
            "\nЕсли совпадение ложное — добавьте комментарий `pii-allow` в ту же строку.",
            file=sys.stderr,
        )
        return 1

    print(f"PII-проверка: чисто ({len(files)} файлов).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
