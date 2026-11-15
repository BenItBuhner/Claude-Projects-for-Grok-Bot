#!/usr/bin/env bash
# claude-projects-mcp on an always-on Mac.
#
# The plugin drives the Claude desktop app through the Linux accessibility tree, so on a Mac it runs inside a small
# Ubuntu VM (Lima, instance "ccp") with its own headless display. Your Mac's own Claude app is not touched. The VM starts
# at login (a LaunchAgent), Lima forwards the VM's localhost ports to the Mac, so the sign-in viewport opens in your
# normal browser at http://127.0.0.1:6179/vnc.html.
#
#   bash hosts/macos/setup.sh [--bot-name NAME] [--operator-name NAME] [--timezone Area/City] [--no-sleep] [--dry-run]
#   --no-sleep   also stop the Mac sleeping while on power (sudo pmset -c sleep 0; asks for your password)
#
# Afterwards drive everything from the Mac with hosts/macos/ccp (it runs ccp inside the VM), e.g.
#   hosts/macos/ccp setup status
set -euo pipefail
NAME="${CCP_LIMA_NAME:-ccp}"
REPO_URL="${CCP_REPO_URL:-https://github.com/BenItBuhner/Claude-Projects-for-Grok-Bot}"
HERE="$(cd "$(dirname "$0")" && pwd)"
INSTALL_ARGS=(); NO_SLEEP=0; DRY=0; HAVE_TZ=0
while [ $# -gt 0 ]; do
  case "$1" in
    --bot-name|--operator-name|--timezone)
      [ $# -ge 2 ] && [ -n "$2" ] && [ "${2#--}" = "$2" ] || { echo "$1 needs a value" >&2; exit 2; }
      [ "$1" = --timezone ] && HAVE_TZ=1
      INSTALL_ARGS+=("$1" "$2"); shift 2;;
    --no-sleep) NO_SLEEP=1; shift;;
    --dry-run) DRY=1; shift;;
    -h|--help) sed -n '2,15p' "$0"; exit 0;;
    *) echo "unknown option: $1" >&2; exit 2;;
  esac
done
say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
note() { printf '    note: %s\n' "$*"; }
run() { if [ "$DRY" = 1 ]; then echo "    [dry-run] $*"; else "$@"; fi; }

[ "$(uname -s)" = Darwin ] || { echo "This script is for macOS. On Linux run ./install.sh (or ccp setup) directly." >&2; exit 2; }
MAJOR="$(sw_vers -productVersion | cut -d. -f1)"
[ "$MAJOR" -ge 13 ] || { echo "macOS 13 or newer is needed (found $(sw_vers -productVersion))." >&2; exit 2; }
if [ "$HAVE_TZ" = 0 ]; then
  TZ_MAC="$(readlink /etc/localtime 2>/dev/null | sed 's#.*/zoneinfo/##' || true)"
  [ -n "$TZ_MAC" ] && INSTALL_ARGS+=(--timezone "$TZ_MAC")
fi

# Homebrew lives in /opt/homebrew (Apple silicon) or /usr/local (Intel); a non-login shell often has neither on PATH.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
if command -v brew >/dev/null 2>&1; then eval "$(brew shellenv)"; fi
find_limactl() { command -v limactl 2>/dev/null || { for p in /opt/homebrew/bin/limactl /usr/local/bin/limactl; do [ -x "$p" ] && { echo "$p"; return 0; }; done; return 1; }; }
if ! LIMACTL="$(find_limactl)"; then
  command -v brew >/dev/null 2>&1 || { echo "Lima is needed (https://lima-vm.io). Install Homebrew (https://brew.sh) and re-run, or: brew install lima" >&2; exit 3; }
  say "Installing Lima with Homebrew"
  run brew install lima
  hash -r
  if [ "$DRY" = 1 ]; then LIMACTL="$(brew --prefix)/bin/limactl"
  else LIMACTL="$(find_limactl)" || { echo "brew installed lima but limactl is not on PATH; open a new terminal and re-run" >&2; exit 3; }; fi
fi

vm_status() { "$LIMACTL" list --format '{{.Status}}' "$NAME" 2>/dev/null || true; }
if [ -z "$(vm_status)" ]; then
  say "Creating the Ubuntu 24.04 VM \"$NAME\" (2 CPUs, 4 GB RAM, 20 GB disk, no access to your Mac's files)"
  run "$LIMACTL" create --name="$NAME" --cpus=2 --memory=4 --disk=20 --mount-none --tty=false template://ubuntu-24.04
fi
if [ "$DRY" = 0 ] && [ "$(vm_status)" != Running ]; then
  say "Starting the VM"
  "$LIMACTL" start --tty=false "$NAME"   # a real failure stops the script here
  for _ in $(seq 1 60); do [ "$(vm_status)" = Running ] && break; sleep 2; done
  [ "$(vm_status)" = Running ] || { echo "the VM \"$NAME\" did not reach Running (status: $(vm_status)); see: $LIMACTL list" >&2; exit 4; }
elif [ "$DRY" = 1 ]; then
  echo "    [dry-run] $LIMACTL start --tty=false $NAME (unless it is already Running), then wait for Running"
fi

say "Installing the plugin inside the VM"
Q=""; for x in "${INSTALL_ARGS[@]+"${INSTALL_ARGS[@]}"}"; do Q+=" $(printf '%q' "$x")"; done
MAC_NAME="$(scutil --get LocalHostName 2>/dev/null || hostname)"
run "$LIMACTL" shell "$NAME" -- bash -lc "set -e
  sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git ca-certificates >/dev/null
  for i in \$(seq 1 15); do sudo loginctl enable-linger \"\$USER\" && break; sleep 2; done
  [ -d ~/claude-projects-mcp ] || git clone $(printf '%q' "$REPO_URL") ~/claude-projects-mcp
  cd ~/claude-projects-mcp
  ./ccp setup choose macos_runtime --label $(printf '%q' "VM on $MAC_NAME")
  ./ccp setup preflight --host-kind macos-vm || true
  ./ccp setup install$Q"

# Start the VM at login. The helper exits quietly when the VM is already running, so the 10-minute re-check doesn't
# fill the log; logs go to ~/Library/Logs, not world-readable /tmp.
SUPPORT="$HOME/Library/Application Support/claude-projects-for-grok-bot"
HELPER="$SUPPORT/start-vm.sh"
LOG="$HOME/Library/Logs/claude-projects-vm.log"
PLIST="$HOME/Library/LaunchAgents/com.github.claude-projects-for-grok-bot.vm.plist"
say "Starting the VM at login ($PLIST)"
if [ "$DRY" = 0 ]; then
  mkdir -p "$SUPPORT" "$(dirname "$PLIST")" "$(dirname "$LOG")"
  cat > "$HELPER" <<SH
#!/bin/bash
[ "\$("$LIMACTL" list --format '{{.Status}}' "$NAME" 2>/dev/null)" = Running ] && exit 0
echo "\$(date '+%Y-%m-%d %H:%M:%S') starting $NAME"
exec "$LIMACTL" start --tty=false "$NAME"
SH
  chmod 700 "$HELPER"
  cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.github.claude-projects-for-grok-bot.vm</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$HELPER</string></array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$(dirname "$LIMACTL"):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>600</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict></plist>
PL
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
else
  echo "    [dry-run] write $HELPER and $PLIST (logs: $LOG), then launchctl bootstrap gui/$(id -u)"
fi

if [ "$NO_SLEEP" = 1 ]; then say "Stopping sleep while on power (sudo)"; run sudo pmset -c sleep 0
else
  S="$(pmset -g 2>/dev/null | awk '$1=="sleep"{print $2; exit}' || true)"
  [ "${S:-1}" = 0 ] || note "this Mac sleeps after ${S:-?} min on power, which pauses the plugin. Fix: sudo pmset -c sleep 0 (or re-run with --no-sleep)."
fi
if pmset -g batt 2>/dev/null | grep -q InternalBattery; then note "this Mac is a laptop: the plugin only works while it's awake and plugged in."; fi

cat <<MSG

Done. The VM "$NAME" runs the plugin and starts at login (keep this user logged in, or turn on automatic login).
Next, from this Mac:   $HERE/ccp setup status
Sign-in opens in your browser at http://127.0.0.1:6179/vnc.html once you run:   $HERE/ccp setup signin start
MSG
