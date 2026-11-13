"""Example tests: commands the engine must refuse, and which state a refused command leaves behind.

order-service validates orders first, so these only arrive through a bug or a hand-made message.
The engine still refuses them (defence in depth): it raises InvalidCommandError, produces no events,
and the consumer sends the message to orders.v1.dlq after 5 attempts.
"""

import dataclasses

import pytest

from matching_engine.domain.model import InvalidCommandError, Outcome, PlaceOrder
from tests.builders import ALICE, BOB, Market, cancel, expire, limit, market, oid


def refused_with_no_change(m: Market, command: object, match: str) -> None:
    before = m.book.state_hash()
    with pytest.raises(InvalidCommandError, match=match):
        m.submit(command)  # type: ignore[arg-type]
    assert m.book.state_hash() == before


def test_a_limit_price_off_the_5_paise_tick_is_refused() -> None:
    refused_with_no_change(Market(), limit(1, ALICE, "BUY", 10, 24603), "tick")


def test_a_limit_price_below_one_tick_is_refused() -> None:
    refused_with_no_change(Market(), limit(1, ALICE, "BUY", 10, 0), "tick")


def test_a_limit_order_without_a_limit_price_is_refused() -> None:
    order = dataclasses.replace(limit(1, ALICE, "BUY", 10, 24600), limit_price_paise=None)
    refused_with_no_change(Market(), order, "limit_price_paise")


def test_a_limit_order_with_a_protection_price_is_refused() -> None:
    order = dataclasses.replace(limit(1, ALICE, "BUY", 10, 24600), protection_price_paise=27005)
    refused_with_no_change(Market(), order, "protection_price_paise")


def test_a_market_order_without_a_protection_price_is_refused() -> None:
    order = dataclasses.replace(market(1, ALICE, "BUY", 10, 27005), protection_price_paise=None)
    refused_with_no_change(Market(), order, "protection_price_paise")


def test_a_market_order_with_a_limit_price_is_refused() -> None:
    order = dataclasses.replace(market(1, ALICE, "BUY", 10, 27005), limit_price_paise=24600)
    refused_with_no_change(Market(), order, "limit_price_paise")


def test_a_protection_price_off_the_tick_is_refused() -> None:
    refused_with_no_change(Market(), market(1, ALICE, "SELL", 10, 22093), "tick")


@pytest.mark.parametrize("quantity", [0, -1, 10_001])
def test_a_quantity_outside_1_to_10000_is_refused(quantity: int) -> None:
    refused_with_no_change(Market(), limit(1, ALICE, "BUY", quantity, 24600), "quantity")


def test_a_command_for_another_ticker_is_refused_by_this_book() -> None:
    m = Market("CALVERA")
    refused_with_no_change(m, limit(1, ALICE, "BUY", 1, 24600, ticker="KORVIN"), "ticker")
    refused_with_no_change(m, cancel(1, ALICE, ticker="KORVIN"), "ticker")
    refused_with_no_change(m, expire(1, ALICE, ticker="KORVIN"), "ticker")


def test_a_command_of_an_unknown_kind_is_refused() -> None:
    class Strange(PlaceOrder):
        pass

    order = limit(1, ALICE, "BUY", 1, 24600)
    strange = Strange(**{f.name: getattr(order, f.name) for f in dataclasses.fields(order)})
    m = Market()
    m.book.decide(strange, new_id=m.new_id, now="2026-11-20T10:42:01.250Z")  # a subclass is fine
    with pytest.raises(InvalidCommandError, match="unknown command"):
        m.book.decide(object(), new_id=m.new_id, now="2026-11-20T10:42:01.250Z")  # type: ignore[arg-type]


def test_an_order_id_the_book_already_knows_is_refused_with_no_events() -> None:
    m = Market()
    m.submit_all(limit(1, ALICE, "BUY", 10, 24600))
    resting_again = m.submit(limit(1, ALICE, "BUY", 10, 24600))
    assert (resting_again.events, resting_again.outcome) == ((), Outcome.REFUSED)
    m.submit_all(limit(2, BOB, "SELL", 10, 24600))  # fills order 1
    filled_again = m.submit(limit(1, ALICE, "BUY", 10, 24600))
    assert (filled_again.events, filled_again.outcome) == ((), Outcome.REFUSED)
    assert m.book.resting(oid(1)) is None
