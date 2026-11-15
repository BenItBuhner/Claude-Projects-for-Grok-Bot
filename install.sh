#!/usr/bin/env bash
# claude-projects-mcp installer (Ubuntu 22.04+/Debian 12+, x86_64 or arm64; user-level systemd, or the plugin's own
# process supervisor on hosts without it, such as a container whose PID 1 is tini).
#
# Run it ON THE COMPUTER THAT WILL RUN CLAUDE CODE. It sets up a private headless display (the app's own
# "viewport"), the official Claude desktop app, the claude-projects CLI and the MCP server. Idempotent: re-run any time.
#
# Only two things need a person:
#   1. choosing this computer (you are running the script here), and
#   2. signing in to Claude once inside the viewport (the script prints how to open it).
#
#   ./install.sh [--display :99] [--bot-name NAME] [--operator-name NAME] [--timezone Area/City]
#                [--screen 540x1170] [--viewport-bind 127.0.0.1] [--no-apt] [--no-http | --http] [--mcp-port N]
#                [--service-mode auto|systemd|process] [--claude-md] [--dry-run] [--uninstall] [--apt-only]
#   --no-http    no HTTP MCP server (agents connect over stdio); remembered in the config until --http
#   --mcp-port   port for the HTTP MCP server (default 8791; remembered in the config)
#   --service-mode  auto (default): user systemd when it is running, else the process supervisor (claude_projects/proc.py)
#   --claude-md  also add the origin-bracket note (AGENTS.md) to ~/.claude/CLAUDE.md for local Claude Code sessions
#   --apt-only   only install the system packages and the Claude app, then stop (run it with sudo when sudo needs a
#                password the agent can't type: sudo ./install.sh --apt-only; then the agent runs ./install.sh --no-apt)
# The agent-driven setup flow wraps this script: claude-projects setup status (see skill/SKILL.md, Part A).
set -euo pipefail
USER="${USER:-$(id -un)}"   # unset in some containers (docker exec)

REPO="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# Migration: variables exported under the short prefixes of releases before 1.0.3 still work; the CLAUDE_PROJECTS_* name wins
for k in $(compgen -e); do case "$k" in CPG_*|CCP_*) n="CLAUDE_PROJECTS_${k#???_}"; [ -n "${!n+x}" ] || export "$n=${!k}";; esac; done
CONFIG_DIR="${CLAUDE_PROJECTS_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/claude-projects-mcp}"
CONFIG_FILE="${CLAUDE_PROJECTS_CONFIG:-$CONFIG_DIR/config.json}"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
DISPLAY_ID=":99"; BOT_NAME=""; OPERATOR=""; TZ_NAME=""; SCREEN="540x1170"; VIEWPORT_BIND="127.0.0.1"
DO_APT=1; HTTP_OPT=""; MCP_PORT_OPT=""; MODE_OPT=auto; DRY=0; UNINSTALL=0; CLAUDE_MD=0; APT_ONLY=0
UNITS=(claude-projects-xvfb claude-projects-wm claude-projects-keyring claude-projects-desktop claude-projects-vnc claude-projects-novnc claude-projects-mcp)
# Migration: the service names of releases before 1.0.3 (two generations of short prefixes)
OLD_UNITS=(); for p in cpg ccp; do for u in xvfb wm keyring claude-desktop vnc novnc mcp refresh; do OLD_UNITS+=("$p-$u"); done; done

while [ $# -gt 0 ]; do
  case "$1" in
    --display) DISPLAY_ID="$2"; shift 2;;
    --bot-name) BOT_NAME="$2"; shift 2;;
    --operator-name) OPERATOR="$2"; shift 2;;
    --timezone) TZ_NAME="$2"; shift 2;;
    --screen) SCREEN="$2"; shift 2;;
    --viewport-bind) VIEWPORT_BIND="$2"; shift 2;;
    --no-apt) DO_APT=0; shift;;
    --no-http) HTTP_OPT=0; shift;;
    --http) HTTP_OPT=1; shift;;
    --mcp-port) MCP_PORT_OPT="$2"; shift 2;;
    --service-mode) MODE_OPT="$2"; shift 2;;
    --claude-md) CLAUDE_MD=1; shift;;
    --dry-run) DRY=1; shift;;
    --uninstall) UNINSTALL=1; shift;;
    --apt-only) APT_ONLY=1; shift;;
    -h|--help) sed -n '2,25p' "$0"; exit 0;;
    *) echo "unknown option: $1" >&2; exit 2;;
  esac
done
[[ "$DISPLAY_ID" =~ ^:[0-9]+$ ]] || { echo "--display must look like :99" >&2; exit 2; }
N="${DISPLAY_ID#:}"; VNC_PORT=$((5900 + N)); NOVNC_PORT=$((6080 + N))
[ -z "$MCP_PORT_OPT" ] || [[ "$MCP_PORT_OPT" =~ ^[0-9]+$ && "$MCP_PORT_OPT" -ge 1024 && "$MCP_PORT_OPT" -le 65535 ]] || { echo "--mcp-port must be a port number 1024-65535" >&2; exit 2; }
case "$MODE_OPT" in auto|systemd|process) ;; *) echo "--service-mode must be auto, systemd or process" >&2; exit 2;; esac

SUDO=sudo; AS="with sudo"; [ "$(id -u)" = 0 ] && { SUDO="env"; AS="as root"; }  # as root, `env VAR=x cmd` keeps the same command shape
say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
run() { if [ "$DRY" = 1 ]; then echo "    [dry-run] $*"; else "$@"; fi; }
cfg_get() { python3 -c "import json,sys;v=json.load(open(sys.argv[1])).get(sys.argv[2]);print('' if v is None else (str(v).lower() if isinstance(v,bool) else v))" "$CONFIG_FILE" "$1" 2>/dev/null || true; }

# How the services run: user systemd when its manager answers, else the plugin's own supervisor (claude_projects/proc.py).
# A machine that boots with systemd but whose user manager isn't up yet is fixed by logging in once (or linger),
# not by switching modes.
pid1="$(cat /proc/1/comm 2>/dev/null || echo unknown)"
MODE="$MODE_OPT"
if [ "$MODE" = auto ]; then
  MODE="$(cfg_get service_mode)"
  if [ -z "$MODE" ] || [ "$MODE" = auto ]; then
    if systemctl --user show-environment >/dev/null 2>&1; then MODE=systemd
    elif [ "$pid1" = systemd ]; then MODE=systemd-unreachable
    else MODE=process; fi
  fi
fi
SVC() { if [ "$MODE" = process ]; then PYTHONPATH="$REPO" python3 -m claude_projects.proc "$@"; else systemctl --user "$@"; fi; }
# Migration: an install from before 1.0.3 runs the same services under older names. Stop and remove those before the
# claude-projects-* ones start, so the two sets never run side by side (same display and ports), and delete the
# old module and CLI shims that a checkout may still have lying around. Config, sign-in and state are kept.
remove_old_units() {
  local u found=0
  for u in "${OLD_UNITS[@]}"; do [ -f "$UNIT_DIR/$u.service" ] && found=1; done
  if [ "$found" = 1 ]; then
    say "Moving the services to their new names (claude-projects-*)"
    for u in "${OLD_UNITS[@]}"; do
      if [ -f "$UNIT_DIR/$u.timer" ]; then run SVC disable --now "$u.timer" 2>/dev/null || true; run rm -f "$UNIT_DIR/$u.timer"; fi
      [ -f "$UNIT_DIR/$u.service" ] || continue
      run SVC disable --now "$u.service" 2>/dev/null || true; run rm -f "$UNIT_DIR/$u.service"
    done
    if [ "$MODE" = systemd ]; then run SVC daemon-reload || true; fi
  fi
  local f
  for f in "$REPO"/ccp "$REPO"/ccp_*.py "$REPO"/cpg_*.py "$REPO"/hosts/macos/ccp "$REPO"/hosts/windows/ccp.cmd; do
    [ -e "$f" ] && ! git -C "$REPO" ls-files --error-unmatch "${f#"$REPO"/}" >/dev/null 2>&1 && run rm -f "$f"
  done
  f="$HOME/.local/bin/ccp"
  if [ -L "$f" ]; then case "$(readlink -f "$f" 2>/dev/null)" in "$REPO"/*|"") run rm -f "$f";; esac; fi
  return 0
}
unit_log_hint() { if [ "$MODE" = process ]; then echo "tail -n 50 ${CLAUDE_PROJECTS_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/claude-projects-mcp}/proc/logs/$1.log"; else echo "journalctl --user -u $1 -n 50"; fi; }

if [ "$UNINSTALL" = 1 ]; then
  say "Stopping and removing user services (config, sign-in and state are kept)"
  for u in "${UNITS[@]}"; do run SVC disable --now "$u.service" 2>/dev/null || true; run rm -f "$UNIT_DIR/$u.service"; done
  run SVC disable --now claude-projects-refresh.timer 2>/dev/null || true; run rm -f "$UNIT_DIR/claude-projects-refresh.service" "$UNIT_DIR/claude-projects-refresh.timer"
  remove_old_units
  if [ "$MODE" = process ]; then run SVC down || true; else run SVC daemon-reload || true; fi
  echo "Remove $CONFIG_DIR and ~/.local/state/claude-projects-mcp yourself if you also want the config and cache gone."
  exit 0
fi

# ---------------------------------------------------------------- 1. system packages
. /etc/os-release 2>/dev/null || true
case "${ID:-}:${ID_LIKE:-}" in *debian*|*ubuntu*) ;; *) echo "This installer supports Ubuntu/Debian (Claude desktop for Linux is Debian-based). Found: ${PRETTY_NAME:-unknown}" >&2; exit 1;; esac
# Listed explicitly and installed without "Recommends", which roughly halves the download on a bare Ubuntu image
# (fonts and xauth are the recommends the app and Xvfb actually need, so they are listed here).
PKGS=(xvfb xauth xfonts-base fonts-dejavu-core fonts-liberation openbox x11vnc novnc websockify python3-venv python3-gi
      gir1.2-atspi-2.0 at-spi2-core xdotool xclip gnome-keyring libsecret-1-0 dbus dbus-user-session libxss1 iproute2
      poppler-utils curl gnupg git ca-certificates)
APT_INSTALL=(apt-get install -y -qq --no-install-recommends)
# The first of firefox-esr / chromium that apt can really install here (a candidate other than "(none)"), or nothing.
# CLAUDE_PROJECTS_BROWSER_CANDIDATES overrides the list (tests).
apt_browser_pkg() {
  local p c
  for p in ${CLAUDE_PROJECTS_BROWSER_CANDIDATES:-firefox-esr chromium}; do
    c="$(apt-cache policy "$p" 2>/dev/null | awk '/Candidate:/ {print $2; exit}')"
    if [ -n "$c" ] && [ "$c" != "(none)" ]; then echo "$p"; return 0; fi
  done
  return 0
}
if [ "$DO_APT" = 1 ]; then
  say "Installing system packages ($AS)"
  missing=(); for p in "${PKGS[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
  APT_UPDATED=0
  if [ ${#missing[@]} -gt 0 ]; then run $SUDO apt-get update -qq; APT_UPDATED=1; run $SUDO DEBIAN_FRONTEND=noninteractive "${APT_INSTALL[@]}" "${missing[@]}"; fi
  # a real browser for the in-viewport sign-in. Only install a package this distro really has: firefox-esr and chromium
  # are real debs on Debian, but Ubuntu has neither (its chromium-browser/firefox debs are only stubs for snaps), so
  # there the snap is the path. Checking the apt candidate first keeps apt's "no installation candidate" error away.
  if ! "$REPO/bin/claude-signin-browser" --check >/dev/null 2>&1; then
    say "Installing a browser for the in-viewport sign-in"
    BROWSER_PKG="$(apt_browser_pkg)"
    if [ -z "$BROWSER_PKG" ] && [ "$APT_UPDATED" = 0 ]; then run $SUDO apt-get update -qq; BROWSER_PKG="$(apt_browser_pkg)"; fi
    if [ -n "$BROWSER_PKG" ]; then run $SUDO DEBIAN_FRONTEND=noninteractive "${APT_INSTALL[@]}" "$BROWSER_PKG" || true
    else echo "    no firefox-esr or chromium deb here (normal on Ubuntu): using the chromium snap"; fi
    if ! "$REPO/bin/claude-signin-browser" --check >/dev/null 2>&1 && command -v snap >/dev/null && snap version >/dev/null 2>&1; then
      run $SUDO snap install chromium || true
    fi
    "$REPO/bin/claude-signin-browser" --check >/dev/null 2>&1 || echo "    warning: no usable sign-in browser yet; claude-projects setup signin start will say how to add one" >&2
  fi
  if ! command -v claude-desktop >/dev/null 2>&1; then
    say "Installing the official Claude desktop app from Anthropic's apt repository"
    KEY=/usr/share/keyrings/claude-desktop-archive-keyring.asc
    run $SUDO curl -fsSLo "$KEY" https://downloads.claude.ai/claude-desktop/key.asc
    if [ "$DRY" = 0 ]; then
      FPR="$(gpg --show-keys --with-colons "$KEY" 2>/dev/null | awk -F: '/^fpr/{print $10; exit}')"
      [ "$FPR" = "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE" ] || { echo "Claude apt key fingerprint mismatch ($FPR); aborting" >&2; $SUDO rm -f "$KEY"; exit 1; }
      echo "deb [arch=amd64,arm64 signed-by=$KEY] https://downloads.claude.ai/claude-desktop/apt/stable stable main" | $SUDO tee /etc/apt/sources.list.d/claude-desktop.list >/dev/null
    fi
    run $SUDO apt-get update -qq; run $SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y -qq claude-desktop
  fi
else
  say "Skipping apt (--no-apt); these must already be installed: ${PKGS[*]} claude-desktop"
fi

if [ "$APT_ONLY" = 1 ]; then
  say "System packages done (--apt-only). Now, as the regular user (not root): $REPO/install.sh --no-apt ..."
  exit 0
fi

# ---------------------------------------------------------------- 1b. service manager
if [ "$MODE" = systemd-unreachable ]; then
  echo "This machine runs systemd, but the user manager for $USER isn't reachable (systemctl --user)." >&2
  echo "Fix: log in once as $USER (a real login, not su), or: sudo loginctl enable-linger $USER, then re-run." >&2
  echo "(Or run the plugin's own supervisor instead: --service-mode process.)" >&2
  exit 3
fi
if [ "$MODE" = process ]; then
  say "Service mode: process (no user systemd here; PID 1 is $pid1). claude_projects/proc.py runs the services."
  command -v dbus-daemon >/dev/null 2>&1 || [ "$DRY" = 1 ] || { echo "dbus-daemon is missing (apt package dbus)" >&2; exit 3; }
else
  say "Service mode: systemd (user units)"
fi

# ---------------------------------------------------------------- 2. python env
say "Creating the Python environment in $REPO/.venv"
run python3 -m venv --system-site-packages "$REPO/.venv"
run "$REPO/.venv/bin/pip" install -q --upgrade pip
run "$REPO/.venv/bin/pip" install -q -r "$REPO/requirements.txt"
run "$REPO/.venv/bin/pip" install -q --no-deps -r "$REPO/requirements-nodeps.txt"

# ---------------------------------------------------------------- 3. config + local secrets (never printed)
say "Writing $CONFIG_FILE"
run mkdir -p "$CONFIG_DIR"; run chmod 700 "$CONFIG_DIR"
if [ "$DRY" = 0 ]; then
  [ -z "$TZ_NAME" ] && TZ_NAME="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
  CLAUDE_PROJECTS_REPO="$REPO" CLAUDE_PROJECTS_CFG="$CONFIG_FILE" CLAUDE_PROJECTS_DISP="$DISPLAY_ID" CLAUDE_PROJECTS_BN="$BOT_NAME" CLAUDE_PROJECTS_OP="$OPERATOR" CLAUDE_PROJECTS_TZ="$TZ_NAME" \
  CLAUDE_PROJECTS_VNC_PORT="$VNC_PORT" CLAUDE_PROJECTS_NOVNC_PORT="$NOVNC_PORT" CLAUDE_PROJECTS_SCREEN="$SCREEN" CLAUDE_PROJECTS_VBIND="$VIEWPORT_BIND" \
  CLAUDE_PROJECTS_HTTP="$HTTP_OPT" CLAUDE_PROJECTS_MCPPORT="$MCP_PORT_OPT" CLAUDE_PROJECTS_MODE="$MODE" \
  "$REPO/.venv/bin/python" - <<'PY'
import json, os, pathlib
p = pathlib.Path(os.environ['CLAUDE_PROJECTS_CFG']); cfg = json.loads(p.read_text()) if p.is_file() else {}
repo = os.environ['CLAUDE_PROJECTS_REPO']
# viewer_ports: the ports this installer gives x11vnc and noVNC, so the "pause while someone watches" check is explicit in
# the config. Ports set by hand are kept; ports that were the old display's defaults follow a --display change.
def _defaults(disp):
    try: n = int(str(disp).split(':', 1)[1].split('.', 1)[0])
    except Exception: return None
    return [5900 + n, 6080 + n]
old_ports = [int(x) for x in (cfg.get('viewer_ports') or [])]
if not old_ports or old_ports == _defaults(cfg.get('display')):
    cfg['viewer_ports'] = [int(os.environ['CLAUDE_PROJECTS_VNC_PORT']), int(os.environ['CLAUDE_PROJECTS_NOVNC_PORT'])]
cfg.update({'display': os.environ['CLAUDE_PROJECTS_DISP'], 'launcher': f'{repo}/bin/claude-desktop-launch', 'idb_python': f'{repo}/.venv/bin/python'})
cfg.update({'screen': os.environ.get('CLAUDE_PROJECTS_SCREEN') or '540x1170', 'viewport_bind': os.environ.get('CLAUDE_PROJECTS_VBIND') or '127.0.0.1'})
# new installs keep the noVNC viewport off except while someone signs in (claude-projects setup signin); existing ones keep theirs
cfg.setdefault('viewport_mode', 'on-demand' if not p.is_file() else 'always')
# new installs get their own keyring folder, so an existing desktop login.keyring is never asked to unlock; existing
# installs keep theirs (the app's sign-in is stored in it)
cfg.setdefault('keyring_data_dir', str(pathlib.Path.home() / '.local/share/claude-projects-mcp/keyring') if not p.is_file() else '')
if os.environ.get('CLAUDE_PROJECTS_MODE'): cfg['service_mode'] = os.environ['CLAUDE_PROJECTS_MODE']
if os.environ.get('CLAUDE_PROJECTS_HTTP'): cfg['mcp_http'] = os.environ['CLAUDE_PROJECTS_HTTP'] == '1'
if os.environ.get('CLAUDE_PROJECTS_MCPPORT'): cfg['mcp_port'] = int(os.environ['CLAUDE_PROJECTS_MCPPORT'])
for env, key in (('CLAUDE_PROJECTS_BN', 'bot_name'), ('CLAUDE_PROJECTS_OP', 'operator_name'), ('CLAUDE_PROJECTS_TZ', 'timezone')):
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
MCP_PORT="$(cfg_get mcp_port)"; MCP_PORT="${MCP_PORT:-${MCP_PORT_OPT:-8791}}"
DO_HTTP="$(cfg_get mcp_http)"; case "${DO_HTTP:-${HTTP_OPT:-1}}" in false|0) DO_HTTP=0;; *) DO_HTTP=1;; esac
KEYRING_DIR="$(cfg_get keyring_data_dir)"
if [ -n "$KEYRING_DIR" ]; then KEYRING_ENV="Environment=XDG_DATA_HOME=$KEYRING_DIR"; run mkdir -p "$KEYRING_DIR"; run chmod 700 "$KEYRING_DIR"
else KEYRING_ENV="; keyring: the default data dir (an install from before private keyrings)"; fi
run mkdir -p "$UNIT_DIR"
remove_old_units
for f in "$REPO"/systemd/*; do
  out="$UNIT_DIR/$(basename "$f")"
  if [ "$DRY" = 1 ]; then echo "    [dry-run] render $(basename "$f") -> $out"; continue; fi
  sed -e "s#@DISPLAY@#$DISPLAY_ID#g" -e "s#@SCREEN@#$SCREEN#g" -e "s#@REPO@#$REPO#g" -e "s#@CONFIG_DIR@#$CONFIG_DIR#g" \
      -e "s#@CONFIG_FILE@#$CONFIG_FILE#g" -e "s#@VNC_PORT@#$VNC_PORT#g" -e "s#@NOVNC_PORT@#$NOVNC_PORT#g" \
      -e "s#@VIEWPORT_BIND@#$VIEWPORT_BIND#g" -e "s#@NOVNC_WEB@#$NOVNC_WEB#g" -e "s#@MCP_HOST@#$MCP_HOST#g" -e "s#@MCP_PORT@#$MCP_PORT#g" \
      -e "s#@KEYRING_ENV@#$KEYRING_ENV#g" \
      "$f" > "$out"
done
[ "$MODE" = process ] && { run SVC up || { echo "the process supervisor did not start; see its log under the state dir (proc/supervisor.log)" >&2; exit 4; }; }
run SVC daemon-reload
START=(claude-projects-xvfb claude-projects-wm claude-projects-keyring claude-projects-vnc claude-projects-novnc claude-projects-desktop); [ "$DO_HTTP" = 1 ] && START+=(claude-projects-mcp)
FAILED=()
for u in "${START[@]}"; do run SVC enable --now "$u.service" || FAILED+=("$u"); done
if [ "$DO_HTTP" = 0 ]; then run SVC disable --now claude-projects-mcp.service >/dev/null 2>&1 || true; fi   # stdio only: no HTTP server
run SVC enable --now claude-projects-refresh.timer || FAILED+=(claude-projects-refresh.timer)
if [ "$MODE" = systemd ]; then
  command -v loginctl >/dev/null && run loginctl enable-linger "$USER" 2>/dev/null || true   # keep services up without a login session
fi

# ---------------------------------------------------------------- 4b. optional: bracket note for local Claude Code sessions
if [ "$CLAUDE_MD" = 1 ]; then
  say "Adding the origin-bracket note (AGENTS.md) to ~/.claude/CLAUDE.md"
  if [ "$DRY" = 0 ]; then
    mkdir -p "$HOME/.claude"; touch "$HOME/.claude/CLAUDE.md"
    if ! grep -q 'BEGIN claude-projects-mcp origin' "$HOME/.claude/CLAUDE.md"; then
      { printf '\n<!-- BEGIN claude-projects-mcp origin -->\n'; cat "$REPO/AGENTS.md"; printf '<!-- END claude-projects-mcp origin -->\n'; } >> "$HOME/.claude/CLAUDE.md"
    fi
  fi
fi

# ---------------------------------------------------------------- 5. self-check + the two human steps
if [ "$DRY" = 0 ]; then
  sleep 8
  say "Self-check"
  for u in "${START[@]}"; do printf '    %-22s %s\n' "$u" "$(SVC is-active "$u.service" 2>/dev/null || true)"; done
  "$REPO/claude-projects" --json origin --message "self-check" >/dev/null && echo "    origin bracket          ok"
  if [ "$DO_HTTP" = 1 ]; then
    curl -fsS "http://127.0.0.1:$MCP_PORT/healthz" >/dev/null 2>&1 && echo "    mcp /healthz            ok" || echo "    mcp /healthz            not up yet (check: $(unit_log_hint claude-projects-mcp))"
  fi
fi
if [ ${#FAILED[@]} -gt 0 ] && [ "$DRY" = 0 ]; then
  echo "Some services did not start: ${FAILED[*]}" >&2
  for u in "${FAILED[@]}"; do echo "    $(unit_log_hint "$u")" >&2; done
  exit 5
fi
[ "$DO_HTTP" = 1 ] && HTTP_LINE="  HTTP:  http://$MCP_HOST:$MCP_PORT/mcp   with header  Authorization: Bearer <contents of $CONFIG_DIR/mcp.token>" \
  || HTTP_LINE="  HTTP:  off (stdio only; turn it on with ./install.sh --no-apt --http [--mcp-port N])"
cat <<MSG

Installed (services: $MODE). Two steps need you:

  1. This computer is now the one that runs Claude Code (display $DISPLAY_ID). Nothing else to choose.
  2. Sign in to Claude once inside the viewport (the guided way: $REPO/claude-projects setup signin start):
       - on this computer, open   http://$VIEWPORT_BIND:$NOVNC_PORT/vnc.html
         or from another one:     ssh -L $NOVNC_PORT:127.0.0.1:$NOVNC_PORT $USER@$(hostname)   then open http://127.0.0.1:$NOVNC_PORT/vnc.html
       - VNC password: in $CONFIG_DIR/vncpass.txt (read it there; it is never printed)
       - in the app, click "Get started" (older versions say "Sign in"); the login page opens in a browser inside the
         same viewport. If it shows a claude:// link instead of returning to the app:  $REPO/bin/claude-open-url 'claude://...'
       - close the viewer tab when done (UI actions pause while a viewer is connected).

Then check:  $REPO/claude-projects status       (signed_in: true)

Connect an MCP client:
  stdio (same machine or over SSH):  env PYTHONPATH=$REPO $REPO/.venv/bin/python -m claude_projects.mcp_server --stdio
$HTTP_LINE
MSG
