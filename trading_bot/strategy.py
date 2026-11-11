"""Signal generation: combines an SMA/EMA trend filter with an MACD crossover
trigger and an RSI confirmation filter into a single BUY/SELL/HOLD decision.

The strategy is intentionally long-only (no short selling): it opens long
positions on bullish confluence and closes them on bearish confluence.
Whenever the indicators disagree or there isn't enough history, it returns
HOLD rather than guessing — see the "no order on unclear signals" requirement.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pandas as pd

from trading_bot.config import Config
from trading_bot.indicators import ema, macd, rsi, sma
from trading_bot.patterns import PatternSignal, generate_pattern_signal


class Action(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass(frozen=True)
class SignalResult:
    action: Action
    reason: str
    indicators: dict


def _crossed_above(prev_a: float, prev_b: float, curr_a: float, curr_b: float) -> bool:
    return prev_a <= prev_b and curr_a > curr_b


def _crossed_below(prev_a: float, prev_b: float, curr_a: float, curr_b: float) -> bool:
    return prev_a >= prev_b and curr_a < curr_b


def _macd_bull_cross_within(df: pd.DataFrame, lookback_bars: int) -> tuple[bool, int | None]:
    """Whether a bullish MACD crossover happened at any point within the
    last `lookback_bars` bars (not just the current one), as long as the
    MACD line is still above the signal line on the current bar (momentum
    hasn't already reversed again since). The crossover itself typically
    fires before the SMA trend filter confirms an uptrend -- requiring it on
    the exact same bar as the trend filter lets most valid entries slip by.

    Returns (found, bars_ago); bars_ago is 0 for the current bar, 1 for one
    bar back, etc. (None when no crossover was found in the window).
    """
    latest = df.iloc[-1]
    if not (latest["macd_line"] > latest["macd_signal_line"]):
        return False, None

    max_back = min(lookback_bars, len(df) - 1)
    for bars_ago in range(max_back):
        idx = len(df) - 1 - bars_ago
        curr = df.iloc[idx]
        prev = df.iloc[idx - 1]
        if curr[["macd_line", "macd_signal_line"]].isna().any() or prev[["macd_line", "macd_signal_line"]].isna().any():
            continue
        if _crossed_above(prev["macd_line"], prev["macd_signal_line"], curr["macd_line"], curr["macd_signal_line"]):
            return True, bars_ago
    return False, None


def compute_indicators(df: pd.DataFrame, config: Config) -> pd.DataFrame:
    """Adds SMA, EMA, RSI and MACD columns to a copy of the OHLCV dataframe."""
    out = df.copy()
    close = out["close"]

    out["sma_fast"] = sma(close, config.sma_fast)
    out["sma_slow"] = sma(close, config.sma_slow)
    out["ema_fast"] = ema(close, config.sma_fast)
    out["ema_slow"] = ema(close, config.sma_slow)
    out["rsi"] = rsi(close, config.rsi_period)

    macd_line, signal_line, histogram = macd(close, config.macd_fast, config.macd_slow, config.macd_signal)
    out["macd_line"] = macd_line
    out["macd_signal_line"] = signal_line
    out["macd_hist"] = histogram

    return out


def generate_signal(df: pd.DataFrame, config: Config, has_open_position: bool) -> SignalResult:
    """Evaluates the latest bar of an indicator-enriched dataframe (see
    compute_indicators) and returns the trading decision plus the reasoning
    that should be logged.
    """
    if len(df) < config.min_bars_required:
        return SignalResult(
            Action.HOLD,
            f"Not enough history yet ({len(df)}/{config.min_bars_required} bars) — skipping.",
            {},
        )

    latest = df.iloc[-1]
    prev = df.iloc[-2]

    required = ["sma_fast", "sma_slow", "rsi", "macd_line", "macd_signal_line"]
    if latest[required].isna().any() or prev[required].isna().any():
        return SignalResult(Action.HOLD, "Indicators not fully warmed up yet — skipping.", {})

    indicators = {
        "close": round(float(latest["close"]), 4),
        "sma_fast": round(float(latest["sma_fast"]), 4),
        "sma_slow": round(float(latest["sma_slow"]), 4),
        "rsi": round(float(latest["rsi"]), 2),
        "macd_line": round(float(latest["macd_line"]), 4),
        "macd_signal_line": round(float(latest["macd_signal_line"]), 4),
    }

    uptrend = latest["sma_fast"] > latest["sma_slow"]
    downtrend = latest["sma_fast"] < latest["sma_slow"]
    macd_bull_cross, macd_bull_cross_bars_ago = _macd_bull_cross_within(df, config.macd_cross_lookback_bars)
    macd_bear_cross = _crossed_below(
        prev["macd_line"], prev["macd_signal_line"], latest["macd_line"], latest["macd_signal_line"]
    )
    rsi_ok_for_entry = config.rsi_oversold < latest["rsi"] < config.rsi_overbought
    rsi_overbought = latest["rsi"] >= config.rsi_overbought

    if has_open_position:
        reasons = []
        if downtrend:
            reasons.append(f"death cross (SMA{config.sma_fast} < SMA{config.sma_slow})")
        if macd_bear_cross:
            reasons.append("MACD bearish crossover")
        if rsi_overbought:
            reasons.append(f"RSI overbought ({indicators['rsi']} >= {config.rsi_overbought})")
        if len(reasons) >= config.exit_confirmations_required:
            return SignalResult(Action.SELL, "Exit signal: " + ", ".join(reasons) + ".", indicators)
        if reasons:
            return SignalResult(
                Action.HOLD,
                f"{len(reasons)}/{config.exit_confirmations_required} Exit-Bedingungen erfuellt "
                f"({', '.join(reasons)}), noch keine Aktion.",
                indicators,
            )
        return SignalResult(Action.HOLD, "Position open, no exit condition met.", indicators)

    if uptrend and macd_bull_cross and rsi_ok_for_entry:
        cross_timing = "this bar" if macd_bull_cross_bars_ago == 0 else f"{macd_bull_cross_bars_ago} bar(s) ago"
        reason = (
            f"Uptrend (SMA{config.sma_fast} {indicators['sma_fast']} > "
            f"SMA{config.sma_slow} {indicators['sma_slow']}), MACD bullish crossover "
            f"{cross_timing} ({indicators['macd_line']} > {indicators['macd_signal_line']}), "
            f"RSI confirms momentum without being overbought ({indicators['rsi']})."
        )
        return SignalResult(Action.BUY, reason, indicators)

    missing = []
    if not uptrend:
        missing.append("no confirmed uptrend")
    if not macd_bull_cross:
        missing.append(f"no MACD bullish crossover within the last {config.macd_cross_lookback_bars} bar(s)")
    if not rsi_ok_for_entry:
        missing.append(f"RSI outside entry range ({indicators['rsi']})")
    return SignalResult(Action.HOLD, "No entry: " + ", ".join(missing) + ".", indicators)


def generate_pattern_signal_from_config(df: pd.DataFrame, config: Config) -> PatternSignal:
    """Adapter zwischen der config-losen patterns.py und der bestehenden
    Config-Struktur: uebersetzt die PATTERN_*-Einstellungen in die einfachen
    Zahlenparameter, die patterns.generate_pattern_signal() erwartet.
    `df` sollte die rohen OHLCV-Bars sein (nicht das indikator-angereicherte
    DataFrame aus compute_indicators -- patterns.py braucht nur OHLCV).
    """
    return generate_pattern_signal(
        df,
        pivot_window=config.pattern_pivot_window,
        min_pivots=config.pattern_min_pivots,
        max_pivots=config.pattern_max_pivots,
        breakout_threshold_pct=config.pattern_breakout_threshold_pct,
        sr_zone_tolerance_pct=config.pattern_sr_zone_tolerance_pct,
    )


def combine_with_pattern_signal(base_signal: SignalResult, pattern_signal: PatternSignal, config: Config) -> SignalResult:
    """Kombiniert das bestehende Indikator-Signal mit dem unabhaengigen
    Pattern-Signal (Trendlinien/Muster aus patterns.py). Das Pattern-Modul
    ERSETZT die bestehende Logik nicht und kann von sich aus KEINEN Trade
    ausloesen: ist das Basis-Signal HOLD, bleibt es HOLD, egal wie stark das
    Pattern-Signal ist. Es kann nur ein vorhandenes BUY/SELL bestaetigen
    (und damit durchlassen) oder verwerfen (und damit auf HOLD zuruecksetzen).

    Zwei Modi ueber config.pattern_combine_mode:
    - "confirm":  Pattern-Signal muss dem Indikator-Signal in Richtung UND
                  Mindest-Konfidenz (config.pattern_min_confidence) zustimmen.
    - "weighted": gewichtete Kombination aus Indikator-Richtung (+-1) und
                  Pattern-Score (config.pattern_weight steuert das Gewicht
                  des Pattern-Anteils); nur wenn die Kombination weiterhin
                  in die urspruengliche Richtung zeigt, bleibt das Signal.
    """
    if not config.pattern_enabled or base_signal.action == Action.HOLD:
        return base_signal

    direction = 1 if base_signal.action == Action.BUY else -1

    if config.pattern_combine_mode == "weighted":
        combined_score = (1 - config.pattern_weight) * direction + config.pattern_weight * pattern_signal.score
        agrees = (direction > 0 and combined_score > 0) or (direction < 0 and combined_score < 0)
        detail = f"Pattern gewichtet (score={pattern_signal.score:+.2f}, combined={combined_score:+.2f}): {pattern_signal.reason}"
    else:  # "confirm" (default / fail-safe fuer unbekannte Werte -- validate() faengt das vorher ab)
        wanted_direction: str = "bullish" if direction > 0 else "bearish"
        agrees = pattern_signal.direction == wanted_direction and pattern_signal.confidence >= config.pattern_min_confidence
        detail = f"Pattern (Konfidenz {pattern_signal.confidence:.2f}): {pattern_signal.reason}"

    if agrees:
        return SignalResult(base_signal.action, f"{base_signal.reason} | Bestaetigt durch {detail}", base_signal.indicators)

    return SignalResult(
        Action.HOLD,
        f"{base_signal.reason} | Verworfen, da Pattern nicht zustimmt ({detail}) -> HOLD.",
        base_signal.indicators,
    )
