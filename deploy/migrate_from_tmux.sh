#!/usr/bin/env bash
# Sichere Migration von den bestehenden tmux-Sessions ("bot", "api") auf
# echte systemd-Services. Killt NIEMALS automatisch die tmux-Sessions --
# das passiert nur in Schritt 4, und nur wenn:
#   1. explizit --stop-tmux uebergeben wurde,
#   2. die Verifikation in Schritt 3 erfolgreich war,
#   3. UND du es in einer interaktiven Shell nochmal einzeln bestaetigst.
#
# Ablauf:
#   1. Bestandsaufnahme (tmux-Sessions, .env, trades.csv, venv, bereits
#      vorhandene systemd/Cron-Dateien) -- reine Anzeige, keine Aenderung.
#   2. deploy/install.sh --apply (richtet Watchdog/Services/Cron/logrotate/
#      tmpfs/journald ein -- siehe deploy/README.md fuer Details).
#   3. Verifikation: sind beide neuen Services aktiv, antwortet das
#      Dashboard tatsaechlich, gibt es frische Fehler im Bot-Log?
#   4. Nur mit --stop-tmux + bestandener Verifikation + manueller
#      Bestaetigung: die alten tmux-Sessions sauber beenden.
#
# Nutzung (auf dem Pi, als root/sudo, vom Projekt-Root aus):
#   sudo deploy/migrate_from_tmux.sh                 # Schritte 1-3
#   sudo deploy/migrate_from_tmux.sh --stop-tmux      # zusaetzlich Schritt 4
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

STOP_TMUX=no
for arg in "$@"; do
    case "$arg" in
        --stop-tmux) STOP_TMUX=yes ;;
        --help|-h)
            echo "Nutzung: $0 [--stop-tmux]"
            exit 0
            ;;
        *)
            echo "Unbekannte Option: $arg (siehe --help)" >&2
            exit 1
            ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "Bitte mit sudo ausfuehren: sudo $0 ${STOP_TMUX:+--stop-tmux}" >&2
    exit 1
fi

TMUX_USER="${SUDO_USER:-$(stat -c '%U' "$REPO_ROOT")}"

section() { echo ""; echo "=================================================================="; echo " $1"; echo "=================================================================="; }

# ---------- Schritt 1: Bestandsaufnahme ----------
section "Schritt 1/4: Bestandsaufnahme"

echo "-- tmux-Sessions (als Benutzer $TMUX_USER) --"
if sudo -u "$TMUX_USER" tmux has-session -t bot 2>/dev/null; then
    echo "  'bot': LAEUFT"
    BOT_TMUX_RUNNING=yes
else
    echo "  'bot': nicht gefunden"
    BOT_TMUX_RUNNING=no
fi
if sudo -u "$TMUX_USER" tmux has-session -t api 2>/dev/null; then
    echo "  'api': LAEUFT"
    API_TMUX_RUNNING=yes
else
    echo "  'api': nicht gefunden"
    API_TMUX_RUNNING=no
fi

echo ""
echo "-- Konfiguration --"
if [[ -f "$REPO_ROOT/trading_bot/.env" ]]; then
    echo "  trading_bot/.env: vorhanden ($(wc -l < "$REPO_ROOT/trading_bot/.env") Zeilen -- Inhalt wird hier nicht angezeigt)"
    if grep -qE '^ALPACA_API_KEY=.+' "$REPO_ROOT/trading_bot/.env" 2>/dev/null; then
        echo "  ALPACA_API_KEY: gesetzt"
    else
        echo "  ALPACA_API_KEY: FEHLT ODER LEER -- der Bot wird ohne das nicht starten"
    fi
    if grep -qE '^ALPACA_PAPER=true' "$REPO_ROOT/trading_bot/.env" 2>/dev/null; then
        echo "  Modus: PAPER"
    else
        echo "  Modus: pruefen -- ALPACA_PAPER=true wurde nicht gefunden (evtl. LIVE!)"
    fi
else
    echo "  trading_bot/.env: FEHLT -- der Bot kann ohne diese Datei nicht starten."
fi

if [[ -f "$REPO_ROOT/trades.csv" ]]; then
    echo "  trades.csv: vorhanden ($(wc -l < "$REPO_ROOT/trades.csv") Zeilen)"
else
    echo "  trades.csv: noch nicht vorhanden (wird beim ersten Trade automatisch angelegt)"
fi

if [[ -x "$REPO_ROOT/trading_bot/.venv/bin/python" ]]; then
    echo "  venv: vorhanden ($REPO_ROOT/trading_bot/.venv)"
else
    echo "  venv: FEHLT unter $REPO_ROOT/trading_bot/.venv -- siehe trading_bot/README.md (Setup), sonst schlagen die neuen Services fehl."
fi

echo ""
echo "-- Bereits vorhandene systemd-/Cron-Konfiguration --"
for f in /etc/systemd/system/tradingbot.service /etc/systemd/system/tradingbot-dashboard.service \
         /etc/cron.d/tradingbot-healthcheck /etc/cron.d/tradingbot-weekly-reboot; do
    if [[ -f "$f" ]]; then
        echo "  $f existiert bereits (wird gleich neu geschrieben)"
    fi
done

# ---------- Schritt 2: systemd-Services einrichten ----------
section "Schritt 2/4: systemd-Services einrichten (deploy/install.sh --apply)"
"$SCRIPT_DIR/install.sh" --apply

# ---------- Schritt 3: Verifikation ----------
section "Schritt 3/4: Verifikation"
echo "Warte 10s, damit beide Services Zeit zum Hochfahren haben..."
sleep 10

BOT_OK=yes
DASH_OK=yes

if systemctl is-active --quiet tradingbot.service; then
    echo "  [OK] tradingbot.service ist aktiv"
else
    echo "  [FEHLER] tradingbot.service ist NICHT aktiv (Status: $(systemctl is-active tradingbot.service 2>&1))"
    BOT_OK=no
fi

if systemctl is-active --quiet tradingbot-dashboard.service; then
    echo "  [OK] tradingbot-dashboard.service ist aktiv"
else
    echo "  [FEHLER] tradingbot-dashboard.service ist NICHT aktiv (Status: $(systemctl is-active tradingbot-dashboard.service 2>&1))"
    DASH_OK=no
fi

# Oeffentliche Root-Route von api_server.py, kein Token noetig -- reiner
# Erreichbarkeits-/TLS-Check.
dash_http_code="$(curl -k -s -o /dev/null -w '%{http_code}' --max-time 5 https://127.0.0.1:5000/ 2>/dev/null || echo "000")"
if [[ "$dash_http_code" == "200" ]]; then
    echo "  [OK] Dashboard-API antwortet auf https://127.0.0.1:5000/ (HTTP 200)"
else
    echo "  [FEHLER] Dashboard-API antwortet nicht wie erwartet (HTTP $dash_http_code)"
    DASH_OK=no
fi

recent_errors="$(journalctl -u tradingbot.service --since '-1 min' 2>/dev/null | grep -iE 'error|exception|traceback' || true)"
if [[ -n "$recent_errors" ]]; then
    echo "  [WARNUNG] Fehlermeldungen im frischen tradingbot-Log:"
    while IFS= read -r line; do echo "      $line"; done <<< "$recent_errors"
    BOT_OK=no
fi

if [[ "$BOT_OK" == "yes" && "$DASH_OK" == "yes" ]]; then
    echo ""
    echo "  Verifikation OK -- beide neuen Services laufen und antworten."
    VERIFY_OK=yes
else
    echo ""
    echo "  Verifikation FEHLGESCHLAGEN -- siehe Meldungen oben."
    echo "  Debuggen mit: sudo journalctl -u tradingbot -n 50 / -u tradingbot-dashboard -n 50"
    VERIFY_OK=no
fi

# ---------- Schritt 4: alte tmux-Sessions beenden (nur mit --stop-tmux) ----------
section "Schritt 4/4: Alte tmux-Sessions"

if [[ "$BOT_TMUX_RUNNING" == "no" && "$API_TMUX_RUNNING" == "no" ]]; then
    echo "  Keine der beiden tmux-Sessions laeuft (mehr) -- nichts zu tun."
    exit 0
fi

if [[ "$STOP_TMUX" != "yes" ]]; then
    echo "  --stop-tmux nicht angegeben -- tmux-Sessions 'bot'/'api' bleiben unangetastet."
    echo "  Sobald du dem neuen Setup vertraust: sudo $0 --stop-tmux"
    exit 0
fi

if [[ "$VERIFY_OK" != "yes" ]]; then
    echo "  Die Verifikation in Schritt 3 ist fehlgeschlagen -- tmux-Sessions werden"
    echo "  DESHALB NICHT beendet, auch wenn --stop-tmux angegeben wurde."
    echo "  Erst die Probleme oben beheben, dann diesen Befehl erneut ausfuehren."
    exit 1
fi

if [[ ! -t 0 ]]; then
    echo "  Keine interaktive Sitzung erkannt -- das Beenden der tmux-Sessions wird aus"
    echo "  Sicherheitsgruenden nur in einer interaktiven Shell bestaetigt."
    echo "  Bitte direkt auf dem Pi ausfuehren: sudo $0 --stop-tmux"
    exit 1
fi

echo "  Die neuen systemd-Services laufen und wurden erfolgreich verifiziert."
read -r -p "  tmux-Sessions 'bot' und 'api' jetzt wirklich beenden? [j/N] " confirm
if [[ ! "$confirm" =~ ^[jJyY] ]]; then
    echo "  Abgebrochen -- tmux-Sessions bleiben aktiv."
    exit 0
fi

if [[ "$BOT_TMUX_RUNNING" == "yes" ]]; then
    sudo -u "$TMUX_USER" tmux kill-session -t bot 2>/dev/null \
        && echo "  tmux-Session 'bot' beendet." \
        || echo "  tmux-Session 'bot' konnte nicht beendet werden (evtl. schon weg)."
fi
if [[ "$API_TMUX_RUNNING" == "yes" ]]; then
    sudo -u "$TMUX_USER" tmux kill-session -t api 2>/dev/null \
        && echo "  tmux-Session 'api' beendet." \
        || echo "  tmux-Session 'api' konnte nicht beendet werden (evtl. schon weg)."
fi

echo ""
echo "Migration abgeschlossen -- Bot und Dashboard laufen jetzt als systemd-Services."
