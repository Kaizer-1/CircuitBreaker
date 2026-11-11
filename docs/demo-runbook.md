# Demo-Day Runbook

> **Status:** started in Phase 4 with what is known so far; Phase 8 completes it (rehearsals, timings, the metrics
> report). It also keeps the **Later list**: ideas found while building that are not in the frozen design
> ([roadmap, design freeze](07-roadmap.md#design-freeze)).

> **Runbook:** a step-by-step checklist for running the system on the day, written so that whoever presents can follow
> it under pressure, with what to do when something goes wrong.

## 1. Before the audience arrives

| Step | Command or action | Why |
|---|---|---|
| 1 | Laptop on power; in a spare terminal run **`caffeinate -dims`** and leave it running until the demo ends | macOS then never sleeps (`-d` display, `-i` idle, `-m` disk, `-s` system on power). A sleeping laptop freezes Docker: on waking, market-data has every missed 5-second candle to close (it saves them in chunks since Phase 4) and the bots' 12-hour sessions may have run out (they sign in again since Phase 4); both recover, but the market stalls meanwhile. `Ctrl+C` ends it |
| 2 | Join the phone hotspot; set `PUBLIC_BASE_URL` to the laptop's address on it | The QR code must point where phones can reach ([developer setup §10](08-developer-setup.md#10-phones-on-a-hotspot-demo-day)) |
| 3 | **A person** runs `CONFIRM=yes make demo-reset` | Wipes all data, starts everything and checks the clean start: five empty books, only the seeded accounts, no humans. Rehearsal traders, test accounts and leftover orders are gone, and the leaderboard starts empty. Data-wiping commands are run by a person, never by a script or an assistant |
| 4 | `make bots` | The 20 bots: the market moves and the books fill |
| 5 | Wait at least **5 minutes** | Time travel needs a history to scrub through, and the timeline marks the biggest moves |
| 6 | `python3 scripts/bots_check.py 120` | The market trades at least 5 times a minute per ticker (20 to 50 is normal), the book is two-sided, every price is within ±10% of its reference, nothing crossed, no refused bot order, no 5xx. For a full rehearsal, `python3 scripts/bots_check.py 1800` checks the band over 30 minutes |
| 7 | `python3 scripts/time_travel_check.py 5 5` | Seeks answer fast and all five tickers say "hashes match" against the live engine |
| 8 | Projector: `/dashboard?theme=paper` (or `terminal` in a dim room), sign in with the presenter passcode | The QR box must not warn about localhost |

Do not rebuild or restart containers once the audience is in: a gateway restart drops every phone's and bot's
connection (the bots are back within about a minute).

## 2. During the demo

| Screen | Keys and controls |
|---|---|
| Dashboard | 1–5 choose the ticker shown big; **B** (or the header button) hides or shows the leaderboard |
| Time travel | Terminal: on-screen **F5 TIME TRAVEL**; paper: **Time travel** under the masthead. Drag the timeline or click a ▲ ▼ marker; **Space** plays and pauses; speed 1× to 60×; **Verify against live** for the proof; **L** or **Back to live** |

Projector safety: offensive nicknames are refused at join (a word list, [api-gateway README](../services/api-gateway/README.md#nickname-blocklist-projector-safety));
anything that slips through is off the screen with **B**.

## 3. If something goes wrong

| Symptom | Check | Fix |
|---|---|---|
| No trades for a minute | `make status`; `docker compose logs bot-traders` | `make bots-stop` then `make bots` |
| Charts frozen, trading goes on | market-data logs ("saving the read model failed") | `docker compose restart market-data-service`: it rebuilds from the event store |
| Time travel says "busy" | More than 3 traders time-travelling: the presenter keeps a reserved place | Retry; the presenter's request is never refused |
| A rude name on the leaderboard | — | **B** hides it; add the word to the blocklist file after the demo |

## 4. The Later list

Found while building, outside the frozen design, to reconsider in Phase 8 or after the course.

| Idea | Found | Why it waits |
|---|---|---|
| An index of each trader's open orders in order-service. `GET /v1/orders?status=open` (Q-07) now reads one row per order in the trader's history (up to 1,000 per call), and every bot lists its open orders after a reconnect | Phase 4: after two hours of trading, a gateway restart made 20 bots list at once and order-service ran out of memory | Owner's decision (2026-11-11): a data-model change. The bots back off after a failure and list at most two at a time, which keeps it safe for the demo |

## Viva Notes

**Q: Why reset everything before a demo?**
A: `make demo-reset` gives every rehearsal and the demo the same starting state: no test accounts on the leaderboard,
no orders left from earlier runs, an empty history. A script checks that state, so we know rather than hope.

**Q: Why `caffeinate`?**
A: A laptop that sleeps pauses every container. The system recovers on waking (Phase 4 fixed the two ways it used to
freeze), but the audience would see a stalled market. `caffeinate -dims` keeps macOS awake for as long as it runs.

**Q: Why wait five minutes after starting the bots?**
A: Time travel shows the past, so there must be one: a few minutes of trading gives the timeline real moves to mark
and the replay something to play.

**Q: What is the "Later list" for?**
A: The design is frozen until Phase 7, so ideas found while building are written down instead of built. Each entry
says why it waits, so the decision can be revisited with the reason in front of us.
