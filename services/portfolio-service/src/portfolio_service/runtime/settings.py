"""Base settings: every service reads its configuration from environment variables (12-factor)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class BaseServiceSettings(BaseSettings):
    """Fields every service has. Each service's config.py extends this with its own dependencies."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str
    environment: str = "dev"
    log_level: str = "INFO"
