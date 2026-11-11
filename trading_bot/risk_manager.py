"""Position sizing, stop-loss / take-profit price calculation and the daily
loss limit circuit breaker. Kept isolated from order execution so the sizing
rules or loss limit can be swapped without touching the Alpaca-facing code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from trading_bot.config import Config


def round_to_valid_tick(price: float) -> float:
    """Rundet einen Preis auf Alpacas gueltiges minimales Preis-Inkrement
    (SEC Reg NMS Rule 612, die "Sub-Penny Rule", von Alpaca durchgesetzt --
    eine Verletzung liefert den Fehler "sub-penny increment does not
    fulfill minimum pricing criteria"): Preise >= $1 muessen in Ein-Cent-
    Schritten vorliegen (2 Nachkommastellen), Preise < $1 in Schritten von
    $0.0001 (4 Nachkommastellen).

    Zentrale Stelle fuer diese Regel -- JEDER Preis, der als limit_price
    oder stop_price an Alpaca geschickt wird, muss hier durch, egal wo er
    berechnet wird (Bracket-Order-Preise, nachgelegte Schutz-Stop-Loss-
    Order, ein etwaiger zukuenftiger weiterer Preis-Berechnungspfad).
    """
    return round(price, 2) if price >= 1.0 else round(price, 4)


@dataclass
class BracketPrices:
    stop_loss: float
    take_profit: float


class RiskManager:
    def __init__(self, config: Config):
        self._config = config
        # Tag (UTC-Datum), an dem das Tagesverlust-Limit ausgeloest hat --
        # bleibt fuer den Rest dieses Tages gesetzt, auch wenn sich der
        # Kontostand zwischenzeitlich wieder erholt (siehe
        # daily_loss_limit_hit).
        self._halted_day: date | None = None

    def daily_loss_pct(self, equity: float, last_equity: float) -> float:
        """Tagesveraenderung des Kontostands in Prozent gegenueber dem
        Schlussstand des vorherigen Handelstags (Alpacas `last_equity`).
        Negativ = Verlust. 0.0, wenn kein Vortageswert vorliegt.
        """
        if last_equity <= 0:
            return 0.0
        return (equity - last_equity) / last_equity * 100

    def daily_loss_limit_hit(self, today: date, equity: float, last_equity: float) -> bool:
        """True, sobald der Kontostand heute um mindestens
        MAX_DAILY_LOSS_PCT unter dem Vortagesschluss liegt.

        Bewusst auf dem Kontostand (equity vs. last_equity, beides direkt
        von Alpaca) statt auf einem selbst mitgezaehlten "realisierten
        Tagesverlust": der alte Zaehler erfasste nur Verkaeufe per
        Strategie-Signal -- Stop-Loss-Ausfuehrungen ueber die Bracket-Order
        (dort entstehen die meisten Verluste) liefen komplett daran vorbei,
        und nach einem Neustart stand er wieder bei 0. Der Kontostand
        enthaelt dagegen alles (Stops, Signal-Exits, offene Buchverluste)
        und ueberlebt jeden Neustart, weil nichts im Speicher gehalten wird.

        Einmal ausgeloest, bleibt das Limit fuer den Rest des Tages aktiv
        (`_halted_day`), auch wenn sich der Kontostand wieder ueber die
        Schwelle erholt -- sonst wuerde der Bot an einem schwankenden Tag
        direkt an der Schwelle immer wieder ein- und aussetzen. Nach einem
        Neustart wird einfach erneut am aktuellen Kontostand gemessen.
        """
        if self._halted_day == today:
            return True
        if self.daily_loss_pct(equity, last_equity) <= -self._config.max_daily_loss_pct:
            self._halted_day = today
            return True
        return False

    def max_positions_reached(self, open_positions: int) -> bool:
        """True, wenn keine weitere Position eroeffnet werden darf
        (MAX_OPEN_POSITIONS; 0 = keine Obergrenze). Begrenzt die
        Gesamt-Investitionsquote: jede Position ist POSITION_SIZE_PCT des
        Kontos gross, ohne Obergrenze koennte der Bot sonst bei genug
        gleichzeitigen Signalen das ganze Konto in stark gleichlaufende
        Titel stecken.
        """
        limit = self._config.max_open_positions
        return limit > 0 and open_positions >= limit

    def calculate_position_size(self, equity: float, buying_power: float, price: float) -> int:
        """Fixed fractional sizing: at most position_size_pct of equity per trade,
        never exceeding available buying power.
        """
        if price <= 0:
            return 0
        capital_at_risk = equity * (self._config.position_size_pct / 100)
        capital_at_risk = min(capital_at_risk, buying_power)
        qty = int(capital_at_risk // price)
        return max(qty, 0)

    def calculate_bracket_prices(self, entry_price: float) -> BracketPrices:
        stop_loss = round_to_valid_tick(entry_price * (1 - self._config.stop_loss_pct / 100))
        take_profit = round_to_valid_tick(entry_price * (1 + self._config.take_profit_pct / 100))
        return BracketPrices(stop_loss=stop_loss, take_profit=take_profit)
