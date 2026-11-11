"""Appends every trade decision (and the reasoning behind it) to a CSV file."""

from __future__ import annotations

import csv
import os
import threading
from dataclasses import dataclass, fields
from datetime import datetime, timezone


@dataclass
class TradeLogEntry:
    timestamp: str
    symbol: str
    action: str
    qty: int
    price: float
    stop_loss: float
    take_profit: float
    reason: str
    order_id: str
    status: str


class TradeLogger:
    def __init__(self, path: str):
        self._path = path
        self._fieldnames = [f.name for f in fields(TradeLogEntry)]
        # Der Haupt-Scan und der unabhaengige Sicherheits-Check laufen in
        # getrennten Threads und koennen beide gleichzeitig loggen (siehe
        # bot.py) -- ohne Lock koennten zwei parallele Schreibzugriffe auf
        # dieselbe CSV-Datei sich gegenseitig ueberschreiben oder Zeilen
        # verstuemmeln.
        self._lock = threading.Lock()
        if not os.path.exists(self._path) or os.path.getsize(self._path) == 0:
            with open(self._path, "w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=self._fieldnames).writeheader()

    def log(
        self,
        symbol: str,
        action: str,
        reason: str,
        qty: int = 0,
        price: float = 0.0,
        stop_loss: float = 0.0,
        take_profit: float = 0.0,
        order_id: str = "",
        status: str = "",
    ) -> None:
        entry = TradeLogEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            symbol=symbol,
            action=action,
            qty=qty,
            price=price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            reason=reason,
            order_id=order_id,
            status=status,
        )
        with self._lock:
            with open(self._path, "a", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=self._fieldnames).writerow(entry.__dict__)
