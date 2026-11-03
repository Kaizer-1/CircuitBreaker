"""Configuration from environment variables (see .env.example)."""

from pydantic import SecretStr

from portfolio_service.runtime.settings import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "portfolio-service"

    kafka_bootstrap_servers: str = "localhost:19092"

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "portfolio"
    postgres_user: str = "portfolio_service"
    postgres_password: SecretStr  # no default: a secret never lives in code
