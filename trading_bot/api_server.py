"""Read-only Status-API fuer das Mobile-Dashboard.

Laeuft NEBEN dem Trading-Bot (gleicher Rechner, z. B. Raspberry Pi), liest
dieselben Alpaca-Keys aus trading_bot/.env ueber die bestehende Config/
OrderExecutor-Logik (keine eigene Alpaca-Anbindung, kein doppelter Code) und
stellt zwei Endpoints bereit, die die statische index.html (GitHub Pages)
per fetch() abfragt: `/api/status` (Account/Positionen/letzte Trades) und
`/api/equity-history` (Kontostand-Verlauf fuer den Chart, siehe
build_equity_history_payload).

Nur lesend: es werden keine Order-Endpunkte exponiert. Zugriff ausschliesslich
mit gueltigem Header `X-Dashboard-Token`, Token wird beim ersten Start
generiert und in dashboard_token.txt (neben dieser Datei, nicht eingecheckt)
abgelegt.

Auf schwache Hardware (Raspberry Pi Zero W) zugeschnitten: `/api/status`
wird STATUS_CACHE_SECONDS lang zwischengespeichert (siehe
_get_status_cached), `_read_recent_trades()` liest trades.csv nur vom Ende
her rueckwaerts statt die ganze (potenziell ueber Monate gewachsene) Datei
zu parsen, und Antworten ab GZIP_MIN_BYTES werden komprimiert, wenn der
Client es akzeptiert (siehe _maybe_gzip_response).

Laeuft ueber HTTPS mit dem per `sudo tailscale cert` erzeugten
Tailscale-TLS-Zertifikat (Dateien liegen im Projekt-Root, nicht im
trading_bot-Unterordner).

Start (immer vom Projekt-Root aus, wie main.py):
    python -m trading_bot.api_server
"""

from __future__ import annotations

import csv
import gzip
import logging
import secrets
import stat
import sys
import threading
import time
from datetime import date, datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

from trading_bot.config import Config, load_config
from trading_bot.order_executor import OrderExecutionError, OrderExecutor

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("dashboard_api")

TOKEN_PATH = Path(__file__).resolve().parent / "dashboard_token.txt"
MAX_RECENT_TRADES = 10
STATUS_CACHE_SECONDS = 15.0
GZIP_MIN_BYTES = 1024

# Kontostand-Verlauf (siehe build_equity_history_payload). Alpaca kennt kein
# period="all" -- 20A (20 Jahre) ist grosszuegig mehr als jedes Paper-/
# Live-Konto jemals alt sein kann, Alpaca schneidet die Antwort automatisch
# auf die tatsaechlich vorhandene Historie zu (kein Padding mit leeren Tagen
# davor, siehe Alpaca-Doku: taegliche Aufloesung hat kein Limit, Daten gibt
# es ab Account-Erstellung).
EQUITY_HISTORY_PERIODS: dict[str, str] = {"1w": "1W", "1m": "1M", "3m": "3M", "all": "20A"}
EQUITY_HISTORY_CACHE_SECONDS = 300.0
BERLIN_TZ = ZoneInfo("Europe/Berlin")

# Diese Datei liegt in trading_bot/, das Projekt-Root ist eine Ebene hoeher --
# die Zertifikatsdateien liegen im Root, nicht im trading_bot-Unterordner.
# Ueber Path(__file__) aufgeloest, damit das unabhaengig vom aktuellen
# Arbeitsverzeichnis funktioniert (genau wie main.py per `-m` gestartet).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
TAILSCALE_HOSTNAME = "tradingbot.tailed8a6b.ts.net"
TLS_CERT_PATH = PROJECT_ROOT / f"{TAILSCALE_HOSTNAME}.crt"
TLS_KEY_PATH = PROJECT_ROOT / f"{TAILSCALE_HOSTNAME}.key"


def _load_or_create_token() -> str:
    """Beim allerersten Start wird ein zufaelliges Token generiert und
    dauerhaft in dashboard_token.txt gespeichert (Dateirechte auf "nur
    Owner darf lesen/schreiben" beschraenkt) -- bei jedem weiteren Start
    wird dasselbe Token wiederverwendet, damit sich das Dashboard nicht
    nach jedem Neustart neu konfigurieren muss.
    """
    if TOKEN_PATH.exists():
        return TOKEN_PATH.read_text(encoding="utf-8").strip()

    token = secrets.token_urlsafe(32)
    TOKEN_PATH.write_text(token, encoding="utf-8")
    TOKEN_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600 -- nur der Owner
    logger.info("Neues Dashboard-Token erzeugt und in %s gespeichert.", TOKEN_PATH)
    logger.info("Dashboard-Token (einmalig in die Einstellungen der Dashboard-Seite eintragen): %s", token)
    return token


def _resolve_ssl_context() -> tuple[str, str]:
    """Prueft, dass die Tailscale-TLS-Zertifikatsdateien im Projekt-Root
    vorhanden sind, und gibt ihre Pfade als (cert, key)-Tupel fuer
    app.run(ssl_context=...) zurueck. Bricht mit einer klaren Fehlermeldung
    ab statt mit einem unklaren Traceback, falls eine Datei fehlt.
    """
    missing = [p for p in (TLS_CERT_PATH, TLS_KEY_PATH) if not p.exists()]
    if missing:
        logger.error(
            "TLS-Zertifikat(e) nicht gefunden: %s\n"
            "Erwartet werden '%s' und '%s' im Projekt-Root (%s), nicht im "
            "trading_bot-Unterordner.\n"
            "Falls die Tailscale-Zertifikate fehlen oder abgelaufen sind, auf "
            "dem Pi erneut ausfuehren: sudo tailscale cert %s",
            ", ".join(str(p) for p in missing),
            TLS_CERT_PATH.name,
            TLS_KEY_PATH.name,
            PROJECT_ROOT,
            TAILSCALE_HOSTNAME,
        )
        sys.exit(1)

    return (str(TLS_CERT_PATH), str(TLS_KEY_PATH))


def _require_token(token: str) -> Callable:
    """Decorator-Fabrik statt eines globalen Tokens -- macht create_app()
    unabhaengig von Modul-Globals und damit einfach mit einem Fake-Token
    testbar.
    """

    def decorator(view: Callable) -> Callable:
        @wraps(view)
        def wrapped(*args, **kwargs):
            if request.headers.get("X-Dashboard-Token") != token:
                return jsonify({"error": "unauthorized"}), 401
            return view(*args, **kwargs)

        return wrapped

    return decorator


def _parse_trade_log_row(fieldnames: list[str], raw_line: bytes, path: str) -> dict | None:
    """Parst eine einzelne, schon als vollstaendig erkannte CSV-Datenzeile
    (roh, noch mit evtl. trailing '\\r' -- TradeLogger schreibt im
    'excel'-Dialekt mit '\\r\\n'-Zeilenende) zu demselben dict-Format, das
    vorher csv.DictReader + die manuelle Feld-Konvertierung lieferten.

    Gibt None zurueck (statt zu werfen) bei allem, was nicht als gueltige
    BUY/SELL-Zeile durchgeht -- eine andere action (HOLD/ALERT/...), eine
    kaputte/unvollstaendige Zeile (z.B. ein gerade erst halb geschriebener
    letzter Datensatz, waehrend der Bot parallel in dieselbe Datei
    schreibt), oder eine Zeile mit falscher Feldanzahl. Das Verhalten ist
    bewusst identisch zum alten `except (KeyError, ValueError): skip`.
    """
    text = raw_line.rstrip(b"\r").decode("utf-8", errors="replace")
    if not text:
        return None
    try:
        fields = next(csv.reader([text]))
    except csv.Error:
        return None
    if len(fields) != len(fieldnames):
        # Klassischer Fingerabdruck einer abgeschnittenen/halb geschriebenen
        # Zeile (Feld fehlt oder ein offenes Anfuehrungszeichen hat den
        # Rest der Zeile verschluckt) -- kein Logging bei JEDER dieser
        # Zeilen noetig, HOLD/ALERT-Zeilen mit anderem Feld-Layout gibt es
        # in dieser CSV nicht, nur wirklich kaputte Zeilen landen hier.
        return None
    row = dict(zip(fieldnames, fields))

    if row.get("action") not in ("BUY", "SELL"):
        return None
    try:
        return {
            "timestamp": row["timestamp"],
            "symbol": row["symbol"],
            "action": row["action"],
            "qty": int(float(row["qty"])),
            "price": float(row["price"]),
            "reason": row["reason"],
        }
    except (KeyError, ValueError):
        logger.warning("Ueberspringe unlesbare Zeile in %s: %r", path, row)
        return None


def _read_recent_trades(path: str, limit: int = MAX_RECENT_TRADES, chunk_size: int = 65536) -> list[dict]:
    """Liest die letzten `limit` BUY/SELL-Zeilen aus trades.csv (HOLD/HALT/
    ERROR/ALERT-Zeilen zaehlen nicht als "Trade"), neueste zuerst.

    Liest dafuer NUR vom Dateiende her rueckwaerts in `chunk_size`-Bloecken,
    statt die komplette Datei vorwaerts zu parsen -- bei einer ueber Monate
    gewachsenen trades.csv (auf dem Pi Zero gemessen: 6.9 MB / 57644 Zeilen,
    7+ Sekunden fuer einen kompletten Vorwaerts-Scan) ist das der
    dominierende Kostenfaktor von /api/status, obwohl uns nur die letzten
    `limit` Zeilen interessieren.

    Algorithmus (Standard-"Tail"-Technik, rueckwaerts in Bytes statt
    Zeilen): die Kopfzeile wird einmalig vom Dateianfang gelesen (fuer die
    Spaltennamen UND um deren Byte-Laenge als Abbruchgrenze zu kennen),
    danach wird in chunk_size-Schritten vom Dateiende Richtung Kopfzeile
    gelesen. Jeder Chunk wird an '\\n' in Zeilen zerlegt; die jeweils erste
    Zeile eines Chunks ist fast immer am Chunk-Rand abgeschnitten (ihr
    wahrer Anfang liegt im naechsten, weiter vorne liegenden Chunk) und
    wird deshalb als `pending_prefix` an dessen letzte Zeile angehaengt,
    statt sofort geparst zu werden -- das verhindert, dass eine echte Zeile
    an einer zufaelligen Chunk-Grenze fälschlich in zwei Haelften zerfaellt.
    Innerhalb eines Chunks werden die Zeilen rueckwaerts verarbeitet, womit
    Treffer automatisch in "neueste zuerst"-Reihenfolge entstehen -- keine
    abschliessende Umkehrung noetig.

    Bewusst NICHT abgesichert: ein einzelnes CSV-Feld (z.B. `reason`), das
    ein eingebettetes Zeilenumbruch-Zeichen enthaelt (RFC-4180-Mehrzeilen-
    Felder wuerden beim Zerlegen an '\\n' faelschlich auseinandergerissen).
    Ungefaehrlich hier: jede Stelle in bot.py, die `reason`/`detail` baut,
    verkettet ausschliesslich mit Leerzeichen, nie mit '\\n' -- trades.csv
    enthaelt also nie echte eingebettete Zeilenumbrueche in einem Feld.

    Liest nie unter einem Lock -- das Schreiben (siehe TradeLogger._lock)
    darf dadurch nie blockiert werden. Eine Zeile, die gerade erst zur
    Haelfte geschrieben wurde (Bot schreibt parallel), fuehrt einfach zu
    einem Feldanzahl-Mismatch beim Parsen und wird uebersprungen -- nie als
    gueltig ausgegeben (siehe _parse_trade_log_row).
    """
    csv_path = Path(path)
    if not csv_path.exists():
        return []

    matches: list[dict] = []
    try:
        with csv_path.open("rb") as f:
            f.seek(0, 2)
            file_size = f.tell()
            if file_size == 0:
                return []

            f.seek(0)
            header_line = f.readline()
            header_end = f.tell()
            if not header_line or file_size <= header_end:
                return []  # leer oder nur Kopfzeile, keine Datenzeilen

            try:
                fieldnames = next(csv.reader([header_line.rstrip(b"\r\n").decode("utf-8", errors="replace")]))
            except csv.Error:
                return []

            pos = file_size
            pending_prefix = b""

            while pos > header_end and len(matches) < limit:
                read_size = min(chunk_size, pos - header_end)
                pos -= read_size
                f.seek(pos)
                chunk = f.read(read_size)

                lines = chunk.split(b"\n")
                if pending_prefix:
                    lines[-1] += pending_prefix

                if pos > header_end:
                    # Erste Zeile dieses Chunks ist (fast immer) an der
                    # Chunk-Grenze abgeschnitten -- ihr Anfang steckt im
                    # naechsten, noch zu lesenden (frueheren) Chunk.
                    pending_prefix = lines.pop(0)
                else:
                    pending_prefix = b""

                for raw_line in reversed(lines):
                    if not raw_line.strip():
                        continue
                    parsed = _parse_trade_log_row(fieldnames, raw_line, path)
                    if parsed is not None:
                        matches.append(parsed)
                        if len(matches) >= limit:
                            break
    except OSError as exc:
        logger.warning("Konnte %s nicht lesen: %s", path, exc)
        return []

    return matches  # bereits neueste zuerst (siehe Docstring)


def build_status_payload(executor: OrderExecutor, config: Config) -> dict:
    """Baut die komplette /api/status-Antwort. Von der Flask-Schicht
    getrennt, damit sich die eigentliche Logik ohne HTTP/Token-Kram mit
    einem Fake-Executor testen laesst.

    get_account() und get_all_positions() laufen bewusst NACHEINANDER,
    nicht parallel ueber einen ThreadPoolExecutor: beide teilen sich
    dieselbe TradingClient-Instanz (also dieselbe requests.Session),
    und requests selbst dokumentiert Session-Objekte NICHT als
    offiziell thread-sicher fuer gleichzeitige Requests -- insbesondere
    die Cookie-Jar-Mitpflege bei jeder Antwort ist ein bekannter, nicht
    abgesicherter Streitpunkt. Dass Alpacas API hier keine Cookies
    nutzt (Header-basierte API-Key-Auth, frisch pro Aufruf erzeugt)
    macht das praktische Risiko vermutlich klein, aber "vermutlich
    klein" ist nicht dasselbe wie verifiziert sicher. Der tatsaechliche
    Zeitgewinn waere ausserdem gering (gemessen: 0.64s + 0.19s
    sequentiell -- Parallelisierung spart bestenfalls die kuerzere
    Dauer, hier ca. 0.19s) und steht in keinem Verhaeltnis zum Risiko,
    den gemeinsam genutzten Alpaca-Client eines Trading-Bots einer
    nicht offiziell abgesicherten Nebenlaeufigkeits-Annahme auszusetzen.
    Der dominierende Kostenfaktor war ohnehin _read_recent_trades()
    (siehe deren Docstring) und der fehlende Cache (siehe
    _get_status_cached) -- beide beheben den eigentlichen Engpass
    bereits, ohne dieses Risiko einzugehen.
    """
    account = executor.get_account()
    positions = executor.get_all_positions()

    equity = float(account.equity)
    last_equity = float(account.last_equity)
    day_pl_dollar = equity - last_equity
    day_pl_percent = (day_pl_dollar / last_equity * 100) if last_equity > 0 else 0.0

    positions_payload = [
        {
            "symbol": symbol,
            "qty": float(position.qty),
            "avg_entry_price": float(position.avg_entry_price),
            "current_price": float(position.current_price),
            "unrealized_pl": float(position.unrealized_pl),
            "unrealized_pl_percent": float(position.unrealized_plpc) * 100,
        }
        for symbol, position in positions.items()
    ]

    return {
        "mode": "paper" if config.paper else "live",
        "account": {
            "equity": equity,
            "cash": float(account.cash),
            "buying_power": float(account.buying_power),
            "day_pl_dollar": day_pl_dollar,
            "day_pl_percent": day_pl_percent,
        },
        "positions": positions_payload,
        "recent_trades": _read_recent_trades(config.trade_log_path),
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


# Thread-sicherer In-Memory-Cache fuer /api/status (siehe STATUS_CACHE_SECONDS
# oben) -- anders als der bestehende _equity_history_cache (dort reicht ein
# unsynchronisiertes dict, siehe dessen Kommentar) bewusst mit einem Lock,
# weil explizit angefordert und weil hier (im Gegensatz zum Equity-Cache)
# auch ein kuenftiger threaded=True-Server ohne weitere Aenderung sicher
# bleiben soll. Haelt EIN (Zeitstempel, Payload)-Tupel statt eines dicts pro
# Schluessel -- es gibt nur einen Status, keine Varianten wie beim Zeitraum
# der Equity-History.
_status_cache_lock = threading.Lock()
_status_cache: tuple[float, dict] | None = None


def _get_status_cached(executor: OrderExecutor, config: Config) -> dict:
    """Wie build_status_payload(), aber haelt das Ergebnis
    STATUS_CACHE_SECONDS lang vor -- mehrfaches Neuladen der Seite oder
    mehrere gleichzeitig offene Geraete loesen dadurch nicht bei jedem
    Aufruf erneut zwei Alpaca-Calls und ein CSV-Lesen aus.

    Bei einem Fehler (OrderExecutionError aus build_status_payload) wird
    NICHTS gecacht und die Exception laeuft ungefangen durch -- die
    aufrufende Flask-Route soll weiterhin den echten Fehler sehen und
    NIE einen alten, evtl. laengst ueberholten Stand als "aktuell"
    ausliefern, nur weil er zufaellig noch im Cache lag.
    """
    global _status_cache

    now = time.monotonic()
    with _status_cache_lock:
        if _status_cache is not None and (now - _status_cache[0]) < STATUS_CACHE_SECONDS:
            return _status_cache[1]

    # Bewusst AUSSERHALB des Locks: build_status_payload() macht Netzwerk-
    # Aufrufe (Alpaca) und liest trades.csv, beides kann je nach Umstaenden
    # spuerbar dauern -- den Lock dafuer zu halten wuerde jeden anderen
    # Request (auch auf andere Routen, die denselben Lock NICHT anfassen,
    # waere kein Problem, aber jeden parallelen /api/status-Request schon)
    # unnoetig lange blockieren, obwohl nur das kurze Lesen/Schreiben des
    # Cache-Eintrags selbst geschuetzt werden muss. Ein seltener Doppel-
    # Abruf bei exakt gleichzeitigem Cache-Miss (zwei Requests sehen beide
    # "abgelaufen", fragen beide frisch ab) ist kein Korrektheitsproblem,
    # nur minimal ineffizient -- und mit dem aktuell einzelnen Flask-
    # Dev-Server (kein threaded=True, siehe main()) ohnehin unmoeglich.
    payload = build_status_payload(executor, config)

    with _status_cache_lock:
        _status_cache = (now, payload)
    return payload


def _extract_equity_points(raw: dict) -> tuple[list[dict], float | None]:
    """Wandelt die rohe Alpaca-Portfolio-History-Antwort (parallele Listen
    `timestamp`/`equity`, siehe OrderExecutor.get_portfolio_history) in eine
    saubere, chronologisch sortierte Liste von Tagespunkten um.

    Verteidigt sich gegen genau die Eigenheiten, die Alpaca fuer dieses
    Endpoint bekannt hat:
    - `None`-Werte in `timestamp` oder `equity` (laut Beobachtung gelegentlich
      vorhanden) -- ein solcher Index wird komplett uebersprungen, nicht als
      0 oder NaN eingetragen, sonst gaebe es einen falschen Ausschlag im
      Chart.
    - Tage ohne Handel (Wochenende/Feiertag) liefert Alpaca schlicht gar
      nicht erst zurueck -- es muss hier nichts extra gefiltert werden, die
      Liste enthaelt dadurch automatisch nur echte Handelstage, keine
      kuenstlichen Spruenge auf der x-Achse.
    - Timestamps sind Unix-Sekunden in UTC; fuer die Anzeige wird daraus das
      Kalenderdatum in Europe/Berlin gebildet (nicht einfach das UTC-Datum --
      wuerde bei einem spaeten UTC-Zeitstempel sonst einen Tag zu frueh
      anzeigen).

    Gibt (points, base_value) zurueck -- `base_value` faellt auf den ersten
    gueltigen Equity-Wert zurueck, falls Alpaca keinen mitliefert (ebenfalls
    `Optional` im Response-Schema).
    """
    timestamps = raw.get("timestamp") or []
    equities = raw.get("equity") or []

    points: list[dict] = []
    for ts, equity in zip(timestamps, equities):
        if ts is None or equity is None:
            continue
        try:
            ts_int = int(ts)
            equity_float = float(equity)
        except (TypeError, ValueError):
            continue
        local_date = datetime.fromtimestamp(ts_int, tz=timezone.utc).astimezone(BERLIN_TZ).date()
        points.append({"timestamp": ts_int, "date": local_date.isoformat(), "equity": equity_float})

    points.sort(key=lambda p: p["timestamp"])

    base_value = raw.get("base_value")
    try:
        base_value = float(base_value) if base_value is not None else None
    except (TypeError, ValueError):
        base_value = None
    if base_value is None and points:
        base_value = points[0]["equity"]

    return points, base_value


def _compute_equity_stats(points: list[dict], base_value: float | None) -> dict:
    """Kennzahlenzeile fuers Dashboard: aktueller Kontostand, P&L im Zeitraum
    ($ und %), bester/schlechtester Tag, maximaler Drawdown. Reine,
    Flask-freie Funktion auf der schon bereinigten Punkteliste -- billige
    O(n)-Schleifen ueber hoechstens ein paar tausend Tagespunkte, selbst auf
    einem Pi Zero kein Thema.
    """
    if not points:
        return {
            "current_equity": None,
            "base_value": base_value,
            "period_pl_dollar": None,
            "period_pl_percent": None,
            "best_day": None,
            "worst_day": None,
            "max_drawdown_dollar": None,
            "max_drawdown_percent": None,
        }

    current_equity = points[-1]["equity"]
    period_pl_dollar = (current_equity - base_value) if base_value else None
    period_pl_percent = (period_pl_dollar / base_value * 100) if base_value and period_pl_dollar is not None else None

    best_day = None
    worst_day = None
    peak = points[0]["equity"]
    max_drawdown_dollar = 0.0
    max_drawdown_percent = 0.0

    for prev, curr in zip(points, points[1:]):
        change_dollar = curr["equity"] - prev["equity"]
        change_percent = (change_dollar / prev["equity"] * 100) if prev["equity"] else 0.0
        day = {"date": curr["date"], "change_dollar": change_dollar, "change_percent": change_percent}
        if best_day is None or change_dollar > best_day["change_dollar"]:
            best_day = day
        if worst_day is None or change_dollar < worst_day["change_dollar"]:
            worst_day = day

        peak = max(peak, curr["equity"])
        drawdown_dollar = curr["equity"] - peak
        drawdown_percent = (drawdown_dollar / peak * 100) if peak else 0.0
        if drawdown_dollar < max_drawdown_dollar:
            max_drawdown_dollar = drawdown_dollar
            max_drawdown_percent = drawdown_percent

    return {
        "current_equity": current_equity,
        "base_value": base_value,
        "period_pl_dollar": period_pl_dollar,
        "period_pl_percent": period_pl_percent,
        "best_day": best_day,
        "worst_day": worst_day,
        "max_drawdown_dollar": max_drawdown_dollar,
        "max_drawdown_percent": max_drawdown_percent,
    }


def build_equity_history_payload(executor: OrderExecutor, period_key: str) -> dict:
    """Baut die komplette /api/equity-history-Antwort fuer einen der
    erlaubten Zeitraeume (siehe EQUITY_HISTORY_PERIODS). Von der Flask-
    Schicht und vom Cache getrennt, damit sich die Aufbereitung ohne
    HTTP/Token/Cache-Kram mit einem Fake-Executor testen laesst.
    """
    alpaca_period = EQUITY_HISTORY_PERIODS[period_key]
    raw = executor.get_portfolio_history(alpaca_period, timeframe="1D")
    points, base_value = _extract_equity_points(raw)
    stats = _compute_equity_stats(points, base_value)
    return {"period": period_key, "points": points, **stats}


# Einfache In-Memory-TTL-Cache (siehe EQUITY_HISTORY_CACHE_SECONDS) pro
# Zeitraum -- der Flask-Dev-Server hier laeuft einzeln (kein threaded=True,
# siehe main()), Requests werden also sequentiell abgearbeitet, ein Lock ist
# dafuer nicht noetig. Zweck: mehrfaches Neuladen der Dashboard-Seite oder
# ein Wechsel zwischen den vier Zeitraum-Buttons soll nicht bei jedem Klick
# erneut Alpaca treffen (Rate-Limit-Schonung UND weniger Arbeit fuer den
# Pi Zero).
_equity_history_cache: dict[str, tuple[float, dict]] = {}


def _get_equity_history_cached(executor: OrderExecutor, period_key: str) -> dict:
    now = time.monotonic()
    cached = _equity_history_cache.get(period_key)
    if cached is not None and (now - cached[0]) < EQUITY_HISTORY_CACHE_SECONDS:
        return cached[1]

    payload = build_equity_history_payload(executor, period_key)
    _equity_history_cache[period_key] = (now, payload)
    return payload


def _maybe_gzip_response(response):
    """Komprimiert den Response-Body mit gzip, wenn der Client es per
    `Accept-Encoding` akzeptiert und sich das lohnt (ab GZIP_MIN_BYTES) --
    bewusst nur die Python-Standardbibliothek (gzip), keine neue
    Abhaengigkeit wie flask-compress noetig. Kleine Antworten (z.B. eine
    leere Positionsliste) bleiben unkomprimiert, dafuer lohnt sich der
    CPU-Aufwand auf dem Pi Zero nicht. Browser/fetch() entpacken ein
    gzip-komprimiertes Accept-Encoding-Response automatisch, ganz ohne
    Aenderung auf der Frontend-Seite.
    """
    accept_encoding = request.headers.get("Accept-Encoding", "")
    if "gzip" not in accept_encoding.lower():
        return response
    if response.direct_passthrough or response.status_code >= 300:
        return response

    data = response.get_data()
    if len(data) < GZIP_MIN_BYTES:
        return response

    compressed = gzip.compress(data, compresslevel=6)
    response.set_data(compressed)
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Content-Length"] = str(len(compressed))
    # add() statt []= -- flask-cors setzt hier bereits "Vary: Origin"
    # (siehe CORS(app) oben), ein "[]="  wuerde das klammheimlich
    # ueberschreiben statt zu ergaenzen.
    response.headers.add("Vary", "Accept-Encoding")
    return response


def create_app(executor: OrderExecutor, config: Config, token: str) -> Flask:
    app = Flask(__name__)
    CORS(app)  # index.html laeuft auf einer anderen Origin (GitHub Pages) --
    # die eigentliche Absicherung ist das Token, nicht die CORS-Origin.
    app.after_request(_maybe_gzip_response)

    require_token = _require_token(token)

    @app.route("/")
    def index():
        return jsonify({"service": "trading-bot-dashboard-api", "status": "running"})

    @app.route("/api/status")
    @require_token
    def status():
        try:
            payload = _get_status_cached(executor, config)
        except OrderExecutionError as exc:
            logger.error("Alpaca-Abfrage fehlgeschlagen: %s", exc)
            return jsonify({"error": f"Alpaca nicht erreichbar: {exc}"}), 502
        return jsonify(payload)

    @app.route("/api/equity-history")
    @require_token
    def equity_history():
        period_key = request.args.get("period", "all").strip().lower()
        if period_key not in EQUITY_HISTORY_PERIODS:
            return (
                jsonify({"error": f"Ungueltiger Zeitraum '{period_key}', erlaubt: {', '.join(EQUITY_HISTORY_PERIODS)}"}),
                400,
            )
        try:
            payload = _get_equity_history_cached(executor, period_key)
        except OrderExecutionError as exc:
            logger.error("Alpaca-Portfolio-History-Abfrage fehlgeschlagen: %s", exc)
            return jsonify({"error": f"Alpaca nicht erreichbar: {exc}"}), 502
        except requests.exceptions.RequestException as exc:
            # Dasselbe Timeout, das OrderExecutor._client fuer JEDEN Alpaca-
            # Call erzwingt (siehe order_executor.apply_default_request_timeout)
            # -- hier separat behandelt, weil requests.exceptions.Timeout keine
            # OrderExecutionError ist (siehe bot.run_safety_check_once fuer
            # denselben Fall).
            logger.error("Timeout/Netzwerkfehler bei Alpaca-Portfolio-History: %s", exc)
            return jsonify({"error": f"Alpaca nicht erreichbar (Timeout): {exc}"}), 502
        return jsonify(payload)

    return app


def main() -> None:
    try:
        config = load_config()
    except ValueError as exc:
        logger.error("Konfigurationsfehler: %s", exc)
        sys.exit(1)

    ssl_context = _resolve_ssl_context()
    token = _load_or_create_token()
    executor = OrderExecutor(config)
    app = create_app(executor, config, token)

    logger.info("Dashboard-API startet auf https://0.0.0.0:5000 (%s trading)", "paper" if config.paper else "LIVE")
    app.run(host="0.0.0.0", port=5000, debug=False, ssl_context=ssl_context)


if __name__ == "__main__":
    main()
