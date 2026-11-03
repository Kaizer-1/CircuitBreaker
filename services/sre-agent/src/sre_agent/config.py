"""Configuration from environment variables (see .env.example)."""

from pydantic import SecretStr

from sre_agent.runtime.settings import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "sre-agent"

    kafka_bootstrap_servers: str = "localhost:19092"

    cassandra_contact_points: str = "localhost"  # comma-separated
    cassandra_port: int = 9042
    cassandra_keyspace: str = "sre_agent"
    cassandra_username: str = "sre_agent_role"
    cassandra_password: SecretStr  # no default: a secret never lives in code
