"""Configuration from environment variables (see .env.example)."""

from pydantic import SecretStr

from market_data_service.runtime.settings import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "market-data-service"

    kafka_bootstrap_servers: str = "localhost:19092"

    cassandra_contact_points: str = "localhost"  # comma-separated
    cassandra_port: int = 9042
    cassandra_keyspace: str = "market_data"
    cassandra_username: str = "market_data_role"
    cassandra_password: SecretStr  # no default: a secret never lives in code
