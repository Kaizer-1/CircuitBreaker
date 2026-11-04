# web

> **Status:** Phase 2 — one screen so far: **System Status** (polls the gateway's `/v1/system/status` every 3 s and shows every
> service and dependency as healthy or not). The report is admin-only since Phase 2, so the page first asks for the
> presenter passcode (A-04, local placeholder `cb-dev-admin-passcode`) and keeps the token for the browser tab
> (`sessionStorage`). The trader, presenter and admin views below arrive in Phase 3.

|                 |                                                  |
| --------------- | ------------------------------------------------ |
| **Owning team** | Trading Team                                     |
| **Owner**       | **Kaizer Dewaswala**                             |
| **Type**        | Client of the public API (not a bounded context) |

## Responsibility

React + Vite + TypeScript single-page app with TradingView Lightweight Charts. Views:

| View                                                                                          | Who           | Uses                   |
| --------------------------------------------------------------------------------------------- | ------------- | ---------------------- |
| **Join** (`/join`, opened from the QR code)                                                   | Trader        | A-01                   |
| **Trade**: instrument list, candlestick chart, order book, order ticket, my orders, portfolio | Trader        | Q-02–Q-10, A-05, A-07  |
| **Leaderboard**                                                                               | Everyone      | Q-11                   |
| **Time travel**: pick a moment or replay a window                                             | Trader, Admin | Q-12, Q-13             |
| **Presenter**: QR code, big charts, leaderboard, live incident feed                           | Admin         | Q-02, Q-06, Q-11, Q-19 |
| **Chaos Panel**: scenarios, start/stop, experiment results                                    | Admin         | Q-21–Q-23, A-10, A-11  |
| **Agent controls**: approval toggle, pause, approve/decline                                   | Admin         | Q-20, A-14, A-15       |

Mobile-first (phones on classroom Wi-Fi): small bundle, reconnecting WebSockets, prices formatted from paise in Indian
digit grouping (₹1,00,000.00).

## Owned data

None on the server. The access token is kept in the browser's local storage.

## Run and test it alone

```bash
cd web
npm ci                  # install from package-lock.json (Node >= 22.12, 24 LTS recommended)
npm run dev             # http://localhost:5173, proxies /v1 to the gateway on :8080 (start it with `make up`)
npm test                # vitest + Testing Library
npm run lint && npm run format:check && npm run typecheck
npm run build           # type-check and build to dist/
```

**Without the backend:** `make mock` (repo root) starts the API mocked from the specs: Prism on :4010 and the
WebSocket mock on :4011 (`mock/ws-mock.mjs`, a moving market for Q-06 and a portfolio for Q-10). Then
`npm run dev:mock` serves the app with `/v1` proxied to them.

In Docker (`make up`) the built files are served by unprivileged nginx on host port 5173, which forwards `/v1/` to
`api-gateway` so the browser sees a single origin (no CORS). Versions: React 19, Vite 8, TypeScript 6.0 (pinned below 6.1
because typescript-eslint does not support TypeScript 7 yet), ESLint 10, Vitest 5.

## Viva Notes

**Q: Why is the frontend owned by the Trading team rather than a separate frontend team?**
A: The main user journey (join → order → fill) is the trading stream; one owner avoids a hand-off between a frontend and
a backend team for every feature.

**Q: How does the UI stay correct if a WebSocket message is missed?**
A: On every (re)connect the server sends a fresh snapshot first, then incremental updates.

**Q: Why doesn't the browser call services directly?**
A: Everything goes through api-gateway, which handles auth, rate limits and routing; services stay private.
