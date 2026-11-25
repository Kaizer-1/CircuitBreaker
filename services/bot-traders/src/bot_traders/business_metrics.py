"""bot-traders' own Prometheus metrics (listed in its README)."""

from dataclasses import dataclass

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


@dataclass(frozen=True)
class BusinessMetrics:
    bot_orders_total: Counter
    bots_active: Gauge
    bot_order_lateness_seconds: Histogram
    bot_quote_gap_seconds: Histogram
    bot_panic_requests_total: Counter

    def timing(self, kind: str, strategy: str, seconds: float) -> None:
        """The bots' own timing (application/bot.py TimingListener)."""
        histogram = (
            self.bot_order_lateness_seconds if kind == "lateness" else self.bot_quote_gap_seconds
        )
        histogram.labels(strategy).observe(max(seconds, 0.0))


def register_business_metrics(registry: CollectorRegistry) -> BusinessMetrics:
    return BusinessMetrics(
        bot_orders_total=Counter(
            "bot_orders_total",
            "Bot order actions by strategy and result: placed, cancelled, skipped (held back by the"
            " bot's own ledger) and refused (the exchange said no; must stay 0).",
            ["strategy", "result"],
            registry=registry,
        ),
        bots_active=Gauge("bots_active", "Bots signed in and trading.", registry=registry),
        bot_order_lateness_seconds=Histogram(
            "bot_order_lateness_seconds",
            "How much later than planned a bot order went out: a starved event loop, or slow"
            " answers to the orders sent before it in the same interval.",
            ["strategy"],
            buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
            registry=registry,
        ),
        bot_quote_gap_seconds=Histogram(
            "bot_quote_gap_seconds",
            "How long a side had no resting order of a bot after the exchange filled its last one,"
            " until the bot placed a new one (market makers: time without a quote).",
            ["strategy"],
            buckets=(0.5, 1, 2, 3, 4, 5, 6, 8, 10, 15, 30),
            registry=registry,
        ),
        bot_panic_requests_total=Counter(
            "bot_panic_requests_total",
            "The panic sell's requests (chaos TRAFFIC_SPIKE) by kind (SELL, GET) and answer: the"
            " HTTP status, no_answer, or dropped (not sent: too many unanswered). Shows the rate"
            " offered against the rate order-service answered; never read by the SRE agent.",
            ["kind", "result"],
            registry=registry,
        ),
    )
