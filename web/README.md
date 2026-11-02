# web

> **Status:** Phase 0 — design only, no code yet.

| | |
|---|---|
| **Owning team** | Trading Team |
| **Owner** | **Kaizer Dewaswala** |
| **Type** | Client of the public API (not a bounded context) |

## Responsibility

React + Vite + TypeScript single-page app with TradingView Lightweight Charts. Views:

| View | Who | Uses |
|---|---|---|
| **Join** (`/join`, opened from the QR code) | Trader | A-01 |
| **Trade**: instrument list, candlestick chart, order book, order ticket, my orders, portfolio | Trader | Q-02–Q-10, A-05, A-07 |
| **Leaderboard** | Everyone | Q-11 |
| **Time travel**: pick a moment or replay a window | Trader, Admin | Q-12, Q-13 |
| **Presenter**: QR code, big charts, leaderboard, live incident feed | Admin | Q-02, Q-06, Q-11, Q-19 |
| **Chaos Panel**: scenarios, start/stop, experiment results | Admin | Q-21–Q-23, A-10, A-11 |
| **Agent controls**: approval toggle, pause, approve/decline | Admin | Q-20, A-14, A-15 |

Mobile-first (phones on classroom Wi-Fi): small bundle, reconnecting WebSockets, prices formatted from paise in Indian
digit grouping (₹1,00,000.00).

## Owned data

None on the server. The access token is kept in the browser's local storage.

## Viva Notes

**Q: Why is the frontend owned by the Trading team rather than a separate frontend team?**
A: The main user journey (join → order → fill) is the trading stream; one owner avoids a hand-off between a frontend and
a backend team for every feature.

**Q: How does the UI stay correct if a WebSocket message is missed?**
A: On every (re)connect the server sends a fresh snapshot first, then incremental updates.

**Q: Why doesn't the browser call services directly?**
A: Everything goes through api-gateway, which handles auth, rate limits and routing; services stay private.
