"""Configuration from environment variables (see .env.example)."""

from chaos_service.runtime.settings import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "chaos-service"

    kafka_bootstrap_servers: str = "localhost:19092"

    # The targets' chaos hooks (ADR-0020). Inside Kubernetes and Compose: http://<service>:8000
    market_data_url: str = "http://localhost:8003"
    portfolio_url: str = "http://localhost:8004"
    bot_traders_url: str = "http://localhost:8005"
    start_consumers: bool = True  # False in unit tests: no Kafka or Kubernetes at start-up
