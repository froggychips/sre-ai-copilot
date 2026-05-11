from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional, List


class Settings(BaseSettings):
    """
    Application configuration settings using Pydantic-settings v2.
    Loads variables from environment or a .env file.
    """
    ENV: str = Field("development", description="Environment (production/development)")
    ANTHROPIC_API_KEY: str = Field(..., description="API key for Anthropic Claude")
    
    DATABASE_URL: str = Field(
        "postgresql://user:password@localhost:5432/dbname", 
        description="PostgreSQL connection string"
    )
    
    REDIS_URL: str = Field(
        "redis://localhost:6379/0", 
        description="Redis connection URL for Celery broker and backend"
    )
    
    MODEL_NAME: str = Field(
        "claude-sonnet-4-6",
        description="The model to use"
    )

    # Backend для LLM-вызовов:
    #   anthropic — AsyncAnthropic SDK + ANTHROPIC_API_KEY (default, production)
    #   claude_cli — subprocess `claude --print` (local PoC без API key)
    LLM_BACKEND: str = Field("anthropic", description="anthropic|claude_cli")
    CLAUDE_CLI_TIMEOUT_SECONDS: float = 180.0

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
        1024, 
        description="Maximum number of tokens to generate in LLM responses"
    )
    
    LOG_LEVEL: str = Field(
        "INFO", 
        description="Standard logging level"
    )

    DISCORD_WEBHOOK_URL: str = Field(..., description="Discord webhook URL")
    
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
    OTLP_EXPORTER_ENDPOINT: str = "http://tempo:4317"
    AUDIT_LOG_PATH: str = "./audit.log"

    # TeamCity integration — обогащение incident-а recent-deploys.
    # Ходим через mcp-teamcity-server (external/mcp/teamcity-server, MR !1):
    #   prod: https://mcp-teamcity.lastoasisgame.com/mcp (после деплоя)
    #   local PoC: http://127.0.0.1:8001/mcp (server поднят локально)
    TEAMCITY_MCP_URL: str = Field("", description="mcp-teamcity-server MCP endpoint URL")
    TEAMCITY_MCP_TOKEN: str = Field("", description="Bearer для mcp_auth (пусто = no-auth local PoC)")
    TEAMCITY_WEB_URL: str = Field("https://wo-teamcity.lastoasisgame.com", description="TC web UI base — для viewLog ссылок в Discord")
    TC_TIMEOUT_SECONDS: float = 5.0
    TC_LOOKBACK_MINUTES: int = 60
    TC_BACKEND_PROJECT_ID: str = "Wo_Backend_K8sNewCluster"

    # Configuration for pydantic-settings v2
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )


# Module-level instance for global access
settings = Settings()
