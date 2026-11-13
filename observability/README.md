# observability — dashboards and alert rules as code

Everything Grafana shows and everything Prometheus alerts on is a file here, reviewed in pull requests like code
([ADR-0008](../docs/adr/0008-observability-stack.md)). `charts/observability` installs it into the cluster (it links
to these folders); `make k8s-up` installs that chart.

| Folder | What | Installed as |
|---|---|---|
| `dashboards/` | Grafana dashboards (JSON): **business** (the circuit breaker first, then orders, trades, latency, consumer lag, bots, time travel), **system** (pods, CPU and memory, restarts, replicas, golden signals per service), **follow-an-order** (Loki: every log line of one correlation ID) | ConfigMaps labelled `grafana_dashboard=1`, loaded by Grafana's sidecar into the folder *CircuitBreaker* |
| `alerts/` | Prometheus alert rules for the Phase 7 SRE agent: `services.yaml` (error rates, latency, over-provisioning, circuit breaker, memory growth), `platform.yaml` (crash loops, consumer lag, dead letters) | PrometheusRule objects |
| `tests/` | promtool unit tests of the rules: each alert quiet before its condition, firing 20–40 s after it; normal trading, an idle service and the data stores fire nothing | Run by `make test-alerts` and CI |

**Rules for changing them**

- Edit a dashboard in Grafana (signed in as admin), then *Export → JSON* and replace the file; or edit the JSON. The
  data source uids are fixed: `prometheus` and `loki`. Colours follow the web app's Terminal skin (amber `#ffa028`,
  green `#45d07a`, red `#ff5c5c`, neutrals `#e8e3d6`, `#a39d90`, `#8c867a`); business counts are `orders/s` and
  `trades/s`, never `req/s`.
- An alert's labels are what the agent reads: `service` (the Deployment it may act on), `severity`, `remediable`
  (`"false"` = escalate, never act). Never point a `remediable` alert at a data store.
- Every rule change needs a test in `tests/alerts_test.yaml`; `make test-alerts` must pass. The timing, thresholds and
  the reasons are in the [chaos scenarios §5](../docs/03-architecture/06-chaos-scenarios.md#5-alert-rules-prometheus-used-by-the-agent).
- `make k8s-check c=alerts` causes each alert's condition on the running cluster and times it.

## Viva Notes

**Q: Why keep dashboards and alerts in Git rather than clicking them together in Grafana?**
A: They are reviewed and versioned like code, a fresh cluster gets exactly the same ones, and an alert that the SRE
agent acts on is as important as any code path, so it deserves a test.

**Q: How do you test an alert without breaking production?**
A: Two ways: promtool feeds synthetic metrics to the rules and checks when they fire (in CI), and `make k8s-check
c=alerts` causes the real condition on the local cluster (a stopped service, an overload, a growing process) and
times the alert, then puts everything back.

**Q: Why must Loki labels stay low-cardinality?**
A: Loki keeps one stream per combination of label values and indexes only labels. Four labels with a handful of
values each (namespace, app, container, level) make a few dozen streams; a label per order or per request would make
millions and overwhelm it. The correlation ID is stored with each line (structured metadata) and filtered at query
time instead.
