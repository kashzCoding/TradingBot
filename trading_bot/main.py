"""Entry point: wires up all modules and starts the trading loop.

Run with:  python -m trading_bot.main
"""

from __future__ import annotations

import logging
import sys

from trading_bot.bot import TradingBot
from trading_bot.config import load_config
from trading_bot.data_feed import MarketDataFeed
from trading_bot.order_executor import OrderExecutor
from trading_bot.risk_manager import RiskManager
from trading_bot.trade_logger import TradeLogger


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        config = load_config()
    except ValueError as exc:
        logging.error("Configuration error: %s", exc)
        sys.exit(1)

    executor = OrderExecutor(config)
    data_feed = MarketDataFeed(config)
    risk_manager = RiskManager(config)
    trade_logger = TradeLogger(config.trade_log_path)

    bot = TradingBot(config, data_feed, executor, risk_manager, trade_logger)

    try:
        bot.run_forever()
    except KeyboardInterrupt:
        logging.info("Shutdown requested — exiting cleanly.")


if __name__ == "__main__":
    main()
