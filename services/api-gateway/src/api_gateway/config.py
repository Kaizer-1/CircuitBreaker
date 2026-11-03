"""Configuration from environment variables (see .env.example)."""

from pydantic import Field

from api_gateway.runtime.settings import BaseServiceSettings

# The services the gateway routes to and reports on, with their host ports from `make up`.
DEFAULT_UPSTREAMS = {
    "order-service": "http://localhost:8001",
    "matching-engine": "http://localhost:8002",
    "market-data-service": "http://localhost:8003",
    "portfolio-service": "http://localhost:8004",
    "bot-traders": "http://localhost:8005",
    "sre-agent": "http://localhost:8006",
    "chaos-service": "http://localhost:8007",
}


class Settings(BaseServiceSettings):
    service_name: str = "api-gateway"

    # Service name -> base URL. Inside Compose and Kubernetes: http://<service>:8000.
    service_upstreams: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_UPSTREAMS))
    upstream_timeout_seconds: float = 5.0
    status_timeout_seconds: float = 2.0
