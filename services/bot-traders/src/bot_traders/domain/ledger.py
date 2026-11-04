"""What a bot may still spend, so it never sends an order portfolio-service would refuse.

The portfolio (Q-09) says what was available when it was read. Orders placed since then hold
more: the ledger subtracts them until a later portfolio read includes them. It never adds money
back on its own (a fill or a cancel is only believed once the portfolio shows it), so it can
only under-estimate what is available: refusals for funds (no margin) or shares (no short
selling) cannot happen. A MARKET buy is costed with a 2% cushion over portfolio-service's
reservation, in case the last price moved since the bot last saw it.
"""

from dataclasses import dataclass, field
from typing import Any

from bot_traders.domain.market import Place, buy_protection, valid_shape

CUSHION = 1.02


@dataclass
class _Pending:
    cash: int
    shares: dict[str, int]
    answered_at: float  # when order-service answered (the reservation was made by then)


@dataclass
class Ledger:
    cash: int = 0  # available cash in the last portfolio read
    shares: dict[str, int] = field(default_factory=dict)  # available shares per ticker
    quantity: dict[str, int] = field(default_factory=dict)  # all shares held, per ticker
    _pending: list[_Pending] = field(default_factory=list)

    def refresh(self, portfolio: dict[str, Any], requested_at: float) -> None:
        """A portfolio read that was *requested* at `requested_at`: it includes every order that
        was answered before then (A-05 reserves before it answers)."""
        self.cash = int(portfolio["available_cash_paise"])
        self.shares = {h["ticker"]: int(h["available_quantity"]) for h in portfolio["holdings"]}
        self.quantity = {h["ticker"]: int(h["quantity"]) for h in portfolio["holdings"]}
        self._pending = [p for p in self._pending if p.answered_at >= requested_at]

    def available_cash(self) -> int:
        return self.cash - sum(p.cash for p in self._pending)

    def available_shares(self, ticker: str) -> int:
        return self.shares.get(ticker, 0) - sum(p.shares.get(ticker, 0) for p in self._pending)

    def cost(self, order: Place, last_price: int) -> int:
        if order.side == "SELL":
            return 0
        if order.limit_price_paise is not None:
            return order.quantity * order.limit_price_paise
        return int(order.quantity * buy_protection(last_price) * CUSHION) + 1

    def refusal(self, order: Place, last_price: int) -> str | None:
        """Why the exchange would refuse this order, or None if it would accept it."""
        shape = valid_shape(order)
        if shape is not None:
            return shape
        if order.side == "BUY" and self.cost(order, last_price) > self.available_cash():
            return "INSUFFICIENT_FUNDS"
        if order.side == "SELL" and order.quantity > self.available_shares(order.ticker):
            return "INSUFFICIENT_SHARES"
        return None

    def hold(self, order: Place, last_price: int, answered_at: float) -> None:
        """An order was accepted: keep its money or shares aside until a portfolio read shows it."""
        shares = {order.ticker: order.quantity} if order.side == "SELL" else {}
        self._pending.append(_Pending(self.cost(order, last_price), shares, answered_at))
