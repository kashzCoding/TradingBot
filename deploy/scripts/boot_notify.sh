#!/usr/bin/env bash
# Laeuft einmal bei jedem Boot (siehe tradingbot-boot-notify.service) und
# meldet per Telegram, OB der vorherige Neustart geplant war (weekly_reboot.sh
# hat den Marker gesetzt) oder ungeplant (Watchdog-Reset, Stromausfall,
# Kernel-Panic) -- ein Hardware-Watchdog kann selbst nicht mehr VOR dem
# Reset benachrichtigen (das System ist ja gerade komplett eingefroren),
# daher nur diese nachtraegliche Meldung nach dem naechsten Hochfahren.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NOTIFY="$SCRIPT_DIR/notify.sh"
LOG_FILE="/var/log/healthcheck.log"
MARKER="/var/lib/tradingbot/reboot-marker"

log() {
    printf '%s [%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" "$2" >> "$LOG_FILE" 2>/dev/null || true
}

if [[ -f "$MARKER" ]]; then
    rm -f "$MARKER"
    log "INFO" "System nach geplantem woechentlichem Neustart wieder hochgefahren."
    "$NOTIFY" "✅ Geplanter Neustart abgeschlossen, der Pi ist wieder online."
else
    log "WARNING" "System nach UNGEPLANTEM Neustart wieder hochgefahren (evtl. Watchdog-Reset oder Stromausfall)."
    "$NOTIFY" "⚠️ Der Pi hatte einen UNGEPLANTEN Neustart (evtl. Watchdog-Reset oder Stromausfall) und ist jetzt wieder online. Falls das haeufiger vorkommt, lohnt sich eine genauere Ursachensuche."
fi
