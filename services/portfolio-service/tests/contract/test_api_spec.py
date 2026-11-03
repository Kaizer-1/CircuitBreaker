"""Contract check: api/<service>.yaml and the running app must have the same operations (both ways).

Same file in every service. Operational endpoints (/healthz, /readyz, /metrics and, on the gateway,
/v1/system/status) are not in the specs; they are hidden from the app's schema, so excluded.
"""

from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI

from portfolio_service.config import Settings
from portfolio_service.main import create_app

SERVICE = Path(__file__).resolve().parents[2].name  # services/<service>/tests/contract/<this file>
REPO_ROOT = Path(__file__).resolve().parents[4]
METHODS = {"get", "post", "put", "delete", "patch"}

Operations = dict[tuple[str, str], dict[str, Any]]


def _operations(paths: dict[str, Any]) -> Operations:
    return {
        (method.upper(), path): op
        for path, item in paths.items()
        for method, op in item.items()
        if method in METHODS
    }


def _spec_operations(has_api_spec: bool) -> Operations:
    """`has_api_spec` comes from the service's conftest: False only for a service with no API."""
    spec_file = REPO_ROOT / "api" / f"{SERVICE}.yaml"
    if not has_api_spec:
        assert not spec_file.exists(), f"{SERVICE} now has a spec: set has_api_spec in conftest"
        return {}
    assert spec_file.exists(), f"missing spec {spec_file}"
    return _operations(yaml.safe_load(spec_file.read_text())["paths"])


def _app_operations(settings: Settings) -> Operations:
    app: FastAPI = create_app(settings, readiness_checks=[])
    return _operations(app.openapi()["paths"])


def test_every_operation_in_the_spec_exists_in_the_app(
    settings: Settings, has_api_spec: bool
) -> None:
    missing = sorted(set(_spec_operations(has_api_spec)) - set(_app_operations(settings)))
    assert not missing, f"in api/{SERVICE}.yaml but not in the app: {missing}"


def test_every_operation_in_the_app_exists_in_the_spec(
    settings: Settings, has_api_spec: bool
) -> None:
    extra = sorted(set(_app_operations(settings)) - set(_spec_operations(has_api_spec)))
    assert not extra, f"in the app but not in api/{SERVICE}.yaml: {extra}"


def test_operation_ids_and_seeds_ids_match_the_spec(settings: Settings, has_api_spec: bool) -> None:
    spec, app = _spec_operations(has_api_spec), _app_operations(settings)
    for key, spec_op in spec.items():
        app_op = app[key]
        assert app_op.get("operationId") == spec_op["operationId"], key
        assert app_op.get("x-seeds-id") == spec_op["x-seeds-id"], key
