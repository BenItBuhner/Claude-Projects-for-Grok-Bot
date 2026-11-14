#!/usr/bin/env bash
# claude-projects-mcp installer (Ubuntu 22.04+/Debian 12+, x86_64 or arm64, user-level systemd).
#
# Run it ON THE COMPUTER THAT WILL RUN CLAUDE CODE. It sets up a private headless display (the app's own
# "viewport"), the official Claude desktop app, the ccp CLI and the MCP server. Idempotent: re-run any time.
#
# Only two things need a person:
#   1. choosing this computer (you are running the script here), and
#   2. signing in to Claude once inside the viewport (the script prints how to open it).
#
#   ./install.sh [--display :99] [--bot-name NAME] [--operator-name NAME] [--timezone Area/City]
#                [--screen 540x1170] [--viewport-bind 127.0.0.1] [--no-apt] [--no-http] [--claude-md]
#                [--dry-run] [--uninstall]
#   --claude-md  also add the origin-bracket note (CLAUDE.md) to ~/.claude/CLAUDE.md for local Claude Code sessions
set -euo pipefail

REPO="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
CONFIG_DIR="${CCP_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/claude-projects-mcp}"
CONFIG_FILE="${CCP_CONFIG:-$CONFIG_DIR/config.json}"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
DISPLAY_ID=":99"; BOT_NAME=""; OPERATOR=""; TZ_NAME=""; SCREEN="540x1170"; VIEWPORT_BIND="127.0.0.1"
DO_APT=1; DO_HTTP=1; DRY=0; UNINSTALL=0; CLAUDE_MD=0
UNITS=(ccp-xvfb ccp-wm ccp-keyring ccp-claude-desktop ccp-vnc ccp-novnc ccp-mcp)

while [ $# -gt 0 ]; do
  case "$1" in
    --display) DISPLAY_ID="$2"; shift 2;;
    --bot-name) BOT_NAME="$2"; shift 2;;
    --operator-name) OPERATOR="$2"; shift 2;;
    --timezone) TZ_NAME="$2"; shift 2;;
    --screen) SCREEN="$2"; shift 2;;
    --viewport-bind) VIEWPORT_BIND="$2"; shift 2;;
    --no-apt) DO_APT=0; shift;;
    --no-http) DO_HTTP=0; shift;;
    --claude-md) CLAUDE_MD=1; shift;;
    --dry-run) DRY=1; shift;;
    --uninstall) UNINSTALL=1; shift;;
    -h|--help) sed -n '2,16p' "$0"; exit 0;;
    *) echo "unknown option: $1" >&2; exit 2;;
  esac
done
[[ "$DISPLAY_ID" =~ ^:[0-9]+$ ]] || { echo "--display must look like :99" >&2; exit 2; }
N="${DISPLAY_ID#:}"; VNC_PORT=$((5900 + N)); NOVNC_PORT=$((6080 + N))

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
run() { if [ "$DRY" = 1 ]; then echo "    [dry-run] $*"; else "$@"; fi; }

if [ "$UNINSTALL" = 1 ]; then
  say "Stopping and removing user services (config, sign-in and state are kept)"
  for u in "${UNITS[@]}"; do run systemctl --user disable --now "$u.service" 2>/dev/null || true; run rm -f "$UNIT_DIR/$u.service"; done
  run systemctl --user disable --now ccp-refresh.timer 2>/dev/null || true; run rm -f "$UNIT_DIR/ccp-refresh.service" "$UNIT_DIR/ccp-refresh.timer"
  run systemctl --user daemon-reload
  echo "Remove $CONFIG_DIR and ~/.local/state/claude-projects-mcp yourself if you also want the config and cache gone."
  exit 0
fi

# ---------------------------------------------------------------- 1. system packages
. /etc/os-release 2>/dev/null || true
case "${ID:-}:${ID_LIKE:-}" in *debian*|*ubuntu*) ;; *) echo "This installer supports Ubuntu/Debian (Claude desktop for Linux is Debian-based). Found: ${PRETTY_NAME:-unknown}" >&2; exit 1;; esac
PKGS=(xvfb openbox x11vnc novnc websockify python3-venv python3-gi gir1.2-atspi-2.0 at-spi2-core xdotool xclip
      gnome-keyring libsecret-1-0 dbus-user-session libxss1 iproute2 poppler-utils curl gnupg git ca-certificates)
if [ "$DO_APT" = 1 ]; then
  say "Installing system packages (sudo)"
  missing=(); for p in "${PKGS[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
  if [ ${#missing[@]} -gt 0 ]; then run sudo apt-get update -qq; run sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"; fi
  if ! command -v chromium >/dev/null && ! command -v chromium-browser >/dev/null && ! command -v google-chrome-stable >/dev/null \
     && ! command -v firefox >/dev/null && ! command -v firefox-esr >/dev/null; then
    say "Installing a browser for the in-viewport sign-in"
    run sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq firefox-esr || run sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq chromium || true
  fi
  if ! command -v claude-desktop >/dev/null 2>&1; then
    say "Installing the official Claude desktop app from Anthropic's apt repository"
    KEY=/usr/share/keyrings/claude-desktop-archive-keyring.asc
    run sudo curl -fsSLo "$KEY" https://downloads.claude.ai/claude-desktop/key.asc
    if [ "$DRY" = 0 ]; then
      FPR="$(gpg --show-keys --with-colons "$KEY" 2>/dev/null | awk -F: '/^fpr/{print $10; exit}')"
      [ "$FPR" = "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE" ] || { echo "Claude apt key fingerprint mismatch ($FPR); aborting" >&2; sudo rm -f "$KEY"; exit 1; }
      echo "deb [arch=amd64,arm64 signed-by=$KEY] https://downloads.claude.ai/claude-desktop/apt/stable stable main" | sudo tee /etc/apt/sources.list.d/claude-desktop.list >/dev/null
    fi
    run sudo apt-get update -qq; run sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq claude-desktop
  fi
else
  say "Skipping apt (--no-apt); these must already be installed: ${PKGS[*]} claude-desktop"
fi

# ---------------------------------------------------------------- 2. python env
say "Creating the Python environment in $REPO/.venv"
run python3 -m venv --system-site-packages "$REPO/.venv"
run "$REPO/.venv/bin/pip" install -q --upgrade pip
run "$REPO/.venv/bin/pip" install -q -r "$REPO/requirements.txt"

# ---------------------------------------------------------------- 3. config + local secrets (never printed)
say "Writing $CONFIG_FILE"
run mkdir -p "$CONFIG_DIR"; run chmod 700 "$CONFIG_DIR"
if [ "$DRY" = 0 ]; then
  [ -z "$TZ_NAME" ] && TZ_NAME="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
  CCP_REPO="$REPO" CCP_CFG="$CONFIG_FILE" CCP_DISP="$DISPLAY_ID" CCP_BN="$BOT_NAME" CCP_OP="$OPERATOR" CCP_TZ="$TZ_NAME" \
  CCP_VNC_PORT="$VNC_PORT" CCP_NOVNC_PORT="$NOVNC_PORT" \
  "$REPO/.venv/bin/python" - <<'PY'
import json, os, pathlib
p = pathlib.Path(os.environ['CCP_CFG']); cfg = json.loads(p.read_text()) if p.is_file() else {}
repo = os.environ['CCP_REPO']
# viewer_ports: the ports this installer gives x11vnc and noVNC, so the "pause while someone watches" check is explicit in
# the config. Ports set by hand are kept; ports that were the old display's defaults follow a --display change.
def _defaults(disp):
    try: n = int(str(disp).split(':', 1)[1].split('.', 1)[0])
    except Exception: return None
    return [5900 + n, 6080 + n]
old_ports = [int(x) for x in (cfg.get('viewer_ports') or [])]
if not old_ports or old_ports == _defaults(cfg.get('display')):
    cfg['viewer_ports'] = [int(os.environ['CCP_VNC_PORT']), int(os.environ['CCP_NOVNC_PORT'])]
cfg.update({'display': os.environ['CCP_DISP'], 'launcher': f'{repo}/bin/claude-desktop-launch', 'idb_python': f'{repo}/.venv/bin/python'})
for env, key in (('CCP_BN', 'bot_name'), ('CCP_OP', 'operator_name'), ('CCP_TZ', 'timezone')):
    if os.environ.get(env): cfg[key] = os.environ[env]
cfg.setdefault('environment_labels', {}); cfg.setdefault('environment_ids', {})
p.write_text(json.dumps(cfg, indent=2) + '\n'); os.chmod(p, 0o600)
PY
  umask 077
  [ -s "$CONFIG_DIR/mcp.token" ]     || head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 48 > "$CONFIG_DIR/mcp.token"
  [ -s "$CONFIG_DIR/keyring-pass" ]  || head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24 > "$CONFIG_DIR/keyring-pass"
  if [ ! -s "$CONFIG_DIR/vncpass" ]; then
    head -c 12 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 8 > "$CONFIG_DIR/vncpass.txt"
    x11vnc -storepasswd "$(cat "$CONFIG_DIR/vncpass.txt")" "$CONFIG_DIR/vncpass" >/dev/null 2>&1
  fi
  chmod 600 "$CONFIG_DIR"/mcp.token "$CONFIG_DIR"/keyring-pass "$CONFIG_DIR"/vncpass "$CONFIG_DIR"/vncpass.txt
fi

# ---------------------------------------------------------------- 4. user services
say "Installing user services for display $DISPLAY_ID"
NOVNC_WEB=/usr/share/novnc; [ -d "$NOVNC_WEB" ] || NOVNC_WEB="$(dirname "$(readlink -f "$(command -v websockify 2>/dev/null || echo /usr/share/novnc/x)")")"
MCP_HOST="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('mcp_host','127.0.0.1'))" "$CONFIG_FILE" 2>/dev/null || echo 127.0.0.1)"
MCP_PORT="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('mcp_port',8791))" "$CONFIG_FILE" 2>/dev/null || echo 8791)"
run mkdir -p "$UNIT_DIR"
for f in "$REPO"/systemd/*; do
  out="$UNIT_DIR/$(basename "$f")"
  if [ "$DRY" = 1 ]; then echo "    [dry-run] render $(basename "$f") -> $out"; continue; fi
  sed -e "s#@DISPLAY@#$DISPLAY_ID#g" -e "s#@SCREEN@#$SCREEN#g" -e "s#@REPO@#$REPO#g" -e "s#@CONFIG_DIR@#$CONFIG_DIR#g" \
      -e "s#@CONFIG_FILE@#$CONFIG_FILE#g" -e "s#@VNC_PORT@#$VNC_PORT#g" -e "s#@NOVNC_PORT@#$NOVNC_PORT#g" \
      -e "s#@VIEWPORT_BIND@#$VIEWPORT_BIND#g" -e "s#@NOVNC_WEB@#$NOVNC_WEB#g" -e "s#@MCP_HOST@#$MCP_HOST#g" -e "s#@MCP_PORT@#$MCP_PORT#g" \
      "$f" > "$out"
done
run systemctl --user daemon-reload
START=(ccp-xvfb ccp-wm ccp-keyring ccp-vnc ccp-novnc ccp-claude-desktop); [ "$DO_HTTP" = 1 ] && START+=(ccp-mcp)
for u in "${START[@]}"; do run systemctl --user enable --now "$u.service"; done
run systemctl --user enable --now ccp-refresh.timer
command -v loginctl >/dev/null && run loginctl enable-linger "$USER" 2>/dev/null || true   # keep services up without a login session

# ---------------------------------------------------------------- 4b. optional: bracket note for local Claude Code sessions
if [ "$CLAUDE_MD" = 1 ]; then
  say "Adding the origin-bracket note to ~/.claude/CLAUDE.md"
  if [ "$DRY" = 0 ]; then
    mkdir -p "$HOME/.claude"; touch "$HOME/.claude/CLAUDE.md"
    if ! grep -q 'BEGIN claude-projects-mcp origin' "$HOME/.claude/CLAUDE.md"; then
      { printf '\n<!-- BEGIN claude-projects-mcp origin -->\n'; cat "$REPO/CLAUDE.md"; printf '<!-- END claude-projects-mcp origin -->\n'; } >> "$HOME/.claude/CLAUDE.md"
    fi
  fi
fi

# ---------------------------------------------------------------- 5. self-check + the two human steps
if [ "$DRY" = 0 ]; then
  sleep 8
  say "Self-check"
  for u in "${START[@]}"; do printf '    %-22s %s\n' "$u" "$(systemctl --user is-active "$u.service" 2>/dev/null || true)"; done
  "$REPO/ccp" --json origin --message "self-check" >/dev/null && echo "    origin bracket          ok"
  if [ "$DO_HTTP" = 1 ]; then
    curl -fsS "http://127.0.0.1:$MCP_PORT/healthz" >/dev/null 2>&1 && echo "    mcp /healthz            ok" || echo "    mcp /healthz            not up yet (check: journalctl --user -u ccp-mcp)"
  fi
fi
cat <<MSG

Installed. Two steps need you:

  1. This computer is now the one that runs Claude Code (display $DISPLAY_ID). Nothing else to choose.
  2. Sign in to Claude once inside the viewport:
       - on this computer, open   http://$VIEWPORT_BIND:$NOVNC_PORT/vnc.html
         or from another one:     ssh -L $NOVNC_PORT:127.0.0.1:$NOVNC_PORT $USER@$(hostname)   then open http://127.0.0.1:$NOVNC_PORT/vnc.html
       - VNC password: in $CONFIG_DIR/vncpass.txt (read it there; it is never printed)
       - sign in to Claude in the app; the login page opens in a browser inside the same viewport.
         If the browser shows a claude:// link instead of returning to the app:  $REPO/bin/claude-open-url 'claude://...'
       - close the viewer tab when done (UI actions pause while a viewer is connected).

Then check:  $REPO/ccp status       (signed_in: true)

Connect an MCP client:
  stdio (same machine or over SSH):  $REPO/.venv/bin/python $REPO/ccp_mcp.py --stdio
  HTTP:  http://$MCP_HOST:$MCP_PORT/mcp   with header  Authorization: Bearer <contents of $CONFIG_DIR/mcp.token>
MSG
