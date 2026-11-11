"""Regelbasierte, quantitative Trendlinien- und Chartmuster-Erkennung.

Bildet nach, was ein Mensch beim manuellen Chart-Reading tun wuerde --
Trendlinien durch Swing-Hochs/-Tiefs ziehen, auf Ausbrueche achten, hoehere
Tiefs/tiefere Hochs als Trendbestaetigung werten, einfache Umkehrmuster
erkennen -- aber vollstaendig deterministisch aus den OHLCV-Daten berechnet.
Keine manuell eingezeichneten Linien, keine versteckte State: jeder Aufruf
berechnet die Linien frisch aus den aktuell uebergebenen Kerzen neu.

Bewusst unabhaengig von `config.py`/`strategy.py`/Alpaca gehalten: nimmt nur
ein OHLCV-DataFrame und einfache Zahlenparameter entgegen, damit sich das
Modul komplett isoliert unit-testen laesst. Die Anbindung an die bestehende
Config-Struktur und die Kombination mit dem Indikator-Signal passiert in
strategy.py (generate_pattern_signal_from_config / combine_with_pattern_signal).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pandas as pd

Direction = Literal["bullish", "bearish", "neutral"]


@dataclass(frozen=True)
class Pivot:
    """Ein lokales Hoch oder Tief (Swing-Punkt) im Kursverlauf."""

    index: int  # Position im DataFrame (0-basiert, positional)
    price: float
    kind: Literal["high", "low"]


@dataclass(frozen=True)
class TrendLine:
    """Eine per linearer Regression durch mehrere Swing-Punkte gefittete Linie.

    kind="support"    -> durch Swing-Lows, steigende Linie (Aufwaertstrend)
    kind="resistance" -> durch Swing-Highs, fallende Linie (Abwaertstrend)
    """

    kind: Literal["support", "resistance"]
    slope: float
    intercept: float
    pivot_indices: list[int]
    r_squared: float

    def value_at(self, index: int) -> float:
        """Extrapolierter Linienwert an einer beliebigen (auch zukuenftigen) Position."""
        return self.slope * index + self.intercept

    @property
    def angle_degrees(self) -> float:
        return float(np.degrees(np.arctan(self.slope)))


@dataclass(frozen=True)
class PatternSignal:
    """Ergebnis der Pattern-Erkennung fuer den letzten Balken eines DataFrames."""

    direction: Direction
    confidence: float  # 0.0 (kein Signal) .. 1.0 (sehr stark)
    reason: str
    pattern_type: str  # z.B. "trendline_breakout_up", "higher_low", "double_top", "none"
    support_line: TrendLine | None = None
    resistance_line: TrendLine | None = None
    sr_zones: list[dict] = field(default_factory=list)

    @property
    def score(self) -> float:
        """Vorzeichenbehafteter Score fuer die gewichtete Kombination:
        +confidence bei bullish, -confidence bei bearish, 0 bei neutral.
        """
        if self.direction == "bullish":
            return self.confidence
        if self.direction == "bearish":
            return -self.confidence
        return 0.0


def find_swing_pivots(df: pd.DataFrame, window: int = 4) -> list[Pivot]:
    """Erkennt lokale Hoch-/Tiefpunkte ueber ein symmetrisches Fenster:
    ein Balken ist ein Swing-High, wenn sein High das Maximum unter den
    `window` Kerzen links UND rechts ist (analog fuer Swing-Low mit dem
    Minimum). Die letzten `window` Balken koennen noch nicht bestaetigt
    werden (es fehlen die rechten Nachbarn) und werden ausgelassen -- ein
    Pivot ist also immer erst mit `window` Kerzen Verzoegerung "fertig".
    """
    if len(df) < window * 2 + 1:
        return []

    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    n = len(df)
    pivots: list[Pivot] = []

    for i in range(window, n - window):
        high_window = highs[i - window : i + window + 1]
        # np.argmax gibt bei Gleichstand den ERSTEN Treffer zurueck -> nur
        # zaehlen, wenn das Maximum genau in der Mitte liegt, sonst wuerden
        # bei flachen Hochs mehrere benachbarte Balken faelschlich als
        # eigene Pivots markiert.
        if highs[i] == high_window.max() and np.argmax(high_window) == window:
            pivots.append(Pivot(index=i, price=float(highs[i]), kind="high"))

        low_window = lows[i - window : i + window + 1]
        if lows[i] == low_window.min() and np.argmin(low_window) == window:
            pivots.append(Pivot(index=i, price=float(lows[i]), kind="low"))

    return pivots


def _fit_line(pivots: list[Pivot]) -> tuple[float, float, float]:
    """Lineare Regression (kleinste Quadrate) durch die gegebenen Pivots.
    Gibt (slope, intercept, r_squared) zurueck.
    """
    xs = np.array([p.index for p in pivots], dtype=float)
    ys = np.array([p.price for p in pivots], dtype=float)
    slope, intercept = np.polyfit(xs, ys, 1)
    predicted = slope * xs + intercept
    ss_res = float(np.sum((ys - predicted) ** 2))
    ss_tot = float(np.sum((ys - ys.mean()) ** 2))
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return float(slope), float(intercept), r_squared


def fit_trendline(
    pivots: list[Pivot],
    kind: Literal["support", "resistance"],
    min_pivots: int = 3,
    max_pivots: int = 5,
) -> TrendLine | None:
    """Fittet eine Trendlinie durch die JUENGSTEN `max_pivots` Swing-Lows
    (kind="support", Aufwaertstrend) bzw. Swing-Highs (kind="resistance",
    Abwaertstrend). Braucht mindestens `min_pivots` passende Punkte, sonst
    None. Wird bei jedem Aufruf komplett neu aus den aktuell vorliegenden
    Pivots berechnet -- es gibt keine gespeicherte/"eingezeichnete" Linie,
    die Anzahl und Auswahl der Pivots passt sich automatisch an, wie viele
    Kerzen gerade vorliegen (Anforderung: Linien laufend neu berechnen).
    """
    wanted_kind: Literal["high", "low"] = "low" if kind == "support" else "high"
    relevant = [p for p in pivots if p.kind == wanted_kind]
    if len(relevant) < min_pivots:
        return None

    relevant = relevant[-max_pivots:]  # nur die juengsten Pivots -> Linie bleibt aktuell
    slope, intercept, r_squared = _fit_line(relevant)

    # Ein Aufwaertstrend (support) braucht steigende Tiefs (slope > 0), ein
    # Abwaertstrend (resistance) fallende Hochs (slope < 0). Widerspricht
    # die Regression dem erwarteten Verlauf, ist es kein brauchbarer Trend
    # -> lieber kein Signal als ein irrefuehrendes.
    if kind == "support" and slope <= 0:
        return None
    if kind == "resistance" and slope >= 0:
        return None

    return TrendLine(
        kind=kind,
        slope=slope,
        intercept=intercept,
        pivot_indices=[p.index for p in relevant],
        r_squared=max(0.0, r_squared),
    )


def _higher_lows(pivots: list[Pivot], count: int = 2) -> bool:
    lows = [p for p in pivots if p.kind == "low"][-count:]
    return len(lows) == count and all(lows[i].price < lows[i + 1].price for i in range(count - 1))


def _lower_highs(pivots: list[Pivot], count: int = 2) -> bool:
    highs = [p for p in pivots if p.kind == "high"][-count:]
    return len(highs) == count and all(highs[i].price > highs[i + 1].price for i in range(count - 1))


def _detect_double_top_or_bottom(pivots: list[Pivot], tolerance_pct: float) -> tuple[str, float] | None:
    """Stark vereinfachte Umkehrmuster-Erkennung: die letzten zwei
    Swing-Highs (Double Top) bzw. Swing-Lows (Double Bottom) liegen
    innerhalb `tolerance_pct` Prozent beieinander UND es liegt ein
    ausreichend tiefer Gegenzug (Tief zwischen den Hochs bzw. Hoch zwischen
    den Tiefs) dazwischen -- sonst waeren zwei zufaellig aehnlich hohe
    Ausschlaege in reinem Kursrauschen schon ein "Double Top". Eigenstaendig
    ein schwaecheres Signal als ein bestaetigter Trendlinienbruch, daher
    niedrigere Basis-Konfidenz.
    """
    highs = [p for p in pivots if p.kind == "high"]
    lows = [p for p in pivots if p.kind == "low"]

    if len(highs) >= 2:
        h1, h2 = highs[-2], highs[-1]
        avg = (h1.price + h2.price) / 2
        diff_pct = abs(h1.price - h2.price) / avg * 100 if avg else 100.0
        between_low = min((p.price for p in lows if h1.index < p.index < h2.index), default=None)
        if diff_pct <= tolerance_pct and between_low is not None:
            depth_pct = (avg - between_low) / avg * 100
            if depth_pct >= tolerance_pct:  # echter Gegenzug, nicht nur Rauschen auf gleicher Hoehe
                closeness = max(0.0, 1.0 - diff_pct / tolerance_pct)
                return "double_top", round(closeness * 0.6, 3)

    if len(lows) >= 2:
        l1, l2 = lows[-2], lows[-1]
        avg = (l1.price + l2.price) / 2
        diff_pct = abs(l1.price - l2.price) / avg * 100 if avg else 100.0
        between_high = max((p.price for p in highs if l1.index < p.index < l2.index), default=None)
        if diff_pct <= tolerance_pct and between_high is not None:
            depth_pct = (between_high - avg) / avg * 100
            if depth_pct >= tolerance_pct:
                closeness = max(0.0, 1.0 - diff_pct / tolerance_pct)
                return "double_bottom", round(closeness * 0.6, 3)

    return None


def find_support_resistance_zones(pivots: list[Pivot], tolerance_pct: float = 0.5) -> list[dict]:
    """Clustert gehaeufte Pivot-Preise zu Support-/Resistance-Zonen (einfaches
    Preis-Binning): mehrere Pivots nahe beieinander deuten auf ein Niveau
    hin, an dem der Kurs wiederholt gedreht hat. Gibt Zonen mit
    durchschnittlichem Preis und Pivot-Anzahl zurueck, groesste Zone zuerst.
    """
    zones: list[dict] = []
    for kind in ("high", "low"):
        prices = sorted(p.price for p in pivots if p.kind == kind)
        cluster: list[float] = []
        for price in prices:
            if cluster and abs(price - cluster[-1]) / cluster[-1] * 100 > tolerance_pct:
                if len(cluster) >= 2:
                    zones.append({"kind": kind, "price": sum(cluster) / len(cluster), "pivot_count": len(cluster)})
                cluster = []
            cluster.append(price)
        if len(cluster) >= 2:
            zones.append({"kind": kind, "price": sum(cluster) / len(cluster), "pivot_count": len(cluster)})
    return sorted(zones, key=lambda z: z["pivot_count"], reverse=True)


def _confidence(n_pivots: int, r_squared: float, angle_degrees: float, volume_confirmed: bool, max_pivots: int) -> float:
    """Kombiniert mehrere Qualitaetsmerkmale einer Trendlinie zu einem
    Konfidenzwert zwischen 0 und 1:
    - mehr bestaetigende Pivots -> hoehere Konfidenz (bis max_pivots gedeckelt)
    - bessere Passgenauigkeit der Regression (R^2) -> hoehere Konfidenz
    - ein deutlicherer (nicht zu flacher) Winkel -> hoehere Konfidenz
    - Volumenbestaetigung (falls verfuegbar) gibt einen kleinen Bonus
    """
    pivot_score = min(1.0, n_pivots / max_pivots)
    fit_score = max(0.0, min(1.0, r_squared))
    angle_score = min(1.0, abs(angle_degrees) / 45.0)
    base = 0.4 * pivot_score + 0.4 * fit_score + 0.2 * angle_score
    if volume_confirmed:
        base = min(1.0, base + 0.15)
    return round(base, 3)


def generate_pattern_signal(
    df: pd.DataFrame,
    pivot_window: int = 4,
    min_pivots: int = 3,
    max_pivots: int = 5,
    breakout_threshold_pct: float = 0.3,
    sr_zone_tolerance_pct: float = 0.5,
) -> PatternSignal:
    """Hauptfunktion des Moduls: erkennt Pivots, fittet Support-/Resistance-
    Trendlinien und prueft fuer den LETZTEN Balken von `df` auf einen
    signifikanten Trendlinienbruch, Trendbestaetigung durch hoehere
    Tiefs/tiefere Hochs, oder ein einfaches Double-Top/Bottom-Umkehrmuster.

    `df` braucht die Spalten 'open','high','low','close', optional 'volume'
    fuer die Volumen-Bestaetigung. Erwartet aufsteigend sortierte Zeilen
    (aeltester Balken zuerst), wie sie data_feed.py liefert.
    """
    min_bars = (pivot_window * 2 + 1) + max(min_pivots - 1, 0) * 2
    if len(df) < min_bars + pivot_window:
        return PatternSignal("neutral", 0.0, "Nicht genug Kerzen fuer Pivot-/Trendlinien-Erkennung.", "none")

    pivots = find_swing_pivots(df, window=pivot_window)
    if len(pivots) < 2:
        return PatternSignal("neutral", 0.0, "Keine ausreichenden Swing-Punkte gefunden.", "none")

    support = fit_trendline(pivots, "support", min_pivots, max_pivots)
    resistance = fit_trendline(pivots, "resistance", min_pivots, max_pivots)
    sr_zones = find_support_resistance_zones(pivots, sr_zone_tolerance_pct)

    latest_index = len(df) - 1
    latest_close = float(df["close"].iloc[-1])
    prev_close = float(df["close"].iloc[-2])

    volume_confirmed = False
    if "volume" in df.columns and len(df) > 21:
        recent_avg_volume = float(df["volume"].iloc[-21:-1].mean())
        if recent_avg_volume > 0:
            volume_confirmed = float(df["volume"].iloc[-1]) > 1.2 * recent_avg_volume

    candidates: list[PatternSignal] = []

    # 1) Trendlinienbruch nach oben: Kurs schliesst signifikant ueber der
    #    (fallenden) Resistance-Linie, im Balken davor war er noch darunter.
    if resistance is not None:
        line_value = resistance.value_at(latest_index)
        prev_line_value = resistance.value_at(latest_index - 1)
        breakout_up = prev_close <= prev_line_value and latest_close > line_value * (1 + breakout_threshold_pct / 100)
        if breakout_up:
            n_pivots = len(resistance.pivot_indices)
            confidence = _confidence(n_pivots, resistance.r_squared, resistance.angle_degrees, volume_confirmed, max_pivots)
            candidates.append(
                PatternSignal(
                    "bullish",
                    confidence,
                    f"Ausbruch ueber Abwaertstrendlinie ({n_pivots} Hochs, "
                    f"Winkel {resistance.angle_degrees:.1f}°, R²={resistance.r_squared:.2f})"
                    + (", Volumen bestaetigt" if volume_confirmed else ""),
                    "trendline_breakout_up",
                    support,
                    resistance,
                    sr_zones,
                )
            )

    # 2) Trendlinienbruch nach unten: Bruch der (steigenden) Support-Linie.
    if support is not None:
        line_value = support.value_at(latest_index)
        prev_line_value = support.value_at(latest_index - 1)
        breakout_down = prev_close >= prev_line_value and latest_close < line_value * (1 - breakout_threshold_pct / 100)
        if breakout_down:
            n_pivots = len(support.pivot_indices)
            confidence = _confidence(n_pivots, support.r_squared, support.angle_degrees, volume_confirmed, max_pivots)
            candidates.append(
                PatternSignal(
                    "bearish",
                    confidence,
                    f"Bruch der Aufwaertstrendlinie ({n_pivots} Tiefs, "
                    f"Winkel {support.angle_degrees:.1f}°, R²={support.r_squared:.2f})"
                    + (", Volumen bestaetigt" if volume_confirmed else ""),
                    "trendline_breakdown",
                    support,
                    resistance,
                    sr_zones,
                )
            )

    # 3) Trendbestaetigung ohne Bruch: hoehere Tiefs / tiefere Hochs.
    if not candidates and _higher_lows(pivots):
        candidates.append(
            PatternSignal("bullish", 0.4, "Hoehere Tiefs bestaetigen Aufwaertstrend.", "higher_low", support, resistance, sr_zones)
        )
    if not candidates and _lower_highs(pivots):
        candidates.append(
            PatternSignal("bearish", 0.4, "Tiefere Hochs bestaetigen Abwaertstrend.", "lower_high", support, resistance, sr_zones)
        )

    # 4) Einfaches Umkehrmuster (optional laut Anforderung, schwaechere Konfidenz).
    double_pattern = _detect_double_top_or_bottom(pivots, tolerance_pct=sr_zone_tolerance_pct * 2)
    if double_pattern:
        pattern_type, confidence = double_pattern
        direction: Direction = "bearish" if pattern_type == "double_top" else "bullish"
        candidates.append(
            PatternSignal(
                direction,
                confidence,
                f"{pattern_type.replace('_', ' ').title()} erkannt.",
                pattern_type,
                support,
                resistance,
                sr_zones,
            )
        )

    if not candidates:
        return PatternSignal("neutral", 0.0, "Kein Trendlinienbruch/-muster erkannt.", "none", support, resistance, sr_zones)

    return max(candidates, key=lambda c: c.confidence)
