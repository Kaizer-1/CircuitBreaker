# bot-traders

> **Status:** Phase 3 — the 20 bots and their four strategies are built. They are **off by default**: `make bots`
> starts them, `make bots-stop` stops them. Layout:
> [coding standards §3](../../docs/05-coding-standards.md#3-folder-layout-inside-every-service) (package `bot_traders`).

| | |
|---|---|
| **Owning team** | Trading Team |
| **Owner** | **Kaizer Dewaswala** |
| **Team type** | Stream-aligned ([team topology](../../docs/04-team-topology.md)) |
| **Bounded context** | Automated Trading (supporting) |

## Responsibility

Runs 20 bots in one process (asyncio): 4 market makers (always quote bids and asks — the initial liquidity), 6 momentum, 5 mean-reversion and 5 noise traders. Bots log in with `POST /v1/bot-sessions` and trade through the public API exactly like humans, reacting to `CandleClosed` events.

## How it works

| Code | Role |
|---|---|
| `domain/strategies.py` | The four strategies as pure decisions, and `roster()`: which bot runs which strategy on which tickers |
| `domain/ledger.py` | What a bot may still spend, so it never sends an order the exchange would refuse |
| `domain/market.py` | Tick size, quantity limits, MARKET protection (±10%), intents (`Place`, `Cancel`) |
| `application/bot.py` | One bot: refresh (Q-09), follow its Q-10 stream for fills, decide, send its orders spread over the next interval |
| `application/fleet.py` | All 20: sign-in (A-03), their Q-10 streams, the shared view of each ticker, routing each `CandleClosed` |
| `adapters/gateway.py` | The public API on api-gateway (A-03, A-05, A-07, Q-02, Q-04, Q-07, Q-09, the Q-10 WebSocket) |

| Bots | Strategy | Tickers | What it does |
|---|---|---|---|
| `bot-01`…`bot-04` | Market maker | all 5 | Always one bid and one ask per ticker, 8 / 12 / 16 / 20 basis points from a fair price (the last close, leaning 5% towards the reference price every candle, and away from inventory it holds too much of by at most 15 basis points). Re-quotes when the price moves or a quote is 6 candles old |
| `bot-05`…`bot-10` | Momentum | 3 each | After a move of 8+ bp and at least 3 ticks over 6 candles, a small MARKET order the same way |
| `bot-11`…`bot-15` | Mean reversion | 3 each | When the price is 20+ bp away from its moving average (pulled 20% towards the reference), a LIMIT order against the move at the touch |
| `bot-16`…`bot-20` | Noise | 2 each | Small random orders (half MARKET, half LIMIT within 0.3%) |

**Never an invalid order.** Before every order the bot checks it against its **ledger**: tick size, 1–10,000 shares, and
whether it has the cash (no margin) or the shares (no short selling). The ledger starts from the last portfolio read
(Q-09) and sets aside every order placed since, until a later read includes it. It never adds money back on its own, so
it can only under-estimate what is available. A MARKET buy is costed at portfolio-service's reservation (last price + 10%,
rounded up to the tick) plus 2%. An order the ledger holds back counts as `skipped`. One the exchange refuses counts as
`refused`, which must stay 0.

**Knowing which orders still rest.** Each bot follows its own portfolio stream (Q-10): `fill` and `order_closed`
say at once when an order traded or finished. It lists its open orders (Q-07) only at start-up and after the stream
reconnects. Until 2026-11-14 that query read one row per order in the trader's history, and polling it from 20 bots
slowed order placement for everyone (p95 above 1 s; about 30 ms after the change). Since order-service's open-orders
table it reads only what is open, but following the stream is still the right design: the stream says at once.

**Backing off after a failure.** A step that fails (a timeout, a 5xx) pauses that bot for 1 s, then 2, 4 … up to
30 s; a step that works resets the pause. Found in Phase 4 after two hours of trading: a gateway restart made every bot
list its open orders at once, each listing read up to 1,000 rows, the bots timed out and retried every candle, and the
abandoned listings piled up in order-service until it ran out of memory (192 MiB) and restarted, again and again. With
the back-off, the same restart costs about 45 seconds of failed steps and order-service's memory stays flat. Until
2026-11-14 the fleet also let at most two bots list at once, with a 30-second timeout for that one heavy call. The
open-orders table made the listing cheap (it reads only what is open), so both were removed. Measured on kind
(2026-11-15), both gateway pods deleted at once while the bots traded: the gateway was back in 4 s, all 20 portfolio
streams reconnected and all 20 bots listed their open orders together (p95 0.46 s), 14 steps failed, all within 7
seconds, order-service's memory stayed at 115 MiB and the bots' order rate never dropped.

**Sessions run out.** A bot session lasts 12 hours (A-03). When a call is refused (401) or the portfolio stream closes
with `4401`, the bot drops its session and signs in again on its next step, then lists its open orders (it cannot know
what finished meanwhile). Found in Phase 4: a stack left running for two days had every bot refused for ever.

**Quoting both sides, always.** A market maker replaces a quote with one `Requote`: the new quote is placed first and
the old one cancelled right after, at the same moment, so its side is never empty in between (before Phase 4 the
cancel and the new quote went out at different moments of the interval: on average 3.4 of the 4 makers' quotes were
on each side; now 3.8 to 4.0). A placement whose answer is lost (a timeout, a dropped connection) is asked again with
the same `Idempotency-Key`, so order-service returns the order that may already exist and the bot keeps track of it
instead of leaving a quote it never cancels. `scripts/bots_check.py` fails if a side holds fewer than 3.5 or more than
4.5 maker quotes on average, or the two sides differ by more than one level.

**Timing (an implementation detail).** Bots act on `CandleClosed` (policy P-09), on the 5s candles only. If every bot
sent its orders the moment a candle closed, trades would come in a burst every 5 s. So each bot spreads that candle's
orders over the following interval at random offsets (5% to 95% of it). The offsets come from the bot's own random
generator, seeded by `BOT_RANDOM_SEED`, so a seeded rehearsal places the same orders. A test checks that order times are
spread evenly through the interval and not bunched at the candle boundary.

**A market maker re-quotes a filled side at once** (2026-11-14). Until then it waited for the next candle after a fill,
and its side stayed without a quote for **3.3 s on average, up to about 8 s** (measured on the CI runner with
`bot_quote_gap_seconds`). Missing quotes are fills × gap, so the busiest ticker had the fewest: one CI run averaged 3.4 of
4 maker bids on a ticker trading 44.5 times a minute and failed the bots check (floor 3.5). It was not the runner's CPU:
on 2 CPUs with a load average of 3 to 6 the bots sent their orders 0.02 to 0.05 s late on average
(`bot_order_lateness_seconds`). Now a fill that empties a maker's side asks the fleet for a quick step on that ticker,
its orders spread over 1 second instead of 5; a fill during a step on that ticker re-quotes when the step ends. The
other strategies keep their candle rhythm.

**The strategy interface is the extension point.** A strategy is any object with a `name` and
`decide(view, rng) -> list[Intent]`.

**The panic sell (Phase 7, chaos `TRAFFIC_SPIKE`; `application/panic.py`, [ADR-0020](../../docs/adr/0020-chaos-triggers-through-each-targets-own-hook.md)).**
chaos-service calls the internal hook A-23 `POST /internal/v1/faults` `{"fault": "PANIC_SELL", "requests_per_second":
110, "duration_seconds": 300}`; A-24 `DELETE` ends it early. The bots' own accounts then send the calibrated mix through
api-gateway: request *n* at start + *n* / rate, from bot-01 to bot-20 in turn, 9 MARKET sells of 1 share (random ticker)
for every `GET /v1/orders` ([chaos scenarios §3.3](../../docs/03-architecture/06-chaos-scenarios.md#33--traffic-spike-traffic_spike)).
It was first planned as one more strategy, but a strategy decides once per candle and waits for its answers (a
**closed loop**): under overload it would slow down with order-service and hide the overload. A crowd does not wait,
so the panic is an **open loop** on its own connections (up to 2,000 unanswered, 10 s timeout, no retries); beyond
2,000 unanswered a request is counted as `dropped` instead of sent. The normal strategies keep trading: the market
makers' bids are what the panic sells into, so the price falls on the dashboard. Each bot sends about 5.5 requests/s,
far under the gateway's 50/s per bot token. The credentials never leave this service. Only with
`CHAOS_HOOKS_ENABLED=true` (otherwise 404); only chaos-service may call it (network policy); the gateway never forwards
`/internal/`. `bot_panic_requests_total{kind,result}` counts what was offered and how order-service answered.

Other rules: a repeated `CandleClosed` (same deterministic `event_id`) or a stale one (closed more than two intervals ago:
the consumer is catching up) is ignored. Market makers quote once at start-up, so the first buyer finds a seller. The
fleet reads each ticker's best bid and ask (Q-04) once per candle for all bots.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `BOTS_ENABLED` | `false` | Off: the service only answers `/healthz`, `/readyz`, `/metrics`. `make bots` / `make bots-stop` recreate the container with it on or off |
| `BOT_RANDOM_SEED` | none | A number makes every bot's choices repeat run after run (`make bots seed=42`). The market still depends on timing and on what humans do |
| `BOT_ACTIVITY` | `1.0` | Scales how often and how much every bot trades (`make bots activity=1.5`) |
| `GATEWAY_URL`, `BOT_SECRET` | | The public API and the A-03 bot secret (Compose: `http://api-gateway:8000`, the dev placeholder) |
| `CHAOS_HOOKS_ENABLED` | `false` | The panic-sell hook A-23/A-24 (staging and the demo prod only) |

At the default activity, the simulated hour in the tests gives about 8 orders/s, 2.7 trades/s, spreads of 10–12 basis
points and prices within a few percent of their reference (the test fails outside ±10%; `scripts/bots_check.py` checks
the same band live).

**Phase 4 rebalance (prices ran away).** HELIOR (₹88.20, where one tick is 5.7 basis points) climbed 13–15% above its
reference within a simulated hour in 3 of 5 runs. A market maker that had sold shares bid up to 30 basis points above
its fair price to buy them back, the last price followed, and momentum took a two-tick wiggle for a trend. Three changes,
none of them a price limit: the makers' fair price leans 5% (was 2%) towards the reference every candle, their inventory
lean is at most 15 basis points (was 30), and momentum needs a move of at least 3 ticks. Over 12 simulated hours the
worst excursion is now 5.3%, most stay within 2.6%. Through the gateway that is about 23 requests/s, well under
the 50/s per bot token.

## Owned data

None persistent; strategy state in memory, rebuilt on restart from the public API (portfolio, open orders).

## Events

| | |
|---|---|
| **Produces** | — (places/cancels orders via the public API). |
| **Consumes** | `CandleClosed` ← `candles.v1` (group `bot-traders.candles.v1`). |

Schemas: [event catalog](../../docs/03-architecture/03-event-catalog.md).

## API

No business API: the bots use public A-03, A-05, A-07, Q-02, Q-04, Q-07, Q-09 and the Q-10 WebSocket through
`api-gateway`. Its only operations are the internal chaos hook A-23/A-24 (above; spec
[`api/bot-traders.yaml`](../../api/bot-traders.yaml)).

Every service also exposes `/healthz`, `/readyz`, `/metrics` ([coding standards §7](../../docs/05-coding-standards.md#7-standard-operational-endpoints)).

**Business metrics:** `bot_orders_total{strategy,result}` (`placed`, `cancelled`, `skipped`, `refused`), `bots_active`,
`bot_panic_requests_total{kind,result}` (the panic sell: HTTP status, `no_answer` or `dropped`).
**The bots' own timing** (`scripts/bots_check.py` prints both): `bot_quote_gap_seconds{strategy}`, how long a side
had no resting order of a bot after the exchange filled its last one, until it placed a new one (for a market
maker: time without a quote); `bot_order_lateness_seconds{strategy}`, how much later than planned an order went out
(a starved event loop, or slow answers to the orders sent before it in the same interval).

## Synchronous dependencies

`api-gateway` (public API).

## Scaling and operations

**Exactly 1 replica** (a second copy would run every bot twice).

## Run and test it alone

```bash
cd services/bot-traders
cp .env.example .env                 # local settings (git-ignored)
uv sync                              # create the virtual environment from uv.lock
uv run pytest                        # unit tests, incl. a simulated hour of all 20 bots (~25 s)
uv run ruff check . && uv run ruff format --check . && uv run mypy

make up && make bots seed=42         # from the repo root: the stack, then the bots
curl -s localhost:8005/metrics | grep bot_orders_total
make bots-stop
```

**Ready means:**

- Redpanda, and the topic(s) `candles.v1` must exist (the init job creates them)
- with bots on: `bots`: all 20 have signed in and the market makers have quoted

## Viva Notes

**Q: Why do bots use the public API?**
A: Fairness (same rules and latency as humans), one secured entry path, and their traffic continuously exercises the real code.

**Q: Why are bots needed at all?**
A: Humans start with cash only and short selling is banned, so market-maker bots seeded with shares provide the first sellers and keep prices moving.

**Q: Why one service for 20 bots?**
A: They are the same code with different parameters; 20 deployments would add cost with no independence benefit.

**Q: How do you know a bot never sends an invalid order?**
A: Its ledger can only under-estimate what it may spend, and a test runs all 20 bots for a simulated hour against an
exchange with the real rules: not one order is refused.

**Q: Why spread orders over the interval?**
A: Acting all at once on each candle would make trades arrive in bursts every 5 s. Random offsets from a seeded
generator make the flow continuous and still repeatable.
