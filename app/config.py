from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional


class Settings(BaseSettings):
    """
    Application configuration settings using Pydantic-settings v2.
    Loads variables from environment or a .env file.
    """

    GEMINI_API_KEY: str = Field(..., description="API key for Gemini authentication")
    
    DATABASE_URL: str = Field(
        "postgresql://user:password@localhost:5432/dbname", 
        description="PostgreSQL connection string"
    )
    
    REDIS_URL: str = Field(
        "redis://localhost:6379/0", 
        description="Redis connection URL for Celery broker and backend"
    )
    
    MODEL_NAME: str = Field(
        "gemini-pro", 
        description="The model to use"
    )
    
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
    OIDC_WELL_KNOWN_URL: Optional[str] = None
    JWT_ALGORITHM: str = "RS256"
    JWT_AUDIENCE: Optional[str] = None
    
    # Security
    NEW_RELIC_WEBHOOK_SECRET: Optional[str] = None
    SAFE_MODE: bool = True
    APPROVAL_REQUIRED: bool = True
    
    # Observability
    OTLP_EXPORTER_ENDPOINT: str = "http://tempo:4317"

    # Configuration for pydantic-settings v2
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )


# Module-level instance for global access
settings = Settings()
