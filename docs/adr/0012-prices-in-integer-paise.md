# 0012 — Money and Prices as Integer Paise

- **Status:** Accepted
- **Date:** 2026-11-02
- **Deciders:** Kaizer Dewaswala, MD Asif Ali, Mohammad Oweis

## Context

All money is INR with a tick size of ₹0.05. Binary floating-point numbers cannot represent most decimal fractions
exactly (`0.1 + 0.2 = 0.30000000000000004`). Over thousands of trades, rounding errors would make balances drift and
the leaderboard wrong, and a tick-size check on floats is unreliable.

## Decision

Represent **every** price and amount as an **integer number of paise** (₹1 = 100 paise) in code, databases, events and
APIs. Field names end in `_paise` (`limit_price_paise: 24600` = ₹246.00). The tick rule is `price_paise % 5 == 0`.
Conversion to rupees happens only in the UI, for display. Percentages (P&L %) are display values computed at read time.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| **Floats** | Rounding errors; equality and modulo checks unreliable. |
| **Decimal types** (Python `Decimal`, SQL `NUMERIC`) | Exact, but JSON has no decimal type (would need strings), and every language/library must agree on precision and rounding. Integers are simpler everywhere. |
| **Integer ticks** (1 unit = ₹0.05) | Exact and compact, but confusing for humans reading data (`4920` ticks = ₹246.00?). Paise are intuitive. |

## Consequences

- ✅ Exact arithmetic; trivial tick-size validation; identical in Python, JSON, Cassandra `bigint` and Postgres `bigint`.
- ✅ Self-documenting field names.
- ❌ UI must format paise as rupees (Indian grouping: 1,00,000).
- ❌ Division (averages, P&L %) must be rounded explicitly — rule: average prices round half-up to the nearest paisa.

## Viva Notes

**Q: Why not floats for money?**
A: Floats can't represent 0.05 exactly, so totals drift and comparisons fail. Integers are exact.

**Q: How do you check the tick size?**
A: `price_paise % 5 == 0`, plus a JSON Schema `multipleOf: 5` in the API and events.

**Q: Why paise and not Decimal?**
A: JSON has no decimal type, so we'd pass strings and every service would need matching precision rules. Integers work
identically everywhere.
