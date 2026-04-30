from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional

class Settings(BaseSettings):
    GEMINI_API_KEY: str
    DATABASE_URL: str = "postgresql://sre:sre@postgres:5432/sre_db"
    REDIS_URL: str = "redis://redis:6379/0"
    DISCORD_WEBHOOK_URL: str
    
    # Auth
    OIDC_WELL_KNOWN_URL: Optional[str] = None
    JWT_ALGORITHM: str = "RS256"
    JWT_AUDIENCE: Optional[str] = None
    
    # Security
    NEW_RELIC_WEBHOOK_SECRET: Optional[str] = None
    SAFE_MODE: bool = True
    APPROVAL_REQUIRED: bool = True
    
    # Observability
    LOG_LEVEL: str = "INFO"
    OTLP_EXPORTER_ENDPOINT: str = "http://tempo:4317"
    
    K8S_CONTEXT: str = ""
    
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
