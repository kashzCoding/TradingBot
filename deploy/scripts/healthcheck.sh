#!/usr/bin/env bash
# Health-Check fuer den Trading-Bot-Pi -- alle 5 Minuten per Cron
# (siehe deploy/cron/tradingbot-healthcheck).
#
# Prueft: Bot-/Dashboard-Prozess aktiv, Alpaca-API erreichbar, Tailscale
# verbunden, Speicherplatz, freier RAM. Behebt automatisch, was sich sicher
# automatisch beheben laesst (Service neu starten, Tailscale neu verbinden,
# bei kritischem Speicherplatz Logs rotieren), und benachrichtigt per
# Telegram -- aber nur beim UEBERGANG in/aus einem Problemzustand, nicht bei
# jedem 5-Minuten-Lauf erneut (siehe notify_once/clear_flag).
#
# Muss als root laufen (systemctl restart, tailscale up, logrotate -f und
# das Schreiben nach /var/log verlangen das). Wird ueber /etc/cron.d/
# eingerichtet, dort ist der ausfuehrende User bereits explizit "root".
set -uo pipefail
# Bewusst OHNE "set -e": ein einzelner fehlgeschlagener Check darf nicht die
# restlichen Pruefungen in diesem Lauf verhindern.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

BOT_SERVICE="tradingbot.service"
DASHBOARD_SERVICE="tradingbot-dashboard.service"
LOG_FILE="/var/log/healthcheck.log"
STATE_DIR="/tmp/tradingbot-health"
NOTIFY="$SCRIPT_DIR/notify.sh"
LOGROTATE_CONF="/etc/logrotate.d/tradingbot"

DISK_WARN_FREE_PCT=15   # Warnung, wenn weniger frei ist als das
DISK_CRIT_FREE_PCT=5    # aktives Aufraeumen, wenn weniger frei ist als das
RAM_WARN_FREE_PCT=10    # nur Benachrichtigung, keine automatische Aktion

if [[ $EUID -ne 0 ]]; then
    echo "healthcheck.sh muss als root laufen (systemctl/tailscale/logrotate/${LOG_FILE})." >&2
    exit 1
fi

mkdir -p "$STATE_DIR"

log() {
    printf '%s [%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" "$2" >> "$LOG_FILE"
}

# Benachrichtigt nur beim ERSTEN Erkennen eines Problems (Flag-Datei in
# tmpfs unter $STATE_DIR) -- verhindert, dass dieselbe Meldung alle 5
# Minuten erneut verschickt wird, solange das Problem fortbesteht. Die
# Flag-Datei verschwindet automatisch bei einem Reboot (tmpfs), was hier
# genau richtig ist: nach einem Neustart ist ohnehin ein Neuanfang gerecht.
notify_once() {
    local flag="$1" message="$2"
    local flag_path="$STATE_DIR/$flag"
    if [[ ! -f "$flag_path" ]]; then
        touch "$flag_path"
        "$NOTIFY" "$message"
    fi
}

# Entfernt das Flag und meldet die Wiederherstellung -- aber nur, wenn das
# Flag ueberhaupt gesetzt war (sonst wuerde jeder gesunde Lauf eine
# "wieder ok"-Meldung fuer etwas verschicken, das nie kaputt war).
clear_flag() {
    local flag="$1" recovered_message="$2"
    local flag_path="$STATE_DIR/$flag"
    if [[ -f "$flag_path" ]]; then
        rm -f "$flag_path"
        "$NOTIFY" "$recovered_message"
    fi
}

# ---------- 1) Bot-/Dashboard-Prozess ----------
check_service() {
    local service="$1" flag="$2" label="$3"
    if systemctl is-active --quiet "$service"; then
        clear_flag "$flag" "✅ $label laeuft wieder normal."
        log "OK" "$label aktiv."
        return
    fi

    log "ERROR" "$label ist NICHT aktiv -- versuche Neustart."
    systemctl restart "$service" >>"$LOG_FILE" 2>&1
    sleep 5

    if systemctl is-active --quiet "$service"; then
        log "RECOVERED" "$label nach Neustart wieder aktiv."
        notify_once "$flag" "🟡 $label war down und wurde automatisch neu gestartet."
    else
        log "CRITICAL" "$label konnte nicht neu gestartet werden."
        notify_once "$flag" "🔴 $label ist down, automatischer Neustart ist fehlgeschlagen. Bitte manuell pruefen: systemctl status $service"
    fi
}

# ---------- 2) Alpaca-Erreichbarkeit (einfacher Request-Test, keine Keys) ----------
check_alpaca() {
    local env_file="$REPO_ROOT/trading_bot/.env"
    local base_url="https://paper-api.alpaca.markets"
    if [[ -f "$env_file" ]] && grep -qiE '^[[:space:]]*ALPACA_PAPER[[:space:]]*=[[:space:]]*(false|0|no|off)[[:space:]]*$' "$env_file"; then
        base_url="https://api.alpaca.markets"
    fi

    local http_code
    http_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$base_url/v2/clock" 2>>"$LOG_FILE")"

    # Jede HTTP-Antwort (auch 401 ohne Auth-Header) beweist, dass die Alpaca-
    # API netzwerkseitig erreichbar ist -- nur ein curl-Fehlschlag (Timeout,
    # DNS, TLS-Fehler; curl gibt dann "000" als Code aus) zaehlt als
    # "nicht erreichbar". Es werden bewusst keine echten API-Keys benutzt.
    if [[ -n "$http_code" && "$http_code" != "000" ]]; then
        clear_flag "alpaca_unreachable" "✅ Alpaca-API wieder erreichbar."
        log "OK" "Alpaca-API erreichbar (HTTP $http_code)."
    else
        log "WARNING" "Alpaca-API nicht erreichbar (curl-Fehlschlag, Code '$http_code')."
        notify_once "alpaca_unreachable" "🟠 Alpaca-API ist vom Pi aus nicht erreichbar (Netzwerk/DNS/TLS). Der Bot faengt das selbst mit Retries ab, aber laenger anhaltend solltest du das pruefen."
    fi
}

# ---------- 3) Tailscale ----------
check_tailscale() {
    if tailscale status >/dev/null 2>&1; then
        clear_flag "tailscale_down" "✅ Tailscale wieder verbunden."
        log "OK" "Tailscale verbunden."
        return
    fi

    log "ERROR" "Tailscale nicht verbunden -- versuche 'tailscale up'."
    tailscale up >>"$LOG_FILE" 2>&1
    sleep 5

    if tailscale status >/dev/null 2>&1; then
        log "RECOVERED" "Tailscale nach 'tailscale up' wieder verbunden."
        notify_once "tailscale_down" "🟡 Tailscale war getrennt und wurde automatisch wieder verbunden."
    else
        log "CRITICAL" "Tailscale konnte nicht automatisch wiederverbunden werden."
        notify_once "tailscale_down" "🔴 Tailscale ist getrennt, 'tailscale up' hat nicht funktioniert (evtl. erneute Anmeldung noetig). Dashboard/Fernzugriff evtl. nicht erreichbar!"
    fi
}

# ---------- 4) Speicherplatz ----------
check_disk() {
    local used_pct free_pct
    used_pct="$(df --output=pcent / | tail -1 | tr -dc '0-9')"
    free_pct=$((100 - used_pct))
    log "INFO" "Freier Speicherplatz auf / : ${free_pct}%."

    if (( free_pct < DISK_CRIT_FREE_PCT )); then
        log "CRITICAL" "Speicherplatz kritisch (${free_pct}% frei) -- erzwinge Log-Rotation und Journal-Bereinigung."
        [[ -f "$LOGROTATE_CONF" ]] && logrotate -f "$LOGROTATE_CONF" >>"$LOG_FILE" 2>&1
        journalctl --vacuum-size=20M >>"$LOG_FILE" 2>&1

        used_pct="$(df --output=pcent / | tail -1 | tr -dc '0-9')"
        local free_pct_after=$((100 - used_pct))
        log "INFO" "Speicherplatz nach Aufraeumen: ${free_pct_after}% frei."
        notify_once "disk_critical" "🔴 Speicherplatz war kritisch (${free_pct}% frei) -- Logs rotiert und Journal bereinigt, jetzt ${free_pct_after}% frei. Bitte trotzdem zeitnah pruefen."
    elif (( free_pct < DISK_WARN_FREE_PCT )); then
        log "WARNING" "Speicherplatz knapp (${free_pct}% frei)."
        notify_once "disk_warning" "🟠 Speicherplatz auf dem Pi wird knapp: nur noch ${free_pct}% frei."
    else
        clear_flag "disk_critical" "✅ Speicherplatz wieder unkritisch (${free_pct}% frei)."
        clear_flag "disk_warning" "✅ Speicherplatz wieder ausreichend (${free_pct}% frei)."
    fi
}

# ---------- 5) RAM (nur Beobachtung/Meldung, keine automatische Aktion) ----------
check_ram() {
    local free_pct
    free_pct="$(free | awk '/^Mem:/ {printf "%d", ($7/$2)*100}')"
    log "INFO" "Freier RAM (available): ${free_pct}%."

    if (( free_pct < RAM_WARN_FREE_PCT )); then
        notify_once "ram_low" "🟠 Wenig freier RAM auf dem Pi: nur noch ${free_pct}% verfuegbar."
    else
        clear_flag "ram_low" "✅ Freier RAM wieder ausreichend (${free_pct}%)."
    fi
}

log "INFO" "--- Health-Check gestartet ---"
check_service "$BOT_SERVICE" "bot_down" "Trading-Bot"
check_service "$DASHBOARD_SERVICE" "dashboard_down" "Dashboard-API"
check_alpaca
check_tailscale
check_disk
check_ram
log "INFO" "--- Health-Check abgeschlossen ---"
