import pytest
from pydantic import SecretStr

from api_gateway.config import Settings

# Test-only values (never used outside these tests).
JWT_SECRET = "test-only-jwt-secret-at-least-32-bytes-long"
BOT_SECRET = "test-only-bot-secret"
ADMIN_PASSCODE = "test-only-passcode"


@pytest.fixture
def settings() -> Settings:
    """Test settings: upstreams point at a closed local port, so no real service is contacted."""
    return Settings(
        service_upstreams={"order-service": "http://127.0.0.1:9"},
        jwt_secret=SecretStr(JWT_SECRET),
        bot_secret=SecretStr(BOT_SECRET),
        admin_passcode=SecretStr(ADMIN_PASSCODE),
    )


@pytest.fixture
def has_api_spec() -> bool:
    """False only for a service with no business API (it has no api/<service>.yaml)."""
    return True
