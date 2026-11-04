import os

import pytest
from hypothesis import HealthCheck
from hypothesis import settings as hypothesis_settings
from pydantic import SecretStr

from matching_engine.config import Settings

# Hypothesis profiles (tests/property). `ci` is the default for `make test` and CI; `deep` is the
# long run reported for Phase 2: `HYPOTHESIS_PROFILE=deep uv run pytest tests/property`.
for _name, _examples in (("ci", 1_000), ("deep", 25_000), ("dev", 100)):
    hypothesis_settings.register_profile(
        _name,
        max_examples=_examples,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
        print_blob=True,
    )
hypothesis_settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))


@pytest.fixture
def settings() -> Settings:
    """Test settings; tests pass their own readiness checks, so no real dependency is contacted."""
    return Settings(cassandra_password=SecretStr("test-only"), start_consumers=False)


@pytest.fixture
def has_api_spec() -> bool:
    """False only for a service with no business API (it has no api/<service>.yaml)."""
    return True
