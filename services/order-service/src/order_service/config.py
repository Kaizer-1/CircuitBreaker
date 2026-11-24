"""Configuration from environment variables (see .env.example)."""

from pydantic import Field, SecretStr

from order_service.application.fault_profile import FaultProfileName
from order_service.runtime.settings import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "order-service"

    kafka_bootstrap_servers: str = "localhost:19092"

    cassandra_contact_points: str = "localhost"  # comma-separated
    cassandra_port: int = 9042
    cassandra_keyspace: str = "order_service"
    cassandra_username: str = "order_service_role"
    cassandra_password: SecretStr  # no default: a secret never lives in code

    # A-06: the synchronous reservation (ADR-0011). Inside Compose: http://portfolio-service:8000
    portfolio_url: str = "http://localhost:8004"
    reservation_timeout_seconds: float = 0.5
    breaker_failure_threshold: int = 5
    breaker_open_seconds: float = 10.0

    publish_timeout_seconds: float = 60  # never publish an older OrderPlaced (ADR-0011)
    # The Kafka client gives up on a message this long after it was handed over, so the relay
    # starts no OrderPlaced send after 60 - 10 = 50 s and nothing lands after 60 s (ADR-0017).
    publish_delivery_timeout_seconds: float = 10
    relay_interval_seconds: float = 2.0
    start_consumers: bool = True  # False in unit tests: no Cassandra or Kafka at start-up

    # Requests one pod handles at once; beyond it 503 OVERLOADED (chaos scenarios section 3.3).
    max_in_flight: int = Field(default=50, ge=1)
    # Set only by chaos-service's bad deploy (chaos scenarios section 3.1); "" = a normal release.
    fault_profile: FaultProfileName = ""
    fault_error_rate: float = Field(default=0.4, ge=0.05, le=0.9)
