"""discord_dedup: снести webhook_url — в БД лежал Discord-токен

Revision ID: 20260808_0200
Revises: 20260808_0100
Create Date: 2026-08-08 02:00:00.000000

ЧТО БЫЛО. `discord_dedup.webhook_url` (String(512), NOT NULL) хранил ПОЛНЫЙ
webhook URL вида `https://discord.com/api/webhooks/<id>/<token>`. Хвост этого
URL — bearer-эквивалент: кто его прочитал, тот постит в канал от имени бота
без всякой аутентификации. Итог: любой READ-доступ к БД (streaming-реплика,
pg_dump, ночной бэкап, сервис с грантом только на SELECT, да просто
`\\copy discord_dedup to ...`) = право спамить в #infra-error. Строк в таблице
десятки, но токен там один и тот же — компрометируется канал целиком.

ПОЧЕМУ КОЛОНКУ МОЖНО ПРОСТО УДАЛИТЬ. Она нужна была для PATCH-а уже
отправленного сообщения (дедуп «не постим второй раз, а редактируем embed»).
Но webhook URL НЕ приходит извне — он целиком резолвится из конфигурации:
  * enriched-канал  — `settings.DISCORD_WEBHOOK_URL` (service.py);
  * incident-канал  — `_pick_webhook_url(team_owner, severity)`, то есть
    `settings.DISCORD_TEAM_CHANNEL_MAP` + тот же DISCORD_WEBHOOK_URL.
Оба PATCH-хелпера уже получали этот url аргументом и использовали
сохранённое значение лишь как `rec.get("webhook_url") or url` — т.е. хранили
в БД копию того, что и так лежит в переменной. Теперь url резолвится в
рантайме, а секрет остаётся только в env/секрете пода.

ЧТО БУДЕТ С СУЩЕСТВУЮЩИМИ СТРОКАМИ. Строки НЕ удаляются: key/msg_id/embed/
first_ts/last_ts/count живы, дедуп по ним продолжает работать без разрыва —
PATCH существующих сообщений после наката идёт по URL из настроек, который
для тех же сообщений тот же самый. Потери данных нет, счётчики ×N не
сбрасываются.

Исключение, осознанное: если между POST-ом и PATCH-ем webhook РОТИРОВАН или
у сервиса сменился team→channel маппинг, PATCH уйдёт на новый URL и Discord
ответит 404 (webhook правит только свои сообщения). Тогда сообщение просто
перестанет обновлять footer до конца TTL-окна (30 мин) — в лог падает
`discord_*_patch_failed`, дубля НЕ будет. Раньше в этом случае PATCH бы
прошёл по старому токену. Размен принят: после ротации токена «дописать в
старое сообщение» — это ровно то, чего мы больше не хотим уметь.

IN-FLIGHT ПРИ ВЫКАТЕ (rolling update, старые и новые поды рядом):
  * СТАРЫЙ под + новая схема: его ORM-маппинг всё ещё включает webhook_url,
    любой SELECT падает UndefinedColumn. Это НЕ краш — весь dedup_store
    обёрнут в try/except с прозрачным fallback на in-memory dict, так что
    старые реплики на время выката деградируют до per-process дедупа
    (то же поведение, что при недоступном PG). Максимум — дубль-POST между
    репликами в окно выката.
  * НОВЫЙ под + старая схема (миграция ещё не прогнана): webhook_url остался
    NOT NULL без server_default, INSERT из claim() ловит IntegrityError →
    трактуется как «проиграли гонку», строки нет → POST без claim-а.
    Тоже не краш, но cross-replica дедуп на это время выключен.
Оба окна короткие и безопасные, но порядок штатный и предпочтительный:
сначала `alembic upgrade head` (k8s/migrate-job.yaml), потом rollout подов.

DOWNGRADE. Возвращает колонку, но НЕ значения — секретов у нас больше нет и
восстанавливать их неоткуда (в этом и смысл миграции). Существующие строки
бэкфиллятся пустой строкой через server_default=''; старый код это переживает,
потому что читал её как `rec.get("webhook_url") or url` и на пустой строке
падает на тот же fallback-url из настроек.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260808_0200"
down_revision = "20260808_0100"
branch_labels = None
depends_on = None

_TABLE = "discord_dedup"
_COLUMN = "webhook_url"


def _has_column() -> bool:
    """Идемпотентность: таблицу могли создать через Base.metadata.create_all()
    уже без колонки (тестовые/свежие окружения) — тогда дропать нечего."""
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return False
    return _COLUMN in {col["name"] for col in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    if not _has_column():
        return
    # batch_alter_table — ради sqlite (ALTER ... DROP COLUMN появился только
    # в 3.35); на Postgres разворачивается в обычный ALTER TABLE DROP COLUMN.
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_column(_COLUMN)


def downgrade() -> None:
    if _has_column():
        return
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.add_column(
            sa.Column(
                _COLUMN,
                sa.String(512),
                nullable=False,
                # Бэкфилл существующих строк: значения утрачены безвозвратно,
                # NOT NULL держим только ради совместимости со старой схемой.
                server_default="",
            ),
        )
