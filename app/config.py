from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional

class Settings(BaseSettings):
    GEMINI_API_KEY: str
    REDIS_URL: str = "redis://localhost:6379/0"
    DISCORD_WEBHOOK_URL: str
    
    # Security
    NEW_RELIC_WEBHOOK_SECRET: Optional[str] = None
    SAFE_MODE: bool = True
    APPROVAL_REQUIRED: bool = True
    
    # Observability
    LOG_LEVEL: str = "INFO"
    AUDIT_LOG_PATH: str = "audit.log"
    
    K8S_CONTEXT: str = ""
    
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
