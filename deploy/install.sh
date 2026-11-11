#!/usr/bin/env bash
# Richtet die komplette Absicherung aus deploy/ ein: Hardware-Watchdog,
# systemd-Services (Bot, Dashboard, Boot-Benachrichtigung), Health-Check-
# und Weekly-Reboot-Cronjobs, logrotate, tmpfs-/tmp und journald-Limits.
#
# Sicher wiederholt ausfuehrbar (idempotent) -- ueberschreibt nichts
# blind, prueft vor jeder Aenderung, ob sie schon vorhanden ist.
#
# Standardmaessig ein DRY-RUN: zeigt nur, was gemacht wuerde. Erst mit
# --apply werden tatsaechlich Dateien geschrieben/Pakete installiert/
# Services aktiviert. Muss als root laufen (siehe unten).
#
# Nutzung (auf dem Pi, vom Projekt-Root aus):
#   sudo deploy/install.sh              # zeigt geplante Aenderungen
#   sudo deploy/install.sh --apply      # fuehrt sie tatsaechlich aus
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ROOT_PREFIX ist NUR fuer die eigene Testsuite gedacht (deploy/tests/) --
# ersetzt "/" durch ein Scratch-Verzeichnis, damit sich das Skript gegen
# ein Fake-Root testen laesst, ohne das echte System anzufassen. Auf dem
# Pi bleibt das immer leer.
ROOT_PREFIX="${ROOT_PREFIX:-}"

APPLY=no
for arg in "$@"; do
    case "$arg" in
        --apply) APPLY=yes ;;
        --help|-h)
            echo "Nutzung: $0 [--apply]"
            echo "  ohne --apply: Dry-Run, zeigt nur geplante Aenderungen"
            echo "  --apply:      fuehrt die Aenderungen tatsaechlich aus"
            exit 0
            ;;
        *)
            echo "Unbekannte Option: $arg (siehe --help)" >&2
            exit 1
            ;;
    esac
done

if [[ -z "$ROOT_PREFIX" && $EUID -ne 0 ]]; then
    echo "install.sh muss als root laufen (sudo deploy/install.sh --apply)." >&2
    exit 1
fi

DEPLOY_USER="${SUDO_USER:-$(stat -c '%U' "$REPO_ROOT")}"
DEPLOY_GROUP="$(id -gn "$DEPLOY_USER" 2>/dev/null || echo "$DEPLOY_USER")"
VENV_PYTHON="$REPO_ROOT/trading_bot/.venv/bin/python"

log()  { echo "[install] $*"; }
plan() { echo "[dry-run] $*"; }

run_or_plan() {
    # Fuehrt einen Befehl nur bei --apply wirklich aus, zeigt ihn sonst nur an.
    if [[ "$APPLY" == "yes" ]]; then
        "$@"
    else
        plan "wuerde ausfuehren: $*"
    fi
}

write_file() {
    # Schreibt Inhalt in eine Datei -- bei Dry-Run nur eine Ankuendigung,
    # bei --apply tatsaechlich (inkl. Verzeichnis anlegen).
    local path="$1" content="$2"
    if [[ "$APPLY" == "yes" ]]; then
        mkdir -p "$(dirname "$path")"
        printf '%s' "$content" > "$path"
        log "geschrieben: $path"
    else
        plan "wuerde schreiben: $path"
    fi
}

append_if_missing() {
    # Haengt eine Zeile an eine Datei an, falls sie dort noch nicht
    # (exakt) vorkommt -- macht Wiederholungen idempotent.
    local line="$1" path="$2"
    if [[ -f "$path" ]] && grep -qxF "$line" "$path" 2>/dev/null; then
        log "bereits vorhanden in $path: $line"
        return
    fi
    if [[ "$APPLY" == "yes" ]]; then
        mkdir -p "$(dirname "$path")"
        echo "$line" >> "$path"
        log "angehaengt an $path: $line"
    else
        plan "wuerde an $path anhaengen: $line"
    fi
}

render_template() {
    # Ersetzt die Standard-Platzhalter in einer Template-Datei und schreibt
    # das Ergebnis nach $2.
    local template="$1" dest="$2"
    local content
    content="$(sed \
        -e "s|__REPO_ROOT__|$REPO_ROOT|g" \
        -e "s|__USER__|$DEPLOY_USER|g" \
        -e "s|__GROUP__|$DEPLOY_GROUP|g" \
        -e "s|__VENV_PYTHON__|$VENV_PYTHON|g" \
        "$template")"
    write_file "$dest" "$content"
}

echo "=================================================================="
echo " Trading-Bot Absicherung -- $( [[ "$APPLY" == "yes" ]] && echo "WIRD ANGEWENDET" || echo "DRY-RUN (nichts wird veraendert)" )"
echo "  Repo-Root:    $REPO_ROOT"
echo "  Deploy-User:  $DEPLOY_USER (Gruppe: $DEPLOY_GROUP)"
echo "  Venv-Python:  $VENV_PYTHON $( [[ -x "$VENV_PYTHON" ]] || echo '  <-- FEHLT, siehe trading_bot/README.md Setup' )"
echo "=================================================================="
echo ""

# ---------- 1) Hardware-Watchdog ----------
log "--- 1) Hardware-Watchdog ---"
run_or_plan apt-get install -y watchdog

append_if_missing "bcm2835_wdt" "${ROOT_PREFIX}/etc/modules"

CONFIG_TXT="${ROOT_PREFIX}/boot/firmware/config.txt"
[[ -f "$CONFIG_TXT" ]] || CONFIG_TXT="${ROOT_PREFIX}/boot/config.txt"
if [[ -f "$CONFIG_TXT" ]]; then
    append_if_missing "dtparam=watchdog=on" "$CONFIG_TXT"
else
    log "WARNUNG: weder /boot/firmware/config.txt noch /boot/config.txt gefunden -- dtparam=watchdog=on manuell eintragen."
fi

render_template "$SCRIPT_DIR/watchdog/watchdog.conf" "${ROOT_PREFIX}/etc/watchdog.conf"
run_or_plan systemctl enable --now watchdog.service

# ---------- 2) systemd-Services fuer Bot + Dashboard ----------
log "--- 2) systemd-Services ---"
render_template "$SCRIPT_DIR/systemd/tradingbot.service.template" "${ROOT_PREFIX}/etc/systemd/system/tradingbot.service"
render_template "$SCRIPT_DIR/systemd/tradingbot-dashboard.service.template" "${ROOT_PREFIX}/etc/systemd/system/tradingbot-dashboard.service"
render_template "$SCRIPT_DIR/systemd/tradingbot-failure-notify@.service.template" "${ROOT_PREFIX}/etc/systemd/system/tradingbot-failure-notify@.service"
render_template "$SCRIPT_DIR/systemd/tradingbot-boot-notify.service.template" "${ROOT_PREFIX}/etc/systemd/system/tradingbot-boot-notify.service"

run_or_plan systemctl daemon-reload
run_or_plan systemctl enable --now tradingbot.service
run_or_plan systemctl enable --now tradingbot-dashboard.service
run_or_plan systemctl enable --now tradingbot-boot-notify.service

# ---------- 3) Health-Check-Cronjob ----------
log "--- 3) Health-Check (alle 5 Minuten) ---"
render_template "$SCRIPT_DIR/cron/tradingbot-healthcheck.template" "${ROOT_PREFIX}/etc/cron.d/tradingbot-healthcheck"

# ---------- 4) logrotate ----------
log "--- 4) logrotate ---"
render_template "$SCRIPT_DIR/logrotate/tradingbot.template" "${ROOT_PREFIX}/etc/logrotate.d/tradingbot"

# ---------- 5) SD-Karten-Schonung: tmpfs /tmp + journald-Limits ----------
log "--- 5) SD-Karten-Schonung ---"
append_if_missing "tmpfs /tmp tmpfs defaults,noatime,nosuid,nodev,mode=1777,size=64M 0 0" "${ROOT_PREFIX}/etc/fstab"
log "HINWEIS: /tmp als tmpfs wird erst nach einem Neustart aktiv (kein Live-Remount, um laufende Prozesse nicht zu stoeren)."

render_template "$SCRIPT_DIR/journald/tradingbot.conf" "${ROOT_PREFIX}/etc/systemd/journald.conf.d/tradingbot.conf"
run_or_plan systemctl restart systemd-journald

# ---------- 6) Woechentlicher Neustart ----------
log "--- 6) Woechentlicher Neustart (Sonntag 03:00) ---"
render_template "$SCRIPT_DIR/cron/tradingbot-weekly-reboot.template" "${ROOT_PREFIX}/etc/cron.d/tradingbot-weekly-reboot"
run_or_plan mkdir -p "${ROOT_PREFIX}/var/lib/tradingbot"

# ---------- 7) Telegram-Benachrichtigung ----------
log "--- 7) Telegram-Benachrichtigung ---"
if [[ -f "$SCRIPT_DIR/notify.conf" ]]; then
    log "notify.conf existiert bereits, wird nicht ueberschrieben."
else
    if [[ "$APPLY" == "yes" ]]; then
        cp "$SCRIPT_DIR/notify.conf.example" "$SCRIPT_DIR/notify.conf"
        log "notify.conf aus notify.conf.example angelegt -- BITTE TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID EINTRAGEN (siehe deploy/README.md)."
    else
        plan "wuerde notify.conf aus notify.conf.example anlegen (danach Telegram-Zugangsdaten eintragen)"
    fi
fi
run_or_plan chmod +x "$SCRIPT_DIR/scripts/healthcheck.sh" "$SCRIPT_DIR/scripts/notify.sh" \
    "$SCRIPT_DIR/scripts/weekly_reboot.sh" "$SCRIPT_DIR/scripts/boot_notify.sh" \
    "$SCRIPT_DIR/migrate_from_tmux.sh"

# ---------- Cron-Daemon sicherstellen ----------
run_or_plan apt-get install -y cron
run_or_plan systemctl enable --now cron

echo ""
echo "=================================================================="
if [[ "$APPLY" == "yes" ]]; then
    echo " Fertig. Naechste Schritte:"
    echo " 1. Falls neu angelegt: deploy/notify.conf mit Telegram-Zugangsdaten fuellen."
    echo " 2. sudo reboot -- laedt bcm2835_wdt, aktiviert tmpfs /tmp, startet alles frisch."
    echo " 3. Nach dem Reboot: siehe deploy/README.md, Abschnitt 'Status pruefen'."
else
    echo " Dry-Run abgeschlossen, es wurde NICHTS veraendert."
    echo " Zum tatsaechlichen Einrichten: sudo deploy/install.sh --apply"
fi
echo "=================================================================="
