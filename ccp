#!/usr/bin/env bash
# ccp: the old name of the claude-projects CLI (renamed in 1.0.2). Kept so existing units (ExecStart=.../ccp refresh),
# symlinks such as ~/.local/bin/ccp and scripts keep working; it runs ./claude-projects with the same arguments.
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
exec "$HERE/claude-projects" "$@"
