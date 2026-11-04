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
    postgres_pool_size: int = 10

    # A-19: the sweeper asks order-service for expiries (ADR-0017). Inside Compose: http://order-service:8000
    order_service_url: str = "http://localhost:8001"
    orphan_after_seconds: float = 120
    sweep_interval_seconds: float = 10
    ws_ping_seconds: float = 15.0  # Q-10 heartbeat (api/README.md)
    ws_pong_timeout_seconds: float = 45.0
    start_consumers: bool = True  # False in unit tests: no Postgres or Kafka at start-up
