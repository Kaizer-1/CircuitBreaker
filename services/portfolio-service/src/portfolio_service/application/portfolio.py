"""Use cases: open an account (A-02), reserve (A-06), settle and release from matching.v1 (A-09),
and read a portfolio (Q-09). Each runs in one database transaction.

Settlement is idempotent: the event's event_id is recorded in processed_events in the same
transaction as the balance change, so a redelivered event changes nothing. Money is released only
on the engine's events (ADR-0017): a finished order's TradeExecuted, OrderCancelled or OrderExpired.

Every change to a portfolio also notifies the replicas in the same transaction (Q-10, live.py).
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from portfolio_service.application.live import Change, fill_message, order_closed_message
from portfolio_service.domain.model import (
    BOT_STARTING_CASH,
    BOT_STARTING_SHARES,
    HUMAN_STARTING_CASH,
    LOAD_STARTING_CASH,
    REFERENCE_PRICES,
    TICKERS,
    Account,
    AccountKind,
    AccountNotFoundError,
    Holding,
    NicknameTakenError,
    Reservation,
    ReservationRequest,
    ReservationStatus,
    SettlementError,
)
from portfolio_service.domain.rules import portfolio_view, release, reserve, settle_buy, settle_sell
from portfolio_service.ports.store import PortfolioStore, Tx
from portfolio_service.runtime.events import EventEnvelope, format_timestamp

logger = logging.getLogger("portfolio")

BOT_ACCOUNTS = tuple(f"bot-{n:02d}" for n in range(1, 21))
LOAD_ACCOUNTS = tuple(f"load-{n:02d}" for n in range(1, 11))


def seeded_trader_id(nickname: str) -> str:
    """Bots and load accounts keep the same trader_id in every environment and after a reset."""
    prefix, _, number = nickname.partition("-")
    if number.isdigit() and len(number) == 2 and prefix in ("bot", "load"):
        head = "b0b0b0b0" if prefix == "bot" else "10ad10ad"
        return f"{head}-00{number}-4000-8000-0000000000{number}"
    return str(uuid5(NAMESPACE_URL, f"https://circuitbreaker.local/accounts/{nickname}"))


class PortfolioService:
    def __init__(
        self,
        store: PortfolioStore,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_id: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        self._store = store
        self._clock = clock
        self._new_id = new_id

    def _now(self) -> str:
        return format_timestamp(self._clock())

    # ------------------------------------------------------------------ A-02 accounts

    def _new_account(self, nickname: str, kind: AccountKind) -> tuple[Account, list[Holding]]:
        if kind is AccountKind.HUMAN:
            trader_id, cash, shares = self._new_id(), HUMAN_STARTING_CASH, 0
        elif kind is AccountKind.BOT:
            trader_id, cash, shares = (
                seeded_trader_id(nickname),
                BOT_STARTING_CASH,
                BOT_STARTING_SHARES,
            )
        else:
            trader_id, cash, shares = seeded_trader_id(nickname), LOAD_STARTING_CASH, 0
        holdings = [
            Holding(trader_id, t, shares, 0, REFERENCE_PRICES[t]) for t in TICKERS if shares
        ]
        starting_value = cash + sum(h.quantity * REFERENCE_PRICES[h.ticker] for h in holdings)
        account = Account(trader_id, nickname, kind, cash, 0, starting_value, self._now())
        return account, holdings

    async def open_account(self, nickname: str, kind: AccountKind) -> tuple[Account, bool]:
        """(account, created). HUMAN nicknames are unique; BOT and LOAD accounts are idempotent."""
        try:
            async with self._store.transaction() as tx:
                existing = await tx.account_by_nickname(nickname)
                if existing is None:
                    account, holdings = self._new_account(nickname, kind)
                    await tx.insert_account(account)
                    for holding in holdings:
                        await tx.save_holding(holding)
                    logger.info(
                        "account opened",  # local event AccountOpened
                        extra={"trader_id": account.trader_id, "kind": kind.value},
                    )
                    return account, True
        except NicknameTakenError:
            if kind is AccountKind.HUMAN:
                raise
            # Two callers seeded the same bot at once: the other one won; return its account.
            async with self._store.transaction() as tx:
                existing = await tx.account_by_nickname(nickname)
        if existing is None or kind is AccountKind.HUMAN or existing.kind is AccountKind.HUMAN:
            raise NicknameTakenError(nickname)
        return existing, False

    async def seed_accounts(self) -> None:
        for nickname in BOT_ACCOUNTS:
            await self.open_account(nickname, AccountKind.BOT)
        for nickname in LOAD_ACCOUNTS:
            await self.open_account(nickname, AccountKind.LOAD)

    # ------------------------------------------------------------------ A-06 reservations

    async def create_reservation(self, request: ReservationRequest) -> tuple[Reservation, bool]:
        """(reservation, created). A repeat for the same order_id returns the existing one."""
        async with self._store.transaction() as tx:
            # Locking the account first serialises every reservation of this trader, including a
            # retry of this same order, so the check below cannot race.
            account = await tx.account(request.trader_id, lock=True)
            if account is None:
                raise AccountNotFoundError(request.trader_id)
            existing = await tx.reservation(request.order_id)
            if existing is not None:
                return existing, False
            holding = await tx.holding(request.trader_id, request.ticker, lock=True)
            prices = await tx.last_prices()
            reservation = reserve(
                request,
                account,
                holding,
                last_price_paise=prices.get(request.ticker, REFERENCE_PRICES[request.ticker]),
                reservation_id=self._new_id(),
                now=self._now(),
            )
            await tx.save_account(account)
            await tx.save_holding(holding)
            await tx.insert_reservation(reservation)
            await tx.notify(Change(request.trader_id).to_json())  # reserved cash or shares moved
            logger.info(
                "reservation created",  # local event ReservationCreated
                extra={"order_id": request.order_id, "trader_id": request.trader_id},
            )
            return reservation, True

    # ------------------------------------------------------------------ A-09 settlement and release

    async def apply_event(self, envelope: EventEnvelope) -> bool:
        """Apply one matching.v1 event. False for a duplicate or an event type it does not use."""
        handler = {
            "OrderAccepted": self._accepted,
            "TradeExecuted": self._trade,
            "OrderCancelled": self._finished,
            "OrderExpired": self._finished,
        }.get(envelope.event_type)
        if handler is None:
            return False
        async with self._store.transaction() as tx:
            if not await tx.mark_processed(envelope.event_id, envelope.event_type):
                return False
            await handler(tx, envelope.data)
        return True

    async def _accepted(self, tx: Tx, d: dict[str, Any]) -> None:
        reservation = await tx.reservation(d["order_id"], lock=True)
        if reservation is None:
            logger.warning("OrderAccepted without a reservation", extra={"order_id": d["order_id"]})
            return
        reservation.confirmed = True
        await tx.save_reservation(reservation)

    async def _trade(self, tx: Tx, d: dict[str, Any]) -> None:
        buyer_id, seller_id, ticker = d["buyer_trader_id"], d["seller_trader_id"], d["ticker"]
        accounts: dict[str, Account] = {}
        for trader_id in sorted({buyer_id, seller_id}):  # one lock order everywhere: no deadlocks
            account = await tx.account(trader_id, lock=True)
            if account is None:
                raise SettlementError(f"trade {d['trade_id']}: no account {trader_id}")
            accounts[trader_id] = account
        buy = await tx.reservation(d["buy_order_id"], lock=True)
        sell = await tx.reservation(d["sell_order_id"], lock=True)
        holdings = {
            t: await tx.holding(t, ticker, lock=True) for t in sorted({buyer_id, seller_id})
        }
        common = {"quantity": d["quantity"], "price_paise": d["price_paise"]}
        settle_buy(
            buy,
            accounts[buyer_id],
            holdings[buyer_id],
            order_id=d["buy_order_id"],
            order_remaining=d["buy_order_remaining_quantity"],
            **common,
        )
        settle_sell(
            sell,
            accounts[seller_id],
            holdings[seller_id],
            order_id=d["sell_order_id"],
            order_remaining=d["sell_order_remaining_quantity"],
            **common,
        )
        for account in accounts.values():
            await tx.save_account(account)
        for holding in holdings.values():
            await tx.save_holding(holding)
        for reservation in (buy, sell):
            if reservation is not None:  # always true here: settle_* raised otherwise
                await tx.save_reservation(reservation)
        for trader_id, order_id, side in (
            (buyer_id, d["buy_order_id"], "BUY"),
            (seller_id, d["sell_order_id"], "SELL"),
        ):  # local event TradeSettled
            await tx.add_settlement(
                trade_id=d["trade_id"],
                trader_id=trader_id,
                order_id=order_id,
                ticker=ticker,
                side=side,
                executed_at=d["executed_at"],
                **common,
            )
        await tx.set_last_price(ticker, d["price_paise"], d["executed_at"])
        for trader_id, side, remaining, order_id in (
            (buyer_id, "BUY", d["buy_order_remaining_quantity"], d["buy_order_id"]),
            (seller_id, "SELL", d["sell_order_remaining_quantity"], d["sell_order_id"]),
        ):
            messages = [fill_message(d, side)]
            if remaining == 0:
                messages.append(order_closed_message(order_id, "FILLED", None))
            await tx.notify(Change(trader_id, ticker, tuple(messages)).to_json())

    async def _finished(self, tx: Tx, d: dict[str, Any]) -> None:
        """OrderCancelled (any reason) or OrderExpired: release what the reservation still holds."""
        found = await tx.reservation(d["order_id"])
        if found is None or found.status is not ReservationStatus.ACTIVE:
            return  # nothing held (never reserved, or already finished)
        account = await tx.account(found.trader_id, lock=True)
        reservation = await tx.reservation(d["order_id"], lock=True)
        if account is None or reservation is None:
            raise SettlementError(f"order {d['order_id']}: account or reservation vanished")
        holding = await tx.holding(reservation.trader_id, reservation.ticker, lock=True)
        released = release(reservation, account, holding)
        reservation.status = ReservationStatus.RELEASED
        await tx.save_account(account)
        await tx.save_holding(holding)
        await tx.save_reservation(reservation)
        # OrderExpired is the answer for an order the engine never saw: there is no order to close
        # on the phone (order-service shows it REJECTED), only money to give back.
        messages = (
            (order_closed_message(d["order_id"], "CANCELLED", d["reason"]),)
            if "reason" in d
            else ()
        )
        await tx.notify(Change(reservation.trader_id, None, messages).to_json())
        logger.info(
            "reservation released",  # local event ReservationReleased
            extra={"order_id": reservation.order_id, "released": released},
        )

    # ------------------------------------------------------------------ Q-09

    async def portfolio(self, trader_id: str) -> dict[str, Any]:
        async with self._store.transaction() as tx:
            account = await tx.account(trader_id)
            if account is None:
                raise AccountNotFoundError(trader_id)
            holdings = await tx.holdings(trader_id)
            prices = REFERENCE_PRICES | await tx.last_prices()
        return portfolio_view(account, holdings, prices, self._now())
