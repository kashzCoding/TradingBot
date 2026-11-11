#!/usr/bin/env bash
# Schickt eine Telegram-Nachricht an den in notify.conf hinterlegten Chat.
# Wird von healthcheck.sh, weekly_reboot.sh und dem systemd-OnFailure-Hook
# aufgerufen -- ein zentraler Ort fuer die Benachrichtigungslogik.
#
# Nutzung: notify.sh "Nachrichtentext"
#
# Ist notify.conf (noch) nicht eingerichtet, wird das lokal geloggt und das
# Skript beendet sich sauber mit Exit-Code 0 -- ein fehlendes Telegram-Setup
# darf niemals healthcheck.sh/systemd zum Abbruch bringen.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF_FILE="$SCRIPT_DIR/../notify.conf"
LOG_FILE="/var/log/healthcheck.log"

message="${1:-}"
if [[ -z "$message" ]]; then
    echo "Nutzung: $0 <Nachricht>" >&2
    exit 1
fi

log() {
    # Faellt auf stderr zurueck, falls /var/log nicht beschreibbar ist
    # (z.B. beim manuellen Testen ohne root) -- notify.sh soll dabei nicht
    # fehlschlagen, nur informieren.
    printf '%s [%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" "$2" >> "$LOG_FILE" 2>/dev/null \
        || printf '%s [%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" "$2" >&2
}

if [[ ! -f "$CONF_FILE" ]]; then
    log "WARNING" "notify.sh: notify.conf nicht gefunden ($CONF_FILE) -- Benachrichtigung uebersprungen: $message"
    exit 0
fi

# shellcheck source=/dev/null
source "$CONF_FILE"

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" ]]; then
    log "WARNING" "notify.sh: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID nicht gesetzt -- Benachrichtigung uebersprungen: $message"
    exit 0
fi

response="$(curl -s -m 10 -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=${message}" \
    -w $'\n%{http_code}')"
http_code="${response##*$'\n'}"

if [[ "$http_code" != "200" ]]; then
    log "WARNING" "notify.sh: Telegram-Versand fehlgeschlagen (HTTP $http_code): $message"
    exit 0
fi

exit 0
