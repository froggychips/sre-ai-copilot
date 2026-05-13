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

    # Прямой вызов async_process_incident прямо из вебхука (минуя Celery).
    # Полезно в локальном e2e без Redis: блокирует curl, но возвращает реальный
    # результат, виден в логах + persists в DB. Eager-mode Celery несовместим
    # с async endpoint-ом (loop.run_until_complete внутри уже запущенного loop).
    PIPELINE_DIRECT_INVOKE: bool = False

    # Не отправлять реальный Discord webhook, только логировать (для local e2e).
    DISCORD_DRY_RUN: bool = False

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
    SAFE_MODE: bool = True
    APPROVAL_REQUIRED: bool = True
    REPLAY_MODE: bool = False

    # Observability
    OTLP_EXPORTER_ENDPOINT: str = "http://jaeger.monitoring:4317"
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

    # GitLab — обогащение MR-метаданными по SHA из TC-деплоев.
    GITLAB_URL: str = Field("", description="GitLab base URL")
    GITLAB_TOKEN: str = Field("", description="GitLab Personal Access Token (read_api)")
    # Проекты для поиска MR по sha: project_id (числовой) или namespace/name.
    # backend-services живёт в new-wo/backend-services (id найдём динамически).
    GITLAB_BACKEND_PROJECT: str = Field("new-wo/backend-services", description="GitLab project path для backend MR-поиска")

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
    JIRA_PROJECT_KEY: str = Field("WO", description="Jira project key to search in")
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
