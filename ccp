#!/usr/bin/env bash
# ccp - Claude Code Projects CLI. Settings: ~/.config/claude-projects-mcp/config.json or CCP_* env (see ccp_config.py).
set -euo pipefail
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
export PYTHONWARNINGS=ignore
PY="${CCP_PYTHON:-$HERE/.venv/bin/python}"
[ -x "$PY" ] || PY="$HERE/venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
exec "$PY" "$HERE/ccp_core.py" "$@"
