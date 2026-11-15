# INSTALL.md

The install is a guided flow that your agent runs: **[skill/SKILL.md](skill/SKILL.md), Part A**. The agent loops on
`ccp setup status`, runs each `next_action`, and asks you only when a result carries `ask_user`.

| step | command | needs you? |
|---|---|---|
| 1. choose the host | (a question) | yes: which always-on computer |
| 2. preflight | `./ccp setup preflight` | only if a check fails |
| 3. install | `./ccp setup install --bot-name NAME` (Mac: `hosts/macos/setup.sh`, Windows: `hosts\windows\setup.ps1`) | one sudo line, if sudo needs a password |
| 4. sign in | `./ccp setup signin start`, then `./ccp setup signin wait` | yes: sign in once in the noVNC tab |
| 5. Projects beta | `./ccp setup check-beta` | only if the account has no Projects |
| 6. connect | `./ccp setup connect`, then `claude_setup(action="status")` through the connector | only for a tunnel |
| 7. verify | `./ccp setup verify --live` | no |

Doing it by hand instead: [docs/manual-install.md](docs/manual-install.md). Other hosts: [docs/hosts.md](docs/hosts.md).
Never print, paste or upload the MCP token, the viewport password or the keyring password; refer to them by path.
