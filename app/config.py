from typing import List, Optional

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application configuration settings using Pydantic-settings v2.
    Loads variables from environment or a .env file.
    """

    ENV: str = Field("development", description="Environment (production/development)")
    ANTHROPIC_API_KEY: Optional[str] = Field(None, description="API key for Anthropic Claude (not needed when LLM_BACKEND=claude_cli)")

    DATABASE_URL: str = Field(
        "postgresql://user:password@localhost:5432/dbname",
        description="PostgreSQL connection string",
    )

    REDIS_URL: str = Field(
        "redis://localhost:6379/0",
        description="Redis connection URL for Celery broker and backend",
    )

    MODEL_NAME: str = Field("claude-sonnet-4-6", description="The model to use")

    # Backend для LLM-вызовов:
    #   anthropic — AsyncAnthropic SDK + ANTHROPIC_API_KEY (default, production)
    #   claude_cli — subprocess `claude --print` (local PoC без API key)
    LLM_BACKEND: str = Field("anthropic", description="anthropic|claude_cli")
    CLAUDE_CLI_TIMEOUT_SECONDS: float = 180.0
    LLM_TIMEOUT_SECONDS: float = 30.0

    # Eager Celery — для in-process e2e без Redis/worker'а
    CELERY_TASK_ALWAYS_EAGER: bool = False

    # ┌─ HARD GATE: запуск LLM-pipeline ─┐
    # Двойная защита от случайного запуска full incident-pipeline'а:
    #  1. AlertManager route (вне copilot — VMAlertmanagerConfig).
    #  2. ЭТА переменная (в коде, не route-зависимая).
    #
    # Сценарий аварии: кто-то меняет VMAlertmanagerConfig URL с
    # /webhooks/alertmanager/store на /webhooks/alertmanager. Без этого
    # флага все 50 alerts/мин из prod-* идут в process_incident_task →
    # 5 LLM-calls × $0.05 × 50 = ~$12.5/мин = $750/час burn до того как
    # кто-то заметит.
    #
    # Default False: даже при route-misconfiguration пайплайн
    # exit'нется без LLM-вызовов. Включается ОСОЗНАННО через
    # `kubectl set env deployment/copilot-worker LLM_PIPELINE_ENABLED=true`
    # ПОСЛЕ:
    #   - E2E + replay тесты прошли (#30 в roadmap)
    #   - Budget cap в Anthropic console установлен
    #   - severity-фильтр сужен до critical + prod-*
    LLM_PIPELINE_ENABLED: bool = False

    # Celery backpressure / resilience (PR — protect prod worker от перегрузки,
    # memory-leak'ов, висящих задач, OOM при flood incident-ов).
    #
    # `prefetch_multiplier=1` — fair scheduling: worker берёт по одной задаче
    #     зараз. Без этого при flood'е (>2 incidents в секунду) один worker
    #     забирает 4× и другие ждут.
    # `max_tasks_per_child=50` — после 50 задач worker-process restart-ится.
    #     Защита от memory-leak'ов (LLM-агенты держат стейт).
    # `task_time_limit=1800` (30 мин hard) — incident pipeline в худшем случае
    #     5-7 минут; 30 минут — это уже зависание, лучше kill + retry.
    # `task_soft_time_limit=1500` (25 мин soft) — SoftTimeLimitExceeded
    #     можно поймать в task, корректно закрыть, записать в audit.
    # `process_incident_rate_limit` — не больше N в минуту (LLM budget guard).
    CELERY_WORKER_PREFETCH_MULTIPLIER: int = Field(1, description="Fair scheduling: 1 task per worker at a time")
    CELERY_WORKER_MAX_TASKS_PER_CHILD: int = Field(50, description="Worker restart after N tasks (memory leak protection)")
    CELERY_TASK_TIME_LIMIT_SECONDS: int = Field(1800, description="Hard task timeout (30 min)")
    CELERY_TASK_SOFT_TIME_LIMIT_SECONDS: int = Field(1500, description="Soft task timeout (25 min)")
    CELERY_PROCESS_INCIDENT_RATE_LIMIT: str = Field(
        "30/m", description="Rate limit для process_incident — защита LLM-бюджета"
    )

    # Прямой вызов async_process_incident прямо из вебхука (минуя Celery).
    # Полезно в локальном e2e без Redis: блокирует curl, но возвращает реальный
    # результат, виден в логах + persists в DB. Eager-mode Celery несовместим
    # с async endpoint-ом (loop.run_until_complete внутри уже запущенного loop).
    PIPELINE_DIRECT_INVOKE: bool = False

    # Не отправлять реальный Discord webhook, только логировать (для local e2e).
    DISCORD_DRY_RUN: bool = False

    # Discord-enrich tier — детерминированный embed с KG-контекстом,
    # без LLM. Принимает alert на /webhooks/alertmanager/enrich-and-forward,
    # обогащает recent_deploys/upstream-alerts/recurrence/owner-team и
    # шлёт один embed на DISCORD_WEBHOOK_URL.
    #
    # Default False — включается отдельно после канареечного прогона,
    # чтобы `/store`-route не получил Discord-побочку случайно.
    DISCORD_ENRICH_ENABLED: bool = False
    # Окно поиска recent_deploys/incidents_on (минуты).
    ENRICH_DEPLOY_LOOKBACK_MIN: int = 60
    ENRICH_RECURRENCE_LOOKBACK_MIN: int = 1440  # 24 ч
    ENRICH_UPSTREAM_WINDOW_MIN: int = 15

    # Anti-DoS cap для prompt_guard.detect_injection. Поднимать только
    # осознанно — каждый LLM-вызов биллится по входным токенам.
    PROMPT_INPUT_MAX_CHARS: int = 20000

    MAX_TOKENS: int = Field(
        1024, description="Maximum number of tokens to generate in LLM responses"
    )

    LOG_LEVEL: str = Field("INFO", description="Standard logging level")

    DISCORD_WEBHOOK_URL: Optional[str] = Field(None, description="Discord webhook — канал #error (инциденты)")
    DISCORD_WEBHOOK_STATS_URL: Optional[str] = Field(None, description="Discord webhook — канал #stats (cluster health, daily report)")
    # Discord Interactions — для обработки нажатий кнопок 👍/👎.
    # Взять из Discord Developer Portal → Application → General Information.
    DISCORD_PUBLIC_KEY: Optional[str] = Field(None, description="Ed25519 публичный ключ приложения Discord")
    # Application ID нужен для followup-webhook вызовов (deferred response):
    # PATCH /webhooks/{app_id}/{interaction_token}/messages/@original.
    # Взять там же где DISCORD_PUBLIC_KEY (General Information → Application ID).
    DISCORD_APPLICATION_ID: Optional[str] = Field(None, description="Discord application ID for deferred responses")

    # Auth
    JWT_PUBLIC_KEY: str = Field("", description="RSA Public Key for JWT validation")
    OIDC_WELL_KNOWN_URL: Optional[str] = None
    JWT_ALGORITHM: str = "RS256"
    JWT_AUDIENCE: Optional[str] = None

    # CORS
    ALLOWED_ORIGINS: List[str] = Field(default_factory=lambda: ["*"])

    # Security
    ALERTMANAGER_WEBHOOK_SECRET: Optional[str] = None
    # Точка роста #2 (Phase 2): AlertManager API URL для resolve-sync.
    # Сейчас kg_alerts держит `firing` записи без resolved_at годами (видим
    # etcd alerts от 10 апреля). Beat task kg_alerts_resolve_sync периодически
    # сравнивает kg_alerts.fingerprint со списком firing на AM, не-firing
    # пишет resolved_at = NOW().
    ALERTMANAGER_API_URL: str = Field(
        "http://vmalertmanager-vm-victoria-metrics-k8s-stack.monitoring:9093",
        description="AlertManager API root URL (без /api/v2 — добавится в client)",
    )
    SAFE_MODE: bool = True
    APPROVAL_REQUIRED: bool = True
    REPLAY_MODE: bool = False

    # Observability
    # OTLP gRPC endpoint. Пусто → traces не экспортируются (см. setup_telemetry —
    # probe-reachability + graceful-degrade). Пример: "http://jaeger.<ns>:4317".
    OTLP_EXPORTER_ENDPOINT: str = ""
    # AUDIT_LOG_PATH:
    #   "" / "-" / "stdout" → stdout (default, prod-acceptable: Fluent Bit / Loki ловят).
    #   "/path/to/file"     → файл (dev / local-e2e). Под readOnlyRootFilesystem
    #                          в k8s — упадёт, что и требуется (явная ошибка > silent drop).
    AUDIT_LOG_PATH: str = ""

    # VictoriaMetrics — memory/CPU метрики пода за N минут до инцидента.
    # In-cluster: http://vmsingle-vm-victoria-metrics-k8s-stack.monitoring:8428
    # Local dev:  http://localhost:8428 (kubectl port-forward)
    # Пусто = метрики отключены.
    VICTORIA_METRICS_URL: str = Field("", description="VMSingle base URL")
    VICTORIA_METRICS_WINDOW_MINUTES: int = 15

    # TeamCity integration — обогащение incident-а recent-deploys.
    # Режимы (в порядке приоритета):
    #   1. Прямой TC REST API: TC_URL + TC_TOKEN (те же что у локального teamcity-mcp).
    #      Пакет teamcity_mcp.client.TeamCityClient из ~/projects/teamcity-mcp.
    #   2. MCP HTTP server: TEAMCITY_MCP_URL + TEAMCITY_MCP_TOKEN
    #      (задеплоенный mcp-teamcity-server — пока не поднят).
    #   Пусто / оба отсутствуют = graceful degrade (recent_deploy остаётся unknown).
    TC_URL: str = Field("", description="TeamCity base URL")
    TC_TOKEN: str = Field("", description="TeamCity Bearer token")
    TEAMCITY_MCP_URL: str = Field(
        "", description="mcp-teamcity-server MCP endpoint URL (fallback)"
    )
    TEAMCITY_MCP_TOKEN: str = Field(
        "", description="Bearer для mcp_auth (пусто = no-auth local PoC)"
    )
    TEAMCITY_WEB_URL: str = Field(
        "",
        description="TC web UI base — для viewLog ссылок в Discord",
    )
    TC_TIMEOUT_SECONDS: float = 5.0
    TC_LOOKBACK_MINUTES: int = 60
    TC_BACKEND_PROJECT_ID: str = Field("", description="TeamCity project ID для поиска билдов")
    # G3.1 (Phase 2): расширение покрытия. Раньше sync шёл только по
    # TC_BACKEND_PROJECT_ID, deploys coverage держалась на 17%. Теперь
    # comma-separated list дополнительных projects (например `Wo_Auth,Wo_Infra`).
    # Если пусто — fallback на TC_BACKEND_PROJECT_ID.
    TC_PROJECT_IDS: str = Field(
        "",
        description="TeamCity project IDs (comma-separated). Fallback на TC_BACKEND_PROJECT_ID если пусто.",
    )

    # GitLab — обогащение MR-метаданными по SHA из TC-деплоев.
    GITLAB_URL: str = Field("", description="GitLab base URL")
    GITLAB_TOKEN: str = Field("", description="GitLab Personal Access Token (read_api)")
    # Проекты для поиска MR по sha: project_id (числовой) или namespace/name.
    # Пусто → GitLab-enrichment отключён.
    GITLAB_BACKEND_PROJECT: str = Field("", description="GitLab project path (e.g. <group>/<repo>) для backend MR-поиска")

    # ClickHouse prod — blast radius (активные игроки вокруг времени инцидента).
    # Внешний хост, не требует port-forward.
    CH_PROD_HOST: str = Field("", description="ClickHouse prod host")
    CH_PROD_PORT: int = Field(8725, description="ClickHouse HTTP port")
    CH_PROD_USER: str = Field("", description="ClickHouse user")
    CH_PROD_PASSWORD: str = Field("", description="ClickHouse password")
    CH_BLAST_RADIUS_WINDOW_MINUTES: int = Field(15, description="Minutes before/after incident to check active players")

    # Statics Postgres — версии конфигов, проверка entity по ошибке DI.
    # Внешний хост, не требует port-forward.
    STATICS_HOST: str = Field("", description="Statics Postgres host")
    STATICS_PORT: int = Field(31700, description="Statics Postgres port")
    STATICS_USER: str = Field("claude", description="Statics Postgres user")
    STATICS_PASSWORD: str = Field("", description="Statics Postgres password")
    STATICS_RECENT_VERSIONS: int = Field(5, description="How many recent statics versions to compare")

    # Executor stage (PR #2 executor track). Если False — стадия пропускается,
    # пайплайн остаётся чисто advisory. Включать осознанно после merge PR #2
    # и smoke-теста на non-prod. На текущем этапе stage делает только
    # kubectl --dry-run=server: валидирует через kube-apiserver, ничего не пишет.
    EXECUTOR_ENABLED: bool = Field(False, description="Run executor stage in dry-run mode")

    # KG topology auto-sync — список namespace-ов для скана.
    # Пусто → auto-discovery через `kubectl get ns` минус kube-/monitoring-etc.
    # Заполняй через env: KG_SCAN_NAMESPACES="prod-a,prod-b,staging-c"
    KG_SCAN_NAMESPACES: str = Field("", description="Comma-separated namespaces для kg_topology_sync (пусто → auto-discovery)")

    # Daily stats digest — Celery beat task который собирает cluster-health
    # + KG-quality + stale-deployments и шлёт в DISCORD_WEBHOOK_STATS_URL.
    # БЕЗ LLM-вызовов (см. tests/test_stats_digest_no_llm.py).
    # Default False — opt-in через Helm value env.statsDigestEnabled.
    STATS_DIGEST_ENABLED: bool = Field(False, description="Enable daily stats digest beat task")
    STATS_DIGEST_HOUR_UTC: int = Field(9, description="UTC hour to run daily digest (0-23)")

    # Chronic-alerts digest (L5) — каждые 6 часов список «хронических»
    # сервисов: alert повторяется ≥CHRONIC_DIGEST_MIN_FIRES за 24h. Идёт
    # в #stats канал (DISCORD_WEBHOOK_STATS_URL), а не в #error — анти-mute.
    CHRONIC_DIGEST_ENABLED: bool = False
    CHRONIC_DIGEST_INTERVAL_HOURS: int = 6
    CHRONIC_DIGEST_WINDOW_HOURS: int = 24
    CHRONIC_DIGEST_MIN_FIRES: int = 5
    STATS_DIGEST_STALE_DAYS: int = Field(30, description="Threshold days for stale-deployment detection")

    # Executor approval (PR #3 executor track). Если True — Discord-embed
    # получает кнопку "Apply", которая после двухшагового подтверждения
    # запускает kubectl с dry_run=False. Требует:
    #   1. EXECUTOR_ENABLED=true (dry-run работает)
    #   2. DISCORD_PUBLIC_KEY настроен (Interactions endpoint регистрирован)
    #   3. executor_result.status=="dry_run_ok" на конкретном инциденте
    #   4. execution_intent.risk in {"low", "medium"} (high — manual only)
    # Default False — нужен явный опт-ин на проде.
    EXECUTOR_APPROVAL_ENABLED: bool = Field(False, description="Show Apply button on Discord embed")

    # Atlassian Jira — поиск известных инцидентов/задач по сервису.
    # Basic Auth: email + API token (https://id.atlassian.com/manage-profile/security/api-tokens)
    # Пусто = Jira отключена (graceful degrade).
    JIRA_BASE_URL: str = Field("", description="Atlassian Jira base URL (e.g. https://org.atlassian.net)")
    JIRA_EMAIL: str = Field("", description="Jira user email for API basic auth")
    JIRA_API_TOKEN: str = Field("", description="Jira API token")
    JIRA_PROJECT_KEY: str = Field("", description="Jira project key to search in (e.g. PROJ)")
    JIRA_BACKEND_LABEL: str = Field("backend", description="Label marking backend/infra issues")
    JIRA_SEARCH_DAYS: int = Field(30, description="Look-back window for Jira issue search")

    @model_validator(mode="after")
    def _enforce_prod_invariants(self) -> "Settings":
        if self.LLM_BACKEND == "anthropic" and not self.ANTHROPIC_API_KEY:
            raise ValueError(
                "ANTHROPIC_API_KEY is required when LLM_BACKEND=anthropic. "
                "For local dev without an API key, set LLM_BACKEND=claude_cli."
            )
        if self.ENV == "production":
            if not self.SAFE_MODE:
                raise ValueError(
                    "SAFE_MODE=false is forbidden in production. "
                    "Either set SAFE_MODE=true or change ENV."
                )
            if not self.ALERTMANAGER_WEBHOOK_SECRET:
                raise ValueError(
                    "ALERTMANAGER_WEBHOOK_SECRET must be set in production "
                    "to authenticate AlertManager webhook calls."
                )
        return self

    # Configuration for pydantic-settings v2
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )


# Module-level instance for global access
settings = Settings()
