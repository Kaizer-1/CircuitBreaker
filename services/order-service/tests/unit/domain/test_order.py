"""The Order aggregate: validation codes and status transitions."""

import pytest

from order_service.domain.order import (
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    OrderValidationError,
    RejectionReason,
    Side,
    validate,
)

T0 = "2026-11-20T10:42:01.248Z"


def request(**overrides: object) -> OrderRequest:
    fields: dict[str, object] = {
        "ticker": "CALVERA",
        "side": Side.BUY,
        "order_type": OrderType.LIMIT,
        "quantity": 50,
        "limit_price_paise": 24600,
    } | overrides
    return OrderRequest(**fields)  # type: ignore[arg-type]


def order(**overrides: object) -> Order:
    fields: dict[str, object] = {
        "order_id": "7f3c9e10-2b4d-4e6f-8a1b-3c5d7e9f0a12",
        "trader_id": "3d2a1b0c-9e8f-4a7b-8c6d-5e4f3a2b1c0d",
        "ticker": "CALVERA",
        "side": Side.BUY,
        "order_type": OrderType.LIMIT,
        "quantity": 50,
        "limit_price_paise": 24600,
        "protection_price_paise": None,
        "reservation_id": "c1d2e3f4-a5b6-4c7d-8e9f-0a1b2c3d4e5f",
        "status": OrderStatus.PENDING,
        "placed_at": T0,
        "updated_at": T0,
    } | overrides
    return Order(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("overrides", "code", "field"),
    [
        ({"ticker": "TATA"}, RejectionReason.UNKNOWN_TICKER, "ticker"),
        ({"limit_price_paise": 24603}, RejectionReason.INVALID_TICK_SIZE, "limit_price_paise"),
        ({"limit_price_paise": 0}, RejectionReason.INVALID_TICK_SIZE, "limit_price_paise"),
        ({"limit_price_paise": None}, RejectionReason.VALIDATION_FAILED, "limit_price_paise"),
        ({"order_type": OrderType.MARKET}, RejectionReason.VALIDATION_FAILED, "limit_price_paise"),
        ({"quantity": 0}, RejectionReason.VALIDATION_FAILED, "quantity"),
        ({"quantity": 10_001}, RejectionReason.VALIDATION_FAILED, "quantity"),
    ],
)
def test_validation_rejects_with_a_specific_code(
    overrides: dict[str, object], code: RejectionReason, field: str
) -> None:
    with pytest.raises(OrderValidationError) as refused:
        validate(request(**overrides))
    assert (refused.value.code, refused.value.field) == (code, field)


def test_valid_limit_and_market_orders_pass() -> None:
    validate(request())
    validate(request(order_type=OrderType.MARKET, limit_price_paise=None, side=Side.SELL))


def test_accepted_moves_a_pending_order_to_open() -> None:
    o = order()
    o.accepted(1041, T0)
    assert (o.status, o.last_sequence) == (OrderStatus.OPEN, 1041)


def test_fills_track_quantity_and_the_average_price() -> None:
    # The spec's examples: 20 @ 24550, then 30 @ 24600 -> average 24580 (round half up).
    o = order()
    o.accepted(1041, T0)
    o.traded(1042, T0, 20, 24550, remaining=30)
    assert (o.status, o.filled_quantity, o.remaining_quantity, o.average_fill_price_paise) == (
        OrderStatus.PARTIALLY_FILLED,
        20,
        30,
        24550,
    )
    o.traded(1050, T0, 30, 24600, remaining=0)
    assert (o.status, o.remaining_quantity, o.average_fill_price_paise) == (
        OrderStatus.FILLED,
        0,
        24580,
    )


def test_an_event_that_is_not_newer_than_the_last_one_applied_changes_nothing() -> None:
    o = order()
    o.traded(1042, T0, 20, 24550, remaining=30)
    o.traded(1042, T0, 20, 24550, remaining=30)  # a replay
    o.accepted(1041, T0)  # older
    assert (o.filled_quantity, o.status) == (20, OrderStatus.PARTIALLY_FILLED)


def test_cancel_and_cancel_rejected_clear_the_pending_cancel() -> None:
    o = order(status=OrderStatus.OPEN, cancel_requested=True)
    assert not o.cancellable()
    o.cancel_rejected(1100, T0)
    assert (o.cancel_requested, o.cancellable()) == (False, True)
    o.cancel_requested = True
    o.cancelled(1101, T0, "USER_REQUESTED")
    assert (o.status, o.cancel_reason, o.cancel_requested, o.remaining_quantity) == (
        OrderStatus.CANCELLED,
        "USER_REQUESTED",
        False,
        0,
    )
    assert not o.cancellable()


def test_a_full_fill_clears_a_pending_cancel() -> None:
    o = order(status=OrderStatus.OPEN, cancel_requested=True)
    o.traded(1042, T0, 50, 24600, remaining=0)
    assert (o.status, o.cancel_requested) == (OrderStatus.FILLED, False)


def test_order_expired_rejects_a_pending_order_and_ignores_any_other() -> None:
    o = order()
    o.expired(1188, T0)
    assert (o.status, o.rejection_reason) == (OrderStatus.REJECTED, RejectionReason.PUBLISH_TIMEOUT)
    filled = order(status=OrderStatus.FILLED)
    filled.expired(1188, T0)
    assert filled.status is OrderStatus.FILLED


def test_the_engines_facts_win_over_a_publish_timeout() -> None:
    # The relay gave up at 60 s while a hung publish still reached the engine: the order trades.
    o = order()
    o.reject(RejectionReason.PUBLISH_TIMEOUT, T0)
    o.accepted(1041, T0)
    assert (o.status, o.rejection_reason) == (OrderStatus.OPEN, None)
    late = order()
    late.reject(RejectionReason.PUBLISH_TIMEOUT, T0)
    late.traded(1042, T0, 10, 24600, remaining=40)
    assert (late.status, late.rejection_reason) == (OrderStatus.PARTIALLY_FILLED, None)


def test_a_rejected_order_is_not_cancellable_and_has_nothing_remaining() -> None:
    o = order()
    o.reject(RejectionReason.INSUFFICIENT_FUNDS, T0)
    assert (o.cancellable(), o.remaining_quantity, o.average_fill_price_paise) == (False, 0, None)
