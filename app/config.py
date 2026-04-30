from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    GEMINI_API_KEY: str
    REDIS_URL: str = "redis://localhost:6379/0"
    DISCORD_WEBHOOK_URL: str
    
    SAFE_MODE: bool = True
    APPROVAL_REQUIRED: bool = True
    
    K8S_CONTEXT: str = ""
    
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
