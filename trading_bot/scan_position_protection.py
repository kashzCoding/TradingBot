"""Einmaliger, rein lesender Scan ueber alle aktuell offenen Positionen:
prueft fuer jede, ob ein aktives Stop-Loss-Leg existiert, und falls nicht,
ob eine verwaiste Take-Profit-Order (siehe order_executor.is_orphaned_take_profit_order)
die Aktien blockiert oder eine unklare andere Order vorliegt.

Macht KEINE Aenderungen -- kein Cancel, kein neuer Order-Submit. Reines
Reporting, gedacht um VOR dem scharfen Rollout von
bot._check_position_protection() zu sehen, wie viele Positionen im
Portfolio tatsaechlich betroffen sind (nicht nur die, die zufaellig
gerade einen Alert ausgeloest haben).

CLI-Nutzung (vom Projekt-Root aus, mit gueltigen Alpaca-Keys in .env):
    python -m trading_bot.scan_position_protection
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from trading_bot.config import load_config
from trading_bot.order_executor import OrderExecutor, is_orphaned_take_profit_order

logger = logging.getLogger("trading_bot")


@dataclass
class BlockingOrderInfo:
    id: str
    type: str
    order_class: str
    status: str
    created_at: object
    qty: object
    is_orphaned_take_profit: bool


@dataclass
class PositionProtectionStatus:
    symbol: str
    qty: float
    has_active_stop: bool
    blocking_orders: list[BlockingOrderInfo] = field(default_factory=list)

    @property
    def fully_orphaned(self) -> bool:
        """True, wenn die Position ungeschuetzt ist UND ALLE blockierenden
        Orders eindeutig verwaiste Take-Profit-Legs sind (automatisch
        reparierbar durch bot._check_position_protection)."""
        return (
            not self.has_active_stop
            and bool(self.blocking_orders)
            and all(o.is_orphaned_take_profit for o in self.blocking_orders)
        )

    @property
    def unclear(self) -> bool:
        """True, wenn die Position ungeschuetzt ist UND mindestens eine
        blockierende Order NICHT eindeutig als verwaiste Take-Profit-Order
        erkennbar ist (manuelle Pruefung noetig, keine Automatik)."""
        return not self.has_active_stop and any(not o.is_orphaned_take_profit for o in self.blocking_orders)

    @property
    def unprotected_without_blocker(self) -> bool:
        """True, wenn die Position ungeschuetzt ist, aber gar keine
        blockierende Order gefunden wurde (direkt per neuem Stop reparierbar,
        ohne vorherige Stornierung)."""
        return not self.has_active_stop and not self.blocking_orders


def scan_portfolio_protection(executor: OrderExecutor) -> dict[str, PositionProtectionStatus]:
    """Geht einmalig ueber alle offenen Positionen und ermittelt fuer jede
    den Schutzstatus. Rein lesend -- ruft nur get_all_positions(),
    has_active_stop_loss() und get_open_orders() auf, nichts Schreibendes.
    """
    positions = executor.get_all_positions()
    results: dict[str, PositionProtectionStatus] = {}

    for symbol, position in positions.items():
        qty = float(position.qty)
        if qty <= 0:
            continue

        has_stop = executor.has_active_stop_loss(symbol)
        blocking_orders: list[BlockingOrderInfo] = []
        if not has_stop:
            for order in executor.get_open_orders(symbol):
                blocking_orders.append(
                    BlockingOrderInfo(
                        id=str(order.id),
                        type=getattr(order.type, "value", str(order.type)),
                        order_class=getattr(order.order_class, "value", str(order.order_class)),
                        status=getattr(order.status, "value", str(order.status)),
                        created_at=order.created_at,
                        qty=order.qty,
                        is_orphaned_take_profit=is_orphaned_take_profit_order(order),
                    )
                )

        results[symbol] = PositionProtectionStatus(
            symbol=symbol, qty=qty, has_active_stop=has_stop, blocking_orders=blocking_orders
        )

    return results


def format_report(results: dict[str, PositionProtectionStatus]) -> str:
    lines = [f"=== Portfolio-Schutz-Scan: {len(results)} offene Positionen ===", ""]

    for symbol in sorted(results):
        r = results[symbol]
        if r.has_active_stop:
            lines.append(f"{symbol:8} OK      -- aktives Stop-Loss-Leg vorhanden ({r.qty:g} Stk.)")
        elif r.fully_orphaned:
            blockers = "; ".join(f"{o.id} ({o.type}/{o.order_class}, seit {o.created_at})" for o in r.blocking_orders)
            lines.append(f"{symbol:8} FEHLT   -- verwaiste Take-Profit-Order (automatisch reparierbar): {blockers}")
        elif r.unclear:
            blockers = "; ".join(
                f"{o.id} ({o.type}/{o.order_class}/{o.status}, seit {o.created_at})"
                for o in r.blocking_orders
                if not o.is_orphaned_take_profit
            )
            lines.append(f"{symbol:8} FEHLT   -- unklare blockierende Order(n), manuelle Pruefung noetig: {blockers}")
        else:
            lines.append(f"{symbol:8} FEHLT   -- keine blockierende Order gefunden (direkt per neuem Stop reparierbar)")

    unprotected = [r for r in results.values() if not r.has_active_stop]
    orphaned = [r for r in unprotected if r.fully_orphaned]
    unclear = [r for r in unprotected if r.unclear]
    plain = [r for r in unprotected if r.unprotected_without_blocker]

    lines += [
        "",
        "--- Zusammenfassung ---",
        f"Positionen gesamt: {len(results)}",
        f"Mit aktivem Stop-Loss: {len(results) - len(unprotected)}",
        f"Ohne aktiven Stop-Loss: {len(unprotected)}",
        f"  davon verwaiste Take-Profit-Order (automatisch reparierbar): {len(orphaned)}",
        f"  davon unklare blockierende Order (manuelle Pruefung noetig): {len(unclear)}",
        f"  davon ganz ohne blockierende Order (direkt per neuem Stop reparierbar): {len(plain)}",
    ]
    return "\n".join(lines)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    config = load_config()
    executor = OrderExecutor(config)
    results = scan_portfolio_protection(executor)
    print(format_report(results))


if __name__ == "__main__":
    main()
