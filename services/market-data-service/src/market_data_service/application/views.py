"""The JSON shapes of api/market-data-service.yaml (Candle, Trade, OrderBookDepth, Instrument),
shared by the REST routes and the WebSocket so both always say the same thing."""

from datetime import UTC, date, datetime
from typing import Any

from market_data_service.domain.candles import Candle
from market_data_service.domain.instruments import LOT_SIZE, TICK_SIZE_PAISE
from market_data_service.domain.market import TickerMarket, Trade
from market_data_service.events.candles import format_bucket
from market_data_service.runtime.events import format_timestamp


def iso(at_ms: int) -> str:
    return format_timestamp(datetime.fromtimestamp(at_ms / 1000, UTC))


def candle_view(c: Candle) -> dict[str, Any]:
    return {
        "bucket_start": format_bucket(c.bucket_start_ms),
        "open_paise": c.open_paise,
        "high_paise": c.high_paise,
        "low_paise": c.low_paise,
        "close_paise": c.close_paise,
        "volume": c.volume,
        "trade_count": c.trade_count,
    }


def trade_view(t: Trade) -> dict[str, Any]:
    return {
        "trade_id": t.trade_id,
        "sequence": t.sequence,
        "price_paise": t.price_paise,
        "quantity": t.quantity,
        "aggressor_side": t.aggressor_side,
        "executed_at": iso(t.executed_at_ms),
    }


def depth_view(market: TickerMarket, levels: int, now_ms: int) -> dict[str, Any]:
    bids, asks = market.depth_levels(levels)
    best_bid = bids[0].price_paise if bids else None
    best_ask = asks[0].price_paise if asks else None
    return {
        "ticker": market.ticker,
        "as_of_sequence": market.sequence,
        "as_of": iso(market.as_of_ms if market.as_of_ms is not None else now_ms),
        "best_bid_paise": best_bid,
        "best_ask_paise": best_ask,
        "spread_paise": best_ask - best_bid
        if best_bid is not None and best_ask is not None
        else None,
        "bids": [vars(level) for level in bids],
        "asks": [vars(level) for level in asks],
    }


def instrument_view(market: TickerMarket, today: date, now_ms: int) -> dict[str, Any]:
    reference = market.instrument.reference_price_paise
    last = market.last_price_paise
    day = market.day_view(today)
    change = last - reference  # against the reference price (Q-02)
    return {
        "ticker": market.ticker,
        "name": market.instrument.name,
        "tick_size_paise": TICK_SIZE_PAISE,
        "lot_size": LOT_SIZE,
        "reference_price_paise": reference,
        "last_price_paise": last,
        "day_open_paise": day.open_paise,
        "day_high_paise": day.high_paise,
        "day_low_paise": day.low_paise,
        "change_paise": change,
        "change_percent": round(change * 100 / reference, 2),
        "day_volume": day.volume,
        "updated_at": iso(market.as_of_ms if market.as_of_ms is not None else now_ms),
    }
