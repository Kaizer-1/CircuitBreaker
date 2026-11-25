"""Gateway behaviour: sessions, roles, identity headers, rate limits, the /internal/ block,
the proxy and the admin-only status report."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from api_gateway.config import Settings
from api_gateway.domain.rate_limit import RateLimiter
from api_gateway.domain.sessions import InvalidTokenError, Role, Sessions
from api_gateway.main import create_app
from tests.conftest import ADMIN_PASSCODE, BOT_SECRET, JWT_SECRET

RAJU = "3d2a1b0c-9e8f-4a7b-8c6d-5e4f3a2b1c0d"
Handler = Callable[[httpx.Request], httpx.Response]


def _client(settings: Settings, handler: Handler) -> TestClient:
    app = create_app(settings, readiness_checks=[], http_transport=httpx.MockTransport(handler))
    return TestClient(app, raise_server_exceptions=False)


def _with(settings: Settings, **upstreams: str) -> Settings:
    return settings.model_copy(update={"service_upstreams": upstreams})


def bearer(
    role: Role = Role.TRADER, sub: str = RAJU, nickname: str = "RajuTrader"
) -> dict[str, str]:
    token, _ = Sessions(JWT_SECRET).issue(sub, role, nickname)
    return {"Authorization": f"Bearer {token}"}


def portfolio(status: int = 201, trader_id: str = RAJU) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/internal/v1/accounts"
        body = json.loads(request.content)
        return httpx.Response(
            status,
            json={"trader_id": trader_id, "nickname": body["nickname"], "kind": body["kind"]},
        )

    return handler


@pytest.fixture
def with_portfolio(settings: Settings) -> Settings:
    return _with(settings, **{"portfolio-service": "http://portfolio-service:8000"})


# ------------------------------------------------------------------ A-01, A-03, A-04, Q-01


def test_joining_opens_an_account_and_returns_a_12_hour_trader_token(
    with_portfolio: Settings,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return portfolio()(request)

    with _client(with_portfolio, handler) as client:
        response = client.post("/v1/sessions", json={"nickname": "RajuTrader"})
    assert response.status_code == 201
    body = response.json()
    assert (body["token_type"], body["trader_id"], body["nickname"], body["role"]) == (
        "Bearer",
        RAJU,
        "RajuTrader",
        "trader",
    )
    assert json.loads(seen[0].content) == {"nickname": "RajuTrader", "kind": "HUMAN"}
    claims = jwt.decode(body["access_token"], JWT_SECRET, algorithms=["HS256"])
    assert (claims["sub"], claims["role"], claims["exp"] - claims["iat"]) == (
        RAJU,
        "trader",
        12 * 3600,
    )


def test_a_taken_nickname_is_409_and_a_bad_one_is_422(with_portfolio: Settings) -> None:
    with _client(with_portfolio, portfolio(409)) as client:
        taken = client.post("/v1/sessions", json={"nickname": "RajuTrader"})
        bad = client.post("/v1/sessions", json={"nickname": "no spaces!"})
    assert (taken.status_code, taken.json()["code"]) == (409, "NICKNAME_TAKEN")
    assert (bad.status_code, bad.json()["code"]) == (422, "VALIDATION_FAILED")


def test_an_offensive_nickname_is_refused_politely_before_any_account_exists(
    with_portfolio: Settings,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return portfolio()(request)

    with _client(with_portfolio, handler) as client:
        refused = client.post("/v1/sessions", json={"nickname": "B1tch_99"})
        ordinary = client.post("/v1/sessions", json={"nickname": "Harshit"})
    body = refused.json()
    assert (refused.status_code, body["code"]) == (422, "VALIDATION_FAILED")
    assert body["detail"] == (
        "That nickname can't be shown on the big screen. Please choose another one."
    )
    assert body["errors"] == [{"field": "nickname", "message": "Please choose another nickname."}]
    assert ordinary.status_code == 201
    assert [json.loads(r.content)["nickname"] for r in seen] == ["Harshit"]  # only one account


def test_joining_while_portfolio_is_down_is_503(with_portfolio: Settings) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with _client(with_portfolio, down) as client:
        response = client.post("/v1/sessions", json={"nickname": "RajuTrader"})
    assert (response.status_code, response.json()["code"]) == (503, "DEPENDENCY_UNAVAILABLE")
    with _client(with_portfolio, portfolio(500)) as client:
        assert client.post("/v1/sessions", json={"nickname": "RajuTrader"}).status_code == 503
    with _client(_with(with_portfolio), portfolio()) as client:  # no portfolio-service configured
        assert client.post("/v1/sessions", json={"nickname": "RajuTrader"}).status_code == 503


def test_bots_need_the_bot_secret_and_load_accounts_get_the_load_kind(
    with_portfolio: Settings,
) -> None:
    kinds: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        kinds.append(json.loads(request.content)["kind"])
        return portfolio(200, "b0b0b0b0-0007-4000-8000-000000000007")(request)

    with _client(with_portfolio, handler) as client:
        wrong = client.post(
            "/v1/bot-sessions", json={"account_name": "bot-07"}, headers={"X-Bot-Secret": "nope"}
        )
        bot = client.post(
            "/v1/bot-sessions",
            json={"account_name": "bot-07"},
            headers={"X-Bot-Secret": BOT_SECRET},
        )
        load = client.post(
            "/v1/bot-sessions",
            json={"account_name": "load-03"},
            headers={"X-Bot-Secret": BOT_SECRET},
        )
    assert (wrong.status_code, wrong.json()["code"]) == (401, "UNAUTHENTICATED")
    assert (bot.status_code, bot.json()["role"], bot.json()["nickname"]) == (201, "bot", "bot-07")
    assert load.status_code == 201 and kinds == ["BOT", "LOAD"]


def test_the_admin_logs_in_with_the_passcode_and_has_no_trading_account(settings: Settings) -> None:
    with _client(settings, portfolio()) as client:
        wrong = client.post(
            "/v1/admin/sessions", json={"nickname": "presenter", "passcode": "wrong-passcode"}
        )
        admin = client.post(
            "/v1/admin/sessions", json={"nickname": "presenter", "passcode": ADMIN_PASSCODE}
        )
        me = client.get(
            "/v1/session", headers={"Authorization": f"Bearer {admin.json()['access_token']}"}
        )
    assert (wrong.status_code, wrong.json()["code"]) == (401, "INVALID_PASSCODE")
    assert (admin.status_code, admin.json()["role"], admin.json()["trader_id"]) == (
        201,
        "admin",
        None,
    )
    assert me.json() | {"expires_at": "x"} == {
        "trader_id": None,
        "nickname": "presenter",
        "role": "admin",
        "expires_at": "x",
    }


def test_q01_decodes_my_token_and_refuses_bad_ones(settings: Settings) -> None:
    expired = jwt.encode(
        {"sub": RAJU, "role": "trader", "exp": datetime.now(UTC) - timedelta(seconds=1)},
        JWT_SECRET,
        algorithm="HS256",
    )
    forged = jwt.encode(
        {"sub": RAJU, "role": "admin", "exp": 4_000_000_000},
        "another-secret-of-32-bytes-or-more!",
        algorithm="HS256",
    )
    with _client(settings, portfolio()) as client:
        me = client.get("/v1/session", headers=bearer())
        none = client.get("/v1/session")
        expired_r = client.get("/v1/session", headers={"Authorization": f"Bearer {expired}"})
        forged_r = client.get("/v1/session", headers={"Authorization": f"Bearer {forged}"})
        basic = client.get("/v1/session", headers={"Authorization": "Basic abc"})
    assert (me.status_code, me.json()["trader_id"], me.json()["role"]) == (200, RAJU, "trader")
    for response in (none, expired_r, forged_r, basic):
        assert (response.status_code, response.json()["code"]) == (401, "UNAUTHENTICATED")


# ------------------------------------------------------------------ roles and identity headers


def test_the_proxy_sets_identity_from_the_token_and_never_from_the_client(
    settings: Settings,
) -> None:
    seen: dict[str, httpx.Request] = {}

    def upstream(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(202, json={"status": "PENDING"}, headers={"X-Upstream": "yes"})

    with _client(settings, upstream) as client:
        response = client.post(
            "/v1/orders?dry_run=1",
            json={"ticker": "ASTRYX"},
            headers=bearer()
            | {"X-Trader-Id": "forged", "X-Role": "admin", "X-Correlation-Id": "client-set"},
        )
    request = seen["request"]
    assert str(request.url) == "http://127.0.0.1:9/v1/orders?dry_run=1"
    assert (
        request.headers["x-trader-id"],
        request.headers["x-role"],
        request.headers["x-nickname"],
    ) == (
        RAJU,
        "trader",
        "RajuTrader",
    )
    assert response.status_code == 202 and response.headers["X-Upstream"] == "yes"
    # The gateway issues the correlation ID, ignores the client's, and forwards its own.
    assert request.headers["x-correlation-id"] == response.headers["X-Correlation-Id"]
    assert response.headers["X-Correlation-Id"] != "client-set"


def test_every_protected_route_needs_a_token(settings: Settings) -> None:
    with _client(settings, lambda r: httpx.Response(200)) as client:
        for method, path in [
            ("GET", "/v1/orders"),
            ("GET", "/v1/instruments"),
            ("POST", "/v1/chaos/experiments"),
        ]:
            response = client.request(method, path)
            assert (response.status_code, response.json()["code"]) == (401, "UNAUTHENTICATED"), path


def test_roles_are_checked_per_route(settings: Settings) -> None:
    upstreams = {
        s: "http://127.0.0.1:9"
        for s in ("order-service", "market-data-service", "chaos-service", "portfolio-service")
    }
    with _client(_with(settings, **upstreams), lambda r: httpx.Response(200, json={})) as client:
        admin_orders = client.get(
            "/v1/orders", headers=bearer(Role.ADMIN, "presenter", "presenter")
        )
        trader_chaos = client.post("/v1/chaos/experiments", json={}, headers=bearer())
        bot_orders = client.get(
            "/v1/orders", headers=bearer(Role.BOT, "b0b0b0b0-0007-4000-8000-000000000007", "bot-07")
        )
        admin_market = client.get(
            "/v1/instruments", headers=bearer(Role.ADMIN, "presenter", "presenter")
        )
        admin_chaos = client.get(
            "/v1/chaos/scenarios", headers=bearer(Role.ADMIN, "presenter", "presenter")
        )
        trader_reset = client.post("/v1/chaos/reset", headers=bearer())
        admin_reset = client.post(
            "/v1/chaos/reset", headers=bearer(Role.ADMIN, "presenter", "presenter")
        )
    assert (admin_orders.status_code, admin_orders.json()["code"]) == (403, "FORBIDDEN")
    assert (trader_chaos.status_code, trader_chaos.json()["code"]) == (403, "FORBIDDEN")
    assert (trader_reset.status_code, trader_reset.json()["code"]) == (403, "FORBIDDEN")
    assert bot_orders.status_code == admin_market.status_code == admin_chaos.status_code == 200
    assert admin_reset.status_code == 200  # A-20 reaches chaos-service (Phase 7)


def test_the_admin_proxy_call_carries_no_trader_id(settings: Settings) -> None:
    seen: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    with _client(_with(settings, **{"chaos-service": "http://chaos:8000"}), upstream) as client:
        client.get(
            "/v1/chaos/scenarios",
            headers=bearer(Role.ADMIN, "presenter", "presenter") | {"X-Trader-Id": RAJU},
        )
    assert "x-trader-id" not in seen[0].headers
    assert seen[0].headers["x-role"] == "admin"


# ------------------------------------------------------------------ rate limits


def test_a_trader_gets_20_requests_per_second_then_429(settings: Settings) -> None:
    with _client(settings, lambda r: httpx.Response(200, json={})) as client:
        client.app.state.rate_limiter = RateLimiter(clock=lambda: 100.0)  # type: ignore[attr-defined]
        codes = [client.get("/v1/orders", headers=bearer()).status_code for _ in range(21)]
        limited = client.get("/v1/orders", headers=bearer())
        other = client.get("/v1/orders", headers=bearer(sub="5e6f7a8b-9c0d-4e1f-8a2b-3c4d5e6f7a8b"))
    assert codes[:20] == [200] * 20 and codes[20] == 429
    assert limited.json()["code"] == "RATE_LIMITED" and limited.headers["Retry-After"] == "1"
    assert other.status_code == 200  # limits are per trader


def test_a_bot_gets_50_per_second() -> None:
    limiter = RateLimiter(clock=lambda: 0.0)
    assert [limiter.check("bot:b", 50) for _ in range(51)][-2:] == [None, 1]


def test_the_bucket_refills_over_time() -> None:
    now = [0.0]
    limiter = RateLimiter(clock=lambda: now[0])
    for _ in range(20):
        assert limiter.check("trader:t", 20) is None
    assert limiter.check("trader:t", 20) == 1
    now[0] += 0.05  # one token back
    assert limiter.check("trader:t", 20) is None
    small = RateLimiter(clock=lambda: 0.0, max_keys=2)
    for key in ("a", "b", "c"):  # the third key clears the table instead of growing it
        assert small.check(key, 1) is None


def test_joining_is_limited_per_address(with_portfolio: Settings) -> None:
    with _client(with_portfolio, portfolio()) as client:
        client.app.state.rate_limiter = RateLimiter(clock=lambda: 5.0)  # type: ignore[attr-defined]
        codes = [
            client.post("/v1/sessions", json={"nickname": f"Raju{n:02d}"}).status_code
            for n in range(21)
        ]
    assert codes[:20] == [201] * 20 and codes[20] == 429


# ------------------------------------------------------------------ proxy, /internal/, status


def test_proxy_returns_503_when_the_upstream_is_down(settings: Settings) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with _client(settings, down) as client:
        response = client.get("/v1/orders", headers=bearer())
    assert (response.status_code, response.json()["code"]) == (503, "DEPENDENCY_UNAVAILABLE")
    with _client(settings, down) as client:
        unknown = client.get("/v1/portfolio", headers=bearer())  # no address configured
    assert unknown.status_code == 503


def test_internal_paths_are_never_routed(settings: Settings) -> None:
    called: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        called.append(request)
        return httpx.Response(200)

    with _client(settings, upstream) as client:
        get = client.get(
            "/internal/v1/events", headers=bearer(Role.ADMIN, "presenter", "presenter")
        )
        post = client.post("/internal/v1/reservations", json={})
    assert get.status_code == post.status_code == 404
    assert get.json()["code"] == "NOT_FOUND"
    assert called == []


def _readyz_handler(request: httpx.Request) -> httpx.Response:
    ok = {"ok": True, "latency_ms": 1.0}
    if request.url.host == "order-service":
        return httpx.Response(
            200, json={"status": "ready", "checks": {"redpanda": ok, "cassandra": ok}}
        )
    if request.url.host == "matching-engine":
        down = {"ok": False, "latency_ms": 3000.0, "error": "ConnectionError: down"}
        return httpx.Response(
            503, json={"status": "not_ready", "checks": {"redpanda": ok, "cassandra": down}}
        )
    raise httpx.ConnectError("refused")


ADMIN_HEADERS = bearer(Role.ADMIN, "presenter", "presenter")


def test_system_status_is_admin_only(settings: Settings) -> None:
    with _client(settings, _readyz_handler) as client:
        assert client.get("/v1/system/status").status_code == 401
        assert client.get("/v1/system/status", headers=bearer()).status_code == 403
        assert client.get("/v1/system/status", headers=ADMIN_HEADERS).status_code == 200


def test_system_status_combines_every_service_and_groups_dependencies(settings: Settings) -> None:
    upstreams = {
        "order-service": "http://order-service:8000",
        "matching-engine": "http://matching-engine:8000",
        "sre-agent": "http://sre-agent:8000",
    }
    with _client(_with(settings, **upstreams), _readyz_handler) as client:
        response = client.get("/v1/system/status", headers=ADMIN_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unhealthy"
    by_name = {s["name"]: s for s in body["services"]}
    assert by_name["api-gateway"]["status"] == "ready"
    assert by_name["order-service"]["status"] == "ready"
    assert by_name["matching-engine"]["status"] == "not_ready"
    assert by_name["sre-agent"]["status"] == "unreachable"
    deps = {d["name"]: d["status"] for d in body["dependencies"]}
    assert deps == {"redpanda": "ok", "cassandra": "degraded"}


def test_system_status_is_healthy_when_every_service_is_ready(settings: Settings) -> None:
    with _client(
        _with(settings, **{"order-service": "http://order-service:8000"}), _readyz_handler
    ) as client:
        body = client.get("/v1/system/status", headers=ADMIN_HEADERS).json()
    assert body["status"] == "healthy"
    assert body["checked_at"].endswith("Z")


def test_tokens_expire(settings: Settings) -> None:
    sessions = Sessions(JWT_SECRET, hours=1)
    issued = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)
    token, expires = sessions.issue(RAJU, Role.TRADER, "RajuTrader", now=issued)
    assert sessions.verify(token, now=issued + timedelta(minutes=59)).trader_id == RAJU
    with pytest.raises(InvalidTokenError, match="expired"):
        sessions.verify(token, now=expires)
    with pytest.raises(InvalidTokenError):
        sessions.verify(
            jwt.encode(
                {"sub": RAJU, "role": "superuser", "exp": 4_000_000_000},
                JWT_SECRET,
                algorithm="HS256",
            )
        )
