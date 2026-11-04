"""CandleClosed (event catalog, topic candles.v1), built exactly as the contract says."""

from datetime import UTC, datetime

from market_data_service.domain.candles import INTERVALS, Candle
from market_data_service.runtime.events import EventEnvelope, build_event, deterministic_event_id

TOPIC = "candles.v1"


def format_bucket(at_ms: int) -> str:
    """Bucket times are whole seconds: 2026-11-20T10:42:00Z."""
    return datetime.fromtimestamp(at_ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def candle_closed(
    ticker: str, interval: str, candle: Candle, *, environment: str, at: datetime | None = None
) -> EventEnvelope:
    start = format_bucket(candle.bucket_start_ms)
    # UUIDv5 of ticker|interval|bucket_start: every replica emits the same ID for the same candle,
    # so consumers drop the copies (and a corrected candle re-sent after a late trade).
    return build_event(
        "CandleClosed",
        {
            "ticker": ticker,
            "interval": interval,
            "bucket_start": start,
            "bucket_end": format_bucket(candle.bucket_start_ms + INTERVALS[interval]),
            "open_paise": candle.open_paise,
            "high_paise": candle.high_paise,
            "low_paise": candle.low_paise,
            "close_paise": candle.close_paise,
            "volume": candle.volume,
            "trade_count": candle.trade_count,
        },
        producer="market-data-service",
        environment=environment,
        correlation_id=f"candle-{ticker}-{interval}-{start}",
        event_id=deterministic_event_id(f"{ticker}|{interval}|{start}"),
        occurred_at=at,
    )
