# SEED(S) Step 2 — Jobs To Be Done

> **What is a Job To Be Done (JTBD)?** A JTBD describes what an actor is trying to *achieve*, not which button
> they press. We write each one as:
>
> **When** _situation_, **I want to** _motivation_, **so I can** _expected outcome_.
>
> Writing the *situation* and the *outcome* stops us building features nobody needs. Every JTBD below has an ID
> (`J-xx`) and lists the actions (`A-xx`) and queries (`Q-xx`) derived from it in
> [04-actions-and-queries.md](04-actions-and-queries.md).

## Trader (human)

| ID | Job to be done | Derived |
|---|---|---|
| J-01 | **When** I see the QR code on the projector, **I want to** join with just a nickname, **so I can** start trading in seconds without creating an account. | A-01, A-02, Q-01 |
| J-02 | **When** I am deciding what to buy, **I want to** see each stock's live price, change and chart, **so I can** spot a good opportunity. | Q-02, Q-03, Q-05, Q-06 |
| J-03 | **When** I want a specific price, **I want to** place a limit order, **so I can** trade only at that price or better. | A-05, A-06 |
| J-04 | **When** I want to trade right now, **I want to** place a market order, **so I can** get filled immediately without picking a price. | A-05, A-06 |
| J-05 | **When** my order is waiting in the book, **I want to** see its status (open, partially filled, filled), **so I can** know what I own. | Q-07, Q-08, Q-10 |
| J-06 | **When** I change my mind about an open order, **I want to** cancel it, **so I can** get my reserved cash or shares back. | A-07 |
| J-07 | **When** I want to know how I'm doing, **I want to** see my cash, holdings and profit/loss, **so I can** decide my next trade. | Q-09, Q-10 |
| J-08 | **When** I want bragging rights, **I want to** see the leaderboard, **so I can** compare myself with others. | Q-11 |
| J-09 | **When** I want to understand how a price moved, **I want to** see the order book depth, **so I can** judge supply and demand. | Q-04, Q-06 |

## Bot Trader (software)

| ID | Job to be done | Derived |
|---|---|---|
| J-10 | **When** the bot service starts, **I want to** obtain a session for each of my 20 bots, **so I can** trade through the public API like anyone else. | A-03 |
| J-11 | **When** a new candle closes, **I want to** receive it as an event, **so I can** update my strategy (momentum, mean reversion) without polling. | (event `CandleClosed`) |
| J-12 | **When** my strategy decides to trade, **I want to** place and cancel orders, **so I can** provide liquidity and keep prices moving. | A-05, A-07 |
| J-13 | **When** I am about to quote, **I want to** know my cash and holdings and the current book, **so I can** avoid orders that would be rejected. | Q-04, Q-09 |

## Admin / Presenter

| ID | Job to be done | Derived |
|---|---|---|
| J-14 | **When** the demo starts, **I want to** log in as admin, **so I can** access the chaos panel and agent controls. | A-04, Q-01 |
| J-15 | **When** I want to show resilience, **I want to** start a chosen chaos scenario, **so I can** show the audience a realistic failure. | A-10, Q-21 |
| J-16 | **When** an experiment has gone on long enough (or goes wrong), **I want to** stop it, **so I can** return the system to normal manually. | A-11, Q-22, Q-23 |
| J-17 | **When** I want humans in control, **I want to** switch on the approval toggle (or pause the agent), **so I can** review every fix before it runs. | A-14, Q-20 |
| J-18 | **When** the agent proposes a fix, **I want to** approve or decline it, **so I can** keep a human in the loop. | A-15, Q-18 |
| J-19 | **When** I am presenting, **I want to** show the live incident feed, **so I can** let the audience read the agent's explanations. | Q-17, Q-18, Q-19 |
| J-20 | **When** I want to explain event sourcing, **I want to** jump the market back to a past moment or replay it at high speed, **so I can** show the audience exactly what happened. | Q-12, Q-13 |

> J-20 is also useful to Traders ("what happened at 10:42?"), so time travel is available to any logged-in user.

## SRE Agent

| ID | Job to be done | Derived |
|---|---|---|
| J-21 | **When** an alert fires, **I want to** open an incident and gather metrics, logs and deploy history, **so I can** diagnose the cause from evidence. | A-16 |
| J-22 | **When** I have a diagnosis, **I want to** apply exactly one allowlisted remediation (rollback, scale, restart or do nothing), **so I can** restore service with the smallest safe change. | A-17 |
| J-23 | **When** approval mode is on, **I want to** propose my action and wait for a human decision, **so I can** act only with consent. | A-15 (consumer side), A-17 |
| J-24 | **When** I act, **I want to** explain my reasoning in plain English on the incident feed, **so I can** let humans trust and audit me. | Q-19 (producer side) |
| J-25 | **When** the alert has cleared after my action, **I want to** resolve the incident and record the recovery time, **so I can** let the team measure MTTR. | (event `IncidentResolved`) |

## Developer

| ID | Job to be done | Derived |
|---|---|---|
| J-26 | **When** I merge a change, **I want to** have it built, tested and deployed to staging automatically, **so I can** find problems before prod. | A-18 |
| J-27 | **When** staging looks good, **I want to** promote the same image to prod with one approval, **so I can** release safely and repeatably. | A-18 |
| J-28 | **When** a release misbehaves, **I want to** see deploy history next to metrics, **so I can** decide whether to roll back. | (Grafana annotations; same data the agent uses) |

## Internal "jobs" that emerged

While drawing sequence diagrams (step 3) we found jobs that no human asks for directly but the system needs.
They are listed so that **every** action and query has a traceable origin:

| ID | Job to be done | Derived |
|---|---|---|
| J-29 | **When** a trader joins, `api-gateway` needs to open a trading account with ₹1,00,000, **so it can** issue a session for a real account. | A-02 |
| J-30 | **When** an order arrives, `order-service` needs to reserve the cash or shares, **so it can** guarantee no overspending and no short selling. | A-06 |
| J-31 | **When** an order or cancel request is placed, `matching-engine` needs to match it, **so** trades happen fairly. | A-08 |
| J-32 | **When** a trade executes, `portfolio-service` needs to settle it, **so** balances are correct. | A-09 |
| J-33 | **When** time travel is requested, `market-data-service` needs to read market events and snapshots, **so it can** rebuild the past book. | Q-14, Q-15 |
| J-34 | **When** debugging or checking consistency, the team needs to see the engine's live in-memory book, **so it can** compare it with the read model. | Q-16 |
| J-35 | **When** a chaos scenario runs (memory leak, database degraded, panic sell), `chaos-service` needs to switch a fault on and off inside a target service, **so** the failure is realistic and reversible. | A-12, A-13, A-21, A-22, A-23, A-24 |
| J-36 | **When** a reservation has had no `OrderAccepted` for 120 s, `portfolio-service` needs the matching engine to confirm the order can never trade, **so it can** release the money without risking a trade on it (found while building Phase 2; [ADR-0017](../adr/0017-fail-closed-orphan-release.md)). | A-19 |

## Viva Notes

**Q: Why write JTBDs instead of user stories?**
A: User stories ("As a trader I want a buy button") describe a *solution*. JTBDs describe the *situation and
goal*, which leaves room to choose the best solution and keeps us focused on outcomes.

**Q: How do JTBDs connect to your API?**
A: Each JTBD lists the action/query IDs derived from it, and each OpenAPI operation's description starts
with that ID. So any endpoint can be traced back to a real need.

**Q: Why are there "internal" jobs?**
A: Some needs only appear when you draw the sequence diagrams — e.g. reserving cash before matching. SEED(S) is
iterative; we recorded them so nothing in the API is untraceable.

**Q: Which JTBD best justifies event sourcing?**
A: J-20 (time travel). You can only rebuild the exact past order book if you stored every change as an event.
