"""Configuration from environment variables (see .env.example)."""

from bot_traders.runtime.settings import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "bot-traders"

    kafka_bootstrap_servers: str = "localhost:19092"
