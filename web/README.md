# web

> **Status:** Phase 4. Five screens: **System Status**, the **presenter dashboard** and **time travel**
> for the projector, and the phone's **Join** and **Trade** pages. Each has two skins, Pink Paper (light) and Terminal
> (dark), on one layout. The chaos panel, agent controls and incident feed arrive in Phase 7.

|                 |                                                  |
| --------------- | ------------------------------------------------ |
| **Owning team** | Trading Team                                     |
| **Owner**       | **Kaizer Dewaswala**                             |
| **Type**        | Client of the public API (not a bounded context) |

## Responsibility

React + Vite + TypeScript single-page app with TradingView Lightweight Charts. Every call goes through api-gateway.

| Page                    | URL                       | Who                   | Sign-in               | Uses                                     |
| ----------------------- | ------------------------- | --------------------- | --------------------- | ---------------------------------------- |
| **System Status**       | `/` (also `/status`)      | Presenter, developers | Admin passcode (A-04) | `GET /v1/system/status`                  |
| **Presenter dashboard** | `/dashboard`              | Presenter (projector) | Admin passcode (A-04) | Q-02, Q-06 (all five tickers), Q-11, QR  |
| **Time travel**         | `/time-travel`            | Presenter (projector) | Admin passcode (A-04) | Q-03, Q-06, Q-12, Q-13                   |
| **Join**                | `/join` (the QR's target) | Trader (phone)        | None: a nickname      | A-01                                     |
| **Trade**               | `/trade`                  | Trader (phone)        | The session from Join | Q-02, Q-06, Q-07, Q-09, Q-10, A-05, A-07 |
| **Chaos Panel**         | `/chaos`                  | Presenter (projector) | Admin passcode (A-04) | Q-21, Q-22, A-10, A-11, A-20             |
| Incident feed, agent    | (Phase 7, PR 7c)          | Admin                 |                       | Q-17–Q-20, A-14, A-15                    |

- **Presenter dashboard:** the five tickers in a watchlist, the selected one big (keys 1–5 choose it), 5-second candles,
  the order book (best 6 levels), the trades tape, the join QR code, and a status bar (health, live streams, volume).
  Beside the watchlist, the **leaderboard**: the top five human traders by net worth (Q-11, polled every 2 s; ties share
  a rank). Projector safety: the header's **Hide leaderboard** / **Show leaderboard** button (or key B) takes it off
  the screen, the choice is remembered on this device, and a hidden leaderboard is not polled.
- **Trade:** net worth and P&L (live over Q-10), one stock's price and chart, the order ticket (LIMIT or MARKET, BUY or
  SELL; it applies the exchange's rules before sending: tick 5 paise, quantity 1–10,000, available cash or shares),
  open orders with cancel, and holdings. Every touch target is at least 44 px, for one-handed use. In the terminal
  skin, the F-key tabs at the bottom (F1 MARKET … F4 HOLDINGS) scroll their section to the top of the screen without
  leaving the page, and the lit tab follows the scroll (`components/SectionTabs.tsx`). On iPhones a strip in the
  skin's background covers the status bar (`env(safe-area-inset-top)`).
- **Phones use plain http on a LAN address, which is not a secure context.** The page therefore uses no API reserved
  for https or localhost: order keys (the `Idempotency-Key` of A-05) are RFC 4122 v4 UUIDs built from
  `crypto.getRandomValues` (`lib/id.ts`), not `crypto.randomUUID`. A failed action is logged with `console.error`
  and the toast says why in plain words (`lib/errors.ts`): no connection, the exchange's own reason, an exchange
  error with its HTTP status, or "Something went wrong in the app" (never a raw exception).

### Themes: one layout, two skins

The light skin is Pink Paper (newsprint) and the dark skin is Terminal. They share one layout and one set of
components. Only the design tokens change (CSS variables in [`src/theme/tokens.css`](src/theme/tokens.css), under
`[data-theme="paper"]` and `[data-theme="terminal"]`), plus a few details shown in one skin only (`.paper-only`,
`.terminal-only`). The reasons and the token table are in [visual-direction.md](../docs/design/visual-direction.md).

- **Which skin:** `?theme=paper` or `?theme=terminal` in the URL wins. Next comes the choice made with the header
  toggle (remembered on the device), then the device's light/dark setting. An inline script in `index.html` sets the
  skin before the first paint, so the page never flashes the wrong one.
- **The chart** takes its colours in JavaScript, so it re-applies them when the skin changes (tested).
- **Fonts** are self-hosted (Google Fonts' files via Fontsource, only the weights used), so phones on a hotspot with no
  internet still get them.

### Live data

Each screen opens WebSockets through the gateway: Q-06 for each ticker it shows, and Q-10 for the trader's own
portfolio. The token goes in the URL (`?access_token=`) because browsers cannot set headers on a WebSocket. nginx and
the gateway never log it. On every connect the server sends a snapshot, then updates in sequence. The client
(`src/market/marketState.ts`) drops anything at or below the sequence it has. After a disconnect it reconnects
(`src/market/stream.ts`, waiting 1 s, then 2 s, doubling up to 30 s) and starts again from a fresh snapshot
([read side §4](../docs/03-architecture/08-read-side.md#4-snapshot-then-updates-resyncing-a-client)).

### Chaos Panel (presenter)

The four chaos scenarios ([chaos scenarios](../docs/03-architecture/06-chaos-scenarios.md), ADR-0020) as one table, not
a grid of cards: what each breaks, the fix that is right for it (rollback, restart, scale, or do nothing and escalate),
its state (ready; running with the time left and, once the SRE agent opened an incident, how fast it was detected; or
how it ended) and a **Start** / **Stop** button. **Reset everything** (A-20) ends every experiment and undoes every
fault, including replicas the agent added. A start chaos-service refuses (another experiment already affects that
service: `409`) shows its reason in the status strip. Below, chaos-service's own timeline (and the presenter's
resets), newest first. Polled every 2 s (Q-21, Q-22). Reached by the terminal skin's on-screen **F6 CHAOS** key or
the paper skin's **Chaos** section link. The SRE agent never reads any of it: it works from symptoms.

### Time travel (presenter)

Event sourcing shown to an audience (J-20). The screen has the dashboard's layout with a timeline in place of the
watchlist. The APIs (Q-12, Q-13) are open to any signed-in user; the _screen_ lives in the presenter's area, reached by
the terminal skin's on-screen **F5 TIME TRAVEL** key or the paper skin's **Time travel** section link (the real F5 key
stays the browser's reload).

- **Timeline** (`src/components/Timeline.tsx`): the session's one-minute closes from its first trade, markers ▲ ▼ at
  the five biggest one-minute moves, and a handle at the moment shown. Drag or click to seek; a marker jumps to its
  minute. The handle is a slider: ← → one minute (Shift: ten seconds), Page Up/Down ten minutes, Home the start, End
  now.
- **Seek** (Q-12): the book, last price, trades and state hash at that moment. The chart's 5-second candles come from
  Q-03 up to that moment, the one holding it rebuilt from the trades so far (`candlesUpTo`).
- **Play** (Q-13) from the moment shown at 1×, 2×, 5×, 10× or 60×, for up to 30 minutes; pause, resume and speed
  changes take effect at once. Every fill of each replayed command goes on the tape, so it shows what the live market
  showed. A replay that closes is not reopened; the screen says why (for example, a full replica).
- **The proof.** **Verify against live** (and **Back to live**) ask Q-12 for now (one second ahead: market-data counts
  up to two seconds ahead as now) and show **Verified at sequence N: hashes match** with the live engine's hash,
  without changing what is shown. Dragging the timeline to its right edge (or End) rebuilds now instead. When a replay
  ends, its end hash is compared with Q-12 at the same moment.
- **One sequence, labelled hashes.** The hero, the banner and the status bar all show the same sequence: the shown
  book's last event. A hash always says which sequence it belongs to: live, "verified at N"; paused, the hash of the
  shown sequence (a replay paused mid-way asks Q-12 for it); while playing, "computed when paused".
- **Never mistaken for live.** In every past state (seeking, paused, playing, ended) a banner sits under the header —
  paper: "From the archives · _date, time_"; terminal: amber `HISTORICAL MODE · time` — with **Back to live**, and every
  panel's title carries a tag; in the terminal skin the panels' outlines turn amber. A test renders every state in both
  skins.
- Keys: 1–5 choose the ticker, Space plays or pauses, L goes back to live. State: one reducer
  (`src/timetravel/state.ts`); actions: `src/timetravel/useTimeTravel.ts`; the socket: `src/timetravel/replay.ts`.

### The join QR code and `PUBLIC_BASE_URL`

The QR code encodes `PUBLIC_BASE_URL/join`. The web container writes `PUBLIC_BASE_URL` into `/config.js` when it
starts (`docker/config.sh`), so changing it needs no rebuild. It must be an address phones can reach: the laptop's LAN
address on the phone hotspot ([developer setup §10](../docs/08-developer-setup.md#10-phones-on-a-hotspot-demo-day)).
When it is localhost, `127.x`, or not set, the presenter dashboard shows **"QR points to localhost. Phones can't join.
Set PUBLIC_BASE_URL."** in place of the join text.

### Nicknames

A nickname is shown exactly as typed: 3–20 letters, digits or `_` (the A-01 rule in `api/api-gateway.yaml`). If it is
taken (409 `NICKNAME_TAKEN`), the join page tries again with a short random suffix, for example `RajuTrader_7k`. It
makes up to three tries and then asks for another name. The suffix is shown in a quieter colour, so the trader sees
which part they typed.

## Owned data

None on the server. In the browser:

| Key                          | Where            | What                                                                       |
| ---------------------------- | ---------------- | -------------------------------------------------------------------------- |
| `circuitbreaker.trader`      | `localStorage`   | The trader's session (A-01 token, 12 h), so a phone reload stays signed in |
| `circuitbreaker.admin_token` | `sessionStorage` | The presenter's token (A-04), for this browser tab only                    |
| `circuitbreaker.theme`       | `localStorage`   | The skin chosen with the toggle                                            |

Every read and write is wrapped in `try`/`catch`: in a private window storage may be blocked, and then the session
lasts until the page is closed.

## Run and test it alone

```bash
cd web
npm ci                  # install from package-lock.json (Node >= 22.12, 24 LTS recommended)
npm run dev             # http://localhost:5173, proxies /v1 to the gateway on :8080 (start it with `make up`)
npm test                # vitest + Testing Library (the component tests run in both skins)
npm run lint && npm run format:check && npm run typecheck
npm run build           # type-check and build to dist/
```

**Without the backend:** `make mock` (repo root) starts the API mocked from the specs: Prism on :4010 and the
WebSocket mock on :4011 (`mock/ws-mock.mjs`, a moving market for Q-06 and a portfolio for Q-10). Then
`npm run dev:mock` serves the app with `/v1` proxied to them.

**Screenshots of the real screens:** with `make up` and `make bots` running,
`node scripts/screenshots.mjs <output dir>` uses Playwright with the installed Chrome. It renders the dashboard
(1920×1080, plus the localhost-warning case) and the phone (390×844) in both skins. A trader joins through the real
join page first. The script fails instead of taking a shot when:

- any element overflows its container or the screen;
- the terminal skin's fixed tabs hide the last item;
- the hero price does not return to its normal colour after a flash.

**The phone as phones see it:** `make test-phone` (repo root; in CI) runs `scripts/phone_check.mjs` with Playwright
against `http://cb.test:5173`, a name Chrome maps to the laptop and treats as an ordinary (non-secure) http site;
`make test-phone url=http://<LAN IP>:5173` uses the real address. At 390×844 in both skins it buys 1 CALVERA at
market and waits for it in Holdings, re-sends the same order key (one order), taps each F-key tab (section at the
top, tab lit, no reload) and fails on any console error. Needs the bots trading (a seller); it trades as `load-13`,
which is never ranked.

In Docker (`make up`) unprivileged nginx (mainline 1.31, Alpine; the image runs `apk upgrade` while building, and CI's
Trivy gate fails on a CRITICAL vulnerability with a fix) serves the built files on host port 5173. It forwards `/v1/` (and the
WebSocket upgrade on `/v1/ws/`) to `api-gateway`, so the browser sees a single origin (no CORS). Versions: React 19,
Vite 8, TypeScript 6.0 (pinned below 6.1 because typescript-eslint does not support TypeScript 7 yet), ESLint 10,
Vitest 5, Lightweight Charts 5.2.1, Playwright 1.64.1 (development only).

## Viva Notes

**Q: Why is the frontend owned by the Trading team rather than a separate frontend team?**
A: The main user journey (join → order → fill) is the trading stream. One owner avoids a hand-off between a frontend
team and a backend team for every feature.

**Q: How does the UI stay correct if a WebSocket message is missed?**
A: On every (re)connect the server sends a fresh snapshot first, then updates that each carry a sequence. The client
drops anything at or below the sequence it has, so a missed message is repaired by the next snapshot and never shows up
twice.

**Q: Why doesn't the browser call services directly?**
A: Everything goes through api-gateway, which handles auth, rate limits and routing, and the services stay private.

**Q: The trader's token is in `localStorage`, which a script injected into the page could read. Why accept that?**
A: A phone that reloads must stay signed in, and an `HttpOnly` cookie would need a cookie-based session in the gateway
that the design does not have. The risk is small here: the money is play money, the token expires after 12 hours, the
app renders no HTML from users (React escapes text), and every script it loads comes from its own origin. The admin
token lives only for the tab.

**Q: Why does the QR code need `PUBLIC_BASE_URL` at all? Can't the page use its own address?**
A: The presenter's laptop usually opens the page at `localhost`, and a phone that scans `localhost` reaches itself. A
container cannot see the laptop's LAN address either. So the address is configuration, and the dashboard warns loudly
when it is still localhost.
