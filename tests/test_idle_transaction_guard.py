"""Транзакция не должна простаивать открытой.

Инцидент 08.08.2026: таск открывал сессию, читал БД, уходил в долгий внешний
вызов (kubectl до 180с, HTTP в Discord) — и всё это время транзакция висела
в `idle in transaction`, держа ACCESS SHARE на прочитанных таблицах. Замер на
проде: 6 таких транзакций, старшей 25 минут. Следствия:

  * миграция `kg_services` не смогла взять ACCESS EXCLUSIVE, встала в очередь
    и заблокировала 7 читателей — приложение висело 6 минут;
  * запросы копились, event loop API не отвечал на liveness за 5с, kubelet
    убивал под: 103 рестарта за 14 часов.

Основная защита — серверный потолок на простой транзакции (engine). Плюс
guard на порядок операций в синке топологии: пока kubectl идёт раньше
первого SQL, транзакция во время внешних вызовов не открыта вовсе.
"""
from __future__ import annotations

from unittest.mock import patch


def test_engine_sets_idle_in_transaction_timeout():
    """Postgres-соединения получают idle_in_transaction_session_timeout.

    Это защита от класса проблем: любой забытый/затянувшийся простой
    транзакции обрывается сервером, а не живёт до перезапуска пода.
    """
    import app.database as db_mod

    if db_mod._is_sqlite:
        # На sqlite опция неприменима — engine создаётся без connect_args.
        assert "connect_args" not in db_mod._pool_kwargs
        return

    options = db_mod._pool_kwargs["connect_args"]["options"]
    assert "idle_in_transaction_session_timeout" in options
    # Значение должно быть конечным и разумным: не 0 (=выключено).
    value = int(options.split("idle_in_transaction_session_timeout=")[1].split()[0])
    assert 0 < value <= 600_000, f"неожиданный таймаут: {value}ms"


def test_timeout_targets_idle_only_not_active_queries():
    """Потолок ставится именно на ПРОСТОЙ, а не на длительность запроса.

    Разница принципиальная: `statement_timeout` оборвал бы легитимный
    многоминутный синк, а `idle_in_transaction_session_timeout` трогает
    только соединения, которые внутри транзакции ничего не делают.
    """
    import app.database as db_mod

    if db_mod._is_sqlite:
        return
    options = db_mod._pool_kwargs["connect_args"]["options"]
    assert "statement_timeout" not in options


def test_module_engine_pins_session_timezone_to_utc():
    """У модульного engine в options есть `-c timezone=UTC`.

    Отдельный от pool-config guard: инвариант «в БД naive UTC, сессия
    принудительно в UTC» (docstring app/database.py, docs/SEMANTIC_CONTRACT.md
    §10) должен держаться на РЕАЛЬНОМ engine, а не только в билдере. Соседство
    двух `-c`-параметров в одной строке — тоже часть проверки: раньше там был
    один, и дописать второй легко неправильно (без пробела опции склеятся и
    libpq отвергнет коннект).
    """
    import app.database as db_mod

    if db_mod._is_sqlite:
        assert "connect_args" not in db_mod._pool_kwargs
        return

    options = db_mod._pool_kwargs["connect_args"]["options"]
    assert "-c timezone=UTC" in options, f"options={options!r}"
    assert "idle_in_transaction_session_timeout" in options


def test_topology_sync_reads_k8s_before_touching_db():
    """В `sync_topology_resources` kubectl идёт РАНЬШЕ первого запроса к БД.

    Пока порядок такой, транзакция на время внешних вызовов не открыта и
    ACCESS SHARE никого не держит. Если чтение k8s переедет за запросы к БД,
    вернётся ровно тот простой, что 08.08.2026 заблокировал миграцию.

    Намеренно НЕ проверяем rollback внутри функции: откатывать чужую
    незакоммиченную работу библиотечный код не вправе — за простой отвечает
    серверный idle_in_transaction_session_timeout (тесты выше).
    """
    from app.knowledge_graph import k8s_topology_resources_sync as tsync

    order: list[str] = []

    class _TxSpySession:
        """Любое обращение к сессии = потенциальное открытие транзакции."""

        def __getattr__(self, name):
            order.append(f"db.{name}")
            return lambda *a, **kw: None

    with patch.object(tsync, "_kubectl_get_deployments_all",
                      lambda: order.append("kubectl") or []), \
         patch.object(tsync, "sync_all_services",
                      lambda db, deployments_index=None: order.append("sql") or {}), \
         patch.object(tsync, "sync_all_ingresses_declarative",
                      lambda db: order.append("sql") or {}):
        tsync.sync_topology_resources(_TxSpySession())

    assert order[0] == "kubectl", (
        f"первым должен быть внешний вызов, получили {order[0]!r} — "
        "транзакция уедет в kubectl открытой"
    )
    assert "sql" in order and order.index("kubectl") < order.index("sql")
