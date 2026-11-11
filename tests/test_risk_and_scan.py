"""Tests fuer Tagesverlust-Limit, Positions-Obergrenze, Kerzen-Filter und
Scan-Ausrichtung. Ausfuehren vom Projekt-Root:

    python -m unittest discover -s tests -v

Alpaca wird komplett durch Mocks ersetzt, kein Netzwerkzugriff.
"""

import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd

from trading_bot.bot import TradingBot, seconds_until_next_scan
from trading_bot.config import Config
from trading_bot.data_feed import drop_incomplete_bars, timeframe_to_offset
from trading_bot.risk_manager import RiskManager
from trading_bot.strategy import Action, SignalResult
from trading_bot.trade_logger import TradeLogger


def make_config(**overrides) -> Config:
    return Config(api_key="test", secret_key="test", watchlist_env=["AAPL", "MSFT", "NVDA"], **overrides)


def make_bars(last_start: str, periods: int = 5, freq: str = "15min") -> pd.DataFrame:
    index = pd.date_range(end=pd.Timestamp(last_start, tz="UTC"), periods=periods, freq=freq)
    return pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1000}, index=index
    )


class DailyLossLimitTest(unittest.TestCase):
    def setUp(self):
        self.rm = RiskManager(make_config(max_daily_loss_pct=3.0))
        self.day = date(2026, 11, 11)

    def test_not_hit_above_threshold(self):
        self.assertFalse(self.rm.daily_loss_limit_hit(self.day, 97_100.0, 100_000.0))

    def test_hit_at_threshold(self):
        self.assertTrue(self.rm.daily_loss_limit_hit(self.day, 97_000.0, 100_000.0))

    def test_counts_losses_however_they_happened(self):
        # Der alte Zaehler sah Stop-Loss-Ausfuehrungen nie. Der Kontostand
        # enthaelt sie -- ein frischer RiskManager (= nach Neustart) loest
        # allein aus equity/last_equity aus.
        fresh_after_restart = RiskManager(make_config(max_daily_loss_pct=3.0))
        self.assertTrue(fresh_after_restart.daily_loss_limit_hit(self.day, 96_000.0, 100_000.0))

    def test_stays_hit_for_the_day_even_if_equity_recovers(self):
        self.assertTrue(self.rm.daily_loss_limit_hit(self.day, 96_500.0, 100_000.0))
        self.assertTrue(self.rm.daily_loss_limit_hit(self.day, 99_500.0, 100_000.0))

    def test_resets_on_next_day(self):
        self.assertTrue(self.rm.daily_loss_limit_hit(self.day, 96_500.0, 100_000.0))
        self.assertFalse(self.rm.daily_loss_limit_hit(date(2026, 11, 12), 96_600.0, 96_500.0))

    def test_missing_last_equity_never_triggers(self):
        self.assertFalse(self.rm.daily_loss_limit_hit(self.day, 50_000.0, 0.0))


class MaxPositionsTest(unittest.TestCase):
    def test_limit(self):
        rm = RiskManager(make_config(max_open_positions=25))
        self.assertFalse(rm.max_positions_reached(24))
        self.assertTrue(rm.max_positions_reached(25))
        self.assertTrue(rm.max_positions_reached(26))

    def test_zero_means_unlimited(self):
        self.assertFalse(RiskManager(make_config(max_open_positions=0)).max_positions_reached(500))

    def test_negative_rejected_by_validate(self):
        with self.assertRaises(ValueError):
            make_config(max_open_positions=-1).validate()


class DropIncompleteBarsTest(unittest.TestCase):
    def test_running_bar_is_dropped(self):
        bars = make_bars("2026-11-11 15:15")  # laeuft bis 15:30
        now = datetime(2026, 11, 11, 15, 22, tzinfo=timezone.utc)
        result = drop_incomplete_bars(bars, "15Min", now)
        self.assertEqual(len(result), 4)
        self.assertEqual(result.index[-1], pd.Timestamp("2026-11-11 15:00", tz="UTC"))

    def test_finished_bar_is_kept(self):
        bars = make_bars("2026-11-11 15:15")
        now = datetime(2026, 11, 11, 15, 30, 30, tzinfo=timezone.utc)
        self.assertEqual(len(drop_incomplete_bars(bars, "15Min", now)), 5)

    def test_bar_ending_exactly_now_is_kept(self):
        bars = make_bars("2026-11-11 15:15")
        now = datetime(2026, 11, 11, 15, 30, tzinfo=timezone.utc)
        self.assertEqual(len(drop_incomplete_bars(bars, "15Min", now)), 5)

    def test_daily_bar_of_running_day_is_dropped(self):
        bars = make_bars("2026-11-11 05:00", freq="1D")
        now = datetime(2026, 11, 11, 16, 0, tzinfo=timezone.utc)
        self.assertEqual(len(drop_incomplete_bars(bars, "1Day", now)), 4)

    def test_empty_and_none_pass_through(self):
        self.assertIsNone(drop_incomplete_bars(None, "15Min"))
        empty = pd.DataFrame()
        self.assertTrue(drop_incomplete_bars(empty, "15Min").empty)

    def test_naive_index_is_treated_as_utc(self):
        bars = make_bars("2026-11-11 15:15")
        bars.index = bars.index.tz_localize(None)
        now = datetime(2026, 11, 11, 15, 22, tzinfo=timezone.utc)
        self.assertEqual(len(drop_incomplete_bars(bars, "15Min", now)), 4)

    def test_offsets(self):
        start = pd.Timestamp("2026-01-31 00:00", tz="UTC")
        self.assertEqual(start + timeframe_to_offset("5Min"), pd.Timestamp("2026-01-31 00:05", tz="UTC"))
        self.assertEqual(start + timeframe_to_offset("1Hour"), pd.Timestamp("2026-01-31 01:00", tz="UTC"))
        self.assertEqual(start + timeframe_to_offset("1Month"), pd.Timestamp("2026-02-28 00:00", tz="UTC"))
        with self.assertRaises(ValueError):
            timeframe_to_offset("abc")


class SecondsUntilNextScanTest(unittest.TestCase):
    @staticmethod
    def epoch(h, m, s=0):
        return datetime(2026, 11, 11, h, m, s, tzinfo=timezone.utc).timestamp()

    def test_mid_interval_waits_until_boundary_plus_delay(self):
        self.assertAlmostEqual(seconds_until_next_scan(self.epoch(15, 7), 900, 30), 8 * 60 + 30)

    def test_right_after_a_scan_waits_a_full_interval(self):
        self.assertAlmostEqual(seconds_until_next_scan(self.epoch(15, 15, 30), 900, 30), 900)

    def test_between_boundary_and_delay_waits_only_the_rest(self):
        self.assertAlmostEqual(seconds_until_next_scan(self.epoch(15, 15, 10), 900, 30), 20)

    def test_long_scan_does_not_drift(self):
        # Scan lief 4 Minuten: der naechste startet trotzdem um :30:30.
        self.assertAlmostEqual(seconds_until_next_scan(self.epoch(15, 19, 30), 900, 30), 11 * 60)

    def test_five_minute_interval(self):
        self.assertAlmostEqual(seconds_until_next_scan(self.epoch(15, 7), 300, 30), 3 * 60 + 30)


class RunCycleRiskTest(unittest.TestCase):
    """run_cycle() mit gemocktem Alpaca: Limit sperrt nur Einstiege,
    Obergrenze begrenzt Kaeufe, Stop-Verluste zaehlen mit."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def make_bot(self, *, equity, last_equity, positions=None, **config_overrides):
        config = make_config(**config_overrides)
        data_feed = MagicMock()
        data_feed.is_market_open.return_value = True
        bars = make_bars("2026-11-11 15:00", periods=300)
        data_feed.get_bars_batch.return_value = {s: bars for s in config.watchlist}

        executor = MagicMock()
        account = MagicMock()
        account.equity = str(equity)
        account.last_equity = str(last_equity)
        account.buying_power = str(equity)
        executor.get_account.return_value = account
        executor.get_all_positions.return_value = positions or {}
        order = MagicMock()
        order.id, order.status = "order-1", "accepted"
        executor.submit_bracket_buy.return_value = order
        executor.close_position.return_value = order

        logger = TradeLogger(os.path.join(self.tmp, f"trades_{id(config)}.csv"))
        bot = TradingBot(config, data_feed, executor, RiskManager(config), logger, MagicMock())
        return bot, executor

    @staticmethod
    def position(qty="10"):
        p = MagicMock()
        p.qty, p.unrealized_pl = qty, "-50"
        return p

    def run_with_signal(self, bot, signal_for):
        def fake_signal(df, config, has_open_position):
            return SignalResult(signal_for(has_open_position), "test signal", {})

        with patch("trading_bot.bot.generate_signal", side_effect=fake_signal):
            bot.run_cycle()

    def test_buys_when_no_limit_is_hit(self):
        bot, executor = self.make_bot(equity=100_000, last_equity=100_000)
        self.run_with_signal(bot, lambda has_pos: Action.BUY)
        self.assertEqual(executor.submit_bracket_buy.call_count, 3)

    def test_daily_loss_blocks_entries_but_not_exits(self):
        bot, executor = self.make_bot(
            equity=96_000, last_equity=100_000, positions={"AAPL": self.position()}
        )
        self.run_with_signal(bot, lambda has_pos: Action.SELL if has_pos else Action.BUY)
        executor.submit_bracket_buy.assert_not_called()
        executor.close_position.assert_called_once_with("AAPL")

    def test_position_cap_limits_buys_within_one_scan(self):
        bot, executor = self.make_bot(
            equity=100_000, last_equity=100_000, positions={"TSLA": self.position()}, max_open_positions=2
        )
        self.run_with_signal(bot, lambda has_pos: Action.BUY)
        self.assertEqual(executor.submit_bracket_buy.call_count, 1)

    def test_phantom_position_does_not_count_towards_cap(self):
        bot, executor = self.make_bot(
            equity=100_000, last_equity=100_000, positions={"TSLA": self.position(qty="0")}, max_open_positions=1
        )
        self.run_with_signal(bot, lambda has_pos: Action.BUY)
        self.assertEqual(executor.submit_bracket_buy.call_count, 1)

    def test_running_bar_is_not_evaluated(self):
        bot, _ = self.make_bot(equity=100_000, last_equity=100_000)
        seen_last_bars = []

        def fake_signal(df, config, has_open_position):
            seen_last_bars.append(df.index[-1])
            return SignalResult(Action.HOLD, "hold", {})

        running = pd.Timestamp.now(tz="UTC").floor("15min")
        bars = make_bars(str(running.tz_localize(None)), periods=300)
        bot._data_feed.get_bars_batch.return_value = {s: bars for s in bot._config.watchlist}
        with patch("trading_bot.bot.generate_signal", side_effect=fake_signal):
            bot.run_cycle()
        self.assertTrue(seen_last_bars)
        for last in seen_last_bars:
            self.assertLess(last, running)


if __name__ == "__main__":
    unittest.main()
