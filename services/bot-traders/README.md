# bot-traders

> **Status:** Phase 0 — design only, no code yet. This folder will follow the layout in
> [coding standards §3](../../docs/05-coding-standards.md#3-folder-layout-inside-every-service)
> (Python package `bot_traders`).

| | |
|---|---|
| **Owning team** | Trading Team |
| **Owner** | **Kaizer Dewaswala** |
| **Team type** | Stream-aligned ([team topology](../../docs/04-team-topology.md)) |
| **Bounded context** | Automated Trading (supporting) |

## Responsibility

Runs 20 bots in one process (asyncio): 4 market makers (always quote bids and asks — the initial liquidity), 6 momentum, 5 mean-reversion and 5 noise traders. Bots log in with `POST /v1/bot-sessions` and trade through the public API exactly like humans, reacting to `CandleClosed` events.

## Owned data

None persistent; strategy state in memory, rebuilt on restart from the public API (portfolio, open orders).

## Events

| | |
|---|---|
| **Produces** | — (places/cancels orders via the public API). |
| **Consumes** | `CandleClosed` ← `candles.v1`. |

Schemas: [event catalog](../../docs/03-architecture/03-event-catalog.md).

## API

No business API — only `/healthz`, `/readyz`, `/metrics`. Uses public A-03, A-05, A-07, Q-04, Q-07, Q-09 through `api-gateway`.

Every service also exposes `/healthz`, `/readyz`, `/metrics` ([coding standards §7](../../docs/05-coding-standards.md#7-standard-operational-endpoints)).

## Synchronous dependencies

`api-gateway` (public API).

## Scaling and operations

**Exactly 1 replica** (a second copy would run every bot twice).

## Viva Notes

**Q: Why do bots use the public API?**
A: Fairness (same rules and latency as humans), one secured entry path, and their traffic continuously exercises the real code.

**Q: Why are bots needed at all?**
A: Humans start with cash only and short selling is banned, so market-maker bots seeded with shares provide the first sellers and keep prices moving.

**Q: Why one service for 20 bots?**
A: They are the same code with different parameters; 20 deployments would add cost with no independence benefit.
