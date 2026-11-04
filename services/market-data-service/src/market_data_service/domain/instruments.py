"""The five instruments (docs/00-overview.md section 3). All prices are integer paise."""

from dataclasses import dataclass

TICK_SIZE_PAISE = 5
LOT_SIZE = 1


@dataclass(frozen=True)
class Instrument:
    ticker: str
    name: str
    reference_price_paise: int  # the price before any trade has happened


INSTRUMENTS = {
    i.ticker: i
    for i in (
        Instrument("CHAIWALA", "Chaiwala Beverages Ltd", 24550),
        Instrument("JUGAAD", "Jugaad Technologies Ltd", 118000),
        Instrument("DABBA", "Dabba Logistics Ltd", 41275),
        Instrument("MONSOON", "Monsoon Power Ltd", 8820),
        Instrument("FILMY", "Filmy Studios Ltd", 53005),
    )
}
TICKERS = tuple(INSTRUMENTS)
