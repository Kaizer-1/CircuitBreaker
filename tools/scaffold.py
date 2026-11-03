#!/usr/bin/env python3
"""Create a service skeleton from the order-service template (see docs/08-developer-setup.md).

    python3 tools/scaffold.py sre-agent                 # create services/sre-agent/
    python3 tools/scaffold.py order-service --out /tmp/x   # render elsewhere (to compare with the real one)

What it does:
  1. copies the files that must be identical in every service (runtime/, Dockerfile, shared tests, ...)
     from services/order-service, renaming the package;
  2. renders the files that differ per service (pyproject, config, main, business metrics, conftest, .env);
  3. generates the 501 stub routes from api/<service>.yaml (tools/gen_stubs.py);
  4. locks dependencies (uv lock) and formats with ruff.

To add a new service: add an entry to SERVICES below, add its api/ spec and its docker-compose.yml block.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = "order-service"

# Per-service facts, all taken from the docs: ports and stores from docs/03-architecture/01-system-architecture.md
# and 02-data-ownership.md, topics from the event catalog.
SERVICES: dict[str, dict] = {
    # custom=True: the shared files are copied, but config/main/pyproject/tests are written by hand
    # (the gateway has no Kafka or database; it proxies requests and reports system status).
    "api-gateway": {
        "summary": "the single public entry point: routing, sessions and rate limits",
        "topics": (),
        "custom": True,
    },
    "order-service": {
        "summary": "validate, reserve and track orders",
        "topics": ("orders.v1", "matching.v1"),
        "cassandra": "order_service",
    },
    "matching-engine": {
        "summary": "price-time matching and the market event store",
        "topics": ("orders.v1", "matching.v1"),
        "cassandra": "matching_engine",
    },
    "market-data-service": {
        "summary": "candles, depth, trades, live streams and time travel",
        "topics": ("matching.v1", "candles.v1"),
        "cassandra": "market_data",
    },
    "portfolio-service": {
        "summary": "accounts, reservations, settlement and the leaderboard",
        "topics": ("matching.v1",),
        "postgres": "portfolio",
    },
    "bot-traders": {
        "summary": "20 automated traders that use the public API",
        "topics": ("candles.v1",),
    },
    "sre-agent": {
        "summary": "detect, diagnose and remediate incidents",
        "topics": ("incidents.v1",),
        "cassandra": "sre_agent",
    },
    "chaos-service": {
        "summary": "controlled failure injection",
        "topics": ("incidents.v1",),
    },
}

# Business metrics named in the docs (docs/05-coding-standards.md; docs/03-architecture/05-sre-agent-design.md).
# Services not listed have none named yet.
BUSINESS_METRICS: dict[str, list[tuple[str, str, str, list[str]]]] = {
    "order-service": [
        (
            "orders_placed_total",
            "Counter",
            "Orders accepted by order-service, by order type and side.",
            ["order_type", "side"],
        ),
    ],
    "sre-agent": [
        (
            "sre_agent_incidents_total",
            "Counter",
            "Incidents by alert and resolution.",
            ["alert", "resolution"],
        ),
        (
            "sre_agent_actions_total",
            "Counter",
            "Remediation actions by action, service and outcome.",
            ["action", "service", "outcome"],
        ),
        (
            "sre_agent_declined_total",
            "Counter",
            "Remediations declined, by who and why.",
            ["by", "reason"],
        ),
        (
            "sre_agent_time_to_recover_seconds",
            "Histogram",
            "Seconds from incident opened to resolved.",
            [],
        ),
        ("sre_agent_llm_latency_seconds", "Histogram", "LLM call latency in seconds.", []),
        ("sre_agent_llm_errors_total", "Counter", "Failed LLM calls.", []),
        ("sre_agent_llm_tokens_total", "Counter", "LLM tokens used, by model.", ["model"]),
    ],
}

# Files that must be identical (after renaming the package) in every service; the drift check enforces it.
MANAGED_FILES = [
    "Dockerfile",
    ".dockerignore",
    ".python-version",
    "tests/__init__.py",
    "tests/unit/__init__.py",
    "tests/contract/__init__.py",
    "tests/integration/__init__.py",
    "tests/unit/test_runtime.py",
    "tests/contract/test_api_spec.py",
]
MANAGED_DIRS = ["src/{pkg}/runtime"]
LAYER_DOCS = {
    "application": "Use cases (place_order, cancel_order, apply_matching_event). Built in Phase 2.",
    "domain": "Pure business rules and models; imports nothing from api/ or adapters/. Built in Phase 2.",
    "ports": "Interfaces the application needs (repositories, event publisher, clients). Built in Phase 2.",
    "adapters": "Implementations of the ports: cassandra/, redpanda/, http/. Built in Phase 2.",
    "events": "Pydantic models of produced and consumed events (match the event catalog). Built in Phase 2.",
}


def sub(text: str, name: str, pkg: str) -> str:
    return text.replace("order_service", pkg).replace("order-service", name)


def render_pyproject(name: str, info: dict) -> str:
    deps = [
        '"fastapi>=0.162,<1"',
        '"uvicorn>=0.54,<1"',
        '"pydantic-settings>=2.16,<3"',
        '"prometheus-client>=0.26,<1"',
    ]
    deps.append('"aiokafka>=0.15,<1"')
    if "cassandra" in info:
        deps.append('"cassandra-driver>=3.30,<4"')
    if "postgres" in info:
        deps.append('"psycopg[binary]>=3.3,<4"')
    text = (ROOT / "services" / TEMPLATE / "pyproject.toml").read_text()
    start = text.index("dependencies = [")
    end = text.index("]\n", start) + 2
    text = (
        text[:start]
        + "dependencies = [\n"
        + "".join(f"  {d},\n" for d in deps)
        + "]\n"
        + text[end:]
    )
    text = text.replace('name = "order-service"', f'name = "{name}"')
    text = text.replace(
        'description = "CircuitBreaker order-service: validate, reserve and track orders (skeleton)."',
        f'description = "CircuitBreaker {name}: {info["summary"]} (skeleton)."',
    )
    return text


def render_config(name: str, pkg: str, info: dict) -> str:
    fields = ['    kafka_bootstrap_servers: str = "localhost:19092"']
    if "cassandra" in info:
        ks = info["cassandra"]
        fields += [
            "",
            '    cassandra_contact_points: str = "localhost"  # comma-separated',
            "    cassandra_port: int = 9042",
            f'    cassandra_keyspace: str = "{ks}"',
            f'    cassandra_username: str = "{ks}_role"',
            "    cassandra_password: SecretStr  # no default: a secret never lives in code",
        ]
    if "postgres" in info:
        db = info["postgres"]
        fields += [
            "",
            '    postgres_host: str = "localhost"',
            "    postgres_port: int = 5432",
            f'    postgres_db: str = "{db}"',
            f'    postgres_user: str = "{db}_service"',
            "    postgres_password: SecretStr  # no default: a secret never lives in code",
        ]
    secret_import = (
        "from pydantic import SecretStr\n\n" if ("cassandra" in info or "postgres" in info) else ""
    )
    return (
        '"""Configuration from environment variables (see .env.example)."""\n\n'
        f"{secret_import}from {pkg}.runtime.settings import BaseServiceSettings\n\n\n"
        "class Settings(BaseServiceSettings):\n"
        f'    service_name: str = "{name}"\n\n' + "\n".join(fields) + "\n"
    )


def render_main(name: str, pkg: str, info: dict) -> str:
    checks = (
        ["redpanda_check"]
        + (["cassandra_check"] if "cassandra" in info else [])
        + (["postgres_check"] if "postgres" in info else [])
    )
    body = ["        redpanda_check(settings.kafka_bootstrap_servers, TOPICS),"]
    if "cassandra" in info:
        body += [
            "        cassandra_check(",
            '            settings.cassandra_contact_points.split(","),',
            "            settings.cassandra_port,",
            "            settings.cassandra_keyspace,",
            "            settings.cassandra_username,",
            "            settings.cassandra_password.get_secret_value(),",
            "        ),",
        ]
    if "postgres" in info:
        body += [
            "        postgres_check(",
            "            settings.postgres_host,",
            "            settings.postgres_port,",
            "            settings.postgres_db,",
            "            settings.postgres_user,",
            "            settings.postgres_password.get_secret_value(),",
            "        ),",
        ]
    topics = ", ".join(f'"{t}"' for t in info["topics"]) + ("," if len(info["topics"]) == 1 else "")
    return f'''"""{name} app factory.

Run with: uvicorn {pkg}.main:create_app --factory.
"""

from collections.abc import Sequence

from fastapi import FastAPI

from {pkg}.api import stubs
from {pkg}.business_metrics import register_business_metrics
from {pkg}.config import Settings
from {pkg}.runtime.checks import {", ".join(sorted(checks))}
from {pkg}.runtime.health import ReadinessCheck, health_router
from {pkg}.runtime.jsonlog import configure_logging
from {pkg}.runtime.metrics import Metrics, metrics_router
from {pkg}.runtime.middleware import ObservabilityMiddleware
from {pkg}.runtime.problem import install_exception_handlers

# Topics this service produces or consumes, per the event catalog.
TOPICS = ({topics})


def default_checks(settings: Settings) -> list[ReadinessCheck]:
    return [
{chr(10).join(body)}
    ]


def create_app(
    settings: Settings | None = None, readiness_checks: Sequence[ReadinessCheck] | None = None
) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.service_name, settings.log_level)
    metrics = Metrics(settings.service_name)
    business_metrics = register_business_metrics(metrics.registry)
    checks = list(readiness_checks) if readiness_checks is not None else default_checks(settings)

    app = FastAPI(title=settings.service_name, version="0.1.0", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.metrics = metrics
    app.state.business_metrics = business_metrics

    app.add_middleware(
        ObservabilityMiddleware,
        service=settings.service_name,
        metrics=metrics,
        issue_correlation_id=False,
    )
    install_exception_handlers(app)
    app.include_router(health_router(settings.service_name, checks))
    app.include_router(metrics_router(metrics))
    app.include_router(stubs.router)
    return app
'''


def render_business_metrics(name: str, info: dict) -> str:
    metrics = BUSINESS_METRICS.get(name, [])
    if not metrics:
        return (
            '"""Business metrics named in the docs. None are named for this service yet; add them here."""\n\n'
            "from dataclasses import dataclass\n\nfrom prometheus_client import CollectorRegistry\n\n\n"
            "@dataclass(frozen=True)\nclass BusinessMetrics:\n    pass\n\n\n"
            "def register_business_metrics(registry: CollectorRegistry) -> BusinessMetrics:\n"
            "    return BusinessMetrics()\n"
        )
    kinds = sorted({m[1] for m in metrics})
    fields = "\n".join(f"    {m[0]}: {m[1]}" for m in metrics)
    regs = []
    for metric, kind, doc, labels in metrics:
        regs.append(
            f'        {metric}={kind}(\n            "{metric}",\n            "{doc}",\n'
            + (f"            {labels!r},\n".replace("'", '"') if labels else "")
            + "            registry=registry,\n        ),"
        )
    return (
        '"""Business metrics named in the docs. Registered now at zero; later phases update them."""\n\n'
        f"from dataclasses import dataclass\n\nfrom prometheus_client import CollectorRegistry, {', '.join(kinds)}\n\n\n"
        f"@dataclass(frozen=True)\nclass BusinessMetrics:\n{fields}\n\n\n"
        "def register_business_metrics(registry: CollectorRegistry) -> BusinessMetrics:\n"
        "    return BusinessMetrics(\n" + "\n".join(regs) + "\n    )\n"
    )


def render_conftest(name: str, pkg: str, info: dict) -> str:
    has_spec = (ROOT / "api" / f"{name}.yaml").exists()
    secrets = []
    if "cassandra" in info:
        secrets.append('cassandra_password=SecretStr("test-only")')
    if "postgres" in info:
        secrets.append('postgres_password=SecretStr("test-only")')
    uses_secret = bool(secrets)
    imports = "import pytest\n" + ("from pydantic import SecretStr\n" if uses_secret else "")
    return (
        f"{imports}\nfrom {pkg}.config import Settings\n\n\n@pytest.fixture\ndef settings() -> Settings:\n"
        '    """Test settings; tests pass their own readiness checks, so no real dependency is contacted."""\n'
        f"    return Settings({', '.join(secrets)})\n"
        "\n\n@pytest.fixture\ndef has_api_spec() -> bool:\n"
        '    """False only for a service with no business API (it has no api/<service>.yaml)."""\n'
        f"    return {has_spec}\n"
    )


def render_env_example(name: str, info: dict) -> str:
    lines = [
        "# Copy to .env to run this service outside Docker. `.env` is git-ignored; only this example is committed.",
        "# Inside Docker Compose these values are set by docker-compose.yml, so you do not need a .env.",
        "LOG_LEVEL=INFO",
        "ENVIRONMENT=dev",
        "KAFKA_BOOTSTRAP_SERVERS=localhost:19092",
    ]
    if "cassandra" in info:
        ks = info["cassandra"]
        lines += [
            "CASSANDRA_CONTACT_POINTS=localhost",
            "CASSANDRA_PORT=9042",
            f"CASSANDRA_KEYSPACE={ks}",
            f"CASSANDRA_USERNAME={ks}_role",
            "# Local-development placeholder created by dev/cassandra/init-keyspaces.sh",
            f"CASSANDRA_PASSWORD=cb_dev_{ks}",
        ]
    if "postgres" in info:
        db = info["postgres"]
        lines += [
            "POSTGRES_HOST=localhost",
            "POSTGRES_PORT=5432",
            f"POSTGRES_DB={db}",
            f"POSTGRES_USER={db}_service",
            "# Local-development placeholder created by dev/postgres/01-portfolio.sql",
            f"POSTGRES_PASSWORD=cb_dev_{db}",
        ]
    return "\n".join(lines) + "\n"


def scaffold(name: str, out_root: Path) -> Path:
    info = SERVICES[name]
    pkg = name.replace("-", "_")
    src_root = ROOT / "services" / TEMPLATE
    dest = out_root / name
    if dest.exists() and any(f.name != "README.md" for f in dest.iterdir()):
        sys.exit(f"{dest} already has files besides README.md; refusing to overwrite")

    def write(rel: str, text: str) -> None:
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    for rel in MANAGED_FILES:
        write(rel, sub((src_root / rel).read_text(), name, pkg))
    for tmpl in MANAGED_DIRS:
        rel_dir = tmpl.format(pkg="order_service")
        for f in sorted((src_root / rel_dir).glob("*.py")):
            write(f"src/{pkg}/runtime/{f.name}", sub(f.read_text(), name, pkg))

    if "cassandra" in info or "postgres" in info:
        write("migrations/.gitkeep", "")  # CQL / SQL migrations of the service's own store
    write(
        f"src/{pkg}/__init__.py",
        f'"""{name}: {info["summary"]}.\n\nSkeleton only (Phase 1).\n"""\n',
    )
    if not info.get("custom"):
        write(
            "tests/unit/test_stubs.py",
            sub((src_root / "tests/unit/test_stubs.py").read_text(), name, pkg),
        )
    write(
        f"src/{pkg}/api/__init__.py",
        '"""HTTP routers: thin; parse, call a use case, map errors to Problem Details."""\n',
    )
    for layer, doc in LAYER_DOCS.items():
        write(f"src/{pkg}/{layer}/__init__.py", f'"""{doc}"""\n')
    if not info.get("custom"):
        write("pyproject.toml", render_pyproject(name, info))
        write(f"src/{pkg}/config.py", render_config(name, pkg, info))
        write(f"src/{pkg}/main.py", render_main(name, pkg, info))
        write(f"src/{pkg}/business_metrics.py", render_business_metrics(name, info))
        write("tests/conftest.py", render_conftest(name, pkg, info))
        write(".env.example", render_env_example(name, info))

    spec = ROOT / "api" / f"{name}.yaml"
    if spec.exists():
        target = dest / "src" / pkg / "api" / "stubs.py"
        subprocess.run(
            [sys.executable, str(ROOT / "tools" / "gen_stubs.py"), name, str(target)], check=True
        )  # noqa: S603
    else:  # services without an API spec (bot-traders) have no stub routes
        write(
            f"src/{pkg}/api/stubs.py",
            '"""No business API: only /healthz, /readyz and /metrics (docs/05-coding-standards.md)."""\n\nfrom fastapi import APIRouter\n\nrouter = APIRouter()\n',
        )
    return dest


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] not in SERVICES:
        print(__doc__)
        return 2
    name = args[0]
    out_root = Path(args[args.index("--out") + 1]) if "--out" in args else ROOT / "services"
    dest = scaffold(name, out_root)
    if SERVICES[name].get("custom"):
        print(
            f"created {dest} (shared files only: write config, main, pyproject and tests by hand)"
        )
        return 0
    subprocess.run(["uv", "lock", "-q"], cwd=dest, check=True)  # noqa: S603, S607
    subprocess.run(["uv", "sync", "-q"], cwd=dest, check=True)  # noqa: S603, S607
    for cmd in (["check", "--fix", "-q", "."], ["format", "-q", "."]):
        subprocess.run(["uv", "run", "ruff", *cmd], cwd=dest, check=False)  # noqa: S603, S607
    print(f"created {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
