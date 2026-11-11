"""Wraps Alpaca's market data API: historical bars for indicator calculation
and the trading clock used to check whether the market is open.

Bars for multi-symbol watchlists (e.g. the full Nasdaq-100) are fetched in
chunks via get_bars_batch() rather than one HTTP request per symbol, to stay
well under Alpaca's data API rate limits. Multi-symbol requests deliberately
omit `limit` -- alpaca-py's own pagination (RESTClient._get_marketdata) sums
returned bars *across all symbols in the chunk* against a single shared
`limit` budget, so once that budget is used up, whichever symbols happen to
be processed last in that response can come back completely empty, no
matter how generously `limit` is scaled up. Bounding the request via `start`
instead (see estimate_lookback_start) and trimming each symbol's result down
to the desired bar count afterwards avoids this entirely.
"""

from __future__ import annotations

import logging
import math
import re
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.models import Clock

from trading_bot.config import Config
from trading_bot.order_executor import apply_default_request_timeout

logger = logging.getLogger("trading_bot")

_TIMEFRAME_RE = re.compile(r"^(\d+)(Min|Hour|Day|Week|Month)$", re.IGNORECASE)
_UNIT_MAP = {
    "min": TimeFrameUnit.Minute,
    "hour": TimeFrameUnit.Hour,
    "day": TimeFrameUnit.Day,
    "week": TimeFrameUnit.Week,
    "month": TimeFrameUnit.Month,
}

# US-Handelstag: 9:30-16:00 ET = 390 Minuten reine Handelszeit. Nur zur
# Umrechnung "wie viele Kalendertage muss ich zurueckgehen" genutzt -- nicht
# fuer die Indikatorberechnung selbst.
_TRADING_MINUTES_PER_DAY = 390


def parse_timeframe(value: str) -> TimeFrame:
    match = _TIMEFRAME_RE.match(value.strip())
    if not match:
        raise ValueError(f"Invalid TIMEFRAME '{value}'. Expected e.g. '15Min', '1Hour', '1Day'.")
    amount, unit = match.groups()
    return TimeFrame(int(amount), _UNIT_MAP[unit.lower()])


def timeframe_to_offset(value: str) -> pd.DateOffset:
    """Dauer einer Kerze des gegebenen TIMEFRAME-Werts (z.B. '15Min') als
    pandas-Offset -- Monat/Woche/Tag kalendergenau statt als feste
    Sekundenzahl, damit `Kerzenbeginn + Offset` exakt das Kerzenende ergibt.
    """
    match = _TIMEFRAME_RE.match(value.strip())
    if not match:
        raise ValueError(f"Invalid TIMEFRAME '{value}'. Expected e.g. '15Min', '1Hour', '1Day'.")
    amount, unit = int(match.group(1)), match.group(2).lower()
    keyword = {"min": "minutes", "hour": "hours", "day": "days", "week": "weeks", "month": "months"}[unit]
    return pd.DateOffset(**{keyword: amount})


def drop_incomplete_bars(bars: pd.DataFrame, timeframe: str, now: datetime | None = None) -> pd.DataFrame:
    """Entfernt die letzte Kerze, falls ihr Zeitfenster noch nicht
    abgeschlossen ist (Alpaca-Zeitstempel sind linksbasiert: Kerzenbeginn).

    Signale duerfen nur auf fertigen Kerzen entstehen: eine noch laufende
    Kerze aendert Schlusskurs/Hoch/Tief bis zu ihrem Ende, ein darauf
    berechnetes MACD-/SMA-Signal kann bis zum Kerzenschluss wieder
    verschwinden -- und der Backtest kennt ohnehin nur fertige Kerzen, live
    und Backtest wuerden sonst unterschiedliche Dinge messen. Ob Alpaca die
    laufende Kerze ueberhaupt mitliefert, ist nicht dokumentiert; liefert
    es sie nicht, ist das hier schlicht ein No-op.
    """
    if bars is None or bars.empty:
        return bars
    now_ts = pd.Timestamp(now if now is not None else datetime.now(timezone.utc))
    if now_ts.tzinfo is None:
        now_ts = now_ts.tz_localize("UTC")
    last_start = pd.Timestamp(bars.index[-1])
    if last_start.tzinfo is None:
        last_start = last_start.tz_localize("UTC")
    if last_start + timeframe_to_offset(timeframe) > now_ts:
        return bars.iloc[:-1]
    return bars


def estimate_lookback_start(timeframe: TimeFrame, bars_needed: int) -> datetime:
    """Wie weit ein `start`-Datum zurueckliegen muss, damit trotz Wochenenden,
    Feiertagen und Handelspausen sicher `bars_needed` Bars der gegebenen
    Groesse zurueckkommen. Bewusst grosszuegig (Handelstage -> Kalendertage
    mit Wochenend-Faktor + fixem Feiertags-Puffer) -- ein paar Tage zu viel
    Historie anzufragen kostet nur etwas mehr Daten in der Antwort, zu wenig
    wuerde genau das Problem verursachen, das dieses `start`-Datum loesen soll.

    Genutzt von get_bars(), get_bars_batch() UND backfill_bars(): alpaca-py
    liefert bei einer reinen `limit`-only StockBarsRequest (ganz ohne
    `start`) offenbar grundsaetzlich leere Ergebnisse zurueck -- auch mit
    korrektem `feed` und gueltigen Keys. Ein explizites `start` behebt das,
    unabhaengig vom (separaten) fehlenden-`feed`-Problem, das zuvor behoben
    wurde. `end` wird bewusst NICHT gesetzt -- ohne `end` liefert die API bis
    "jetzt", was fuer den Default-Feed "iex" unproblematisch ist (IEX ist
    Realtime, keine 15-Minuten-Verzoegerung -- die betrifft laut Alpaca-Doku
    nur den unbezahlten Zugriff auf den "sip"-Feed).
    """
    unit = timeframe.unit
    if unit == TimeFrameUnit.Minute:
        trading_days_needed = math.ceil((bars_needed * timeframe.amount) / _TRADING_MINUTES_PER_DAY)
    elif unit == TimeFrameUnit.Hour:
        trading_days_needed = math.ceil((bars_needed * timeframe.amount * 60) / _TRADING_MINUTES_PER_DAY)
    elif unit == TimeFrameUnit.Day:
        trading_days_needed = bars_needed * timeframe.amount
    elif unit == TimeFrameUnit.Week:
        trading_days_needed = bars_needed * timeframe.amount * 5
    else:  # Month
        trading_days_needed = bars_needed * timeframe.amount * 21

    # 5 Handelstage pro 7 Kalendertage, plus 10 Tage Puffer fuer Feiertage.
    calendar_days_needed = math.ceil(trading_days_needed * 7 / 5) + 10
    return datetime.now(timezone.utc) - timedelta(days=calendar_days_needed)


class MarketDataFeed:
    def __init__(self, config: Config, trading_client: TradingClient | None = None):
        self._config = config
        self._data_client = StockHistoricalDataClient(config.api_key, config.secret_key)
        self._trading_client = trading_client or TradingClient(
            config.api_key, config.secret_key, paper=config.paper
        )
        # Nur wenn wir den Client selbst gebaut haben -- ein evtl. von
        # aussen injizierter trading_client (siehe Tests) wird nicht
        # angefasst. Siehe apply_default_request_timeout()'s Docstring
        # in order_executor.py fuer den Grund (kein eingebautes Timeout in
        # alpaca-py, is_market_open() wird u.a. vom unabhaengigen
        # Sicherheits-Check-Thread aufgerufen und darf nie unbegrenzt haengen).
        if trading_client is None:
            apply_default_request_timeout(self._trading_client)
        self._timeframe = parse_timeframe(config.timeframe)

    def get_bars(self, symbol: str, limit: int) -> pd.DataFrame:
        """Returns a DataFrame of recent OHLCV bars for one symbol, oldest first."""
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=self._timeframe,
            # limit-only Requests (ganz ohne start) liefern bei alpaca-py
            # leere Ergebnisse zurueck -- siehe estimate_lookback_start().
            start=estimate_lookback_start(self._timeframe, limit),
            limit=limit,
            feed=self._config.alpaca_data_feed,
        )
        bar_set = self._data_client.get_stock_bars(request)
        df = bar_set.df
        if df.empty:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        return df[["open", "high", "low", "close", "volume"]].sort_index()

    def get_bars_batch(self, symbols: list[str], limit: int) -> dict[str, pd.DataFrame]:
        """Fetches recent bars for many symbols using as few HTTP requests as
        possible: symbols are split into chunks of config.data_batch_size,
        one multi-symbol request per chunk, with a short pause between
        chunks. Returns {symbol: DataFrame}; symbols with no data are
        omitted (caller/strategy treats missing history as HOLD).
        """
        results: dict[str, pd.DataFrame] = {}
        chunks = [
            symbols[i : i + self._config.data_batch_size]
            for i in range(0, len(symbols), self._config.data_batch_size)
        ]
        start = estimate_lookback_start(self._timeframe, limit)

        for i, chunk in enumerate(chunks):
            request = StockBarsRequest(
                symbol_or_symbols=chunk,
                timeframe=self._timeframe,
                # limit-only Requests (ganz ohne start) liefern bei alpaca-py
                # leere Ergebnisse zurueck -- siehe estimate_lookback_start().
                start=start,
                # Bewusst OHNE limit=: alpaca-py's Pagination (siehe
                # RESTClient._get_marketdata) zaehlt total_items als Summe
                # UEBER ALLE Symbole im Chunk zusammen und bricht ab, sobald
                # diese Summe `limit` erreicht -- selbst limit*len(chunk)
                # verhindert das nicht zuverlaessig, weil die Bars nicht
                # gleichmaessig auf die Symbole verteilt zurueckkommen (das
                # zuletzt verarbeitete Symbol im Chunk kann dabei komplett
                # leer ausgehen). Ohne limit paginiert der Client stattdessen
                # einfach so lange (in 10.000er-Seiten), bis Alpaca fuer den
                # kompletten Chunk kein next_page_token mehr liefert -- die
                # Datenmenge bleibt trotzdem durch das eng bemessene `start`
                # begrenzt, danach wird unten pro Symbol auf `limit` getrimmt.
                feed=self._config.alpaca_data_feed,
            )
            try:
                bar_set = self._data_client.get_stock_bars(request)
            except Exception:
                logger.exception("Failed to fetch bar batch for %s", chunk)
                continue

            df = bar_set.df
            if not df.empty and isinstance(df.index, pd.MultiIndex):
                for symbol in df.index.get_level_values("symbol").unique():
                    symbol_df = df.xs(symbol, level="symbol")[["open", "high", "low", "close", "volume"]]
                    results[symbol] = symbol_df.sort_index().tail(limit)
            elif not df.empty and len(chunk) == 1:
                results[chunk[0]] = df[["open", "high", "low", "close", "volume"]].sort_index().tail(limit)

            if i < len(chunks) - 1 and self._config.data_batch_pause_seconds > 0:
                time.sleep(self._config.data_batch_pause_seconds)

        return results

    def backfill_bars(
        self,
        symbols: list[str],
        limit: int,
        retries: int = 3,
        retry_pause_seconds: float = 2.0,
    ) -> dict[str, pd.DataFrame]:
        """Laedt beim Bot-Start genug historische Bars pro Symbol, damit
        Indikatoren (SMA/EMA/RSI/MACD) direkt im ersten Zyklus vollstaendig
        berechenbar sind, statt erst tage-/wochenlang live nachzusammeln.

        Nutzt dieselbe Chunk-Strategie wie get_bars_batch (ein multi-symbol
        Request pro data_batch_size Symbole, Pause dazwischen -- haelt die
        Anzahl der HTTP-Requests bei z.B. 99 Nasdaq-100-Symbolen niedrig und
        unter Alpacas Rate-Limits), retried aber zusaetzlich jeden Chunk bis
        zu `retries` mal, bevor die betroffenen Symbole als fehlgeschlagen
        geloggt werden -- ein einzelner kurzzeitiger Ausfall der Alpaca API
        blockiert damit nicht den Start der uebrigen Symbole.
        """
        start = estimate_lookback_start(self._timeframe, limit)
        results: dict[str, pd.DataFrame] = {}
        chunks = [
            symbols[i : i + self._config.data_batch_size]
            for i in range(0, len(symbols), self._config.data_batch_size)
        ]

        for i, chunk in enumerate(chunks):
            request = StockBarsRequest(
                symbol_or_symbols=chunk,
                timeframe=self._timeframe,
                start=start,
                # Bewusst OHNE limit= -- siehe ausfuehrlicher Kommentar in
                # get_bars_batch(): ein gemeinsames limit ueber den ganzen
                # Chunk (auch limit*len(chunk)) laesst zuletzt verarbeitete
                # Symbole leer ausgehen, weil alpaca-py Bars ueber alle
                # Symbole hinweg gegen dasselbe Budget zaehlt. `start` allein
                # begrenzt die Datenmenge ausreichend.
                feed=self._config.alpaca_data_feed,
            )

            bar_set = None
            for attempt in range(1, retries + 1):
                try:
                    bar_set = self._data_client.get_stock_bars(request)
                    break
                except Exception as exc:
                    is_last_attempt = attempt == retries
                    level = logger.warning if not is_last_attempt else logger.error
                    level(
                        "Backfill-Request fuer %s fehlgeschlagen (Versuch %d/%d): %s",
                        chunk,
                        attempt,
                        retries,
                        exc,
                    )
                    if not is_last_attempt:
                        time.sleep(retry_pause_seconds)

            if bar_set is None:
                logger.error("Backfill fuer diese Symbole endgueltig fehlgeschlagen: %s", chunk)
            else:
                df = bar_set.df
                if not df.empty and isinstance(df.index, pd.MultiIndex):
                    for symbol in df.index.get_level_values("symbol").unique():
                        symbol_df = df.xs(symbol, level="symbol")[["open", "high", "low", "close", "volume"]]
                        results[symbol] = symbol_df.sort_index().tail(limit)
                elif not df.empty and len(chunk) == 1:
                    results[chunk[0]] = df[["open", "high", "low", "close", "volume"]].sort_index().tail(limit)

            if i < len(chunks) - 1 and self._config.data_batch_pause_seconds > 0:
                time.sleep(self._config.data_batch_pause_seconds)

        return results

    def get_latest_price(self, symbol: str) -> float:
        bars = self.get_bars(symbol, limit=1)
        if bars.empty:
            raise RuntimeError(f"No market data returned for {symbol}.")
        return float(bars.iloc[-1]["close"])

    def get_clock(self) -> Clock:
        return self._trading_client.get_clock()

    def is_market_open(self) -> bool:
        return bool(self.get_clock().is_open)
