"""Configuration from environment variables (see .env.example)."""

from pydantic import SecretStr

from order_service.runtime.settings import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "order-service"

    kafka_bootstrap_servers: str = "localhost:19092"

    cassandra_contact_points: str = "localhost"  # comma-separated
    cassandra_port: int = 9042
    cassandra_keyspace: str = "order_service"
    cassandra_username: str = "order_service_role"
    cassandra_password: SecretStr  # no default: a secret never lives in code
