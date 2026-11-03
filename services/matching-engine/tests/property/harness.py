"""Runs a workload through one order book per ticker and records what happened, step by step."""

from dataclasses import dataclass, field

from matching_engine.domain.book import OrderBook
from matching_engine.domain.model import Command, Decision, MarketEvent

from tests.builders import id_source
from tests.property.workload import TICKERS, Action, Ledger, Workload

NOW = "2026-11-20T10:42:01.250Z"


@dataclass
class Step:
    command: Command
    decision: Decision
    best_bid: int | None
    best_ask: int | None
    remaining_before: dict[str, int]  # resting order id -> remaining, before this command


@dataclass
class Trace:
    steps: list[Step] = field(default_factory=list)
    books: dict[str, OrderBook] = field(default_factory=dict)
    ledger: Ledger = field(default_factory=Ledger)
    ledger_problems_per_step: list[list[str]] = field(default_factory=list)

    @property
    def events(self) -> list[MarketEvent]:
        return [e for s in self.steps for e in s.decision.events]


def run(workload_actions: list[Action]) -> Trace:
    workload = Workload()
    trace = Trace(books={t: OrderBook(t) for t in TICKERS}, ledger=workload.ledger)
    new_id = id_source()
    for action in workload_actions:
        command = workload.command_for(action)
        if command is None:
            continue
        book = trace.books[command.ticker]
        before = {
            o.order_id: o.remaining_quantity for lv in book.bids() + book.asks() for o in lv.orders
        }
        decision = book.decide(command, new_id=new_id, now=NOW)
        for event in decision.events:
            book.apply(event)
            trace.ledger.apply(event)
        trace.steps.append(Step(command, decision, book.best_bid(), book.best_ask(), before))
        trace.ledger_problems_per_step.append(trace.ledger.negative_or_over_reserved())
    return trace
