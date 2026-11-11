"""Einmalige, rein lesende Messung -- zwei Fragen, die sich nur mit echtem
Alpaca-Zugang beantworten lassen:

1. Wie hat das Konto seit dem ersten Tag mit Daten abgeschnitten, verglichen
   mit "QQQ (Nasdaq-100-ETF) einfach kaufen und halten" im selben Zeitraum?
2. Liefert Alpaca beim Abruf der juengsten Kerzen auch die noch LAUFENDE
   Kerze mit? (Der Bot verwirft sie seit bot/data_feed.drop_incomplete_bars
   ohnehin -- die Messung zeigt nur, ob das in der Praxis etwas aendert.)

Macht KEINE Aenderungen: keine Orders, nichts wird geschrieben.

CLI-Nutzung (vom Projekt-Root aus, mit gueltigen Alpaca-Keys in .env):
    python -m trading_bot.measure_baseline
    python -m trading_bot.measure_baseline --benchmark SPY
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from trading_bot.config import Config, load_config
from trading_bot.data_feed import drop_incomplete_bars, parse_timeframe
from trading_bot.order_executor import OrderExecutor, apply_default_request_timeout


def total_return_pct(values: list[float]) -> float:
    if len(values) < 2 or values[0] <= 0:
        return 0.0
    return (values[-1] / values[0] - 1) * 100


def max_drawdown_pct(values: list[float]) -> float:
    """Groesster Rueckgang vom jeweils bisherigen Hoechststand, als negative Prozentzahl."""
    worst, peak = 0.0, None
    for value in values:
        if peak is None or value > peak:
            peak = value
        if peak and peak > 0:
            worst = min(worst, (value - peak) / peak * 100)
    return worst


def equity_series(raw: dict) -> pd.Series:
    """Tages-Kontostaende aus Alpacas Portfolio-History (None-Werte und
    Tage mit Kontostand 0 vor der ersten Einzahlung werden uebersprungen)."""
    points = {}
    for ts, equity in zip(raw.get("timestamp") or [], raw.get("equity") or []):
        if ts is None or equity is None:
            continue
        try:
            value = float(equity)
        except (TypeError, ValueError):
            continue
        if value > 0:
            points[datetime.fromtimestamp(int(ts), tz=timezone.utc).date()] = value
    return pd.Series(points).sort_index()


def _fetch_bars(client: StockHistoricalDataClient, config: Config, symbol: str, timeframe: TimeFrame, start: datetime) -> pd.DataFrame:
    request = StockBarsRequest(symbol_or_symbols=symbol, timeframe=timeframe, start=start, feed=config.alpaca_data_feed)
    df = client.get_stock_bars(request).df
    if not df.empty and isinstance(df.index, pd.MultiIndex):
        df = df.xs(symbol, level="symbol")
    return df.sort_index()


def report_performance(executor: OrderExecutor, client: StockHistoricalDataClient, config: Config, benchmark: str) -> list[str]:
    equity = equity_series(executor.get_portfolio_history("20A", timeframe="1D"))
    if len(equity) < 2:
        return ["1) Vergleich Konto vs. Benchmark: zu wenig Kontostand-Historie (weniger als 2 Tage)."]

    first_day, last_day = equity.index[0], equity.index[-1]
    start = datetime(first_day.year, first_day.month, first_day.day, tzinfo=timezone.utc) - timedelta(days=5)
    bench = _fetch_bars(client, config, benchmark, TimeFrame.Day, start)
    bench_closes = [
        float(close) for ts, close in bench["close"].items() if first_day <= ts.date() <= last_day
    ] if not bench.empty else []

    lines = [
        f"1) Konto vs. {benchmark} kaufen-und-halten, {first_day} bis {last_day} ({len(equity)} Handelstage)",
        f"   Konto:  {total_return_pct(list(equity)):+7.2f} %   max. Rueckgang {max_drawdown_pct(list(equity)):6.2f} %"
        f"   ({equity.iloc[0]:,.2f} $ -> {equity.iloc[-1]:,.2f} $)",
    ]
    if len(bench_closes) >= 2:
        diff = total_return_pct(list(equity)) - total_return_pct(bench_closes)
        lines += [
            f"   {benchmark + ':':7} {total_return_pct(bench_closes):+7.2f} %   max. Rueckgang {max_drawdown_pct(bench_closes):6.2f} %",
            f"   Differenz Konto minus {benchmark}: {diff:+.2f} Prozentpunkte",
            "   Hinweis: das Konto war nie voll investiert, der Benchmark schon -- in einem",
            "   steigenden Markt liegt ein teilinvestiertes Konto fast zwangslaeufig zurueck.",
        ]
    else:
        lines.append(f"   {benchmark}: keine Kursdaten fuer den Zeitraum erhalten.")
    return lines


def report_running_bar(client: StockHistoricalDataClient, config: Config, symbol: str) -> list[str]:
    now = datetime.now(timezone.utc)
    bars = _fetch_bars(client, config, symbol, parse_timeframe(config.timeframe), now - timedelta(days=5))
    lines = [f"2) Liefert Alpaca die laufende {config.timeframe}-Kerze mit? (Symbol {symbol}, jetzt {now:%Y-%m-%d %H:%M:%S} UTC)"]
    if bars.empty:
        return lines + ["   Keine Kerzen erhalten."]

    for ts in bars.index[-3:]:
        lines.append(f"   Kerzenbeginn {ts:%Y-%m-%d %H:%M} UTC")
    if len(drop_incomplete_bars(bars, config.timeframe, now)) < len(bars):
        lines.append("   ERGEBNIS: JA -- die letzte Kerze laeuft noch. Der Bot verwirft sie jetzt.")
    else:
        age_min = (now - bars.index[-1].to_pydatetime()).total_seconds() / 60
        lines.append(
            f"   ERGEBNIS: NEIN -- die letzte Kerze ist abgeschlossen (Beginn vor {age_min:.0f} Min)."
        )
        lines.append("   Nur aussagekraeftig, wenn die Boerse gerade geoeffnet ist; sonst waehrend der Handelszeit wiederholen.")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--benchmark", default="QQQ", help="Vergleichs-Symbol (Standard: QQQ)")
    args = parser.parse_args()

    config = load_config()
    executor = OrderExecutor(config)
    client = StockHistoricalDataClient(config.api_key, config.secret_key)
    apply_default_request_timeout(client)

    for section in (
        lambda: report_performance(executor, client, config, args.benchmark),
        lambda: report_running_bar(client, config, args.benchmark),
    ):
        try:
            print("\n".join(section()))
        except Exception as exc:  # eine fehlgeschlagene Messung soll die andere nicht verhindern
            print(f"Messung fehlgeschlagen: {exc}")
        print()


if __name__ == "__main__":
    main()
