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

    # Q-14 and Q-15: the event store, read through matching-engine's internal API.
    # Inside Compose: http://matching-engine:8000
    matching_engine_url: str = "http://localhost:8002"

    # Time travel (Q-12) and replay (Q-13): concurrent work per replica, one place of each kept for
    # the admin role so the presenter is never refused (application/slots.py).
    rebuild_slots: int = 4
    replay_slots: int = 4
    admin_reserved_slots: int = 1

    ws_ping_seconds: float = 15.0  # api/README.md heartbeat
    ws_pong_timeout_seconds: float = 45.0
    start_consumers: bool = True  # False in unit tests: no Cassandra or Kafka at start-up
