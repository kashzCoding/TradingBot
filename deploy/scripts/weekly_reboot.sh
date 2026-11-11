#!/usr/bin/env bash
# Geplanter woechentlicher Neustart als zusaetzliches Sicherheitsnetz NEBEN
# dem Hardware-Watchdog (siehe deploy/watchdog/watchdog.conf): raeumt
# kleinere Speicherlecks oder haengende Prozesse regelmaessig auf, auch
# wenn nie ein kompletter Freeze auftritt, der den Watchdog ausloesen wuerde.
# Laeuft per Cron Sonntagnacht, wenn die Boerse sicher durchgehend
# geschlossen ist (siehe deploy/cron/tradingbot-weekly-reboot).
#
# Loggt und benachrichtigt VOR dem Reboot -- so laesst sich dieser geplante
# Neustart im Log klar von einem ungeplanten Watchdog-Reset unterscheiden
# (nach einem Watchdog-Reset gibt es naturgemaess KEINEN entsprechenden
# Log-Eintrag, weil dafuer keine Zeit mehr war).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NOTIFY="$SCRIPT_DIR/notify.sh"
LOG_FILE="/var/log/healthcheck.log"
MARKER="/var/lib/tradingbot/reboot-marker"

if [[ $EUID -ne 0 ]]; then
    echo "weekly_reboot.sh muss als root laufen." >&2
    exit 1
fi

printf '%s [INFO] --- Geplanter woechentlicher Neustart wird ausgefuehrt ---\n' "$(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG_FILE"
"$NOTIFY" "🔄 Geplanter woechentlicher Neustart (Wartungsfenster, Boerse geschlossen) wird jetzt ausgefuehrt."

# Marker MUSS den Reboot ueberleben (also NICHT unter /tmp, das ist tmpfs
# und wird beim Neustart geleert) -- boot_notify.sh liest ihn nach dem
# Hochfahren, um einen geplanten von einem ungeplanten (Watchdog-)Neustart
# zu unterscheiden.
mkdir -p "$(dirname "$MARKER")"
touch "$MARKER"

# Kurze Pause, damit die Telegram-Nachricht sicher noch rausgeht, bevor beim
# Reboot das Netzwerk wegfaellt.
sleep 5

/sbin/reboot
