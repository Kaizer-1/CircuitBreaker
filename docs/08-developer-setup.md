# 08 — Developer Setup

> **Goal:** from a fresh clone, one command starts every service and every piece of infrastructure on your laptop, all
> healthy. This page says what you need, what each command does, which ports are used, how much memory it takes
> (measured), and what to do when something goes wrong.
>
> **Terms used here**
> - **Docker Compose:** runs several containers together from one file, `docker-compose.yml`.
> - **Profile (Compose):** a label that lets you start only part of the file. We have two: `infra` (Redpanda, Cassandra,
>   Postgres and their setup jobs) and `app` (the 8 services and the web app).
> - **Init job:** a container that does one task and exits. Ours create the Redpanda topics and the Cassandra keyspaces.
> - **Healthcheck:** a command Docker runs to ask a container "are you OK?". `depends_on: service_healthy` makes a
>   container wait until the ones it needs are healthy.
> - **Lockfile (`uv.lock`, `package-lock.json`):** the exact version of every dependency, so every machine and CI
>   build the same thing.
> - **Stub:** an endpoint that exists in the API spec but has no logic yet; it answers `501 Not Implemented`.
> - **Contract test:** a test that compares the running app with its OpenAPI spec, in both directions.
> - **Umbrella project (local):** the repository root (`docker-compose.yml` + `Makefile`) that pulls every service together
>   so the whole system runs as one ([coding standards §2](05-coding-standards.md#the-umbrella-project-our-interpretation)).

## 1. Prerequisites

Run `make doctor`. It prints the version of each tool and tells you how to fix anything missing.

| Tool | Version | Why | Install (macOS) |
|---|---|---|---|
| Docker Desktop (Docker + Compose v2) | Compose v2 (`docker compose`), **10 GB RAM** allocated | Runs everything | <https://docs.docker.com/get-docker/> |
| Python | 3.12 | All backend services | `uv python install 3.12` |
| uv | ≥ 0.10 | Python dependencies and virtual environments | `brew install uv` |
| Node.js | **≥ 22.12, 24 LTS recommended** | Web app (Vite 8, ESLint 10 and Vitest 5 need 22.12+) | `brew install node@24 && brew link --overwrite --force node@24` |
| kind, kubectl, Helm | kind ≥ 0.27 (0.33 used), Helm 4, kubectl within a version or two of Kubernetes 1.37 (an older one only warns) | The Kubernetes deployment ([§11](#11-kubernetes-kind)) | `brew install kind kubectl helm` |
| Terraform, kubeseal | Terraform **1.17.x**, kubeseal 0.41 | The platform as code (`make platform`) and sealing secrets ([§11](#11-kubernetes-kind)) | `brew tap hashicorp/tap && brew install hashicorp/tap/terraform kubeseal` |
| make | GNU Make 3.81+ (the macOS one is fine) | Developer entry point | `xcode-select --install` |
| gitleaks, pre-commit | any current | Secret scanning and the commit hooks | `brew install gitleaks pre-commit` |

`make doctor` warns, and does not fail, when Docker has less than about 10 GB. See [§7](#7-resource-use-measured) for why
10 GB and what is actually used.

## 2. First run

```bash
git clone <repository-url> CircuitBreaker && cd CircuitBreaker
make doctor      # check the tools
make up          # build the 9 images and start everything (about a minute)
make status      # combined health of every service and dependency, as JSON
```

Then open:

| URL | What you should see |
|---|---|
| <http://localhost:5173> | **System Status** (sign in with the local passcode `cb-dev-admin-passcode`): "All systems healthy", 8 service cards and 3 dependencies, all green |
| <http://localhost:5173/dashboard> | **Presenter dashboard** for the projector (same passcode): five live tickers, the chart, the order book, the trades and the join QR code. Run `make bots` first, or nothing moves |
| <http://localhost:5173/join> | **Join** (no passcode, a nickname), then **Trade** at `/trade`: the phone page. On a laptop, make the window narrow or use the browser's device mode |
| <http://localhost:8088> | Redpanda Console: the 4 topics and their dead-letter topics |
| `make status` | The same report as JSON (it signs in as admin first; the endpoint is admin-only) |

Add `?theme=paper` or `?theme=terminal` to any page to pick its skin, the light Pink Paper or the dark Terminal. Without
it the page uses the header toggle's choice, then the device's light/dark setting ([web README](../web/README.md#themes-one-layout-two-skins)).

The first run also downloads the base images (Redpanda, Cassandra, Postgres, Python, Node, nginx), so it takes several
minutes depending on your connection. Later runs reuse Docker's cache; from an empty data state on a warm cache,
`make up` reached all 15 containers healthy in **32–51 seconds** on the reference machine.

For development you will also want `make setup` (creates each service's virtual environment, installs the web
dependencies, and installs the git hooks). `make up` does not need it, because the images build from the lockfiles.

### What `make up` starts, in order

```mermaid
flowchart LR
    subgraph infra["profile: infra"]
        RP["redpanda"] --> RI["redpanda-init<br/>4 topics + 4 dead-letter topics"]
        RP --> RC["redpanda-console"]
        CA["cassandra<br/>(password auth on)"] --> CI["cassandra-init<br/>4 keyspaces + 4 roles"]
        PG["postgres<br/>database 'portfolio'"]
    end
    subgraph app["profile: app"]
        GW["api-gateway"] --> WEB["web"]
        OS["order-service"]
        ME["matching-engine"]
        MD["market-data-service"]
        SA["sre-agent"]
        PS["portfolio-service"]
        BT["bot-traders"]
        CH["chaos-service"]
    end
    RI --> OS & ME & MD & SA & PS & BT & CH
    CI --> OS & ME & MD & SA
    PG --> PS
```

A service only starts after the init jobs it needs have finished and its database is healthy. Its own
`/readyz` then checks that it can really log in and use its own keyspace or database and that its Redpanda topics
exist, so "healthy" means "can do its job", not just "the process started".

## 3. Make targets

`make help` prints this list. Targets that work per service accept `s=<service>`, for example `make test s=order-service`.

| Target | What it does |
|---|---|
| `make doctor` | Checks the prerequisites and prints their versions |
| `make setup` | `uv sync` in every service, `npm ci` in `web/`, and installs the git hooks |
| `make infra` | Starts **only** the infrastructure (profile `infra`): Redpanda, Cassandra, Postgres and the init jobs |
| `make up` | Builds and starts everything (both profiles) and waits until every container is healthy |
| `make down` | Stops everything; data volumes are kept |
| `make reset` | Stops everything and **deletes all data volumes**; asks you to type `yes` first |
| `make demo-reset` | **Rehearsal start.** Like `make reset` (asks for `yes`; `CONFIRM=yes make demo-reset` skips the question), then builds and starts everything and checks the starting state: five empty books with no history, only the seeded accounts (`bot-01`…`bot-20` with ₹5,00,000 and 1,000 shares of each ticker, `load-01`…`load-10`), no human accounts. Every rehearsal starts from the same state ([demo-day runbook](demo-runbook.md)) |
| `make ps` | Shows the container status |
| `make logs s=<service>` | Follows logs (all services if `s` is omitted) |
| `make build` | Builds the Docker images (`s=<service>` for one) |
| `make lint` | `ruff check`, `ruff format --check` and `mypy` per service, `redocly lint`, the template-drift check, and web ESLint + Prettier |
| `make fmt` | Auto-fixes and formats Python (ruff) and web (Prettier) |
| `make test` | `pytest` per service (unit, property, smoke and contract tests; matching-engine also enforces 100% branch coverage of `domain/`) and `vitest` for the web app |
| `make test-integration` | Integration tests against real Cassandra/Postgres/Redpanda started by testcontainers (needs Docker); `s=<service>` for one |
| `make test-deep` | Property tests (Hypothesis) at 25,000 examples each instead of 1,000; slow |
| `make bots` | With `make up` running: the 20 bots start trading (they are off by default). `make bots seed=42` repeats a rehearsal's bot choices; `activity=1.5` makes them busier. `make up` turns them off again (the default) |
| `make bots-stop` | The bots stop trading; their container keeps running, idle |
| `make mock` | The public API mocked from the specs, for web work without the backend: Prism (5.16.0) on :4010 serves `api/api-gateway.yaml` with the owning services' examples (`tools/mock_spec.py` copies them in), and `web/mock/ws-mock.mjs` on :4011 streams the Q-06 and Q-10 WebSocket messages (Prism cannot mock WebSockets). Then `cd web && npm run dev:mock` |
| `make demo-trade` | With `make up` running: two traders join, a bot sells to one, who sells to the other, all through the gateway; prints the trades, the three portfolios and the order statuses |
| `make test-e2e` | With `make up` running and the bots off (~7 min): join → trade → settle with exact balances; `kill -9` of the matching engine mid-workload, then the same book hash after a rebuild; the orphan rule with Redpanda stopped (`REJECTED` after 60 s, the hung send never delivered, money held until the engine answers); the 20 bots for 120 s (`scripts/bots_check.py`: trade rate, a two-sided book, prices within ±10% of their reference, the book never crossed in the engine or in market-data, no refused bot order, no 5xx); then, with the bots still trading, time travel: three recorded moments rebuilt with the same hash, a rewind to now verified on all five tickers, a replay ending on Q-12's hash with exactly the engine's fills, four replays at once without slowing orders down; and no test trader on the leaderboard |
| `python3 scripts/bots_check.py [seconds]` | With the bots running: the checks above for any length of time; `1800` for the 30-minute band check before a demo |
| `python3 scripts/time_travel_check.py [minutes] [samples]` | With the bots running: seek times over the last N minutes and "hashes match" against the live engine on all five tickers |
| `make status` | Signs in as admin (`ADMIN_PASSCODE`, default the local placeholder) and prints the gateway's `/v1/system/status` |
| `make test-alerts` | promtool (in Docker) checks the alert rules and runs their unit tests (`observability/tests/`) |
| `make k8s-lint` | The charts without a cluster: `helm lint`, render every environment and every service alone, kubeconform against Kubernetes 1.37, then `test-alerts`. CI runs it on every pull request |
| `make k8s-up` / `k8s-down` / `k8s-status` / `k8s-images` / `k8s-reset` | The Kubernetes deployment: see [§11](#11-kubernetes-kind) |
| `make k8s-bots`, `k8s-bots-stop`, `k8s-demo-trade`, `test-k8s`, `k8s-check c=…` | The Compose targets' Kubernetes twins, and the cluster checks ([§11](#11-kubernetes-kind)) |

> **Not the same as the Terraform target.** `make infra` means *the Compose infrastructure*. The Terraform platform from
> [ADR-0015](adr/0015-terraform-local-platform.md) (kind cluster, namespaces, Argo CD) will be `make platform`; it
> arrives in Phase 6. Until then `make k8s-up` builds the same cluster ([§11](#11-kubernetes-kind)).

## 4. Ports

| Host port | Container port | What |
|---|---|---|
| 5173 | 8080 | `web` (nginx, unprivileged) |
| 8080 | 8000 | `api-gateway` (the public API) |
| 8001–8007 | 8000 | `order-service`, `matching-engine`, `market-data-service`, `portfolio-service`, `bot-traders`, `sre-agent`, `chaos-service` |
| 19092 | 19092 | Redpanda Kafka API for tools on your laptop (inside Docker, services use `redpanda:9092`) |
| 8088 | 8080 | Redpanda Console |
| 9042 | 9042 | Cassandra |
| 5433 | 5432 | Postgres (host port 5433 so a Postgres you installed yourself on 5432 is never confused with this one) |

Every service listens on 8000 inside its container ([system architecture](03-architecture/01-system-architecture.md#container-inventory)).

## 5. Local credentials (development only)

These placeholders exist only in the local Compose setup and are never used outside it. Real environments get their
secrets from Kubernetes Secrets created by Terraform ([ADR-0015](adr/0015-terraform-local-platform.md)).

| What | Login |
|---|---|
| Cassandra administrator (init job only) | `cassandra` / `cassandra` |
| Cassandra role of a service | `<keyspace>_role` / `cb_dev_<keyspace>`, for example `order_service_role` / `cb_dev_order_service`. Allowed on **its own keyspace only**. |
| Postgres administrator | `postgres` / `postgres` |
| Postgres role of portfolio-service | `portfolio_service` / `cb_dev_portfolio`. Owns the `portfolio` database; nobody else can connect to it. |
| Gateway secrets | `JWT_SECRET=cb-dev-jwt-secret-for-local-compose-only`, `BOT_SECRET=cb-dev-bot-secret`, `ADMIN_PASSCODE=cb-dev-admin-passcode` (log in as admin with any nickname and this passcode) |

To run a service outside Docker, copy its `.env.example` to `.env` (git-ignored) and run
`uv run uvicorn <package>.main:create_app --factory --reload` inside its folder.

## 6. How the services are built

All 8 services have the same shape ([coding standards §3](05-coding-standards.md#3-folder-layout-inside-every-service)).
Each file has one of three origins:

| Origin | Files | How it stays right |
|---|---|---|
| **Copied from the template**, identical in every service | `src/<package>/runtime/` (settings, JSON logging, Problem Details, middleware, metrics, health and readiness checks, and since Phase 2 the event envelope and the Kafka publisher/consumer with the dead-letter rule), `Dockerfile`, `.dockerignore`, `.python-version`, the shared tests | `tools/check_template_drift.py` (in `make lint` and CI) fails if any copy differs; `--sync` copies the template's files to every service. See [duplication over coupling](05-coding-standards.md#duplication-over-coupling-why-the-services-share-no-library). |
| **Generated from the OpenAPI spec**, then owned by the service | `src/<package>/api/stubs.py`: one `501` stub per operation | `tests/contract/` compares the app with `api/<service>.yaml`, both ways |
| **Written per service** | `config.py`, `main.py`, `business_metrics.py`, `pyproject.toml`, `tests/conftest.py`, `.env.example` | Rendered by `tools/scaffold.py` from the service table; reviewed like any code |

What every service exposes in Phase 1:

| Endpoint | Behaviour |
|---|---|
| `GET /healthz` | `200 {"status":"ok"}`. Never checks dependencies. |
| `GET /readyz` | `200` when its Redpanda topics exist and its own Cassandra keyspace or Postgres database works; otherwise `503` with the reason per dependency |
| `GET /metrics` | Prometheus: `http_requests_total`, `http_request_duration_seconds`, `http_requests_in_flight`, event counters, process memory, and the business metrics the docs name |
| every operation in its spec | `501` with a Problem Details body (`code: NOT_IMPLEMENTED`) |

Every log line is one JSON object with the `correlation_id`. The gateway issues the ID; every other service passes it
through and returns it in the `X-Correlation-Id` header.

## 7. Resource use (measured)

Measured with `docker stats` on 2026-11-03 (Apple silicon, Docker Desktop with 10 GB), after `make up` from an empty
data state and about a minute of idle. Phase 1 has no business logic, so this is the **floor**; it will grow in later phases.

| Container | Memory used | Limit | Notes |
|---|---:|---:|---|
| cassandra | 1,346 MiB | 2,048 MiB | 512 MB JVM heap plus off-heap memory; peaked at 1,373 MiB while starting |
| redpanda | 150 MiB | 896 MiB | `--memory 512M --smp 1` |
| redpanda-console | 36 MiB | 256 MiB | development tool |
| postgres | 24 MiB | 256 MiB | |
| 7 backend services (each) | 38–40 MiB | 192 MiB | `api-gateway` 40, the other six 38–39 |
| web (nginx) | 10 MiB | 64 MiB | |
| **Whole stack, idle** | **about 1.8 GiB** | | **peak 1.9 GiB** while starting, including the two init jobs |

- **CPU:** under 1% per container when idle.
- **Images:** 90 MB (`web`) and 230–270 MB (each service) on disk.
- **Startup:** `make up` from empty volumes took **32–51 s** with a warm Docker cache.

**Requirement.** Phase 1 needs **at least 4 GB** for Docker (1.9 GiB used plus image builds and the host). We keep the
recommendation and the `make doctor` warning at **10 GB** because Phase 5 runs `staging` and `prod` copies of the data
stores in kind, which will need much more; we will re-measure then. The limits above are the caps in `docker-compose.yml`.

## 8. Adding a new service

1. Write its API in `api/<service>.yaml` (every operation needs `operationId` and `x-seeds-id`) and run `npx @redocly/cli lint`.
2. Add an entry to `SERVICES` in `tools/scaffold.py`: summary, topics, and `cassandra` keyspace or `postgres` database.
3. `python3 tools/scaffold.py <service>`. This creates `services/<service>/`, generates the stubs from the spec, locks
   the dependencies and formats the code.
4. Add its container to `docker-compose.yml` (copy a neighbour; give it the next port, 8008 and up) and to the gateway's
   `SERVICE_UPSTREAMS` if it should appear on the status page.
5. If it owns a Cassandra keyspace, add the keyspace to `dev/cassandra/init-keyspaces.sh`; if it uses new topics, to
   `dev/redpanda/create-topics.sh` and the [event catalog](03-architecture/03-event-catalog.md).
6. Add the service to the CI matrix in `.github/workflows/ci.yml` and to the table in `Makefile` (`SERVICES`).
7. Run `make lint test`, then `make up`.
8. Write `services/<service>/README.md` and update [the data ownership doc](03-architecture/02-data-ownership.md).

## 9. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `make up` seems stuck on `cassandra` | Cassandra is slow to start (first start 30–60 s on a laptop). Its healthcheck has a 60 s grace period and `make up` waits up to 7 minutes. | Wait. Check with `make logs s=cassandra`. |
| Containers exit with code 137, or `docker inspect <name> --format '{{.State.OOMKilled}}'` prints `true` | Docker ran out of memory | Docker Desktop → Settings → Resources → Memory ≥ 10 GB, then `make down && make up` |
| `port is already allocated` or `address already in use` | Another program uses 5173, 8080, 8001–8007, 8088, 9042 or 19092 | Find it with `lsof -nP -iTCP:<port> -sTCP:LISTEN`, stop it, or change the left-hand port in `docker-compose.yml` |
| The portfolio service cannot log in to Postgres from your laptop | You may be reaching your own Postgres on 5432 | Use host port **5433** (`POSTGRES_PORT=5433`) |
| A service stays `starting` or `not_ready` | A dependency is down, or the init jobs failed | `curl localhost:8080/v1/system/status`: the `checks` show which one. `make logs s=redpanda-init` or `s=cassandra-init` for the init jobs. |
| After changing a Cassandra role or password, services cannot log in | The Cassandra data volume still holds the old roles | `make reset` (wipes data) and `make up` |
| `make doctor` says Node is too old although you installed a newer one | An older Homebrew Node formula is still linked | `brew unlink node@20 && brew link --overwrite --force node@24`, then open a new terminal |
| `make lint` fails with `DRIFT` | A file that must be identical in every service was changed in one | Change `services/order-service` (the template) and run `python3 tools/check_template_drift.py --sync`, or revert; see [§6](#6-how-the-services-are-built) |
| `make lint` fails with "still use a retired instrument name" | An old ticker or company name (renamed 2026-11-14) is back in a file or file name | Use the current tickers ([overview §3](00-overview.md#3-the-instruments-stocks)); old names may appear only inside a dated history note fenced by `retired-names:begin YYYY-MM-DD` and `retired-names:end` (`tools/check_retired_names.py`) |
| The pre-commit hook rejects a commit | Lint error, a secret was detected, or `private/` / `CLAUDE.md` was staged | Read the message; `git restore --staged <file>` for the last case |
| The presenter dashboard says "QR points to localhost. Phones can't join." | `PUBLIC_BASE_URL` is not set, or is `localhost` (the Compose default) | [§10](#10-phones-on-a-hotspot-demo-day): set it to the laptop's LAN address and restart `web` |
| A phone cannot open the join address | Not on the same network, the network isolates its clients, or the laptop's firewall blocks Docker | [§10](#10-phones-on-a-hotspot-demo-day), "If a phone cannot connect" |
| Kubernetes: the QR code still shows an old or localhost address | The laptop rejoined the hotspot and got a new address, or the cluster was first started without `PUBLIC_BASE_URL` | `make k8s-status` prints the address in the QR code and the laptop's current one; `make k8s-set-url url=http://<new address>` (data kept) |
| `make k8s-up`: `port is already allocated` for 80, 8090, 3000 or 9090 | Another program on the laptop uses that port | `lsof -nP -iTCP:80 -sTCP:LISTEN`; stop it, then `make k8s-up` again |
| `make k8s-up` stops at "could not download chart" | The chart registry was unreachable (slow network) | Try again; once downloaded, charts are kept in `build/k8s/charts/` and never fetched again |
| `kubectl` prints a version-skew warning | Your kubectl is several versions older than the cluster (1.37) | Harmless; `brew upgrade kubectl` removes it |
| A pod stays `0/1 Running` on Kubernetes | Its `/readyz` fails (a dependency, or a consumer that has not caught up) | `kubectl -n prod logs <pod>`; the readiness body names the failing check: `kubectl -n prod exec <pod> -- python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz')"` |

## 10. Phones on a hotspot (demo day)

Phones join by scanning the QR code on the presenter dashboard. The code encodes `PUBLIC_BASE_URL/join`, so
`PUBLIC_BASE_URL` must be an address **a phone** can reach. `localhost` on the laptop is not one: a phone that opens
`localhost` reaches itself. The containers cannot find the laptop's LAN address for you, so you set it.

**Terms.** A **LAN address** (local area network) is the laptop's address on the network it shares with the phones, for
example `192.168.43.12`. A **hotspot** is a phone sharing its mobile connection over Wi-Fi. **Client isolation** is a
Wi-Fi setting, common on campus and guest networks, that stops devices on the network from reaching each other.

1. **Put the laptop and the phones on one network.** A phone hotspot is the most reliable choice: turn it on and join
   it from the laptop and from the other phones. Classroom Wi-Fi works only if it does not isolate its clients.
2. **Find the laptop's LAN address** on that network:

   | System | Command | Typical hotspot address |
   |---|---|---|
   | macOS | `ipconfig getifaddr en0` (Wi-Fi) | Android hotspot `192.168.43.x`; iPhone hotspot `172.20.10.x` |
   | Linux | `hostname -I` | |
   | Windows | `ipconfig` ("IPv4 Address" of the Wi-Fi adapter) | |

3. **Start the stack with it** (the web container writes it into `/config.js` at start, so there is no rebuild):

   ```bash
   PUBLIC_BASE_URL=http://192.168.43.12:5173 make up
   # stack already running? restart only the web container:
   PUBLIC_BASE_URL=http://192.168.43.12:5173 docker compose up -d web
   ```

   To keep it, put `PUBLIC_BASE_URL=http://192.168.43.12:5173` in a `.env` file at the repository root. Compose reads
   it, and Git ignores it.
4. **Check on the laptop first.**
   - Open `http://192.168.43.12:5173/dashboard`, using the LAN address, not localhost.
   - The join box shows `192.168.43.12:5173/join` and no warning. If it says **"QR points to localhost. Phones can't
     join. Set PUBLIC_BASE_URL."**, step 3 did not take effect: the variable is unset, or it is `localhost`/`127.x`.
   - Then scan the QR code with a phone.
5. **If a phone cannot connect:**
   - Check that the phone is on the same network, not on its mobile data.
   - Open the LAN address in the laptop's own browser. If that fails too, the address is wrong; repeat step 2.
   - On macOS, open System Settings → Network → Firewall. Allow incoming connections for Docker, or turn "Block all
     incoming connections" off.
   - The address can change each time the laptop rejoins the hotspot. Find it again and restart `web` (step 3).

Only port 5173 needs to be reachable. The page calls the API on the same address, and nginx forwards it to the gateway.

**Check a phone's view from the laptop** with `make test-phone url=http://192.168.43.12:5173` (bots trading). Plain
`make test-phone` does the same against `http://cb.test:5173`, a name Chrome is told to send to the laptop. Both are
**not a secure context**, unlike `localhost`, so the page runs exactly as on a phone. A **secure context** is a page
served over https or from localhost; browsers keep some APIs (for example `crypto.randomUUID`) for secure contexts
only. In both skins at 390×844, as test account `load-13` (never on the leaderboard), the check:
- buys 1 CALVERA at market through the order ticket and waits for it in Holdings;
- sends the same order key again and expects no second order;
- taps each F-key tab;
- fails on any console error.

Before 2026-11-14 orders from phones failed silently over the LAN for exactly this reason
([coding standards §6](05-coding-standards.md#6-testing-rules)). If an action fails on a phone, the message says why
(no connection, the exchange's own reason, an exchange error with its HTTP status, or an app error), and the browser
console logs the full error.

## 11. Kubernetes (kind)

The demo platform: the same images on a one-node Kubernetes cluster, with Prometheus, Grafana and Loki. Compose stays
the fast developer loop. Layout and reasoning: [deployment §3–5](03-architecture/07-deployment-and-environments.md#3-kubernetes-with-kind--helm-umbrella-chart).

**Two ways to run it (Phase 6).**

- **GitOps, the demo platform:** `make platform` (Terraform: the cluster, namespaces, Traefik, monitoring, Sealed
  Secrets, Argo CD), and Argo CD deploys `staging` and `prod` from `deploy/envs/` in Git. To change what runs, commit;
  `make sync` makes Argo CD look at once instead of within 30 s; `make argocd` opens its UI on
  <http://localhost:8443>. Before the first run, copy `infra/envs/local/secrets.auto.tfvars.example` to
  `secrets.auto.tfvars` and put Argo CD's read-only deploy key in `private/keys/argocd-deploy-key`; after the first
  run, **back up the sealing key** (`make sealing-key-backup`, then copy it off the laptop). The secrets are random:
  `make k8s-passcode` prints the presenter's passcode.
- **Developing with local builds:** `make k8s-up` in a namespace Argo CD does not manage (with no cluster, it first
  makes one without Argo CD). In a namespace Argo CD manages it deploys nothing: it starts a stopped cluster and
  waits for Argo CD. The commands below work in both.

```bash
make k8s-up          # developing: images + prod; GitOps: start the cluster, wait until Argo CD reports it Synced
make k8s-bots        # the 20 bots trade (seed=, activity= as with make bots)
make k8s-demo-trade  # the demo trade, against the cluster
make k8s-status      # pods, the memory per namespace (the RAM budget's numbers), the QR code's address
make k8s-set-url url=http://192.168.43.12   # point the QR code at the laptop's hotspot address (data kept)
make k8s-down        # stop the cluster: data kept, RAM freed; make k8s-up starts it again in seconds
```

The first `make k8s-up` takes about 7 minutes once the images exist (402 s measured: the platform charts, then
Cassandra's first start), longer when it also builds the 9 images; later ones take seconds to a minute and rebuild only services whose files changed (images are tagged by git SHA, plus a hash of uncommitted
changes). `make k8s-up env=staging` adds staging on port 8090 (about 2 GiB more; it shares the node's CPU with prod).

| URL | What |
|---|---|
| <http://localhost/> | prod: the app, as on <http://localhost:5173> in Compose (`/dashboard`, `/join`, …) |
| <http://localhost:8090/> | staging, if started |
| <http://localhost:3000/> | Grafana: dashboards *CircuitBreaker — Business*, *System*, *Follow an order*. Anyone may look; sign in as `admin` with the password in `infra/envs/local/secrets.auto.tfvars` to change things |
| <http://localhost:9090/> | Prometheus: targets, alert rules (`/alerts`) |

**Phones on Kubernetes.** Exactly as in [§10](#10-phones-on-a-hotspot-demo-day), without the port. **Join the hotspot
first** (the laptop's address belongs to the network it is on), then:

```bash
PUBLIC_BASE_URL=http://192.168.43.12 make k8s-up   # or, cluster already up: make k8s-set-url url=http://192.168.43.12
make k8s-status                                    # ends with "QR code / phones (prod): http://192.168.43.12"
make k8s-test-phone                                # a phone's view of prod, with prod's own bot secret (needs the bots)
```

Phones open `http://192.168.43.12/join` (or scan the QR code). The address is kept through `make k8s-down` /
`k8s-up`, `make k8s-bots`, upgrades and Argo CD syncs (it lives in the ConfigMap `operator-settings`, which Git never
owns); only `PUBLIC_BASE_URL=… make k8s-up` or `make k8s-set-url` change it (`k8s-set-url` restarts only the web pod
and touches no data). `make k8s-status` also warns when the QR code still
says localhost, or when the laptop's current address differs from it (you rejoined the hotspot and got a new one).
Port 80 is open to the network (kind maps it on all interfaces); Grafana and Prometheus are not.

**Checks** (each changes the running system on purpose and puts it back):

| Command | What it proves |
|---|---|
| `make test-k8s` | `k8s-up`, then: every pod ready, the demo trade, the bots for 2 minutes (`bots_check`), the business dashboard's trades/s above 0, one order followed through four services in Loki |
| `make k8s-check c=netpol` | The NetworkPolicies are enforced: allowed connections work, forbidden ones (into another service's data store, around the gateway) fail |
| `make k8s-check c=kill` | An order-service pod deleted while orders flow is replaced, and every accepted order finishes with nothing left reserved |
| `make k8s-check c=scale` | 200 orders/s: order p95 high at 2 order-service replicas, back down at 4 (`scripts/load_orders.py`) |
| `make k8s-check c=alerts` | Each Phase 7 alert's condition is caused for real (about 20 minutes) and the alert fires; `--only over-provisioned` adds the 10-minute one |
| `make k8s-check c=url` | The QR code's address survives a stopped and restarted cluster, `k8s-bots` and a plain `k8s-up`, changes with `PUBLIC_BASE_URL=… k8s-up` and `k8s-set-url`, and a test account's portfolio is unchanged (about 2 minutes) |
| `make k8s-check c=gitops` | Argo CD never undoes the SRE agent: a scale and a restart by hand stay and the Application stays `Synced`; a changed pod template is reported `OutOfSync`, left in place, and `Synced` again after `kubectl rollout undo` (about 6 minutes) |
| `make k8s-check c=gitops-push env=staging` | A commit's way to the cluster, timed: commits and **pushes** a pod annotation on web, waits for `Synced` and `Healthy`, checks an unrelated drifted service is left alone, then `git revert`s and times the way back. Needs a clean tree on the branch Argo CD follows |
| `make k8s-check c=sealed` | Every SealedSecret decrypted; a secret sealed with the right certificate decrypts; one sealed with another key, for the other namespace, or renamed after sealing, does not |
| `make k8s-check c=sealed-drill` | The sealing key's restore drill (needs the backup): the key is lost, an old SealedSecret fails, the backup goes back in, it decrypts again |
| `make k8s-test-phone [env=]` | The phone pages from a non-secure origin (below), against the cluster, with that environment's bot secret |
| `python3 scripts/k8s.py run bots_check.py 120` | Any `scripts/` check against the cluster (port-forwards stand in for Compose's host ports) |

**Follow one order** in Grafana's *Follow an order* dashboard: paste the `X-Correlation-Id` a response returned (or
pick one from *Orders placed*). In Explore, the same query is `{namespace="prod"} | correlation_id="<id>"`.

**Memory** (measured, [deployment §3](03-architecture/07-deployment-and-environments.md#the-ram-budget-measured)):
prod with monitoring uses about 4.3 GiB of process memory on a fresh cluster and about 5.2 GiB after hours of load
tests, within the 5.5 GiB budget (Docker Desktop shows more, including reclaimable page cache); with staging too, about
6.5 GiB. Keep Docker Desktop at 10–11 GB, and run Compose or the cluster, not
both.

**`make k8s-reset`** deletes the cluster and all its data, and Terraform's state with it (it asks first). Like
`make reset`, it is run by a person. Afterwards `make platform` rebuilds everything; with the sealing key backed up,
the SealedSecrets in Git decrypt again on the new cluster.

## Viva Notes

**Q: What does "umbrella project" mean in your repository?**
A: It is our interpretation of the term, not the textbook's: the repository root with `docker-compose.yml` and a
`Makefile` that pulls every service together so the whole system starts with one command. On Kubernetes the same role
is played by the Helm umbrella chart.

**Q: Why do the services return 501 instead of a fake answer or being left out?**
A: Contract first. The skeletons implement the whole API surface from the OpenAPI specs, and a contract test fails if the
app and its spec disagree. A client gets an honest "not built yet" in the standard error format, and each operation
is replaced by real logic without changing its URL.

**Q: Why one Cassandra keyspace and one login per service, if there is only one Cassandra container?**
A: A shared server is a cost decision; a shared database is an architecture decision. Each service's role can use only
its own keyspace, which we tested: a role writing to another service's keyspace is refused. So the data-ownership rule is
enforced by the database, not just by convention.

**Q: Why multi-stage Dockerfiles and a non-root user?**
A: The build stage has the package manager and build tools; the final image only has Python and the installed
environment, so it is smaller and has less to attack. Running as a non-root user means a compromised process cannot
change the container or reach host resources as root.

**Q: What did you measure, and what did it change?**
A: With `docker stats` we measured about 1.8 GiB for the whole idle stack. Cassandra peaked at 1.37 GiB of its 1.5 GiB
limit, which was too close, so we raised its limit to 2 GiB. We kept the 10 GB recommendation because Phase 5 adds more
data stores.
