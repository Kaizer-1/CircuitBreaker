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
        Instrument("CALVERA", "Calvera Consumer Ltd", 24550),
        Instrument("KORVIN", "Korvin Technologies Ltd", 118000),
        Instrument("ASTRYX", "Astryx Logistics Ltd", 41275),
        Instrument("HELIOR", "Helior Power Ltd", 8820),
        Instrument("TAVIRA", "Tavira Media Ltd", 53005),
    )
}
TICKERS = tuple(INSTRUMENTS)
