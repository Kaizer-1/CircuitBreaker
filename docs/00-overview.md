# 00 — Overview

> **Read this first.** It explains what CircuitBreaker is, what is in and out of scope,
> and defines every technical term used anywhere in the documentation (see the [Glossary](#glossary)).

## 1. The problem

Microservices courses usually teach architecture with toy examples (a to-do list, a bookshop). Toy examples
never *fail* in interesting ways, so the hardest and most important part of running microservices —
**operating them when things go wrong** — is never shown.

We want a system that is:

1. **Fun and live**: the examiners and classmates can join it from their phones during the demo.
2. **Genuinely distributed**: several services, an event bus, separate databases, real Kubernetes.
3. **Breakable on purpose**: we can inject realistic failures live.
4. **Self-healing**: an AI agent notices the failure, explains what it thinks happened, and fixes it.

## 2. The vision

**CircuitBreaker is a self-healing simulated stock exchange.**

- **Human traders** scan a **QR code** on the projector, pick a nickname, and receive **₹1,00,000** of
  virtual money. They buy and sell five fictional company stocks from their phones.
- **20 bot traders** trade automatically so the market is always moving, even with no humans.
- A **matching engine** pairs buy and sell orders fairly using **price-time priority**
  (best price first; at the same price, whoever came first).
- **Live candlestick charts** and an **order book** update in real time.
- Every order and trade is stored as an **event** (event sourcing), so we can **time-travel**: rebuild
  and replay the market exactly as it was at any past moment.
- A **Chaos Panel** lets the presenter break the system: a bad deploy, a memory leak, or a traffic spike.
- An **AI SRE Agent** watches metrics, logs and deploy history, diagnoses the failure and fixes it using a
  small, fixed, safe set of tools: **rollback**, **scale** (max 5 replicas), **restart**, **do_nothing**.
  It explains every decision in plain English on a live **incident feed**.

> **About the name.** "Circuit breaker" has two meanings that both fit: on stock exchanges it is a rule that
> halts trading when prices crash; in software it is a resilience pattern that stops calling a failing
> dependency. Our project is about resilience, so the second meaning is the one we build. A trading halt
> is listed as *out of scope* below.

## 3. The instruments (stocks)

All five companies are fictional. Prices are in Indian Rupees (INR). Each "lot" is 1 share.

| Ticker | Company | Reference price | Business |
|---|---|---|---|
| `CALVERA` | Calvera Consumer Ltd | ₹245.50 | Packaged foods and beverages |
| `KORVIN` | Korvin Technologies Ltd | ₹1,180.00 | IT services and software |
| `ASTRYX` | Astryx Logistics Ltd | ₹412.75 | Warehousing and express freight |
| `HELIOR` | Helior Power Ltd | ₹88.20 | Solar and hydro power producer |
| `TAVIRA` | Tavira Media Ltd | ₹530.05 | Film, streaming and broadcast |

**Why these names.** The demo crashes a stock on a projector, so the companies must look credible on an exchange
screen and must not be mistaken for a real listed company. Each name is invented, one per sector, and at most 8
characters (the widest the screens are laid out for). Before choosing them we checked that none is a real symbol or a
well-known company: NSE by exact match against its official equity and SME symbol lists, BSE through its official
scrip search (active and suspended securities by name, symbol and code), and the web for real companies with the
same or a close name. BSE's delisted securities were not fully checked (only the first page of that list).

<!-- retired-names:begin 2026-11-14 -->
> **Amended 2026-11-14 (reference data, within v1).** The instruments were renamed from playful names to the
> professional ones above, before any release, keeping each slot's reference price: `CHAIWALA` (Chaiwala Beverages
> Ltd) → `CALVERA`, `JUGAAD` (Jugaad Technologies Ltd) → `KORVIN`, `DABBA` (Dabba Logistics Ltd) → `ASTRYX`,
> `MONSOON` (Monsoon Power Ltd) → `HELIOR`, `FILMY` (Filmy Studios Ltd) → `TAVIRA`. Reason: the playful names looked
> unprofessional in the final presentation. The API versions did not change (see the changelog in `api/README.md`);
> `tools/check_retired_names.py` (in `make lint` and CI) fails if an old name appears outside a dated note like this.
<!-- retired-names:end -->

The **reference price** is the starting price used before any trade has happened.

## 4. Market rules

| Rule | Value | Why |
|---|---|---|
| Currency | INR, stored as integer **paise** (₹1 = 100 paise) | Avoids floating-point rounding errors with money ([ADR-0012](adr/0012-prices-in-integer-paise.md)) |
| Tick size | ₹0.05 (5 paise) | Every limit price must be a multiple of 5 paise, like real Indian exchanges |
| Lot size | 1 share | Keeps maths simple for phone users |
| Order types | `LIMIT`, `MARKET` | The two order types every trader understands |
| Partial fills | Allowed | An order for 100 shares may fill as 30 + 70 |
| Cancellation | Allowed for any unfilled remainder | Traders must be able to change their minds |
| Short selling | **Not allowed** — you can only sell shares you own | Removes the need for borrowing logic |
| Margin | **Not allowed** — you can only spend cash you have | Removes the need for credit/liquidation logic |
| Market-order protection | A `MARKET` buy will not trade above last price + 10%; a `MARKET` sell will not trade below last price − 10%. Any unfilled remainder is cancelled. | Stops a market order on an empty book from trading at a crazy price, and lets us reserve a known amount of cash |
| Trading hours | Continuous, always open, no auctions | Demo can start any time |
| Order lifetime | Until filled or cancelled ("good till cancelled" within one demo session) | No expiry logic needed |
| Starting money | Humans: ₹1,00,000 cash, no shares. Bots: cash **and** shares | Bots are the initial sellers (*liquidity providers*); without them humans could only buy from each other |

## 5. Scope

### In scope

- The 8 backend services and the `web` frontend listed in [§6](#6-the-services-at-a-glance).
- Price-time-priority matching for `LIMIT` and `MARKET` orders with partial fills and cancellation.
- Event sourcing of the market; time-travel snapshot ("what did the market look like at 10:42:15?")
  and replay ("play the last 5 minutes at 10× speed").
- CQRS read models: portfolio, leaderboard, candles, order-book depth.
- Three chaos scenarios and an AI SRE agent with a fixed tool allowlist and a human-approval toggle.
- Docker Compose for local development; kind + Helm umbrella chart with `staging` and `prod` namespaces.
- Prometheus, Grafana and Loki for observability.
- Terraform for the local platform (kind cluster, namespaces, Argo CD), GitHub Actions CI and Argo CD GitOps
  deployment; LocalStack (simulated AWS) optional.

### Out of scope (deliberately)

| Not doing | Reason |
|---|---|
| Real money, real stock data, real brokers | Legal and ethical reasons; this is a simulation |
| User accounts with passwords, KYC | Joining must take 10 seconds on a phone; a nickname is enough |
| Short selling, margin, derivatives, stop-loss orders | Each adds a large new domain with no extra architecture learning |
| Exchange trading halts ("market circuit breakers") | Interesting, but a business feature, not an architecture lesson |
| Real cloud (AWS/GCP/Azure) | Costs money; kind + Terraform + Argo CD reproduce the textbook's cloud setup locally for free |
| mTLS / service mesh between services | Valuable in production but adds a lot of complexity; we rely on Kubernetes network policies |
| Agent tools beyond rollback/scale/restart/do_nothing | Safety: see [ADR-0009](adr/0009-sre-agent-fixed-tool-allowlist.md) |

## 6. The services at a glance

| Service | One-line responsibility | Owner |
|---|---|---|
| `api-gateway` | Single public entry point: sessions/JWTs, routing, rate limiting, WebSocket proxy | API Team (MD Asif Ali) |
| `order-service` | Validates orders, reserves money/shares, tracks each order's status | Trading (Kaizer Dewaswala) |
| `matching-engine` | Keeps the order books, matches orders by price-time priority, writes the market event store | Trading (Kaizer Dewaswala) |
| `market-data-service` | Builds candles, order-book depth and trade lists; streams them live; time travel and replay | Market Insights (MD Asif Ali) |
| `portfolio-service` | Trader accounts, cash, holdings, reservations, leaderboard | Market Insights (MD Asif Ali) |
| `bot-traders` | Runs 20 automated traders with 4 strategies | Trading (Kaizer Dewaswala) |
| `sre-agent` | Detects incidents, asks an LLM (via OpenRouter) to diagnose, executes one allowlisted fix, explains it | Kaizer Dewaswala (SRE agent role, with Release & Reliability) |
| `chaos-service` | Runs chaos experiments: bad deploy, memory leak, traffic spike | Release & Reliability (Mohammad Oweis) |
| `web` | React + Vite single-page app: trader view, presenter view, chaos panel, incident feed | Trading (Kaizer Dewaswala) |

How we arrived at exactly these services is explained in
[02-domain/03-service-sizing.md](02-domain/03-service-sizing.md).

## 7. How to read the documentation

| If you want to know... | Read |
|---|---|
| Who uses the system and what they need | [01-seeds/](01-seeds/01-actors.md) (SEED(S) process) |
| How we found the service boundaries | [02-domain/](02-domain/01-event-storming.md) |
| How the pieces fit and talk | [03-architecture/](03-architecture/01-system-architecture.md) |
| Exact event and API contracts | [03-event-catalog.md](03-architecture/03-event-catalog.md), [api/](../api/README.md) |
| Who owns what | [04-team-topology.md](04-team-topology.md) |
| How we write code | [05-coding-standards.md](05-coding-standards.md) |
| Why each big decision was made | [adr/](adr/) |
| Where each syllabus topic is covered | [06-syllabus-mapping.md](06-syllabus-mapping.md) |
| What gets built when | [07-roadmap.md](07-roadmap.md) |

## Glossary

Terms are grouped by topic. Every document uses these exact meanings.

### Trading terms

| Term | Meaning |
|---|---|
| **Ticker** | Short code for a stock, e.g. `CALVERA`. |
| **Instrument** | Anything that can be traded; here, one of the five stocks. |
| **Order** | An instruction to buy or sell a quantity of a ticker. |
| **Side** | `BUY` or `SELL`. |
| **LIMIT order** | "Buy/sell at this price or better." If it cannot trade immediately, it *rests* in the order book. |
| **MARKET order** | "Buy/sell now at the best available price." Never rests in the book; any unfilled part is cancelled. |
| **Order book** | For one ticker, the list of resting buy orders (**bids**) and sell orders (**asks**), sorted by price. |
| **Bid / Ask** | A resting buy order / a resting sell order. |
| **Best bid / best ask** | The highest bid / the lowest ask. The gap between them is the **spread**. |
| **Depth** | How much quantity is waiting at each price level. "Level 2" data shows depth per price. |
| **Price-time priority** | Matching rule: better price first; at the same price, the earlier order first (first-in-first-out). |
| **Match / Trade / Fill** | When a buy and a sell agree on price, a trade happens. From one order's point of view that trade is a *fill*. |
| **Partial fill** | Only part of an order's quantity traded; the rest is still open. |
| **Aggressor** | The incoming order that caused a trade by crossing the spread. The trade price is the resting order's price. |
| **Resting order** | An order waiting in the book. |
| **Tick size** | Smallest allowed price step: ₹0.05. |
| **Paise** | 1/100 of a rupee. All prices are stored as integer paise (₹245.50 = `24550`). |
| **Protection price** | The worst price a `MARKET` order may trade at (last price ± 10%). |
| **Reservation** | Money (for buys) or shares (for sells) set aside when an order is accepted, so it cannot be spent twice. |
| **Settlement** | Moving cash and shares between buyer and seller after a trade. |
| **Short selling** | Selling shares you do not own. *Not allowed.* |
| **Margin** | Borrowing money to trade. *Not allowed.* |
| **Liquidity / liquidity provider** | Liquidity = how easy it is to trade without moving the price. Our market-maker bots provide it by always quoting bids and asks. |
| **Candlestick (candle)** | Summary of trades in a time interval: **O**pen, **H**igh, **L**ow, **C**lose price and volume (OHLCV). |
| **Last price / last traded price (LTP)** | Price of the most recent trade. |
| **Leaderboard** | Ranking of traders by net worth = cash + (shares × last price). |
| **Market maker** | A trader (here a bot) that continuously offers to both buy and sell. |

### Microservices and architecture terms

| Term | Meaning |
|---|---|
| **Microservice** | An independently deployable component of bounded scope that supports interoperability through message-based communication (Nadareishvili et al., *Microservice Architecture*, O'Reilly, 2016). **Why we use them**, in the textbook's framing: a microservices architecture exists primarily to **minimise coordination costs** between teams in a complex, multi-team environment, balancing **speed and safety at scale** (Mitra & Nadareishvili, Ch. 4, restating Ch. 1). In our design each one owns one business capability and its data. |
| **Monolith** | One deployable unit containing all features. |
| **Distributed monolith** | Services that are split physically but must be changed and deployed together — the worst of both worlds. |
| **API (Application Programming Interface)** | The contract a piece of software exposes to others. An API is an *interface*; a microservice is a *deployable unit* that may expose one. |
| **Coordination cost** | Time and effort teams spend agreeing, waiting and synchronising with each other. Microservices aim to reduce it. |
| **Independent deployability** | A service can be released without releasing any other service at the same time. |
| **Data ownership** | Exactly one service can write a given piece of data; others ask it or listen to its events. |
| **Bounded context** | A DDD term: a boundary inside which a model and its words have one precise meaning. |
| **DDD (Domain-Driven Design)** | Designing software around the business domain and its language. |
| **Ubiquitous language** | The shared vocabulary used by developers and domain experts inside a bounded context. |
| **Aggregate** | A cluster of objects treated as one unit for changes, protected by rules called invariants (e.g. an `OrderBook`). |
| **Invariant** | A rule that must always be true (e.g. "available cash is never negative"). |
| **Context map** | A diagram of bounded contexts and how they relate. |
| **Upstream / downstream** | In a context map, the upstream context provides a capability; the downstream one consumes it. |
| **Customer–Supplier, Conformist, Anti-Corruption Layer (ACL), Open Host Service (OHS), Shared Kernel, Separate Ways** | Context-mapping relationship types: the supplier serves a customer and keeps compatibility; a conformist adopts the upstream's model as-is; an ACL translates the upstream's model into your own; an OHS publishes one standard interface for all consumers; a shared kernel is a jointly owned piece of model (avoid it); separate ways means no integration. Defined in [bounded contexts](02-domain/02-bounded-contexts.md#context-map). |
| **Event Storming** | A lightweight, DDD-inspired workshop (Alberto Brandolini) that finds bounded contexts in hours. Five phases in the textbook's plan: discover events → enforce the timeline (with hotspots) → reverse narrative to find commands → group around aggregates into bounded contexts → competitive analysis (Mitra & Nadareishvili, Ch. 4). |
| **Hotspot** | In Event Storming, a sticky note (purple) recording an open question or conflict, answered after the session. |
| **Reverse narrative** | Walking the Event Storming timeline backwards, asking what *caused* each event, to find the commands. |
| **Competitive analysis** | Ranking bounded contexts by difficulty and competitive edge (S/M/L) to decide what to build in-house, buy, or give to learners (Ch. 4, credited to Greg Young). |
| **Domain event** | Something that happened in the business, named in the past tense (`TradeExecuted`). |
| **Integration event** | A domain event published on the event bus for other services. |
| **Local event** | A domain event stored only inside its own service (not published). |
| **Command** | A request to do something (`PlaceOrder`); it may be rejected. |
| **Policy** | A rule "whenever event X happens, do command Y" (e.g. "whenever a trade executes, settle it"). |
| **Read model / projection** | Data reshaped specifically for answering queries, built by processing events. |
| **SEED(S)** | **Seven Essential Evolutions of Design for Services**: a seven-step, outside-in way to design services and their APIs — identify actors → identify jobs to be done (JTBDs) → sequence diagrams → derive actions and queries → specify them with an open standard (OpenAPI / AsyncAPI) → get feedback on the spec → implement the microservices. Steps as listed in our course syllabus. |
| **Actor** | Anyone or anything that uses the system to get a job done. |
| **JTBD (Job To Be Done)** | What an actor is trying to achieve, written as "When ___, I want to ___, so I can ___". |
| **Action / Query** | An action changes state (a command); a query only reads state. |
| **Universal Sizing Formula** | The textbook's three principles for sizing microservices: (1) start with just a few, possibly one per bounded context; (2) keep splitting as the system grows, guided by avoiding coordination; (3) stay on a trajectory of decreasing coordination — more important than "perfect" sizing now (Mitra & Nadareishvili, Ch. 4). See [service sizing](02-domain/03-service-sizing.md). |
| **Team Topologies** | A model of four team types (stream-aligned, platform, enabling, complicated-subsystem) and three interaction modes (collaboration, X-as-a-Service, facilitating). |
| **ADR (Architecture Decision Record)** | A short document recording one decision: context, decision, alternatives, consequences. |
| **C4 model** | A way of drawing architecture at four zoom levels: Context, Container, Component, Code. |
| **Synchronous call** | The caller waits for the answer (HTTP request/response). |
| **Asynchronous messaging** | The sender publishes a message and carries on; receivers process it later. |
| **Saga** | A business process spread across services, coordinated by events, with *compensating* steps to undo work if a later step fails. |
| **Idempotent** | Doing the same operation twice has the same effect as doing it once. Essential when messages can be delivered twice. |
| **API gateway** | The single front door that routes external requests to internal services. |
| **JWT (JSON Web Token)** | A signed token proving who the caller is; the gateway issues and verifies it. |
| **WebSocket** | A long-lived two-way connection used to push live updates to the browser. |
| **OpenAPI** | A standard YAML/JSON format for describing HTTP APIs. We use version 3.1. |
| **Problem Details** | Standard JSON error format (RFC 9457) with `type`, `title`, `status`, `detail`. |
| **Correlation ID** | An ID attached to a request and every message it causes, so logs across services can be joined up. |

### Data terms

| Term | Meaning |
|---|---|
| **Event sourcing** | Storing every change as an immutable event and deriving current state by replaying events, instead of overwriting rows. |
| **Event store** | The database holding those events, append-only. Ours is Cassandra. |
| **Snapshot** | A saved copy of state at a given event sequence number, so replay can start from it instead of from the beginning. |
| **Sequence number** | A per-ticker counter that gives every market event a strict order. |
| **Replay** | Re-applying stored events to rebuild state. |
| **Time travel** | Rebuilding the market's state as it was at a past timestamp by replaying events up to that time. |
| **CQRS (Command Query Responsibility Segregation)** | Using different models (and often stores) for writing (commands) and for reading (queries). |
| **Write side / read side** | In CQRS: the part that accepts commands and emits events / the part that answers queries from projections. |
| **Eventual consistency** | Read models may lag a little behind the write side but will catch up. |
| **Cassandra** | A distributed, write-optimised NoSQL database; good for append-only time-ordered data. |
| **Keyspace** | Cassandra's equivalent of a database/schema. Each service gets its own. |
| **Partition key / clustering key** | In Cassandra, the partition key decides which node stores a row; the clustering key decides the sort order inside the partition. |
| **Postgres (PostgreSQL)** | A relational SQL database with strong transactions. |
| **Transaction** | A group of database changes that all happen or none happen. |

### Messaging terms

| Term | Meaning |
|---|---|
| **Event bus / message broker** | Middleware that receives messages from producers and delivers them to consumers. |
| **Redpanda** | A Kafka-compatible event streaming platform (single binary, no ZooKeeper/JVM). |
| **Kafka** | The popular event streaming platform whose protocol Redpanda implements. |
| **Topic** | A named stream of messages, e.g. `matching.v1`. |
| **Partition** | A topic is split into partitions; order is guaranteed only within one partition. |
| **Message key** | Decides the partition; we key market messages by ticker so one ticker's events stay in order. |
| **Producer / consumer** | Service that writes messages / reads messages. |
| **Consumer group** | Consumers sharing the work of reading a topic; each partition goes to one member. |
| **Offset** | A consumer's position in a partition. |
| **Consumer lag** | How many messages a consumer is behind. |
| **At-least-once delivery** | Every message is delivered, possibly more than once (so consumers must be idempotent). |
| **DLQ (Dead-Letter Queue)** | A topic where messages that repeatedly fail processing are parked for inspection. |
| **Envelope** | The standard outer fields (ID, type, time, correlation) wrapped around every event's data. |

### Platform, DevOps and SRE terms

| Term | Meaning |
|---|---|
| **Container / image** | An image is a packaged app plus its dependencies; a container is a running instance of it. |
| **Docker / Docker Compose** | Tool to build and run containers / tool to run several containers together from one YAML file. |
| **Kubernetes (K8s)** | A container orchestrator: runs, scales, restarts and updates containers across machines. |
| **kind (Kubernetes IN Docker)** | Runs a whole Kubernetes cluster inside Docker containers on a laptop. |
| **Pod / Deployment / ReplicaSet** | Pod = one or more containers scheduled together. Deployment = desired state for a set of identical pods. ReplicaSet = the set of pods for one *revision* of a Deployment. |
| **Replica** | One copy of a pod. "Scale to 3" = run 3 replicas. |
| **Namespace** | A named partition of a Kubernetes cluster; we use `staging` and `prod`. |
| **Rollout / revision / rollback** | A rollout replaces pods with a new version (a new revision); a rollback returns to the previous revision. |
| **Liveness / readiness probe** | Kubernetes health checks: "is it alive?" (restart if not) / "can it take traffic?" (route to it only if yes). |
| **Helm / chart / umbrella chart** | Helm is the Kubernetes package manager; a chart is a package; an umbrella chart is a parent chart that deploys many sub-charts together. |
| **RBAC (Role-Based Access Control)** | Kubernetes permissions: which identity may do which action on which resource. |
| **Network policy** | Kubernetes firewall rules between pods. |
| **DevOps** | Culture and practices where the people who build software also deliver and run it, with heavy automation. |
| **CI / CD** | Continuous Integration (build and test every change) / Continuous Delivery (every change can be released automatically). |
| **GitHub Actions** | GitHub's built-in CI/CD service. |
| **IaC (Infrastructure as Code)** | Describing infrastructure in versioned code files instead of clicking in consoles. |
| **Terraform** | An IaC tool; you declare the desired infrastructure and it works out the changes. |
| **Terraform module / provider** | A module is a reusable folder of Terraform code with inputs and outputs; a provider is the plug-in Terraform uses to talk to one platform (kind, Kubernetes, Helm, AWS). |
| **GitOps** | Git is the source of truth for what should run; a tool in the cluster keeps the running system matching Git and reports **drift** (live state that differs from Git). |
| **Argo CD** | The GitOps deployment server we use (also the textbook's choice): it watches `deploy/` in Git and *pulls* changes into Kubernetes. Its words: **sync** (make the cluster match Git), **`Synced` / `OutOfSync`**. |
| **Reconcile through Git** | After an emergency fix in the cluster, make Git say the same thing again, so GitOps doesn't re-apply the problem. Our rollback does it with a `git revert` pull request: "mitigate fast, reconcile through Git" (ADR-0016). |
| **Promotion (commit)** | Releasing to prod by copying an image tag that already passed staging into `deploy/envs/prod/`, through an approved pull request. |
| **LocalStack** | A tool that emulates AWS services locally. **Optional** in our design (see ADR-0015). |
| **Staging / production (prod)** | Staging = a production-like environment to test releases; prod = where "real" users (our demo audience) are. |
| **Observability** | Being able to understand a system's internal state from its outputs: metrics, logs, traces. |
| **Prometheus** | Collects numeric **metrics** by scraping each service's `/metrics` endpoint; also evaluates **alert rules**. |
| **Grafana** | Dashboards for metrics and logs. |
| **Loki** | Log storage and search, designed to work with Grafana. |
| **Grafana Alloy** | Agent that collects container logs and ships them to Loki. |
| **Golden signals / RED** | Key service health metrics: **R**ate (requests/sec), **E**rrors, **D**uration (latency). Plus saturation. |
| **p95 latency** | 95% of requests are faster than this value. |
| **SRE (Site Reliability Engineering)** | Applying software engineering to operations: automation, SLOs, incident response. |
| **Incident** | An unplanned event that hurts service; we open one when an alert fires. |
| **Remediation** | The action taken to fix an incident. |
| **MTTR (Mean Time To Recovery)** | Average time from failure to recovery. |
| **DORA metrics** | Four delivery metrics: deployment frequency, lead time for changes, change failure rate, time to restore service. |
| **Chaos engineering** | Deliberately injecting failures to test and improve resilience. |
| **Chaos experiment** | One run of a chaos scenario, with a start, an end and observed results. |
| **LLM (Large Language Model)** | An AI model that reads and writes text. The SRE agent uses one through OpenRouter (default: a Claude Sonnet model). |
| **OpenRouter** | A hosted gateway that exposes many LLMs (Claude, GPT, Llama, …) behind one OpenAI-compatible API and one API key. |
| **Tool calling (function calling)** | The LLM asks our code to run a named function with JSON arguments instead of replying in free text. |
| **Tool allowlist** | The fixed list of tools the agent may call; anything else is impossible, not just discouraged. |
| **Human-in-the-loop / approval toggle** | When on, a human must approve the agent's proposed action before it runs. |
| **Cooldown** | A waiting period after an action during which the agent may not act again on the same service. |
| **Blast radius** | How much of the system an action could damage if it goes wrong. |

## Viva Notes

**Q: In one sentence, what is CircuitBreaker?**
A: A self-healing simulated stock exchange built as microservices, where humans and bots trade live, every
market change is stored as an event so we can time-travel, and an AI SRE agent diagnoses and fixes injected
failures using only four safe tools.

**Q: Why build a stock exchange for a microservices project?**
A: It naturally has separate business capabilities (orders, matching, market data, portfolios) with
different scaling needs and data, it has a natural event stream (orders and trades) that suits event
sourcing and CQRS, and it is live and fun for a demo.

**Q: Why are prices stored as integer paise?**
A: Floating-point numbers cannot represent values like 0.05 exactly, so sums drift (0.1 + 0.2 ≠ 0.3).
Integers are exact, and a tick-size check becomes simply `price_paise % 5 == 0`.

**Q: Why do bots start with shares but humans don't?**
A: Short selling is not allowed, so someone must own shares on day one or nobody could ever sell.
Market-maker bots are seeded with inventory and act as liquidity providers.

**Q: What is explicitly out of scope and why?**
A: Real money, real cloud, passwords/KYC, short selling, margin and trading halts. Each adds business
complexity without teaching more microservices architecture, and real cloud costs money.
