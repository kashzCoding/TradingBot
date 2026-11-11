"""Loads all runtime configuration from environment variables (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

from trading_bot.nasdaq100 import NASDAQ_100

load_dotenv()


def _get_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _get_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return float(value) if value else default


def _get_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value else default


def _get_list(name: str, default: list[str]) -> list[str]:
    value = os.getenv(name)
    if not value:
        return default
    return [symbol.strip().upper() for symbol in value.split(",") if symbol.strip()]


def _current_trading_mode() -> str:
    return os.getenv("TRADING_MODE", "standard").strip().lower()


# Per-mode defaults for the parameters TRADING_MODE is allowed to steer.
# Only applied when the corresponding .env variable is left unset --
# an explicit value in .env always wins over the mode default.
_MODE_DEFAULTS: dict[str, dict[str, object]] = {
    "timeframe": {"standard": "15Min", "fast": "5Min"},
    "check_interval_minutes": {"standard": 15.0, "fast": 5.0},
    "sma_fast": {"standard": 50, "fast": 9},
    "sma_slow": {"standard": 200, "fast": 21},
    "position_size_pct": {"standard": 2.0, "fast": 1.0},
    "stop_loss_pct": {"standard": 2.0, "fast": 1.0},
    "take_profit_pct": {"standard": 4.0, "fast": 2.0},
}


def _mode_default(env_name: str, config_key: str, caster):
    raw = os.getenv(env_name)
    if raw:
        return caster(raw)
    # Falls back to "standard" defaults for an unrecognized TRADING_MODE
    # instead of raising here -- validate() reports the bad value clearly.
    mode = _current_trading_mode()
    if mode not in _MODE_DEFAULTS[config_key]:
        mode = "standard"
    return _MODE_DEFAULTS[config_key][mode]


@dataclass(frozen=True)
class Config:
    # Alpaca credentials / endpoint
    api_key: str = field(default_factory=lambda: os.getenv("ALPACA_API_KEY", ""))
    secret_key: str = field(default_factory=lambda: os.getenv("ALPACA_SECRET_KEY", ""))
    paper: bool = field(default_factory=lambda: _get_bool("ALPACA_PAPER", True))
    # Welcher Markt-Daten-Feed fuer historische Bars genutzt wird. "iex" ist
    # der Default, weil kostenlose/Paper-Accounts keinen Zugriff auf "sip"
    # (Consolidated Feed) haben -- eine StockBarsRequest ohne explizites
    # feed= faellt sonst still auf "sip" zurueck und liefert dann leere
    # Ergebnisse OHNE Fehler, statt eine verstaendliche Exception zu werfen.
    # Bei einem bezahlten Account mit SIP-Zugang hier auf "sip" umstellen.
    alpaca_data_feed: str = field(default_factory=lambda: os.getenv("ALPACA_DATA_FEED", "iex").strip().lower())

    # Watchlist / timing
    watchlist_env: list[str] = field(default_factory=lambda: _get_list("WATCHLIST", ["AAPL"]))
    use_nasdaq100: bool = field(default_factory=lambda: _get_bool("USE_NASDAQ100", False))

    # "standard" (swing trading defaults) or "fast" (short-term defaults) --
    # see _MODE_DEFAULTS above for exactly which parameters this steers.
    trading_mode: str = field(default_factory=_current_trading_mode)

    timeframe: str = field(default_factory=lambda: _mode_default("TIMEFRAME", "timeframe", str))
    check_interval_minutes: float = field(
        default_factory=lambda: _mode_default("CHECK_INTERVAL_MINUTES", "check_interval_minutes", float)
    )
    # Wie oft der unabhaengige Sicherheits-Check (offene Positionen auf
    # aktives Stop-Loss pruefen) laeuft -- getrennt vom Haupt-Scan-Intervall
    # oben, weil der Sicherheits-Check zeitkritisch ist (Kurs kann zwischen
    # zwei 15-Minuten-Scans deutlich weiter fallen als beabsichtigt) und
    # bewusst haeufiger laufen soll. Nicht mode-aware -- 5 Minuten sind fuer
    # beide TRADING_MODE-Varianten ein sinnvoller Default.
    safety_check_interval_seconds: float = field(
        default_factory=lambda: _get_float("SAFETY_CHECK_INTERVAL_SECONDS", 300.0)
    )
    # Wie viele Sekunden NACH einem Kerzenschluss der Strategie-Scan startet
    # (siehe bot.seconds_until_next_scan). Der Scan richtet sich an den
    # CHECK_INTERVAL_MINUTES-Grenzen der Uhr aus (:00, :15, :30, :45 bei 15
    # Minuten) statt einfach "15 Minuten nach dem letzten Scan" zu laufen;
    # die kleine Verzoegerung gibt Alpaca Zeit, die gerade abgeschlossene
    # Kerze bereitzustellen.
    scan_delay_after_bar_close_seconds: float = field(
        default_factory=lambda: _get_float("SCAN_DELAY_AFTER_BAR_CLOSE_SECONDS", 30.0)
    )

    # Batching for the Nasdaq-100-sized watchlist (keeps requests/minute well
    # under Alpaca's rate limits instead of firing ~100 calls back to back)
    data_batch_size: int = field(default_factory=lambda: _get_int("DATA_BATCH_SIZE", 30))
    data_batch_pause_seconds: float = field(default_factory=lambda: _get_float("DATA_BATCH_PAUSE_SECONDS", 1.0))

    # Strategy parameters (SMA periods are mode-aware; RSI/MACD are not --
    # they keep the same defaults in both modes unless overridden in .env)
    sma_fast: int = field(default_factory=lambda: _mode_default("SMA_FAST", "sma_fast", int))
    sma_slow: int = field(default_factory=lambda: _mode_default("SMA_SLOW", "sma_slow", int))
    rsi_period: int = field(default_factory=lambda: _get_int("RSI_PERIOD", 14))
    rsi_overbought: float = field(default_factory=lambda: _get_float("RSI_OVERBOUGHT", 70))
    rsi_oversold: float = field(default_factory=lambda: _get_float("RSI_OVERSOLD", 30))
    macd_fast: int = field(default_factory=lambda: _get_int("MACD_FAST", 12))
    macd_slow: int = field(default_factory=lambda: _get_int("MACD_SLOW", 26))
    macd_signal: int = field(default_factory=lambda: _get_int("MACD_SIGNAL", 9))
    # Wie viele Bars zurueck ein MACD-Bullish-Crossover noch als gueltiger
    # Entry-Trigger zaehlt (siehe strategy._macd_bull_cross_within) -- der
    # Crossover passiert in der Praxis meist VOR der SMA-Trendbestaetigung,
    # ein Fenster von 1 (nur die aktuelle Bar) laesst dadurch die meisten
    # validen Einstiege durchrutschen. Nicht mode-aware.
    macd_cross_lookback_bars: int = field(default_factory=lambda: _get_int("MACD_CROSS_LOOKBACK_BARS", 5))
    # Wie viele der drei Exit-Bedingungen (Death Cross, MACD-Bear-Crossover,
    # RSI ueberkauft) gleichzeitig zutreffen muessen, bevor eine offene
    # Position verkauft wird (siehe generate_signal(), has_open_position-
    # Zweig). Default 2 statt 1: jede einzelne Bedingung allein kann laufende
    # Aufwaertstrends zu frueh abwuergen (Trend-Erfassung), waehrend 2
    # gleichzeitig weiterhin zuverlaessig vor echten Abwaertstrends schuetzt
    # (Downside-Schutz) -- 1 entspricht exakt dem alten Verhalten. Nicht
    # mode-aware.
    exit_confirmations_required: int = field(
        default_factory=lambda: _get_int("EXIT_CONFIRMATIONS_REQUIRED", 2)
    )

    # Risk management (position size / SL / TP are mode-aware; the daily loss
    # limit is a hard cap that is deliberately identical in both modes --
    # TRADING_MODE=fast must never loosen it)
    position_size_pct: float = field(
        default_factory=lambda: _mode_default("POSITION_SIZE_PCT", "position_size_pct", float)
    )
    stop_loss_pct: float = field(default_factory=lambda: _mode_default("STOP_LOSS_PCT", "stop_loss_pct", float))
    take_profit_pct: float = field(
        default_factory=lambda: _mode_default("TAKE_PROFIT_PCT", "take_profit_pct", float)
    )
    max_daily_loss_pct: float = field(default_factory=lambda: _get_float("MAX_DAILY_LOSS_PCT", 3.0))
    # Obergrenze fuer gleichzeitig offene Positionen (0 = keine Obergrenze).
    # Begrenzt zusammen mit POSITION_SIZE_PCT die Investitionsquote: 25
    # Positionen a 2% sind maximal ~50% des Kontos. Nicht mode-aware.
    max_open_positions: int = field(default_factory=lambda: _get_int("MAX_OPEN_POSITIONS", 25))

    # Pattern-Modul (Trendlinien-/Chartmuster-Erkennung, siehe patterns.py):
    # komplett optional, per Default aus -- aendert bei PATTERN_ENABLED=false
    # nichts am bestehenden Verhalten (siehe strategy.combine_with_pattern_signal).
    pattern_enabled: bool = field(default_factory=lambda: _get_bool("PATTERN_ENABLED", False))
    pattern_pivot_window: int = field(default_factory=lambda: _get_int("PATTERN_PIVOT_WINDOW", 4))
    pattern_min_pivots: int = field(default_factory=lambda: _get_int("PATTERN_MIN_TRENDLINE_PIVOTS", 3))
    pattern_max_pivots: int = field(default_factory=lambda: _get_int("PATTERN_MAX_TRENDLINE_PIVOTS", 5))
    pattern_breakout_threshold_pct: float = field(
        default_factory=lambda: _get_float("PATTERN_BREAKOUT_THRESHOLD_PCT", 0.3)
    )
    pattern_min_confidence: float = field(default_factory=lambda: _get_float("PATTERN_MIN_CONFIDENCE", 0.5))
    # "confirm"  -> Pattern-Signal muss dem bestehenden Signal zustimmen, sonst HOLD
    # "weighted" -> gewichtete Kombination aus Indikator- und Pattern-Score
    pattern_combine_mode: str = field(default_factory=lambda: os.getenv("PATTERN_COMBINE_MODE", "confirm").strip().lower())
    pattern_weight: float = field(default_factory=lambda: _get_float("PATTERN_WEIGHT", 0.4))
    pattern_sr_zone_tolerance_pct: float = field(
        default_factory=lambda: _get_float("PATTERN_SR_ZONE_TOLERANCE_PCT", 0.5)
    )

    # Logging
    trade_log_path: str = field(default_factory=lambda: os.getenv("TRADE_LOG_PATH", "trades.csv"))

    # Telegram-Benachrichtigung bei fehlgeschlagenen Trades (siehe notifier.py).
    # Komplett optional -- ohne Token/Chat-ID werden Fehlschlaege nur wie
    # bisher geloggt, es gibt keine Telegram-Nachricht und keinen Fehler.
    #
    # Zwei getrennte Ziele, EIN Bot (siehe notifier.TradeFailureNotifier fuer
    # die Kanal-Zuordnung jeder Nachrichtenart):
    # - telegram_chat_id_alerts: haeufige Routine-Meldungen (Order-Fehlschlaege,
    #   automatisch nachgelegte Stop-Loss-Orders). TELEGRAM_CHAT_ID (alter
    #   Variablenname) bleibt als Fallback gueltig, damit ein bestehendes
    #   .env ohne Aenderung weiterlaeuft.
    # - telegram_chat_id_status: seltene, wichtige Meldungen (jeder
    #   KRITISCH-Alarm) -- bewusst ein eigener Kanal, damit so etwas nicht in
    #   der Masse der Routine-Meldungen untergeht.
    telegram_bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id_alerts: str = field(
        default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID_ALERTS", os.getenv("TELEGRAM_CHAT_ID", ""))
    )
    telegram_chat_id_status: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID_STATUS", ""))

    @property
    def watchlist(self) -> list[str]:
        """The effective watchlist: the full Nasdaq-100 when USE_NASDAQ100=true,
        otherwise the fixed WATCHLIST from .env.
        """
        return list(NASDAQ_100) if self.use_nasdaq100 else self.watchlist_env

    @property
    def min_bars_required(self) -> int:
        """Minimum bar history needed before indicators are considered valid."""
        return max(self.sma_slow, self.macd_slow + self.macd_signal, self.rsi_period) + 5

    def validate(self) -> None:
        if not self.api_key or not self.secret_key:
            raise ValueError(
                "ALPACA_API_KEY / ALPACA_SECRET_KEY are missing. Copy .env.example to .env and fill them in."
            )
        if self.trading_mode not in ("standard", "fast"):
            raise ValueError(f"TRADING_MODE must be 'standard' or 'fast', got '{self.trading_mode}'.")
        if self.alpaca_data_feed not in ("iex", "sip", "delayed_sip", "otc"):
            raise ValueError(
                f"ALPACA_DATA_FEED must be one of 'iex', 'sip', 'delayed_sip', 'otc', got '{self.alpaca_data_feed}'."
            )
        if not self.watchlist:
            raise ValueError("WATCHLIST must contain at least one symbol (or set USE_NASDAQ100=true).")
        if self.data_batch_size < 1:
            raise ValueError("DATA_BATCH_SIZE must be at least 1.")
        if self.safety_check_interval_seconds <= 0:
            raise ValueError("SAFETY_CHECK_INTERVAL_SECONDS must be greater than 0.")
        if self.sma_fast >= self.sma_slow:
            raise ValueError("SMA_FAST must be smaller than SMA_SLOW.")
        if self.macd_cross_lookback_bars < 1:
            raise ValueError("MACD_CROSS_LOOKBACK_BARS must be at least 1.")
        if not (1 <= self.exit_confirmations_required <= 3):
            raise ValueError("EXIT_CONFIRMATIONS_REQUIRED must be between 1 and 3.")
        if not (0 < self.position_size_pct <= 100):
            raise ValueError("POSITION_SIZE_PCT must be between 0 and 100.")
        if not (0 < self.max_daily_loss_pct <= 100):
            raise ValueError("MAX_DAILY_LOSS_PCT must be between 0 and 100.")
        if self.max_open_positions < 0:
            raise ValueError("MAX_OPEN_POSITIONS must be 0 (no limit) or greater.")
        if self.scan_delay_after_bar_close_seconds < 0:
            raise ValueError("SCAN_DELAY_AFTER_BAR_CLOSE_SECONDS must be 0 or greater.")
        if self.pattern_combine_mode not in ("confirm", "weighted"):
            raise ValueError(
                f"PATTERN_COMBINE_MODE must be 'confirm' or 'weighted', got '{self.pattern_combine_mode}'."
            )
        if not (0.0 <= self.pattern_weight <= 1.0):
            raise ValueError("PATTERN_WEIGHT must be between 0 and 1.")
        if not (0.0 <= self.pattern_min_confidence <= 1.0):
            raise ValueError("PATTERN_MIN_CONFIDENCE must be between 0 and 1.")
        if self.pattern_pivot_window < 1:
            raise ValueError("PATTERN_PIVOT_WINDOW must be at least 1.")
        if self.pattern_min_pivots < 2:
            raise ValueError("PATTERN_MIN_TRENDLINE_PIVOTS must be at least 2 (a line needs 2+ points).")
        if self.pattern_max_pivots < self.pattern_min_pivots:
            raise ValueError("PATTERN_MAX_TRENDLINE_PIVOTS must be >= PATTERN_MIN_TRENDLINE_PIVOTS.")


def load_config() -> Config:
    config = Config()
    config.validate()
    return config
