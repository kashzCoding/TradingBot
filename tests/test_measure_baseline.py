import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pandas as pd

from trading_bot import measure_baseline as mb
from trading_bot.config import Config


def ts(day: int) -> int:
    return int(datetime(2026, 10, day, 13, 30, tzinfo=timezone.utc).timestamp())


class MetricsTest(unittest.TestCase):
    def test_total_return(self):
        self.assertAlmostEqual(mb.total_return_pct([100.0, 110.0, 105.0]), 5.0)
        self.assertEqual(mb.total_return_pct([100.0]), 0.0)

    def test_max_drawdown(self):
        self.assertAlmostEqual(mb.max_drawdown_pct([100.0, 120.0, 90.0, 130.0]), -25.0)
        self.assertEqual(mb.max_drawdown_pct([100.0, 101.0, 102.0]), 0.0)

    def test_equity_series_skips_none_and_zero(self):
        raw = {"timestamp": [ts(1), ts(2), None, ts(5)], "equity": [0, 100000.0, 5.0, None]}
        series = mb.equity_series(raw)
        self.assertEqual(list(series), [100000.0])


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.config = Config(api_key="t", secret_key="t")

    def test_performance_report_compares_same_period(self):
        executor = MagicMock()
        executor.get_portfolio_history.return_value = {
            "timestamp": [ts(1), ts(2), ts(5)],
            "equity": [100000.0, 99000.0, 102000.0],
        }
        index = pd.to_datetime(["2026-09-30 04:00", "2026-10-01 04:00", "2026-10-02 04:00", "2026-10-05 04:00"], utc=True)
        bench = pd.DataFrame({"close": [400.0, 500.0, 510.0, 550.0]}, index=index)
        with patch.object(mb, "_fetch_bars", return_value=bench):
            text = "\n".join(mb.report_performance(executor, MagicMock(), self.config, "QQQ"))
        self.assertIn("+2.00 %", text)   # Konto 100000 -> 102000
        self.assertIn("+10.00 %", text)  # QQQ 500 -> 550, der 30.09. zaehlt nicht mit
        self.assertIn("-8.00 Prozentpunkte", text)

    def test_running_bar_detected(self):
        now = datetime.now(timezone.utc)
        running_start = pd.Timestamp(now).floor("15min")
        index = pd.DatetimeIndex([running_start - timedelta(minutes=15), running_start])
        bars = pd.DataFrame({"close": [1.0, 2.0]}, index=index)
        with patch.object(mb, "_fetch_bars", return_value=bars):
            text = "\n".join(mb.report_running_bar(MagicMock(), self.config, "QQQ"))
        self.assertIn("ERGEBNIS: JA", text)

    def test_finished_bar_detected(self):
        index = pd.to_datetime(["2026-10-01 15:00", "2026-10-01 15:15"], utc=True)
        bars = pd.DataFrame({"close": [1.0, 2.0]}, index=index)
        with patch.object(mb, "_fetch_bars", return_value=bars):
            text = "\n".join(mb.report_running_bar(MagicMock(), self.config, "QQQ"))
        self.assertIn("ERGEBNIS: NEIN", text)


if __name__ == "__main__":
    unittest.main()
