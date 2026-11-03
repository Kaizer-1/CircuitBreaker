import pytest
from pydantic import SecretStr

from order_service.config import Settings


@pytest.fixture
def settings() -> Settings:
    """Test settings; tests pass their own readiness checks, so no real dependency is contacted."""
    return Settings(cassandra_password=SecretStr("test-only"))


@pytest.fixture
def has_api_spec() -> bool:
    """False only for a service with no business API (it has no api/<service>.yaml)."""
    return True
