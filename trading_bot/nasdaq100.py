"""Static Nasdaq-100 ticker list.

Deliberately hardcoded instead of scraped at runtime: no extra network
dependency/failure mode during trading, and the list is trivial to edit
by hand when the index is reconstituted.

Stand: Anfang 2025 (nach bestem Wissen, inkl. Nasdaq-100-Reconstitution
Dezember 2024). Die Zusammensetzung aendert sich durch die jaehrliche
Reconstitution im Dezember sowie gelegentliche Sonderanpassungen
(Fusionen, Uebernahmen, Delistings) -- vor produktivem Einsatz gegen eine
offizielle Quelle abgleichen (z. B. nasdaq.com/market-activity/quotes/nasdaq-100-index
oder der offizielle Nasdaq-100 Fact Sheet) und diese Datei bei Bedarf
manuell aktualisieren.
"""

from __future__ import annotations

NASDAQ_100: list[str] = [
    "AAPL", "ABNB", "ADBE", "ADI", "ADP", "ADSK", "AEP", "AMAT", "AMD", "AMGN",
    "AMZN", "APP", "ARM", "ASML", "AVGO", "BIIB", "BKNG", "BKR", "CCEP", "CDNS",
    "CDW", "CEG", "CHTR", "CMCSA", "COST", "CPRT", "CRWD", "CSCO", "CSGP", "CSX",
    "CTAS", "CTSH", "DASH", "DDOG", "DKNG", "DXCM", "EA", "EXC", "FAST", "FTNT",
    "GEHC", "GFS", "GILD", "GOOG", "GOOGL", "HON", "IDXX", "INTC", "INTU", "ISRG",
    "KDP", "KHC", "KLAC", "LIN", "LRCX", "LULU", "MAR", "MCHP", "MDLZ", "MELI",
    "META", "MNST", "MRVL", "MSFT", "MSTR", "MU", "NFLX", "NVDA", "NXPI", "ODFL",
    "ON", "ORLY", "PANW", "PAYX", "PCAR", "PDD", "PEP", "PLTR", "PYPL", "QCOM",
    "REGN", "ROP", "ROST", "SBUX", "SIRI", "SNPS", "TEAM", "TMUS", "TSLA", "TTD",
    "TTWO", "TXN", "VRSK", "VRSN", "VRTX", "WBD", "WDAY", "XEL", "ZS",
]
