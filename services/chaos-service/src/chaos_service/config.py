"""Configuration from environment variables (see .env.example)."""

from chaos_service.runtime.settings import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "chaos-service"

    kafka_bootstrap_servers: str = "localhost:19092"
