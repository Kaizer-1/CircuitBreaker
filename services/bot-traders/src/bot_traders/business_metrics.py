"""bot-traders' own Prometheus metrics (listed in its README)."""

from dataclasses import dataclass

from prometheus_client import CollectorRegistry, Counter, Gauge


@dataclass(frozen=True)
class BusinessMetrics:
    bot_orders_total: Counter
    bots_active: Gauge


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
    )
