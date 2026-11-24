# API Specifications

OpenAPI 3.1 specs, one per service that exposes HTTP, plus the WebSocket channels (which OpenAPI does not
describe well). This is **SEED(S) step 5**: every action and query from
[04-actions-and-queries.md](../docs/01-seeds/04-actions-and-queries.md) is specified here, and every operation
carries its ID in its description and in an `x-seeds-id` field.

| File | Service | Audience |
|---|---|---|
| [`api-gateway.yaml`](api-gateway.yaml) | `api-gateway` | **The public API** — what the web app and bots call. Sessions + proxied routes. |
| [`order-service.yaml`](order-service.yaml) | `order-service` | Gateway → service |
| [`matching-engine.yaml`](matching-engine.yaml) | `matching-engine` | Internal only (event store reads) |
| [`market-data-service.yaml`](market-data-service.yaml) | `market-data-service` | Gateway → service; internal chaos hooks |
| [`portfolio-service.yaml`](portfolio-service.yaml) | `portfolio-service` | Gateway → service; internal accounts + reservations |
| [`sre-agent.yaml`](sre-agent.yaml) | `sre-agent` | Gateway → service |
| [`chaos-service.yaml`](chaos-service.yaml) | `chaos-service` | Gateway → service (admin) |
| [`shared/common.yaml`](shared/common.yaml) | — | Shared schemas (Problem, Ticker, PricePaise, …), parameters and error responses, referenced by all specs so each shape is defined once |

`bot-traders` has no business API (only the standard `/healthz`, `/readyz`, `/metrics`), so it has no spec.

**Validate:** `npx @redocly/cli lint` (uses [`../redocly.yaml`](../redocly.yaml)). Current status: 0 errors,
0 warnings.

## Conventions

| Rule | Value |
|---|---|
| Versioning | Path prefix `/v1/`. Breaking changes get `/v2/` served alongside `/v1/`. |
| Internal paths | Service-to-service endpoints live under `/internal/v1/…`; `api-gateway` refuses to route any `/internal/` path. |
| JSON | `snake_case` fields; money as integer `*_paise`; times as ISO-8601 UTC `*_at`; enums `UPPER_SNAKE_CASE`. |
| Errors | `application/problem+json` (RFC 9457) with a stable `code` (catalog below) and `correlation_id`. |
| Async commands | `POST /v1/orders` and `DELETE /v1/orders/{order_id}` return **202 Accepted**; the outcome arrives by events/WebSocket. |
| Idempotency | `POST /v1/orders` accepts an `Idempotency-Key` header; internal `POST`s are idempotent by natural key (`order_id`, nickname for bots). |
| Auth (public) | `Authorization: Bearer <JWT>` from A-01/A-03/A-04. Roles: `trader`, `bot`, `admin`. |
| Auth (internal) | The gateway sets `X-Trader-Id`, `X-Role`, `X-Nickname`, `X-Correlation-Id` after verifying the JWT; it strips any client-supplied copies. Services trust these headers only because network policies allow traffic from the gateway and named peers. |
| Pagination | `limit` + opaque `cursor` → `next_cursor` (null on the last page). |

## Trace map: actions and queries → operations

| ID | Name | Public operation (api-gateway) | Service operation | Spec |
|---|---|---|---|---|
| A-01 | Join the market | `POST /v1/sessions` (`joinMarket`) | — (gateway itself) | api-gateway |
| A-02 | Open a trading account | — | `POST /internal/v1/accounts` (`openAccount`) | portfolio-service |
| A-03 | Start a bot session | `POST /v1/bot-sessions` (`startBotSession`) | — (gateway itself) | api-gateway |
| A-04 | Start an admin session | `POST /v1/admin/sessions` (`startAdminSession`) | — (gateway itself) | api-gateway |
| A-05 | Place an order | `POST /v1/orders` (`placeOrder`) | `POST /v1/orders` (`placeOrder`) | order-service |
| A-06 | Reserve cash or shares | — | `POST /internal/v1/reservations` (`createReservation`) | portfolio-service |
| A-07 | Cancel an order | `DELETE /v1/orders/{order_id}` (`cancelOrder`) | `DELETE /v1/orders/{order_id}` (`cancelOrder`) | order-service |
| A-08 | Match an order | — | event-driven (`orders.v1`) | — |
| A-09 | Settle a trade | — | event-driven (`matching.v1`) | — |
| A-10 | Start a chaos experiment | `POST /v1/chaos/experiments` | `POST /v1/chaos/experiments` (`startChaosExperiment`) | chaos-service |
| A-11 | Stop a chaos experiment | `POST /v1/chaos/experiments/{experiment_id}/stop` | same (`stopChaosExperiment`) | chaos-service |
| A-12 | Inject a fault | — | `POST /internal/v1/faults` (`injectFault`) | market-data-service |
| A-13 | Clear a fault | — | `DELETE /internal/v1/faults` (`clearFault`) | market-data-service |
| A-14 | Update agent settings | `PUT /v1/agent/settings` | same (`updateAgentSettings`) | sre-agent |
| A-15 | Decide on a remediation | `POST /v1/incidents/{incident_id}/decision` | same (`decideRemediation`) | sre-agent |
| A-16 | Open an incident | — | agent loop (Prometheus alert) | — |
| A-17 | Execute a remediation | — | agent tool → Kubernetes API | — |
| A-18 | Deploy a new version | — | GitHub Actions + Helm | — |
| A-19 | Request expiry of an unconfirmed order | — | `POST /internal/v1/order-expiries` (`requestOrderExpiry`) | order-service |
| A-20 | Reset all chaos | `POST /v1/chaos/reset` | same (`resetChaos`) | chaos-service |
| A-21 | Degrade the portfolio database | — | `POST /internal/v1/faults` (`injectDatabaseFault`) | portfolio-service |
| A-22 | Clear the database fault | — | `DELETE /internal/v1/faults` (`clearDatabaseFault`) | portfolio-service |
| A-23 | Start a panic sell | — | `POST /internal/v1/faults` (`startPanicSell`) | bot-traders |
| A-24 | Stop the panic sell | — | `DELETE /internal/v1/faults` (`stopPanicSell`) | bot-traders |
| Q-01 | Get my session | `GET /v1/session` (`getCurrentSession`) | — (gateway itself) | api-gateway |
| Q-02 | List instruments | `GET /v1/instruments` | same (`listInstruments`) | market-data-service |
| Q-03 | Get candles | `GET /v1/market/{ticker}/candles` | same (`getCandles`) | market-data-service |
| Q-04 | Get order-book depth | `GET /v1/market/{ticker}/order-book` | same (`getOrderBook`) | market-data-service |
| Q-05 | Get recent trades | `GET /v1/market/{ticker}/trades` | same (`getRecentTrades`) | market-data-service |
| Q-06 | Stream live market | WS `/v1/ws/market/{ticker}` | same | market-data-service (below) |
| Q-07 | List my orders | `GET /v1/orders` | same (`listMyOrders`) | order-service |
| Q-08 | Get one order | `GET /v1/orders/{order_id}` | same (`getOrder`) | order-service |
| Q-09 | Get my portfolio | `GET /v1/portfolio` | same (`getMyPortfolio`) | portfolio-service |
| Q-10 | Stream my portfolio | WS `/v1/ws/portfolio` | same | portfolio-service (below) |
| Q-11 | Get the leaderboard | `GET /v1/leaderboard` | same (`getLeaderboard`) | portfolio-service |
| Q-12 | Market at time T | `GET /v1/time-travel/{ticker}` | same (`getMarketAt`) | market-data-service |
| Q-13 | Stream a replay | WS `/v1/ws/replay/{ticker}` | same | market-data-service (below) |
| Q-14 | Read market events | — | `GET /internal/v1/events` (`listMarketEvents`) | matching-engine |
| Q-15 | Nearest snapshot | — | `GET /internal/v1/snapshots/{ticker}` (`getBookSnapshot`) | matching-engine |
| Q-16 | Live engine book | — | `GET /internal/v1/books/{ticker}` (`getLiveBook`) | matching-engine |
| Q-17 | List incidents | `GET /v1/incidents` | same (`listIncidents`) | sre-agent |
| Q-18 | Get an incident | `GET /v1/incidents/{incident_id}` | same (`getIncident`) | sre-agent |
| Q-19 | Stream incident feed | WS `/v1/ws/incidents` | same | sre-agent (below) |
| Q-20 | Get agent settings | `GET /v1/agent/settings` | same (`getAgentSettings`) | sre-agent |
| Q-21 | List chaos scenarios | `GET /v1/chaos/scenarios` | same (`listChaosScenarios`) | chaos-service |
| Q-22 | List experiments | `GET /v1/chaos/experiments` | same (`listChaosExperiments`) | chaos-service |
| Q-23 | Get an experiment | `GET /v1/chaos/experiments/{experiment_id}` | same (`getChaosExperiment`) | chaos-service |

## WebSocket channels

> **WebSocket:** a long-lived two-way connection; the server pushes messages as things happen, so phones don't
> have to poll. All channels go through `api-gateway`, which authenticates and proxies the upgrade.

**Common rules**

- **Auth:** browsers cannot set headers on WebSocket requests, so the token is passed as a query parameter:
  `?access_token=<JWT>`. The gateway validates it and **redacts it from access logs**.
- **Message envelope:** every message is JSON `{"type": "<message type>", "data": {...}}`.
- **Heartbeat:** the server sends `{"type": "ping"}` every 15 s; clients reply `{"type": "pong"}`. No pong for 45 s
  → connection closed.
- **Reconnect:** clients reconnect with exponential backoff (1 s → 30 s) and receive a fresh `snapshot` first,
  so a missed message never leaves the UI wrong.
- **Close codes:** `4401` unauthenticated, `4403` forbidden, `4404` unknown ticker/incident, `4422` invalid parameters,
  `1013` try again later (a full replica), `1011` server error.

### Q-06 — `/v1/ws/market/{ticker}` (market-data-service, any role)

| Direction | `type` | `data` |
|---|---|---|
| server → client | `snapshot` | On connect: `{ order_book: OrderBookDepth, last_price_paise, candles: Candle[] (last 60 of 5s), recent_trades: Trade[] (last 50, newest first; optional field added in Phase 3) }`, all as of `order_book.as_of_sequence` |
| server → client | `trade` | `Trade` (same schema as Q-05) — every trade |
| server → client | `candle` | `{ interval, closed: boolean, candle: Candle }` — the forming candle every second, and once more with `closed: true` |
| server → client | `book` | `OrderBookDepth` — throttled to at most 4 per second |
| client → server | `subscribe` | Optional `{ intervals: ["5s", "1m"] }` to choose candle intervals (default `["5s"]`) |

```json
{"type": "trade", "data": {"trade_id": "a0b1c2d3-e4f5-4a6b-8c7d-8e9f0a1b2c3d", "sequence": 1042, "price_paise": 24550, "quantity": 20, "aggressor_side": "BUY", "executed_at": "2026-11-20T10:42:01.263Z"}}
```

**Snapshot, then updates in sequence (resync with no gaps or duplicates).** The snapshot and every later message come
from one projection that applies the ticker's `matching.v1` events strictly in `sequence` order. The client keeps the
snapshot's `order_book.as_of_sequence`; it drops a `trade` whose `sequence` is at or below what it already has, replaces
the book with a `book` whose `as_of_sequence` is newer, and replaces the candle with the same `bucket_start` and
`interval`. After any disconnect it reconnects and starts again from a fresh snapshot, so nothing is missed and nothing
counted twice. A client that falls 1,000 messages behind is closed with `1011` and does the same. Close codes: `4404`
unknown ticker, `1011` while market-data is still rebuilding from the event store (retry) or after falling behind.

### Q-10 — `/v1/ws/portfolio` (portfolio-service, roles trader, bot)

| Direction | `type` | `data` |
|---|---|---|
| server → client | `portfolio` | `Portfolio` (same schema as Q-09) — on connect and after every change |
| server → client | `fill` | `{ order_id, trade_id, ticker, side, price_paise, quantity, order_remaining_quantity, executed_at }` |
| server → client | `order_closed` | `{ order_id, status: FILLED \| CANCELLED, cancel_reason: USER_REQUESTED \| UNFILLED_MARKET_ORDER \| SELF_TRADE_PREVENTED \| ORDER_EXPIRED \| null }` |

```json
{"type": "fill", "data": {"order_id": "7f3c9e10-2b4d-4e6f-8a1b-3c5d7e9f0a12", "trade_id": "a0b1c2d3-e4f5-4a6b-8c7d-8e9f0a1b2c3d", "ticker": "CALVERA", "side": "BUY", "price_paise": 24550, "quantity": 20, "order_remaining_quantity": 30, "executed_at": "2026-11-20T10:42:01.263Z"}}
```

After a trade the phone receives `fill` (and `order_closed` if the order is done), then the new `portfolio`. Only the
caller's own portfolio is ever streamed: the gateway takes the trader from the token. A price move re-values a portfolio
that holds that ticker at most once a second. An `OrderExpired` (an order the engine never saw) sends only the new
`portfolio`: there is no order to close. Close codes: `4401` no trader identity, `4404` no such account.

### Q-13 — `/v1/ws/replay/{ticker}?from=…&to=…&speed=…` (market-data-service, any role)

Query parameters: `from`, `to` (ISO-8601 with a UTC offset, window ≤ 30 minutes, `to` not in the future, `from` within
the last 24 hours), `speed` ∈ {1, 2, 5, 10, 60}. The 30-minute cap is for playback only: time travel (Q-12) seeks
anywhere in the last 24 hours, the time-travel horizon (every older event is still stored; only the snapshots that make
seeking fast are not kept longer).

| Direction | `type` | `data` |
|---|---|---|
| server → client | `frame` | `{ as_of, as_of_sequence, order_book: OrderBookDepth, last_trade: Trade \| null, trades: Trade[], candle: Candle (forming 5s candle) }` — first the book at `from` (rebuilt like Q-12), then one per command's group of events (same `causation_id`, so never half a match), paced by original time ÷ speed. `trades` (optional field, Phase 4) lists every fill of that command, oldest first, so the replay's tape equals what the live market published; `last_trade` is its last one |
| server → client | `end` | `{ frames_sent, from, to, as_of_sequence, state_hash }` — once market time reaches `to`; `state_hash` is the book at `to`, equal to Q-12's `state_hash` at `to` (Phase 4). Then the server closes normally (`1000`) |
| client → server | `pause` / `resume` | `{}` — market time stops and starts again |
| client → server | `speed` | `{ speed }`, one of 1, 2, 5, 10, 60 — takes effect at once from the current position (Phase 4); any other value is ignored |

Each market-data replica plays at most 4 replays at once and keeps one of those places for the admin role, so the
presenter is never refused. A replay paused for more than 5 minutes is closed (`1000`), and a disconnect ends the
replay at once; either way its place is free again. Close codes: `4404` unknown ticker, `4422` invalid parameters
(the reason says which), `1013` this replica is already playing its replays (try again later; the gateway passes it
on), `1011` market data is starting or the event store stopped answering.

### Q-19 — `/v1/ws/incidents` (sre-agent, any role; approve/decline buttons shown only to admins)

| Direction | `type` | `data` |
|---|---|---|
| server → client | `snapshot` | On connect: `IncidentSummary[]` of open incidents plus the last 20 resolved |
| server → client | `incident_opened` | `IncidentSummary` |
| server → client | `incident_update` | `{ incident_id, entry: TimelineEntry }` — every diagnosis, proposal, decision, action |
| server → client | `approval_required` | `{ incident_id, remediation: Remediation }` — only when approval mode is on |
| server → client | `incident_resolved` | `IncidentSummary` with `resolution` and `time_to_recover_seconds` |

```json
{"type": "incident_update", "data": {"incident_id": "INC-0007", "entry": {"at": "2026-11-20T11:06:01.398Z", "kind": "EXECUTED", "message": "Rolled back order-service to revision 7. 2/2 pods ready."}}}
```

## Feedback (SEED(S) step 6)

An API design is only "done" once its consumers have tried it. Our feedback loop:

1. **Review by consumers:** every spec change is a pull request reviewed by the *consuming* team (e.g. Market Insights
   reviews `matching-engine.yaml`; Trading reviews `portfolio-service.yaml` reservations).
2. **Try before building:** the web app and bots are developed against a **mock server** generated from these specs,
   so UI feedback arrives before backend code exists. `make mock` runs Prism 5.16.0 on the gateway spec, with each
   proxied operation's examples copied from the owning service's spec (`tools/mock_spec.py`), plus a small WebSocket
   mock (`web/mock/ws-mock.mjs`) for the channels below, which Prism cannot mock; `npm run dev:mock` points the web
   app at both.
3. **Record the outcome:** accepted changes update the spec, the actions/queries table and, if a decision changed, an
   ADR — in the same PR.
4. **Automate the contract:** once code exists, contract tests fail the build if responses drift from the spec.

Phase 0 feedback already applied: cancel became asynchronous (202) after the cancel/fill race was found in the
sequence diagrams; reservations gained idempotency by `order_id` and the orphan rule (ADR-0011).
Phase 2 feedback: the orphan rule became fail-closed, adding A-19 and the cancel reason `ORDER_EXPIRED` (ADR-0017), and
self-trade prevention added the cancel reason `SELF_TRADE_PREVENTED` (order-service spec 1.1.0, gateway spec 1.1.0).
Phase 4 feedback (proving time travel exact to an audience): Q-15 returns the snapshot's finished and expired orders
(`closed`, matching-engine spec 1.2.0); Q-12 returns the rebuilt book's `state_hash` and, at "now", a `verification`
against the live engine book (market-data spec 1.1.0); Q-13 gained the `speed` message, the `end` hash and every fill per
frame (`trades`).
CI feedback (2026-11-14): Q-12 at "now" is answered at the live book's sequence even when the engine traded after
market-data read its clock, instead of leaving `verification` null (market-data spec 1.1.1).
Pre-chaos hardening (2026-11-14): the engine remembers finished orders for one hour of event time (ADR-0017 note "A
memory of one hour"), so Q-15/Q-16 gain `forget_before`, each `closed` order gains `closed_at`, and the `state_hash`
rule covers both (matching-engine spec 1.3.0; market-data spec 1.1.2 describes its hash accordingly).
Phase 7 (chaos): order-service's concurrency limit, documented since Phase 0, was built: `503 OVERLOADED` with
`Retry-After: 1` beyond 50 requests per pod (order-service spec 1.2.1).
market-data's A-13 now gives back the leaked memory, so a chaos reset needs no restart (market-data spec 1.2.1).
portfolio-service gained the "database degraded" chaos hook A-21/A-22 (portfolio spec 1.1.0).

## Error code catalog

| `code` | HTTP | Meaning |
|---|---|---|
| `BAD_REQUEST` | 400 | Malformed request (bad JSON, wrong types) |
| `UNAUTHENTICATED` | 401 | Missing/expired token |
| `INVALID_PASSCODE` | 401 | Wrong admin passcode |
| `FORBIDDEN` | 403 | Role not allowed |
| `NOT_FOUND` | 404 | Resource not found or not yours |
| `NO_ACTIVE_FAULT` | 404 | No chaos fault to clear |
| `NICKNAME_TAKEN` | 409 | Nickname already used |
| `ORDER_NOT_CANCELLABLE` | 409 | Order already finished (a repeat while a cancel is pending answers `202` and re-sends it) |
| `EXPERIMENT_ALREADY_RUNNING` | 409 | Only one chaos experiment at a time |
| `EXPERIMENT_NOT_RUNNING` | 409 | Experiment already ended |
| `FAULT_ALREADY_ACTIVE` | 409 | A fault is already injected |
| `NO_PENDING_REMEDIATION` | 409 | Nothing to approve/decline |
| `VALIDATION_FAILED` | 422 | Field-level validation failed (see `errors`) |
| `UNKNOWN_TICKER` | 422 | Not one of the five tickers |
| `INVALID_TICK_SIZE` | 422 | Price not a multiple of 5 paise |
| `INSUFFICIENT_FUNDS` | 422 | Not enough available cash (no margin) |
| `INSUFFICIENT_SHARES` | 422 | Not enough available shares (no short selling) |
| `TIME_IN_FUTURE` | 422 | Time travel to a moment that hasn't happened |
| `TIME_BEFORE_HORIZON` | 422 | Time travel to a moment more than 24 hours ago (the events are still stored; fast seeking reaches back 24 hours) |
| `RATE_LIMITED` | 429 | Too many requests |
| `DEPENDENCY_UNAVAILABLE` | 503 | A dependency is down / circuit breaker open |
| `OVERLOADED` | 503 | Service at its concurrency limit (traffic spike) |
| `INTERNAL_ERROR` | 500 | Unexpected failure; no internals are leaked, quote the `correlation_id` |
| `NOT_IMPLEMENTED` | 501 | The operation is in the spec but not built yet (the Phase 1 stubs) |

## Changelog

Changes to shared reference data that do not change any API version. Spec versions (`info.version`) record
changes to operations and schemas.

<!-- retired-names:begin 2026-11-14 -->
| Date | Change | Why it stays v1 |
|---|---|---|
| 2026-11-14 | Ticker enum and examples renamed: `CHAIWALA` → `CALVERA`, `JUGAAD` → `KORVIN`, `DABBA` → `ASTRYX`, `MONSOON` → `HELIOR`, `FILMY` → `TAVIRA` (company names too; reference prices unchanged). Details and the reason: [overview §3](../docs/00-overview.md#3-the-instruments-stocks). | Reference data, changed before any release: no client outside this repository has ever used the old values. |
<!-- retired-names:end -->

## Viva Notes

**Q: How do you know every endpoint is needed?**
A: Each operation's description starts with an action or query ID, and each ID comes from a Job To Be Done. The trace
map above shows the full chain; there are no endpoints without an ID.

**Q: Why does the gateway spec reference schemas from the service specs?**
A: So each data shape is defined once, by the team that owns it. If order-service changes `Order`, the public spec
picks it up automatically and the linter checks both.

**Q: Why aren't WebSockets in the OpenAPI files?**
A: OpenAPI describes request/response HTTP; it has no good way to describe a long-lived stream of server-pushed
messages. We document each channel's message types and schemas here instead, reusing the OpenAPI schemas.

**Q: Why is the token in the WebSocket URL? Isn't that insecure?**
A: Browsers can't set an `Authorization` header on WebSocket connections. The trade-off is managed by short-lived
tokens, HTTPS in a real deployment, and redacting the parameter from gateway logs.

**Q: Why do idempotent retries matter?**
A: A phone on a hotspot, or a test runner on 2 CPUs, sometimes gets no answer or a `503` although the order may have
been stored. Retrying blindly could buy twice. With an `Idempotency-Key` the retry is safe: the same key within 24 hours
returns the first order instead of placing a second, and a half-finished first attempt is completed rather than
repeated (the reservation is idempotent per order too). So clients retry a transient failure with the same key; the
e2e test proves it by making the 503 happen and checking that exactly one order exists.
