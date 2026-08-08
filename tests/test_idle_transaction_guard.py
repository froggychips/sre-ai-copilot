"""Транзакция не должна простаивать открытой.

Инцидент 08.08.2026: таск открывал сессию, читал БД, уходил в долгий внешний
вызов (kubectl до 180с, HTTP в Discord) — и всё это время транзакция висела
в `idle in transaction`, держа ACCESS SHARE на прочитанных таблицах. Замер на
проде: 6 таких транзакций, старшей 25 минут. Следствия:

  * миграция `kg_services` не смогла взять ACCESS EXCLUSIVE, встала в очередь
    и заблокировала 7 читателей — приложение висело 6 минут;
  * запросы копились, event loop API не отвечал на liveness за 5с, kubelet
    убивал под: 103 рестарта за 14 часов.

Здесь две проверки: engine выставляет серверный потолок на простой, а синк
топологии не тащит открытую транзакцию через kubectl.
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


def test_topology_sync_releases_transaction_before_kubectl():
    """`sync_topology_resources` отпускает транзакцию до внешних вызовов.

    Регрессия, от которой защищаемся: между открытой транзакцией и записью
    вклинивается kubectl по трём ресурсам (Deployment/StatefulSet/DaemonSet),
    каждый со своим таймаутом, — окно простоя измеряется минутами.
    """
    from app.knowledge_graph import k8s_topology_resources_sync as tsync

    calls: list[str] = []

    class _FakeSession:
        def rollback(self):
            calls.append("rollback")

    def _fake_workloads():
        calls.append("kubectl")
        return []

    with patch.object(tsync, "_kubectl_get_deployments_all", _fake_workloads), \
         patch.object(tsync, "sync_all_services",
                      lambda db, deployments_index=None: calls.append("services") or {}), \
         patch.object(tsync, "sync_all_ingresses_declarative",
                      lambda db: calls.append("ingresses") or {}):
        tsync.sync_topology_resources(_FakeSession())

    assert calls[0] == "rollback", (
        f"первым действием должен быть rollback, получили {calls[0]!r}: "
        "транзакция уедет в kubectl открытой"
    )
    # Перед вторым внешним вызовом транзакцию тоже отпускаем.
    assert calls.index("rollback", 1) < calls.index("ingresses"), (
        "перед sync_all_ingresses_declarative нет rollback"
    )
