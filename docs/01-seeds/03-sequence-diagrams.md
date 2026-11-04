# SEED(S) Step 3 — Sequence Diagrams

> **What is a sequence diagram?** A picture of *who talks to whom, in what order* to complete one job.
> Time flows downwards. Solid arrows (`->>`) are requests or messages; dashed arrows (`-->>`) are replies.
>
> **Why this step matters:** drawing the interactions is where hidden requirements appear. For example, the
> "place order" diagram revealed that cash must be **reserved** before the order reaches the matching engine,
> which created action A-06 and [ADR-0011](../adr/0011-synchronous-reservation-over-saga.md).

**Conventions used in every diagram**

- `web` is the React app on a phone or laptop. Every external call goes through `api-gateway`.
- `Redpanda` is the event bus. Topic names (`orders.v1`, `matching.v1`, …) and event names match the
  [event catalog](../03-architecture/03-event-catalog.md) exactly.
- **Sync** = HTTP, the caller waits. **Async** = an event on Redpanda, the producer does not wait.
- Action/query IDs (`A-xx`, `Q-xx`) refer to [04-actions-and-queries.md](04-actions-and-queries.md).

---

## 1. Join via QR code (J-01 → A-01, A-02)

```mermaid
sequenceDiagram
    autonumber
    actor P as Presenter screen
    actor T as Trader phone (web)
    participant GW as api-gateway
    participant PS as portfolio-service
    participant PG as Postgres (portfolio)

    P->>P: Show QR code encoding the join URL
    T->>T: Scan QR, open /join, type nickname
    T->>GW: POST /v1/sessions  nickname=RajuTrader  (A-01)
    GW->>PS: POST /internal/v1/accounts  nickname, kind=HUMAN  (A-02)
    PS->>PG: INSERT account with cash 1,00,00,000 paise (BEGIN..COMMIT)
    alt nickname already taken
        PG-->>PS: unique violation
        PS-->>GW: 409 NICKNAME_TAKEN
        GW-->>T: 409 problem+json, please choose another nickname
    else account created
        PS->>PS: record local event AccountOpened
        PS-->>GW: 201 trader_id, cash_paise
        GW->>GW: sign JWT  sub=trader_id, role=trader, expires in 12 h
        GW-->>T: 201 access_token, trader_id, nickname
        T->>GW: GET /v1/portfolio and open WebSockets for market and portfolio
    end
```

**Design notes**
- **Why does the gateway call portfolio-service synchronously?** The trader cannot do anything until the account
  exists, so waiting is the correct behaviour. It is one quick call.
- **Why no password?** Joining must take seconds on a phone during a demo. The JWT is stored in the browser;
  if it is lost, the trader simply joins again with a new nickname.
- **What does the phone do with a 409?** The API only says the name is taken. The web join page then tries the
  same name with a short random suffix (`RajuTrader_7k`), up to three times, and shows the suffix in a quieter colour.
  Only if all three are taken does it ask for another name. This is client behaviour; A-01 is unchanged
  ([web README](../../web/README.md#nicknames)).
- ₹1,00,000 = 1,00,00,000 paise (10,000,000).

---

## 2. Place an order (J-03, J-04 → A-05, A-06)

```mermaid
sequenceDiagram
    autonumber
    actor T as Trader (web)
    participant GW as api-gateway
    participant OS as order-service
    participant PS as portfolio-service
    participant OC as Cassandra (order_service keyspace)
    participant RP as Redpanda

    T->>GW: POST /v1/orders  BUY 50 CHAIWALA LIMIT 24600 paise  (A-05)
    GW->>GW: Verify JWT, apply rate limit, add X-Trader-Id and X-Correlation-Id
    GW->>OS: POST /v1/orders
    OS->>OS: Validate ticker, quantity > 0, price multiple of 5 paise
    OS->>PS: POST /internal/v1/reservations  BUY 50 x 24600 = 12,30,000 paise  (A-06)
    alt not enough cash (or not enough shares for a SELL)
        PS-->>OS: 422 INSUFFICIENT_FUNDS
        OS->>OC: Append local event OrderRejected
        OS-->>GW: 422 problem+json
        GW-->>T: 422 Not enough cash
    else reservation created
        PS-->>OS: 201 reservation_id, reserved_cash_paise
        OS->>OC: Append OrderPlaced, status PENDING, published=false
        OS->>RP: Publish OrderPlaced to orders.v1 (key = CHAIWALA)
        OS->>OC: Mark event published
        OS-->>GW: 202 Accepted  order_id, status PENDING
        GW-->>T: 202 Accepted
    end
    Note over OS,RP: If publishing fails, the outbox relay re-publishes later.<br/>The reservation is kept, so money is never double-spent.
```

**Design notes**
- **Why `202 Accepted` and not `201 Created`?** The order is accepted for processing but has not been matched
  yet. Matching happens asynchronously in the matching engine; the status will change to `OPEN`,
  `PARTIALLY_FILLED` or `FILLED` via events.
- **Why reserve synchronously?** "No short selling, no margin" must be enforced *before* the order can trade.
  See [ADR-0011](../adr/0011-synchronous-reservation-over-saga.md) for sync reservation vs. async saga.
- **MARKET orders:** portfolio-service computes a *protection price* (last price ± 10%) and, for buys, reserves
  `quantity × protection price`. Unused money is released when the order finishes.
- **If order-service crashes** between reserving and publishing, the relay never publishes an `OrderPlaced` older than
  60 s (the order becomes `REJECTED`, `PUBLISH_TIMEOUT`), and the reservation is released only after the engine
  confirms the order can never trade ([§7](#7-an-orphaned-reservation-expires-j-36--a-19)).

---

## 3. A trade executes (J-31, J-32 → A-08, A-09)

Continuing from diagram 2. A resting SELL of 20 CHAIWALA at ₹245.50 (by `Bot-03`) is in the book.

```mermaid
sequenceDiagram
    autonumber
    participant RP as Redpanda
    participant ME as matching-engine
    participant EC as Cassandra (matching_engine keyspace)
    participant OS as order-service
    participant PS as portfolio-service
    participant MD as market-data-service
    actor T as Trader (web)
    participant BT as bot-traders

    RP->>ME: OrderPlaced  BUY 50 CHAIWALA LIMIT 24600  (orders.v1)
    ME->>ME: Assign sequence numbers, match by price-time priority (A-08)
    Note over ME: Best ask 24550 is at or below 24600, so trade 20 at 24550<br/>(the resting order's price). 30 remain and rest as a bid.
    ME->>EC: Append batch: OrderAccepted (seq 1041), TradeExecuted (seq 1042)
    ME->>RP: Publish both to matching.v1 (key = CHAIWALA)
    par order status
        RP->>OS: TradeExecuted
        OS->>OS: Status PARTIALLY_FILLED, filled 20, remaining 30
    and settlement
        RP->>PS: TradeExecuted
        PS->>PS: Settle in one SQL transaction (A-09)<br/>buyer reserved cash to shares, seller reserved shares to cash
        PS-->>T: WebSocket /v1/ws/portfolio  fill message
    and market data
        RP->>MD: OrderAccepted, TradeExecuted
        MD->>MD: Update last price, 5s/1m/5m candles, depth, trades
        MD-->>T: WebSocket /v1/ws/market/CHAIWALA  trade, candle, book
    end
    Note over MD,BT: When a candle interval ends
    MD->>RP: Publish CandleClosed to candles.v1
    RP->>BT: CandleClosed, bots update strategies
```

**Design notes**
- **Event store first, then publish.** The engine appends events to Cassandra *before* publishing them. If it
  crashes in between, it re-publishes from the store on restart. Consumers are **idempotent** (they ignore an
  `event_id` they have already processed), so a duplicate does no harm.
- **Price improvement:** the buyer was willing to pay ₹246.00 but paid ₹245.50. The extra reserved
  ₹0.50 × 20 = ₹10 is released when the order finishes (filled or cancelled).
- Three services react to the **same** event independently — this is the main benefit of publish/subscribe.
- **Self-trade prevention:** if the next resting order the incoming order would match belongs to the same trader, the
  engine cancels the incoming order's remainder (`OrderCancelled`, reason `SELF_TRADE_PREVENTED`) instead of letting a
  trader trade with themselves (a *wash trade*). Trades it made before that point stand.

---

## 4. Cancel an order (J-06 → A-07)

```mermaid
sequenceDiagram
    autonumber
    actor T as Trader (web)
    participant GW as api-gateway
    participant OS as order-service
    participant RP as Redpanda
    participant ME as matching-engine
    participant PS as portfolio-service

    T->>GW: DELETE /v1/orders/7f3c...  (A-07)
    GW->>OS: DELETE /v1/orders/7f3c...  X-Trader-Id
    OS->>OS: Check owner, status must be PENDING, OPEN or PARTIALLY_FILLED
    alt not cancellable (FILLED, CANCELLED, REJECTED or not owner)
        OS-->>GW: 409 ORDER_NOT_CANCELLABLE (or 404 if not the owner)
        GW-->>T: 409
    else cancellable
        OS->>OS: Append OrderCancelRequested, cancel_requested = true
        OS->>RP: Publish OrderCancelRequested to orders.v1 (key = ticker)
        OS-->>GW: 202 Accepted
        GW-->>T: 202 Cancel requested
        RP->>ME: OrderCancelRequested
        alt order still has quantity in the book
            ME->>ME: Remove remaining 30 from book, append OrderCancelled
            ME->>RP: OrderCancelled  remaining_quantity 30, reason USER_REQUESTED
            RP->>OS: Status CANCELLED
            RP->>PS: Release remaining reservation, push portfolio update
        else already fully filled a moment ago (race)
            ME->>RP: OrderCancelRejected  reason ALREADY_FILLED
            RP->>OS: Clear cancel_requested, status stays FILLED
        end
    end
```

**Design notes**
- **Why is cancel asynchronous?** Only the matching engine knows whether the order is *still* in the book.
  Another trader's order could fill it a millisecond before the cancel arrives. Because `orders.v1` is keyed by
  ticker, the cancel is processed strictly after the original `OrderPlaced`, so the engine always sees them in order.
- A cancel can therefore *fail* even though the API returned `202`. The UI shows "cancel requested" until the
  `OrderCancelled` or `OrderCancelRejected` event arrives.

---

## 5. Time travel and replay (J-20 → Q-12, Q-13, Q-14, Q-15)

```mermaid
sequenceDiagram
    autonumber
    actor A as Presenter or Trader (web)
    participant GW as api-gateway
    participant MD as market-data-service
    participant ME as matching-engine
    participant EC as Cassandra (matching_engine keyspace)
    participant MC as Cassandra (market_data keyspace)

    A->>GW: GET /v1/time-travel/CHAIWALA?at=2026-11-20T10:42:15Z  (Q-12)
    GW->>MD: forward
    MD->>ME: GET /internal/v1/snapshots/CHAIWALA?at=10:42:15  (Q-15)
    ME->>EC: Latest snapshot taken at or before 10:42:15
    ME-->>MD: Snapshot at sequence 4000 (book as of 10:41:50)
    MD->>ME: GET /internal/v1/events?ticker=CHAIWALA&after_sequence=4000&until=10:42:15  (Q-14)
    ME->>EC: Range read by sequence
    ME-->>MD: Events 4001 to 4187 (paged)
    MD->>MD: Apply events to a fresh in-memory book (no matching, just apply results)
    MD->>MC: Read candles and trades up to 10:42:15
    MD-->>GW: 200 book, candles, last trades, as_of_sequence 4187
    GW-->>A: 200

    Note over A,MD: Replay stream (Q-13)
    A->>GW: WebSocket /v1/ws/replay/CHAIWALA?from=10:40&to=10:45&speed=10
    GW->>MD: upgrade and proxy
    MD->>ME: Same snapshot + events load for the window
    loop for each group of events, paced by original time divided by speed
        MD-->>A: frame  as_of, book, last trade, candle
    end
    MD-->>A: end
```

**Design notes**
- **Replay applies *events*, not *commands*.** Re-running matching could give different results if the matching
  code has changed since. Applying the stored results (`OrderAccepted`, `TradeExecuted`, `OrderCancelled`) is
  deterministic and needs no matching logic in market-data-service.
- **Data ownership is respected:** market-data-service never reads the `matching_engine` keyspace directly; it
  asks matching-engine over HTTP.
- **Snapshots** (every 500 events per ticker) mean we replay at most 500 events, not the whole day.
- Time travel is a **query**: it never changes live state. That is CQRS in action.

---

## 6. Chaos injected → SRE agent remediates (J-15, J-21–J-25 → A-10, A-15, A-16, A-17)

Scenario shown: **bad deploy** of `order-service`. The other two scenarios follow the same shape — see
[06-chaos-scenarios.md](../03-architecture/06-chaos-scenarios.md).

```mermaid
sequenceDiagram
    autonumber
    actor AD as Admin (web)
    participant GW as api-gateway
    participant CH as chaos-service
    participant K8 as Kubernetes API
    participant OS as order-service
    participant PR as Prometheus
    participant LK as Loki
    participant SA as sre-agent
    participant CL as OpenRouter (LLM)
    participant AR as Argo CD
    participant GH as GitHub
    participant RP as Redpanda

    AD->>GW: POST /v1/chaos/experiments  scenario BAD_DEPLOY  (A-10)
    GW->>CH: forward (admin role checked)
    CH->>K8: Patch Deployment order-service: new release 1.4.1 with FAULT_PROFILE=bad-release
    K8->>OS: Roll out revision 8
    CH-->>AD: 201 experiment RUNNING
    Note over OS: About 40 percent of POST /v1/orders now fail with 500
    PR->>OS: Scrape /metrics every 5s
    PR->>PR: Alert OrderServiceHighErrorRate firing (5xx above 5 percent for 1 min)
    SA->>PR: Poll firing alerts (every 15s)
    SA->>SA: Open incident INC-0007 (A-16)
    SA->>RP: IncidentOpened to incidents.v1
    SA->>PR: Evidence: error rate, p95 latency, request rate, memory
    SA->>LK: Evidence: last 50 error log lines for order-service
    SA->>K8: Evidence: rollout history and pod status
    SA->>CL: Evidence + 4 allowlisted tools (exactly one call expected)
    CL-->>SA: tool call rollback_deployment service=order-service, explanation
    SA->>SA: Guardrails: tool and service allowlisted, cooldown ok, action budget ok
    alt approval toggle ON
        SA->>RP: RemediationProposed
        SA-->>AD: Incident feed (WebSocket) shows proposal and explanation
        AD->>GW: POST /v1/incidents/INC-0007/decision  APPROVE  (A-15)
        GW->>SA: forward
        SA->>RP: RemediationApproved
    end
    Note over SA,GH: rollback_deployment = two deterministic steps in code (ADR-0016), not two tools
    SA->>AR: Pre-check: Application order-service sync status (refresh)
    AR-->>SA: OutOfSync (live drift: chaos patched the Deployment)
    SA->>K8: (a) Mitigate: roll back order-service to revision 7, like kubectl rollout undo (A-17)
    alt Application was Synced before (bad release came through Git)
        SA->>GH: (b) Reconcile: push sre-agent/revert-INC-0007 and open git revert PR
        GH-->>SA: PR URL (a human must approve and merge it)
        Note over GH,AR: After merge Argo CD syncs and Application is Synced, no new rollout
    else Application was OutOfSync (this chaos scenario)
        Note over SA: Git already matches the cluster after (a), so no PR (NOT_NEEDED)
    end
    SA->>RP: RemediationExecuted with git_reconciliation status
    SA-->>AD: Incident feed: rolled back, Git status, here is why
    PR->>PR: Error rate normal, alert resolves
    SA->>SA: Verify recovery
    SA->>RP: IncidentResolved  time_to_recover 94s
    RP->>CH: IncidentResolved, mark experiment REMEDIATED
```

**Design notes**
- **The agent never talks to chaos-service.** It diagnoses only from metrics, logs and deploy history.
- **Evidence is collected by plain code, not by the LLM.** The LLM only chooses *one* of four action tools. This
  keeps the agent's powers exactly equal to the allowlist ([ADR-0009](../adr/0009-sre-agent-fixed-tool-allowlist.md)).
- **Guardrails run after the LLM answers** and can override it: an unsafe request becomes `do_nothing` plus an
  escalation message to the admin.
- **Mitigate fast, reconcile through Git.** The rollback first fixes the cluster in seconds. If the bad version came
  from Git, the same tool's code then opens a `git revert` pull request so Git matches the cluster again
  ([ADR-0016](../adr/0016-rollback-reconciles-through-git.md)). In this chaos scenario Git never contained the bad
  version, so the `else` branch applies.

---

## 7. An orphaned reservation expires (J-36 → A-19)

Fail-closed release ([ADR-0017](../adr/0017-fail-closed-orphan-release.md)). Shown: order-service crashed after
reserving cash for order `6c7d...` and before storing it, so no `OrderPlaced` was ever sent.

```mermaid
sequenceDiagram
    autonumber
    participant PS as portfolio-service
    participant PG as Postgres (portfolio)
    participant OS as order-service
    participant OC as Cassandra (order_service keyspace)
    participant RP as Redpanda
    participant ME as matching-engine
    participant EC as Cassandra (matching_engine keyspace)

    loop every 10 s (sweeper, policy P-15)
        PS->>PG: ACTIVE reservations with no OrderAccepted for 120 s,<br/>on partitions this replica owns with no lag
    end
    PS->>OS: POST /internal/v1/order-expiries  order_id, trader_id, ticker  (A-19)
    alt order-service is down
        OS--xPS: timeout, so try again on the next cycle
    else
        OS->>OC: Append OrderExpiryRequested to the outbox (event_id = UUIDv5 of the order_id)
        OS-->>PS: 202 Accepted
        PS->>PG: set expiry_requested_at (money still reserved)
        OS->>RP: OrderExpiryRequested to orders.v1 (key = CHAIWALA)
    end
    RP->>ME: OrderExpiryRequested, after any OrderPlaced already in that partition (policy P-14)
    alt engine never saw the order
        ME->>EC: Append OrderExpired (a tombstone), plus the command ID
        ME->>RP: OrderExpired to matching.v1
    else order is resting in the book
        ME->>EC: Append OrderCancelled, reason ORDER_EXPIRED
        ME->>RP: OrderCancelled to matching.v1
    else order already finished
        Note over ME: nothing to do: earlier events already settled or released the money
    end
    RP->>PS: the answer (after the order's earlier events, same partition)
    PS->>PG: release the reservation in one transaction with processed_events
    Note over RP,ME: Later, a delayed copy of the OrderPlaced arrives
    RP->>ME: OrderPlaced 6c7d...
    ME->>ME: Tombstone found, so refuse it: no OrderAccepted, no trades
```

**Design notes**
- **Why not just release after 120 s?** A timer cannot know what the engine decided. If the engine was down, or
  crashed after storing a trade but before publishing it, the money could be traded after it was handed back.
- **Fail-closed:** if the engine is down, the money stays reserved until it answers. If order-service is down, the
  sweeper asks again on its next cycle.
- **Why through order-service?** The expiry must be in the same `orders.v1` partition as the order so it is handled
  after any `OrderPlaced` already sent, and `orders.v1` has one producer, order-service, with an outbox.

## Viva Notes

**Q: What did sequence diagrams teach you that JTBDs didn't?**
A: Hidden steps and their ordering — e.g. reservations before matching, the race between cancel and fill, and
that the agent needs deploy history. Each became an action, an event or an ADR.

**Q: Why does placing an order return 202 instead of 201?**
A: The order is accepted for processing but not yet matched; matching is asynchronous. 202 is honest: "I have it,
the result comes later". The client watches status via events (WebSocket) or `GET /v1/orders/{order_id}`.

**Q: How do you avoid processing a cancel before the order it cancels?**
A: Both messages go to `orders.v1` with the ticker as key, so they land in the same partition, which Redpanda
delivers in order.

**Q: What happens if the matching engine crashes after saving events but before publishing?**
A: On restart it reads events from its Cassandra store that were not yet published and publishes them. Some
might be published twice; consumers deduplicate by `event_id`, so it is safe (at-least-once + idempotency).

**Q: Why doesn't replay re-run the matching algorithm?**
A: Because events are facts that already happened. Re-running decisions with today's code could produce a
different past. Applying stored facts always reproduces exactly what happened.
