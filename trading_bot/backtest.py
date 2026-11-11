"""Backtest-Modul: spielt die exakt gleiche Entscheidungslogik wie der
Live-Bot (strategy.generate_signal + optional das Pattern-Modul aus
patterns.py) Balken fuer Balken auf historischen Daten durch, BEVOR etwas
live/paper laeuft. Das ist bewusst keine separate Nachbildung der Strategie
-- es werden dieselben Funktionen aus strategy.py aufgerufen wie in bot.py,
damit Backtest- und Live-Verhalten nicht auseinanderlaufen koennen.

Datenquelle: zuerst Alpacas historische Markt-API (wenn API-Keys in der
Config vorhanden sind), sonst/bei Fehler Fallback auf yfinance (Yahoo
Finance, keine Alpaca-Keys noetig -- gut zum Ausprobieren ohne Account).

Simulation: long-only, eine Position gleichzeitig pro Symbol, Stop-Loss/
Take-Profit gemaess denselben Prozentsaetzen wie die Live-Bracket-Order
(risk_manager.calculate_bracket_prices), Positionsgroesse gemaess
config.position_size_pct (nicht 100% des Kapitals pro Trade -- sonst waere
das Ergebnis nicht vergleichbar mit dem tatsaechlichen Risiko im Live-Bot).

Bewusste Vereinfachung: das Tagesverlust-Limit (MAX_DAILY_LOSS_PCT) wird im
Backtest NICHT simuliert (nur SL/TP + Signal-Exit), siehe README.

Performance: compute_indicators() laeuft einmal vorab auf dem kompletten df
(SMA/EMA/RSI/MACD sind rein kausale rolling/ewm-Berechnungen, siehe
indicators.py -- der Wert an Position i haengt nie von Werten > i ab), statt
pro Balken auf einem wachsenden Fenster neu gerechnet zu werden. Die
Backtest-Schleife selbst greift pro Balken nur noch auf einen View
(enriched.iloc[:i+1]) dieses einmal berechneten DataFrames zu.

CLI-Nutzung:
    python -m trading_bot.backtest --symbol AAPL --days 730
    python -m trading_bot.backtest --use-nasdaq100 --days 730 --no-compare
"""

from __future__ import annotations

import argparse
import csv
import logging
import statistics
import time
from dataclasses import dataclass, field, replace

import pandas as pd

from trading_bot.config import Config, load_config
from trading_bot.nasdaq100 import NASDAQ_100
from trading_bot.risk_manager import RiskManager
from trading_bot.strategy import (
    Action,
    combine_with_pattern_signal,
    compute_indicators,
    generate_pattern_signal_from_config,
    generate_signal,
)

logger = logging.getLogger("trading_bot")


@dataclass(frozen=True)
class BacktestTrade:
    entry_time: object
    exit_time: object
    entry_price: float
    exit_price: float
    pnl_pct: float
    exit_reason: str


@dataclass
class BacktestResult:
    symbol: str
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=lambda: [0.0])
    starting_equity: float = 10_000.0
    timeframe: str = ""
    num_bars: int = 0
    buy_hold_return_pct: float = 0.0
    position_size_pct: float = 0.0

    @property
    def num_trades(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t.pnl_pct > 0)
        return wins / len(self.trades) * 100

    @property
    def avg_win_pct(self) -> float:
        wins = [t.pnl_pct for t in self.trades if t.pnl_pct > 0]
        return sum(wins) / len(wins) if wins else 0.0

    @property
    def avg_loss_pct(self) -> float:
        losses = [t.pnl_pct for t in self.trades if t.pnl_pct <= 0]
        return sum(losses) / len(losses) if losses else 0.0

    @property
    def avg_pnl_pct(self) -> float:
        if not self.trades:
            return 0.0
        return sum(t.pnl_pct for t in self.trades) / len(self.trades)

    @property
    def total_return_pct(self) -> float:
        if self.starting_equity <= 0:
            return 0.0
        return (self.equity_curve[-1] / self.starting_equity - 1) * 100

    @property
    def max_drawdown_pct(self) -> float:
        peak = self.equity_curve[0]
        max_dd = 0.0
        for eq in self.equity_curve:
            peak = max(peak, eq)
            if peak > 0:
                max_dd = max(max_dd, (peak - eq) / peak * 100)
        return max_dd

    def _buy_hold_lines(self) -> list[str]:
        return [
            f"Buy & Hold (Kaufen und Halten, 100% Kapital): {self.buy_hold_return_pct:+.2f}%",
            f"Strategie vs. Buy & Hold: {self.total_return_pct - self.buy_hold_return_pct:+.2f} Prozentpunkte",
            f"Hinweis: Die Strategie setzt pro Trade nur {self.position_size_pct:.1f}% des Kapitals ein "
            "(Rest liegt bar), Buy & Hold nutzt 100%. Der Vergleich zeigt daher primaer ob die "
            "Entry/Exit-Logik ueberhaupt in die richtige Richtung tradet, ist aber kein direkter "
            "Rendite-Vergleich bei gleichem Kapitaleinsatz.",
        ]

    def summary(self, label: str = "") -> str:
        header = f"=== Backtest {label} ({self.symbol}) ===" if label else f"=== Backtest ({self.symbol}) ==="
        lines = [header]
        if self.timeframe or self.num_bars:
            lines.append(f"Timeframe: {self.timeframe or '?'} | Balken: {self.num_bars}")
        lines.append(f"Trades: {self.num_trades}")
        if self.num_trades == 0:
            lines.append("Keine Trades ausgeloest (Signale zu selten/uneindeutig fuer den Testzeitraum).")
            lines.extend(self._buy_hold_lines())
            return "\n".join(lines)
        lines.append(f"Trefferquote: {self.win_rate:.1f}%")
        lines.append(f"Avg. Gewinn: {self.avg_win_pct:+.2f}%  |  Avg. Verlust: {self.avg_loss_pct:+.2f}%")
        lines.append(f"Avg. P&L pro Trade: {self.avg_pnl_pct:+.2f}%")
        lines.append(
            f"Gesamtrendite: {self.total_return_pct:+.2f}% "
            f"(Start {self.starting_equity:.2f} -> Ende {self.equity_curve[-1]:.2f})"
        )
        lines.append(f"Max Drawdown: {self.max_drawdown_pct:.2f}%")
        lines.extend(self._buy_hold_lines())
        return "\n".join(lines)


# yfinance-Intervall pro TIMEFRAME-Wert (siehe data_feed._TIMEFRAME_RE fuer
# die unterstuetzten Werte: 1Min,5Min,15Min,30Min,1Hour,1Day).
_YFINANCE_INTERVAL_MAP = {
    "1min": "1m",
    "5min": "5m",
    "15min": "15m",
    "30min": "30m",
    "1hour": "60m",
    "1day": "1d",
}

# Yahoo begrenzt Intraday-Historie hart (Stand 2025); 1d gilt praktisch als
# unbegrenzt (None). Quelle: yfinance/Yahoo-Doku zu den `period`-Limits.
_YFINANCE_MAX_LOOKBACK_DAYS = {
    "1m": 7,
    "5m": 60,
    "15m": 60,
    "30m": 60,
    "60m": 730,
    "1d": None,
}


def _timeframe_to_yfinance_interval(timeframe: str) -> str:
    key = timeframe.strip().lower()
    if key not in _YFINANCE_INTERVAL_MAP:
        raise ValueError(
            f"TIMEFRAME '{timeframe}' wird vom yfinance-Fallback nicht unterstuetzt "
            f"(unterstuetzt: {', '.join(sorted(_YFINANCE_INTERVAL_MAP))})."
        )
    return _YFINANCE_INTERVAL_MAP[key]


def load_historical_data(
    symbol: str,
    lookback_days: int = 730,
    config: Config | None = None,
) -> pd.DataFrame:
    """Laedt historische OHLCV-Daten fuer den Backtest: primaer ueber Alpacas
    historische Markt-API (wenn `config` gueltige API-Keys enthaelt), sonst
    per yfinance-Fallback. Gibt ein aufsteigend sortiertes DataFrame mit
    'open','high','low','close','volume' zurueck.

    Verwendet IMMER config.timeframe (Standard: die .env-Konfiguration, z.B.
    "15Min") statt eines hart codierten Timeframes -- sonst testet der
    Backtest eine andere Strategie als die, die der Live-Bot tatsaechlich
    faehrt (z.B. Golden-Cross auf Tagesbasis statt der konfigurierten
    Intraday-Strategie).
    """
    timeframe = config.timeframe if config is not None else "1Day"

    if config is not None and config.api_key and config.secret_key:
        try:
            from trading_bot.data_feed import MarketDataFeed

            feed = MarketDataFeed(config)
            # grober Umrechnungsfaktor Kalendertage -> Balkenanzahl fuer
            # nicht-taegliche Timeframes (nur fuer den Anfrage-Limit noetig)
            limit = lookback_days if timeframe.lower().endswith("day") else lookback_days * 26
            bars = feed.get_bars(symbol, limit=limit)
            if not bars.empty:
                logger.info(
                    "Historische Daten fuer %s von Alpaca geladen (%d Balken, Timeframe %s).",
                    symbol,
                    len(bars),
                    timeframe,
                )
                return bars
            logger.warning("Alpaca lieferte keine Daten fuer %s, versuche yfinance...", symbol)
        except Exception as exc:
            logger.warning("Alpaca-Abruf fuer %s fehlgeschlagen (%s), versuche yfinance...", symbol, exc)

    return _load_from_yfinance(symbol, lookback_days, timeframe)


def _load_from_yfinance(symbol: str, lookback_days: int, timeframe: str = "1Day") -> pd.DataFrame:
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError(
            "Weder gueltige Alpaca-Keys noch das Paket 'yfinance' verfuegbar. "
            "Installiere yfinance (`pip install yfinance`) fuer den Backtest-Fallback "
            "oder hinterlege ALPACA_API_KEY/ALPACA_SECRET_KEY in .env."
        ) from exc

    interval = _timeframe_to_yfinance_interval(timeframe)
    max_days = _YFINANCE_MAX_LOOKBACK_DAYS.get(interval)
    effective_days = lookback_days
    if max_days is not None and lookback_days > max_days:
        logger.warning(
            "yfinance liefert fuer %s-Bars nur %d Tage Historie, kappe --days auf %d (angefragt: %d).",
            interval,
            max_days,
            max_days,
            lookback_days,
        )
        effective_days = max_days

    period = "max" if max_days is None and effective_days > 730 else f"{max(effective_days, 30)}d"
    raw = yf.download(symbol, period=period, interval=interval, progress=False, auto_adjust=True)
    if raw is None or raw.empty:
        raise RuntimeError(f"yfinance lieferte keine Daten fuer {symbol}.")

    raw = raw.rename(columns=str.lower)
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = [str(c[0]).lower() for c in raw.columns]
    logger.info(
        "Historische Daten fuer %s von yfinance geladen (%d Balken, Timeframe %s).", symbol, len(raw), interval
    )
    return raw[["open", "high", "low", "close", "volume"]].sort_index()


def _combined_signal(window: pd.DataFrame, enriched_window: pd.DataFrame, config: Config, has_open_position: bool):
    """Ruft dieselben Entscheidungsfunktionen wie bot.py auf -- Backtest und
    Live-Bot teilen sich diesen Code, damit sich die Logik nicht auseinander
    entwickeln kann.

    `enriched_window` ist bereits indikator-angereichert (siehe run_backtest:
    compute_indicators() laeuft dort einmal vorab auf dem kompletten df,
    nicht mehr pro Balken) -- `window` bleibt die rohen OHLCV-Bars, exakt
    wie zuvor, fuers Pattern-Modul (das seine eigene, unabhaengige Pivot-/
    Trendlinien-Erkennung auf rohen Kursen braucht, nicht auf SMA/RSI/MACD).
    """
    signal = generate_signal(enriched_window, config, has_open_position=has_open_position)
    if config.pattern_enabled:
        pattern_signal = generate_pattern_signal_from_config(window, config)
        signal = combine_with_pattern_signal(signal, pattern_signal, config)
    return signal


def run_backtest(df: pd.DataFrame, config: Config, symbol: str = "", starting_equity: float = 10_000.0) -> BacktestResult:
    """Simuliert die Strategie Balken fuer Balken auf historischen Daten.

    An jedem Balken i wird nur das Fenster df.iloc[:i+1] verwendet (kein
    Blick in die Zukunft). Ohne offene Position wird bei BUY zum Schlusskurs
    des Balkens eingestiegen und sofort SL/TP gemaess risk_manager gesetzt
    (identisch zur Live-Bracket-Order). Mit offener Position wird bei jedem
    weiteren Balken zuerst auf Stop-Loss/Take-Profit (High/Low-Durchbruch)
    geprueft, danach auf ein SELL-Signal der Strategie.
    """
    buy_hold_return_pct = 0.0
    if len(df) > 0 and float(df["close"].iloc[0]) > 0:
        buy_hold_return_pct = (float(df["close"].iloc[-1]) / float(df["close"].iloc[0]) - 1) * 100

    result = BacktestResult(
        symbol=symbol,
        starting_equity=starting_equity,
        timeframe=config.timeframe,
        num_bars=len(df),
        buy_hold_return_pct=buy_hold_return_pct,
        position_size_pct=config.position_size_pct,
    )
    risk_manager = RiskManager(config)
    position_fraction = config.position_size_pct / 100

    equity = starting_equity
    equity_curve = [equity]

    in_position = False
    entry_price = 0.0
    entry_index = 0
    stop_price = take_price = 0.0

    min_bars = config.min_bars_required
    if len(df) <= min_bars:
        logger.warning(
            "Nur %d Balken verfuegbar, aber min_bars_required=%d -- Backtest liefert keine Trades.",
            len(df),
            min_bars,
        )
        result.equity_curve = equity_curve
        return result

    # SMA/EMA/RSI/MACD sind rein kausale rolling/ewm-Berechnungen (siehe
    # indicators.py) -- der Wert an Position i haengt nie von Werten > i ab.
    # Deshalb reicht es, hier einmal auf dem kompletten df zu rechnen, statt
    # compute_indicators() pro Balken auf einem wachsenden Fenster neu
    # aufzurufen (O(n) statt O(n^2) -- bei ~19.000 15Min-Balken pro Symbol
    # sonst mehrere Minuten allein pro Symbol).
    enriched = compute_indicators(df, config)

    for i in range(min_bars, len(df)):
        window = df.iloc[: i + 1]
        enriched_window = enriched.iloc[: i + 1]
        bar = df.iloc[i]

        if in_position:
            exit_price = None
            exit_reason = ""
            if bar["low"] <= stop_price:
                exit_price, exit_reason = stop_price, "Stop-Loss"
            elif bar["high"] >= take_price:
                exit_price, exit_reason = take_price, "Take-Profit"
            else:
                signal = _combined_signal(window, enriched_window, config, has_open_position=True)
                if signal.action == Action.SELL:
                    exit_price, exit_reason = float(bar["close"]), "Signal"

            if exit_price is not None:
                pnl_pct = (exit_price - entry_price) / entry_price * 100
                equity += equity * position_fraction * (pnl_pct / 100)
                equity_curve.append(equity)
                result.trades.append(
                    BacktestTrade(
                        entry_time=df.index[entry_index],
                        exit_time=df.index[i],
                        entry_price=entry_price,
                        exit_price=exit_price,
                        pnl_pct=pnl_pct,
                        exit_reason=exit_reason,
                    )
                )
                in_position = False

        else:
            signal = _combined_signal(window, enriched_window, config, has_open_position=False)
            if signal.action == Action.BUY:
                entry_price = float(bar["close"])
                entry_index = i
                prices = risk_manager.calculate_bracket_prices(entry_price)
                stop_price, take_price = prices.stop_loss, prices.take_profit
                in_position = True

    result.equity_curve = equity_curve
    return result


def _load_and_run_one(
    symbol: str, config: Config, lookback_days: int, starting_equity: float
) -> BacktestResult | None:
    """Laedt Daten + backtest fuer ein Symbol; gibt None zurueck (statt zu
    werfen) wenn irgendetwas schiefgeht -- fehlende Daten, Rate-Limit,
    Alpaca/yfinance-Fehler etc. sollen im Nasdaq-100-Batch nicht den
    gesamten Lauf abbrechen, sondern nur dieses eine Symbol ueberspringen.
    """
    try:
        df = load_historical_data(symbol, lookback_days=lookback_days, config=config)
        return run_backtest(df, config, symbol=symbol, starting_equity=starting_equity)
    except Exception as exc:
        logger.warning("Backtest fuer %s uebersprungen (%s).", symbol, exc)
        return None


def _summarize_nasdaq100_results(results: list[BacktestResult], total_symbols: int) -> str:
    """Baut die Aggregat-Zusammenfassung ueber alle erfolgreich getesteten
    Symbole -- reine Formatierungs-/Statistiklogik, unabhaengig vom
    tatsaechlichen Datenabruf (dadurch mit synthetischen Ergebnissen testbar).
    """
    lines = [
        "=== Nasdaq-100 Backtest: Zusammenfassung ===",
        f"Erfolgreich getestet: {len(results)}/{total_symbols}",
    ]
    if not results:
        lines.append("Keine Symbole erfolgreich getestet -- keine Statistik moeglich.")
        return "\n".join(lines)

    win_rates = [r.win_rate for r in results]
    total_returns = [r.total_return_pct for r in results]
    buy_holds = [r.buy_hold_return_pct for r in results]
    diffs = [r.total_return_pct - r.buy_hold_return_pct for r in results]

    lines.append("")
    lines.append(f"{'':22}{'Median':>10}{'Durchschnitt':>15}")
    lines.append(f"{'Trefferquote':22}{statistics.median(win_rates):>9.1f}%{statistics.mean(win_rates):>14.1f}%")
    lines.append(
        f"{'Gesamtrendite (Strategie)':22}{statistics.median(total_returns):>9.2f}%{statistics.mean(total_returns):>14.2f}%"
    )
    lines.append(
        f"{'Buy & Hold Rendite':22}{statistics.median(buy_holds):>9.2f}%{statistics.mean(buy_holds):>14.2f}%"
    )
    lines.append(
        f"{'Differenz vs. Buy&Hold':22}{statistics.median(diffs):>8.2f}pp{statistics.mean(diffs):>13.2f}pp"
    )

    uptrend = [r for r in results if r.buy_hold_return_pct > 0]
    downtrend = [r for r in results if r.buy_hold_return_pct < 0]
    lines.append("")
    lines.append("-- Aufgeteilt nach Marktregime (Buy & Hold Rendite im Zeitraum) --")
    if uptrend:
        avg_diff_up = statistics.mean(r.total_return_pct - r.buy_hold_return_pct for r in uptrend)
        lines.append(
            f"Aufwaertstrend (Buy&Hold > 0%, {len(uptrend)} Symbole): "
            f"durchschnittliche Differenz zu Buy&Hold: {avg_diff_up:+.2f} Prozentpunkte"
        )
    else:
        lines.append("Aufwaertstrend (Buy&Hold > 0%): keine Symbole in dieser Gruppe.")
    if downtrend:
        avg_diff_down = statistics.mean(r.total_return_pct - r.buy_hold_return_pct for r in downtrend)
        lines.append(
            f"Abwaertstrend (Buy&Hold < 0%, {len(downtrend)} Symbole): "
            f"durchschnittliche Differenz zu Buy&Hold: {avg_diff_down:+.2f} Prozentpunkte"
        )
    else:
        lines.append("Abwaertstrend (Buy&Hold < 0%): keine Symbole in dieser Gruppe.")

    ranked = sorted(results, key=lambda r: r.total_return_pct - r.buy_hold_return_pct, reverse=True)
    lines.append("")
    lines.append("-- Top 5 (beste Differenz zu Buy & Hold) --")
    for r in ranked[:5]:
        lines.append(
            f"  {r.symbol:6} Strategie {r.total_return_pct:+7.2f}%  Buy&Hold {r.buy_hold_return_pct:+7.2f}%  "
            f"Differenz {r.total_return_pct - r.buy_hold_return_pct:+7.2f}pp  ({r.num_trades} Trades)"
        )
    lines.append("-- Flop 5 (schlechteste Differenz zu Buy & Hold) --")
    for r in ranked[-5:][::-1]:
        lines.append(
            f"  {r.symbol:6} Strategie {r.total_return_pct:+7.2f}%  Buy&Hold {r.buy_hold_return_pct:+7.2f}%  "
            f"Differenz {r.total_return_pct - r.buy_hold_return_pct:+7.2f}pp  ({r.num_trades} Trades)"
        )
    return "\n".join(lines)


def _write_nasdaq100_csv(results: list[BacktestResult], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Symbol", "Trades", "Trefferquote", "Gesamtrendite", "BuyHoldRendite", "Differenz"])
        for r in results:
            writer.writerow(
                [
                    r.symbol,
                    r.num_trades,
                    round(r.win_rate, 2),
                    round(r.total_return_pct, 2),
                    round(r.buy_hold_return_pct, 2),
                    round(r.total_return_pct - r.buy_hold_return_pct, 2),
                ]
            )


def run_nasdaq100_backtest(
    config: Config,
    lookback_days: int,
    starting_equity: float,
    pause_seconds: float = 0.5,
    csv_path: str = "nasdaq100_backtest_results.csv",
) -> None:
    """Backtest ueber die komplette Nasdaq-100-Watchlist (siehe nasdaq100.py)
    statt eines einzelnen Symbols -- prueft, ob das an AAPL/MSFT/NVDA
    beobachtete Muster (Strategie schuetzt gut vor Abwaertstrends, verpasst
    aber Teile von Aufwaertstrends) sich ueber eine groessere Stichprobe
    bestaetigt. Ein einzelnes fehlschlagendes Symbol (fehlende Daten,
    Rate-Limit, ...) bricht den Lauf nicht ab, siehe _load_and_run_one().
    """
    total = len(NASDAQ_100)
    results: list[BacktestResult] = []
    for i, symbol in enumerate(NASDAQ_100, start=1):
        result = _load_and_run_one(symbol, config, lookback_days, starting_equity)
        if result is not None:
            results.append(result)
        logger.info("Fortschritt: %d/%d Symbole verarbeitet, %d/%d erfolgreich getestet.", i, total, len(results), total)
        if i < total and pause_seconds > 0:
            time.sleep(pause_seconds)

    print(_summarize_nasdaq100_results(results, total_symbols=total))
    if results:
        _write_nasdaq100_csv(results, csv_path)
        print(f"\nErgebnis pro Symbol gespeichert unter: {csv_path}")


def _build_cli_config(pattern_enabled: bool) -> Config:
    """Config fuer die CLI: laedt aus .env, wenn vorhanden, aber verlangt
    KEINE Alpaca-Keys -- ein Backtest per yfinance soll ohne Account
    funktionieren. `load_config()` wuerde hier hart fehlschlagen (die
    validate() dort verlangt Keys, weil Live/Paper-Trading ohne sie
    sinnlos waere), deshalb wird die Config hier bewusst OHNE diese
    Pruefung konstruiert.
    """
    return replace(Config(), pattern_enabled=pattern_enabled)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Backtest der Trading-Strategie, optional mit Pattern-Modul (Trendlinien/Muster)."
    )
    parser.add_argument("--symbol", default="AAPL", help="Zu testendes Symbol (Standard: AAPL)")
    parser.add_argument(
        "--use-nasdaq100",
        action="store_true",
        help="Backtest ueber die komplette Nasdaq-100-Watchlist statt eines einzelnen --symbol "
        "(schreibt zusaetzlich nasdaq100_backtest_results.csv). Laeuft immer mit der aktuellen "
        ".env-Konfiguration (wie --no-compare), --symbol wird dabei ignoriert.",
    )
    parser.add_argument("--days", type=int, default=730, help="Lookback in Kalendertagen (Standard: 730)")
    parser.add_argument(
        "--starting-equity", type=float, default=10_000.0, help="Simuliertes Startkapital (Standard: 10000)"
    )
    parser.add_argument(
        "--no-compare",
        action="store_true",
        help="Nur mit der aktuellen .env-Konfiguration testen statt automatisch mit/ohne Pattern-Modul zu vergleichen.",
    )
    args = parser.parse_args()

    if args.use_nasdaq100:
        try:
            config = load_config()
        except ValueError:
            config = Config()
        run_nasdaq100_backtest(config, lookback_days=args.days, starting_equity=args.starting_equity)
        return

    try:
        if args.no_compare:
            try:
                config = load_config()
            except ValueError:
                config = Config()
            df = load_historical_data(args.symbol, lookback_days=args.days, config=config)
            result = run_backtest(df, config, symbol=args.symbol, starting_equity=args.starting_equity)
            print(result.summary("(.env-Konfiguration)"))
            return

        config_off = _build_cli_config(pattern_enabled=False)
        config_on = _build_cli_config(pattern_enabled=True)

        df = load_historical_data(args.symbol, lookback_days=args.days, config=config_off)
    except RuntimeError as exc:
        logging.error("Backtest abgebrochen: %s", exc)
        raise SystemExit(1) from None

    result_off = run_backtest(df, config_off, symbol=args.symbol, starting_equity=args.starting_equity)
    result_on = run_backtest(df, config_on, symbol=args.symbol, starting_equity=args.starting_equity)

    print(result_off.summary("OHNE Pattern-Modul"))
    print()
    print(result_on.summary("MIT Pattern-Modul"))


if __name__ == "__main__":
    main()
