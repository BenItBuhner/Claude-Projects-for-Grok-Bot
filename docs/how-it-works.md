# How it works

```
Grok Bot ──MCP──▶ claude_projects/mcp_server.py ──▶ claude-projects (CLI) ──AT-SPI + xdotool──▶ Claude desktop app on its own display (:99)
                                       └──── private copy of the app's IndexedDB (project list, sessions)
```

- **Reads.** `claude://` deep links open a project or thread, and the transcript is read through the Linux accessibility tree (AT-SPI).
- **Sends.** Text goes into the app's own composer, the Send button is pressed, and the message is read back from the transcript to confirm it landed.
- **One UI action at a time.** A lock file serializes UI work. Actions are refused while a person is connected to the viewport, or when the message box holds an unsent draft.
- **No Claude credentials are read or stored.** You sign in to the app once. The MCP server has its own random bearer token.
- **Where it runs vs. where work runs.** The app runs on the computer you installed it on. A session in it can run there, on another of your machines, or in Anthropic's cloud. Responses report that as `machine` / `environment_info`, and unknown environments stay unknown.

## Tools

| Tool | What it does |
|---|---|
| `claude_projects_list` | Projects with ids, primary thread id, last activity, unread. Served from cache unless `refresh` |
| `claude_project_get` | Primary thread messages and child threads (group, status, PRs) |
| `claude_thread_read` | Transcript of the primary or a child thread, with attachments, blocks and `sent_by_bot` |
| `claude_thread_status` | Compact triage: state, latest message, what it's waiting on, machine |
| `claude_thread_attachment_get` | An image, file or output file from a message |
| `claude_thread_send` | **Write.** Types a message (with the origin bracket) and sends it. `dry_run` checks targeting only |
| `claude_thread_wait` | Polls until Claude has replied and stopped working |
| `claude_thread_create` | **Write.** Asks the primary thread to start a child thread |
| `claude_refresh` | Re-reads the project list and changed projects into the cache |
| `claude_project_create` / `claude_project_pin` | **Write.** Create or pin a project |
| `claude_project_add_repo` / `claude_project_remove_repo` | **Write.** Attach or detach a GitHub repo on an existing project |
| `claude_project_env` | Read-only: repos, cloud environment, variable *names* |
| `claude_project_set_env` | **Write.** Cloud environment variables (secrets come from files and are never echoed) |
| `claude_thread_permissions` | Read-only: permission cards showing in a thread (type, target, prompt, options, `card_id`) |
| `claude_thread_permission` | **Write.** Presses a card option by label (`Add folder`, `Not now`, ...). `dry_run` (default) presses nothing |
| `claude_thread_forward` | **Write.** Relays one message verbatim to another thread under a `Forwarded from ...` header, optionally with its files. `dry_run` by default |
| `claude_staging` | The managed file directory: paths, sizes, TTLs; `sweep` cleans up (dry run by default) |
| `claude_skill_get` / `claude_policy_get` | The canonical agent skill with the user's effective policy / the policy alone |
| `claude_policy_propose` | Appends a proposed policy change to a user-owned file; never applied automatically |
| `claude_app_status` | Health: route, dialogs, viewers, lock holder, sign-in |
| `claude_setup` | Setup state and next step, read-only verify, Projects-beta check, tidy, viewport on/off ([skill Part A](../skill/SKILL.md)) |

The CLI has the same commands: `./claude-projects --help`. `./claude-projects origin --message "hi"` previews the bracket without touching the app.

## Ids

Every Project, primary thread and child thread has a stable id that the app itself uses: Project `chan_...`, primary
thread `cse_...`, child thread `cmsg_...`. Read and status results include `address` = `<chan_>/<cse_|cmsg_>`, which is
what agents should use to refer to a thread (it is also the source line of a forward). Messages are addressed by their
`index` in a thread, attachments by `<message index>.<n>`.

## Permission cards

Claude Code Projects run in Auto permission mode, but the app still shows dynamic cards (add this folder to the session,
add a repository, allow a machine, run a command). The plugin snapshots the open page's accessibility tree into plain
data and finds cards generically: the nearest container of option buttons whose text reads like a permission prompt
(see [`claude_projects/permissions.py`](../claude_projects/permissions.py)). Cards appear in `permission_cards` on reads and status, and
`current_state.waiting_on` becomes `permission`. A reply that stalls in prose ("should I proceed?") sets the same flag
with `permission_hint`. Whether to press anything is the agent's call under the user's policy.

## Skill and policy

[`skill/SKILL.md`](../skill/SKILL.md) is the canonical skill for agents. Its POLICY section is a default: the user
owns `~/.config/claude-projects-mcp/policy.md`, which replaces it when present. `claude_skill_get` returns the skill
with the effective policy merged in, so the skill, the policy and the tool list come from the same server.

## Files

One managed staging root (`<state_dir>/staging`, see [`claude_projects/staging.py`](../claude_projects/staging.py)): `incoming/` for fetched
attachments, `outgoing/<stamp>-<id>/<name>` for files staged for a send (unique per send, so names never collide),
`attach-test/` and `msg/` for scratch. Entries expire per folder (72 h / 24 h / 6 h / 2 h by default) and the oldest are
trimmed above `staging_max_mb`; a sweep runs at most hourly and never touches anything outside the root.

## Transports

| Where the client runs | Command / URL |
|---|---|
| Same computer, or over SSH | `~/claude-projects-mcp/.venv/bin/python ~/claude-projects-mcp/claude_projects/mcp_server.py --stdio` (via `ssh host …`) |
| Elsewhere, through your own tunnel or VPN | `http://127.0.0.1:8791/mcp` + `Authorization: Bearer <~/.config/claude-projects-mcp/mcp.token>`; `bin/grok-bot-bridge <url> <token-file>` turns it into stdio |

## Configuration

`~/.config/claude-projects-mcp/config.json`, or `CLAUDE_PROJECTS_*` environment variables (see [`claude_projects/config.py`](../claude_projects/config.py)):

- `display`, `bot_name`, `operator_name`, `timezone`
- `mcp_host` / `mcp_port`, `allowed_hosts` (add your tunnel hostname here)
- `viewport_mode`: `on-demand` (new installs: noVNC is only on while someone signs in) or `always`
- `viewer_ports`: VNC/noVNC ports; UI actions refuse while a viewer is connected. `install.sh` writes `5900+N` and `6080+N`
  for display `:N`, and an empty value falls back to the same ports
- `environment_labels` / `environment_ids` (optional): map Claude Code's "Running on …" labels to names like "the workstation"
- `staging_dir`, `staging_ttl_hours`, `staging_max_mb`, `attachments_dir`, `skill_path`
- `state_dir`, `ui_lock_file`, `launcher`, `xdotool`, `xclip`, `idb_python`, `extra_lib_path`, `gi_typelib_path` for hosts with a custom layout
- `legacy_field_aliases`: also emit older field names (`time_ct`, `path_on_home_lab`) for agents with an older skill

## What `install.sh` does

- Installs Xvfb, Openbox, x11vnc, noVNC, the AT-SPI and GObject bindings, xdotool and xclip.
- Installs the official `claude-desktop` package from Anthropic's apt repository, after checking the signing-key fingerprint.
- Creates `.venv` and writes the config file.
- Generates a private MCP token, VNC password and keyring password (chmod 600, never printed).
- Enables the user services `claude-projects-*`.

Flags: `--display :N`, `--bot-name`, `--operator-name`, `--timezone`, `--no-apt`, `--apt-only`, `--no-http`, `--claude-md`, `--dry-run`, `--uninstall`.
Agents don't call it directly: `claude-projects setup install` / `reconfigure` / `uninstall` wrap it (see [skill Part A](../skill/SKILL.md)).

## Migration from older names

Releases before 1.0.3 used short internal prefixes for the modules, the CLI shim, the services and the environment
variables. 1.0.3 drops all of them: the code is the `claude_projects/` package, the CLI is `claude-projects` (or
`python -m claude_projects`), the services are `claude-projects-*` and the variables are `CLAUDE_PROJECTS_*`.
Running `claude-projects setup install` (or `install.sh`) on an older install migrates it: it stops and removes the
old services, installs the `claude-projects-*` ones, deletes leftover shim files and a `~/.local/bin` symlink that
pointed at the old CLI name, and keeps the config, sign-in and state. Variables exported under the old prefixes are
still read when the `CLAUDE_PROJECTS_*` name is not set. On Windows, fresh distros use the `claude` account and existing ones keep their default user and home; on a Mac an existing VM under the old name keeps being used. `CLAUDE.md` imports `AGENTS.md`.

Process checks: the CLI and its background jobs run as `python -m claude_projects ...`, the server as
`python -m claude_projects.mcp_server`; match them with `pgrep -fa claude_projects`.

## Development

```bash
python3 -m venv .venv --system-site-packages && .venv/bin/pip install -r requirements.txt pytest && .venv/bin/pip install --no-deps -r requirements-nodeps.txt
.venv/bin/python -m pytest -q           # no app, display or network needed
```
