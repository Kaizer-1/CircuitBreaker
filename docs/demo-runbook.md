# Demo-Day Runbook

> **Status:** started in Phase 4 with what is known so far; Phase 8 completes it (rehearsals, timings, the metrics
> report). It also keeps the **Later list**: ideas found while building that are not in the frozen design
> ([roadmap, design freeze](07-roadmap.md#design-freeze)).

> **Runbook:** a step-by-step checklist for running the system on the day, written so that whoever presents can follow
> it under pressure, with what to do when something goes wrong.

## 1. Before the audience arrives

| Step | Command or action | Why |
|---|---|---|
| 0 | **Mains power, nothing else running.** `pmset -g batt` says `AC Power`. Quit other apps and dev servers (another project's `npm run dev`, a second IDE, browsers with many tabs) and let a pending macOS update wait. With the stack and the bots running, `uptime` shows a 1-minute load average of about **4–6**; above 8, find what else is busy (`top -o cpu`) before the audience arrives | The exchange shares the laptop's 12 cores with everything else on it. On battery macOS slows the CPU: a 60-minute soak on battery measured order p95 615 ms against 281 ms on mains (2026-11-14). A busy Mac stretched Cassandra's pauses from 150 ms to 640 ms and narrows the traffic spike's window: 110 requests/s is calibrated for a quiet laptop (chaos scenarios §3.3) |
| 1 | Laptop on power; in a spare terminal run **`caffeinate -dims`** and leave it running until the demo ends | macOS then never sleeps (`-d` display, `-i` idle, `-m` disk, `-s` system on power). A sleeping laptop freezes Docker: on waking, market-data has every missed 5-second candle to close (it saves them in chunks since Phase 4) and the bots' 12-hour sessions may have run out (they sign in again since Phase 4); both recover, but the market stalls meanwhile. `Ctrl+C` ends it |
| 2 | **First** join the phone hotspot, **then** set `PUBLIC_BASE_URL` to the laptop's address on it (`ipconfig getifaddr en0`) | The QR code must point where phones can reach ([developer setup §10](08-developer-setup.md#10-phones-on-a-hotspot-demo-day)); the address belongs to the network, so it is only known once the laptop is on the hotspot |
| 3 | **A person** runs `CONFIRM=yes make demo-reset` | Wipes all data, starts everything and checks the clean start: five empty books, only the seeded accounts, no humans. Rehearsal traders, test accounts and leftover orders are gone, and the leaderboard starts empty. Data-wiping commands are run by a person, never by a script or an assistant |
| 4 | `make bots` | The 20 bots: the market moves and the books fill |
| 5 | Wait at least **5 minutes** | Time travel needs a history to scrub through, and the timeline marks the biggest moves |
| 6 | `python3 scripts/bots_check.py 120` | The market trades at least 5 times a minute per ticker (20 to 50 is normal), the book is two-sided, every price is within ±10% of its reference, nothing crossed, no refused bot order, no 5xx. For a full rehearsal, `python3 scripts/bots_check.py 1800` checks the band over 30 minutes |
| 7 | `python3 scripts/time_travel_check.py 5 5` | Seeks answer fast and all five tickers say "hashes match" against the live engine |
| 8 | `make test-phone url=http://<LAN IP>:5173` | A phone's view over the real address (not a secure context): a MARKET buy reaches Holdings, the tabs work, in both skins. It trades as `load-13`, never on the leaderboard |
| 9 | Projector: `/dashboard?theme=paper` (or `terminal` in a dim room), sign in with the presenter passcode; scan the QR code with one real phone and place an order | The QR box must not warn about localhost |

Do not rebuild or restart containers once the audience is in: a gateway restart drops every phone's and bot's
connection (the bots are back within about a minute). **Never restart the stack right before presenting**: bring it
up early enough for the warm-up spike below and the 5-minute wait, then leave it running.

### The Kubernetes path (the demo platform from Phase 5)

The same steps on the cluster ([developer setup §11](08-developer-setup.md#11-kubernetes-kind)). Run Compose **or**
the cluster, not both (`make down` first): together they do not fit in Docker's memory.

| Step | Command or action | Why |
|---|---|---|
| 0 | Step 0 above: mains power, other apps closed, `uptime` about 4–6 with the cluster and bots running | The same reasons; the cluster needs more of the laptop than Compose |
| 1 | `caffeinate -dims` as above; **join the hotspot first** and note the laptop's address (`ipconfig getifaddr en0`) | The QR code needs the address on the network the phones are on |
| 2 | **A person** runs `CONFIRM=yes make k8s-reset`, then `PUBLIC_BASE_URL=http://<laptop LAN IP> make k8s-up` | A clean cluster and the QR code pointing at port 80. Data-wiping commands are run by a person. Allow about 7 minutes. The address is kept from then on (restarts, `k8s-bots`) |
| 3 | `make k8s-bots`, wait 5 minutes | The market moves; time travel has a history |
| 3a | `make k8s-status`: the last lines show the QR code's address and warn if it is localhost or not this laptop's current address | Fix with `make k8s-set-url url=http://<LAN IP>` (only the web pod restarts; no data touched) |
| 3b | `make test-phone url=http://<LAN IP>` | A phone's view over the real address, as on the Compose path |
| 3c | **Warm-up spike:** `make k8s-check c=scale`; it must say PASS (2 replicas above 500 ms, 5 replicas back below it). If it fails, wait a few minutes, check step 0, and run it once more | The traffic spike at its calibrated 110 requests/s, once, before anyone watches. In the calibration the only run where 5 replicas did not recover (861 ms) was the first one after Redpanda had restarted; the four after it passed. It stops the bots for about 5 minutes and starts them again |
| 4 | `make test-k8s` is **not** for the audience (it starts the bots itself); use `python3 scripts/k8s.py run bots_check.py 120` and `python3 scripts/k8s.py run time_travel_check.py 5 5` | The same checks as the Compose path, through port-forwards |
| 5 | Projector: `http://<LAN IP>/dashboard?theme=paper`; a second screen or tab: Grafana `http://localhost:3000`, dashboard *CircuitBreaker — Business* | The circuit breaker panel is the first thing Grafana shows |
| 6 | Optional demo moment: *Follow an order*: place an order on a phone, paste its correlation ID (or pick one from *Orders placed*) | One order's journey through four services, from one query |

If something goes wrong on Kubernetes: `make k8s-status` (pods and memory); a pod stuck `0/1` → `kubectl -n prod logs
<pod>`; a service misbehaving → `kubectl -n prod rollout restart deployment/<service>` (rolling, no downtime: two
replicas and a 5 s preStop pause).

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
| Phones cannot open the QR address, or the QR shows an old address | The laptop rejoined the hotspot: `ipconfig getifaddr en0`; Kubernetes: `make k8s-status` | Compose: `PUBLIC_BASE_URL=http://<LAN IP>:5173 docker compose up -d --no-deps web`; Kubernetes: `make k8s-set-url url=http://<LAN IP>` |
| An order fails on a phone | The toast says why (no connection, the exchange's reason, an exchange error with its HTTP status, or an app error); the laptop's browser console on the same address shows the full error | No connection: the phone left the hotspot. Exchange error: `make status` / `make k8s-status` |
| Something is broken and there is no time to fix it | — | Fall back to the last known-good version (§4) |

## 4. Fallback: the last known-good version

Annotated Git tags mark versions that were verified end to end. Falling back changes the code, not the data, so it
takes minutes, not a reset, as long as the data is from the same tickers.

| Tag | Verified | Runs on |
|---|---|---|
| **`v0.5.2-demoable`** (preferred) | The professional ticker names (CALVERA, KORVIN, ASTRYX, HELIOR, TAVIRA), plus everything in `v0.5.1-demoable`; CI green | Compose **and** Kubernetes |
| `v0.5.1-demoable` | Phones ordering over the hotspot address (`make test-phone` on a LAN IP), the tab bar, the QR address kept on Kubernetes, CI green. **Old ticker names:** it cannot read data made with the new ones, so a person resets first | Compose **and** Kubernetes |
| `v0.4-demoable` | Phase 4: time travel, leaderboard; phone ordering over the LAN did **not** work yet (only on the laptop). Old ticker names, as above | Compose only (Kubernetes came in Phase 5) |

**Compose:**

```bash
make down                                         # stops the containers; volumes (data) are kept
git switch -c demo-fallback v0.5.2-demoable       # a branch at the tag (the working tree must be clean)
PUBLIC_BASE_URL=http://<LAN IP>:5173 make up      # builds that version's images
make bots
```

**Kubernetes:**

```bash
git switch -c demo-fallback v0.5.2-demoable
PUBLIC_BASE_URL=http://<LAN IP> make k8s-up       # builds and rolls only the images that differ; data kept
make k8s-bots
```

Back to the latest: `git switch main`, then `make up` or `make k8s-up` again. If a later version changed how data is
stored and the old version cannot read it, **a person** runs `CONFIRM=yes make demo-reset` (Compose) or `CONFIRM=yes
make k8s-reset` (Kubernetes) first; data-wiping commands are never run by a script or an assistant.

## 5. The Later list

Found while building, outside the frozen design, to reconsider in Phase 8 or after the course. Its first two items
were built instead: an index of each trader's open orders (Q-07 `status=open` read the whole order history; the
`open_orders_by_trader` table, pre-chaos hardening PR, 2026-11-14), and pruning the engine's snapshots, which grew one
Cassandra partition per ticker to 2 GB in 19 hours (profiling PR, 2026-11-23: one partition per ticker per hour,
thinned, a 24-hour time-travel horizon; [event sourcing](03-architecture/04-event-sourcing-and-cqrs.md#snapshots)).

Nothing is on the list now.

## Viva Notes

**Q: Why reset everything before a demo?**
A: `make demo-reset` gives every rehearsal and the demo the same starting state: no test accounts on the leaderboard,
no orders left from earlier runs, an empty history. A script checks that state, so we know rather than hope.

**Q: Why `caffeinate`?**
A: A laptop that sleeps pauses every container. The system recovers on waking (Phase 4 fixed the two ways it used to
freeze), but the audience would see a stalled market. `caffeinate -dims` keeps macOS awake for as long as it runs.

**Q: Why does the runbook care about mains power and other apps?**
A: Everything runs on one laptop, so the exchange's speed depends on what else that laptop is doing. We measured
it: on battery, order latency more than doubled; with other apps busy, Cassandra's pauses stretched four times; and
the traffic spike's rate (110 requests/s) only separates 2 replicas from 5 on a quiet Mac. Checking `uptime` takes
seconds and turns "it was slow on the day" into something we can rule out beforehand.

**Q: Why a warm-up spike before the audience arrives?**
A: Measured: in the calibration, the one run where 5 replicas did not bring latency back below 500 ms was the first
after Redpanda had restarted; the next four passed. Running the spike once beforehand both warms the system and proves,
on that day's laptop, that the scenario will resolve, so the live run is not the first one.

**Q: Why run Compose or the cluster, not both?**
A: The cluster with monitoring uses about 4.3 GiB of process memory and Compose about 1.8 GiB, plus Docker's page
cache. Together they crowd an 11 GB Docker allocation, and a demo must not run short of memory.

**Q: Why tag "demoable" versions?**
A: A tag names a commit that was verified end to end, so on the day there is a known-good version one command away.
Later phases (chaos, the SRE agent) change a lot; if one breaks something just before the demo, we fall back instead
of debugging in front of an audience.

**Q: What is the "Later list" for?**
A: The design is frozen until Phase 7, so ideas found while building are written down instead of built. Each entry
says why it waits, so the decision can be revisited with the reason in front of us.
