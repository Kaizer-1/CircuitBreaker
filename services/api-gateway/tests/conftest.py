import pytest

from api_gateway.config import Settings


@pytest.fixture
def settings() -> Settings:
    """Test settings: upstreams point at a closed local port, so no real service is contacted."""
    return Settings(service_upstreams={"order-service": "http://127.0.0.1:9"})


@pytest.fixture
def has_api_spec() -> bool:
    """False only for a service with no business API (it has no api/<service>.yaml)."""
    return True
