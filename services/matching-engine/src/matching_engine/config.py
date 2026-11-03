"""Configuration from environment variables (see .env.example)."""

from pydantic import SecretStr

from matching_engine.runtime.settings import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "matching-engine"

    kafka_bootstrap_servers: str = "localhost:19092"

    cassandra_contact_points: str = "localhost"  # comma-separated
    cassandra_port: int = 9042
    cassandra_keyspace: str = "matching_engine"
    cassandra_username: str = "matching_engine_role"
    cassandra_password: SecretStr  # no default: a secret never lives in code
