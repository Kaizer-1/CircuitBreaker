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
reconnects. That query reads one row per order in the trader's history, and polling it from 20 bots slowed order
placement for everyone (p95 above 1 s; now about 30 ms).

**Backing off after a failure.** A step that fails (a timeout, a 5xx) pauses that bot for 1 s, then 2, 4 … up to
30 s; a step that works resets the pause. Found in Phase 4 after two hours of trading: a gateway restart made every bot
list its open orders at once, each listing read up to 1,000 rows, the bots timed out and retried every candle, and the
abandoned listings piled up in order-service until it ran out of memory (192 MiB) and restarted, again and again. With
the back-off, the same restart costs about 45 seconds of failed steps and order-service's memory stays flat. On top of
it, the fleet lets at most two bots list their open orders at once, and that one heavy call gets a 30-second timeout
instead of the 5 seconds every other call gets, so the listings after a restart finish instead of timing out.

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

**The strategy interface is the extension point.** A strategy is any object with a `name` and
`decide(view, rng) -> list[Intent]`. The Phase 7 traffic-spike scenario's bot burst is to be a **panic sell** (bots dump
shares at market). That is one more strategy class and a different assignment in `roster()`
([roadmap, Phase 7](../../docs/07-roadmap.md)).

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

At the default activity, the simulated hour in the tests gives about 8 orders/s, 2.7 trades/s, spreads of 10–12 basis
points and prices within a few percent of their reference (the test fails outside ±10%; `scripts/bots_check.py` checks
the same band live).

**Phase 4 rebalance (prices ran away).** MONSOON (₹88.20, where one tick is 5.7 basis points) climbed 13–15% above its
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

No business API — only `/healthz`, `/readyz`, `/metrics`. Uses public A-03, A-05, A-07, Q-02, Q-04, Q-07, Q-09 and the Q-10 WebSocket through `api-gateway`.

Every service also exposes `/healthz`, `/readyz`, `/metrics` ([coding standards §7](../../docs/05-coding-standards.md#7-standard-operational-endpoints)).

**Business metrics:** `bot_orders_total{strategy,result}` (`placed`, `cancelled`, `skipped`, `refused`), `bots_active`.

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
