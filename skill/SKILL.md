---
name: claude-projects
description: Set up, verify, maintain and use the claude-projects-mcp plugin (Claude Projects for Grok Bot) - a guided setup flow (host choice, install, noVNC sign-in, Projects-beta check, connect, verify, tidy), then list Projects, read threads, triage status, send or forward messages, handle permission cards, move files. Read before installing or before any claude_* tool call; the POLICY section says what you may do without asking.
---

# Claude Code Projects (claude-projects-mcp)

Two parts. **Part A** sets the plugin up and keeps it healthy; run it when the user asks you to install, re-check,
reconfigure, move or tidy it, when the `claude_*` tools are missing, or when `claude_setup(action="status")` says
anything other than `ready`. **Part B** is how to use it once it works.

# Part A: Setup and maintenance

You drive this yourself. `claude-projects setup status` (or `claude_setup(action="status")` once connected) prints the `state`, the
`next_action` to run, and `ask_user`: the exact question to put to the user, or null. Loop until `state` is `ready`:

1. Run `next_action`.
2. If a result has `ask_user` (or `tell_user`), say exactly that to the user in your own chat, then wait for their answer.
3. Run `claude-projects setup status` again.

Ask the user only where the flow says so. Everything else is yours. Progress is saved on the host
(`~/.local/state/claude-projects-mcp/setup.json`), so if anything stops halfway, `claude-projects setup status` picks up from there.

Rules for the whole setup:
- Never print, paste or upload the MCP token, the viewport password or the keyring password. Say where they are saved
  (a path), never what they are.
- No test messages, threads or Projects. Verification is read-only plus dry runs.
- Ask before anything that changes the user's machine beyond the plugin: sudo commands, sleep settings, uninstalling.
- Shell commands below run on the host, in the checkout: `cd ~/claude-projects-mcp && ./claude-projects setup ...` (add `--json`
  for machine-readable output). On a Mac host run `hosts/macos/claude-projects setup ...`; on Windows, `hosts\windows\claude-projects.cmd setup ...`.

## A1. Choose the host (ask once)

Ask, and recommend:

> "Which computer should run this? It has to stay on all the time, because your Projects are reached through the
> Claude app running on it. I recommend an always-on machine of yours: a Linux server, mini PC or desktop that never
> sleeps is best, and an always-on Mac or Windows PC works too. If you don't have one, I can run it on my own box
> instead, but that's a fallback: it only works while the box is up."

| choice | how it runs | notes |
|---|---|---|
| Always-on Linux (Ubuntu 22.04+ / Debian 12+) | natively, headless display | **recommended** |
| Always-on Mac (macOS 13+) | a small Ubuntu VM (Lima) that starts at login: `bash hosts/macos/setup.sh` | the Mac's own Claude app is not touched; the script hasn't been run on a real Mac yet, so tell the user and check each step |
| Always-on Windows PC | a dedicated WSL2 distro kept running from login: `hosts\windows\setup.ps1` | first run may need an admin PowerShell and a reboot |
| A laptop (any of the above) | same as its OS | warn: works only while it's awake and plugged in |
| Your own (Grok Bot) box | natively, stdio on the same machine, the plugin's own process supervisor (no systemd there) | **fallback**: say clearly that it only works while the box is up |

You need a shell on the chosen computer: a registered machine, SSH, or the box itself. On a Mac or PC you can't reach
yet, ask the user to turn on Remote Login (Mac) or OpenSSH Server (Windows), or to run the one setup script themselves.

### Mac or Windows: the Linux runtime

Why: the plugin drives the Claude app through the **Linux** accessibility tree on its own headless display, so it
always runs the Linux build of the app. Native Linux is the recommended host. On a Mac or PC it runs inside a small
Linux runtime, with its own copy of the app signed in separately; the user's own Claude app and files are never touched.

| | macOS 13+ | Windows 10 22H2 / 11 |
|---|---|---|
| runtime | Lima VM `claude-projects`: Ubuntu 24.04, 2 CPUs, 4 GB, 20 GB, `--mount-none` | WSL2 distro `ClaudeProjects`: Ubuntu 24.04, user `claude` without sudo, systemd on in `/etc/wsl.conf` |
| one-shot script | `bash hosts/macos/setup.sh` | `powershell -ExecutionPolicy Bypass -File hosts\windows\setup.ps1` |
| run claude-projects | `limactl shell claude-projects -- bash -lc 'cd ~/claude-projects-mcp && ./claude-projects setup status'` (or `hosts/macos/claude-projects`) | base64 script via `wsl.exe -d ClaudeProjects -u claude --exec /bin/bash -c ...` (or `hosts\windows\claude-projects.cmd` from cmd) |
| root steps | `sudo` inside the VM (Lima's user has it) | from Windows: `wsl.exe -d ClaudeProjects -u root ...` |
| stays running | LaunchAgent starts the VM at login; linger inside | hidden scheduled task keeps `sleep infinity` running in the distro; optional at-startup task |
| ports | Lima forwards the guest's localhost to the Mac's `127.0.0.1` | WSL `localhostForwarding` to Windows `127.0.0.1` |
| viewer link | `http://127.0.0.1:6179/vnc.html` in the Mac browser | `http://127.0.0.1:6179/vnc.html` in the Windows browser |
| password file | in the VM: `~/.config/claude-projects-mcp/vncpass.txt` | in the distro: `/home/claude/.config/claude-projects-mcp/vncpass.txt` |
| always-on | `pmset -c sleep 0` (ask), auto login | `powercfg /change standby-timeout-ac 0` (ask), auto sign-in or `-AtStartup` |

Prefer the script. When it isn't available or stops halfway, do the same steps by hand: they're spelled out, with
verify commands and common failures, in [references/macos.md](references/macos.md) and
[references/windows.md](references/windows.md) (in the public repo:
`https://github.com/BenItBuhner/Claude-Projects-for-Grok-Bot/blob/main/skill/references/`). After the runtime is up,
the rest is the same guided flow as Linux (A2 to A8), run inside the VM or distro.

Two pitfalls that cost the most time:
- **Quoting across the boundary.** On a Mac, put the command after `bash -lc` in single quotes, or `~`, `$USER` and
  `$@` expand on the Mac. On Windows, never send bash with quotes or `$` through `wsl.exe -- ...` (wsl.exe expands
  `$vars`, PowerShell 5.1 breaks embedded double quotes, PowerShell 7 rewrites a bare `~`): send the script as base64
  through `--exec`, as shown in the Windows reference.
- **The runtime stopping.** A Lima VM doesn't come back after a Mac restart without the LaunchAgent; a WSL distro shuts
  down minutes after its last Windows process exits without the keep-alive task. `claude-projects setup verify` passing once
  doesn't prove either: check after a restart (`limactl list` / `wsl -l -v`).

## A2. Preflight

```bash
git clone https://github.com/BenItBuhner/Claude-Projects-for-Grok-Bot ~/claude-projects-mcp   # if not cloned yet
cd ~/claude-projects-mcp && ./claude-projects setup preflight            # Linux; on Mac/Windows it says which script to run
# a container without systemd (how the Grok Bot box runs) is detected as the box; --host-kind box says so outright
```

Each check is `pass` / `warn` / `fail` with a `fix`. Fix what you can, ask about the rest, re-run. Typical ones: user
systemd and linger (on a host without systemd, such as the box, the plugin runs its own supervisor instead and
`service_mode` says `process`), sudo, about 2 GB free memory and 3 GB disk, a free display (it suggests one), free ports, reaching
downloads.claude.ai, and sleep. `host_class` says which row of the table above this computer is; when it's a laptop or
the box, pass the warning on to the user before you install.

## A3. Install

```bash
./claude-projects setup install --bot-name "<your bot name>" [--operator-name "<user's first name>"] [--timezone Area/City]
```

- If sudo needs a password, the result is `needs_sudo` with `ask_user`: the user pastes **one line**
  (`sudo ~/claude-projects-mcp/install.sh --apt-only`). Then run `./claude-projects setup install --no-apt`.
- Mac and Windows: the host script runs this step inside the VM or distro for you.
- It is idempotent. Re-running keeps the token and the sign-in.
- It runs the preflight first and stops at `preflight_failed` (with the failed checks) instead of installing on a host
  that can't work. `--dry-run` does the same checks.
- On the box it installs stdio only (no HTTP server, so no port to collide with). Elsewhere the HTTP server listens on
  8791; `--mcp-port N` moves it and `--no-http` turns it off.
- Without systemd, nothing restarts the services after the box restarts until the MCP server starts (it brings the
  supervisor up) or you run `./claude-projects setup start`. `status` does it too.
- Installs from 1.0.2 and earlier have their services under older names, so `status` reports them as not installed
  yet. `install` migrates them in place (old services removed, `claude-projects-*` installed, config, sign-in and state
  kept); see "Migration from older names" in `docs/how-it-works.md`.
- An older hand-built install (`claude-projects-mcp.service` with `venv/`) is recognized as installed. `install` leaves
  it alone (`legacy_layout`); migrating needs `--migrate` and the user's OK.

## A4. Sign in (noVNC)

```bash
./claude-projects setup signin start        # turns the viewport on, prints the link, the password-file path and ask_user
./claude-projects setup signin wait         # polls every 30 s for up to 15 min, then waits for the viewer to close
```

Give the user `ask_user` as it is: open the link (on the host itself, the `ssh -L` line from another computer, or
`http://127.0.0.1:6179/vnc.html` on a Mac or PC host), enter the password from the file it names, click
"Get started" (older app versions say "Sign in") in the Claude app, and log in in the browser that opens inside the
same window.

On the box, the user has no shell there and can't reach its localhost. `signin start` first returns
`needs_viewer_route` with an `ask_user`: ask whether you may open a temporary, password-protected link (a Cloudflare
quick tunnel; needs `cloudflared` on the box). After a yes, run `./claude-projects setup signin start --tunnel`; with the user's
own URL, `--public-url <url>`. On the box the link carries a **one-time** viewport password, so send it only to the
user. `signin wait` replaces that password and closes the tunnel when the sign-in is done. If that browser stops on a
`claude://...` link instead of returning to the app, run `bin/claude-open-url 'claude://...'` with that link.
When `wait` sees the sign-in it closes the sign-in browser and turns the viewport off again (on-demand viewport).
If `wait` times out, run it again; `signin start` resends the link. To use a different account later:
`./claude-projects setup signin start --switch`.

## A5. Check for Claude Code Projects (beta)

```bash
./claude-projects setup check-beta
```

Read-only: it looks for the Projects entry in the app's sidebar, opens the Projects view, and reads whether the app
has a project list. Verdicts:

- `has_projects`: go on.
- `no_projects`: tell the user `tell_user` plainly (the account has no Claude Code Projects yet; it's a beta). Then
  **stay idle**: don't uninstall, don't retry on a loop, don't try to get the beta for them. The background refresh is
  switched off. Offer `signin start --switch` if they have another account. Re-check only when they ask.
- `app_outdated`: the app package has an update. Ask before running the apt upgrade line it gives, then check again.
- `unknown`: show the user the screenshot path and ask what they see.

## A6. Connect yourself

`./claude-projects setup connect` lists the options and recommends one:

- **same host** (the box): stdio, `<repo>/.venv/bin/python <repo>/claude_projects/mcp_server.py --stdio`. No token involved.
- **ssh**: the same command over `ssh user@host ...` (Mac: `limactl shell claude-projects -- bash -lc '...'`; Windows: `wsl.exe -d ClaudeProjects -u claude --exec ...`; `claude-projects setup connect` prints the exact line).
- **tunnel**: the user's own tunnel or VPN to `127.0.0.1:8791`, `allowed_hosts` in config, a private copy of the
  token file, and `bin/grok-bot-bridge <url> <token-file>`. Nothing here sets up a tunnel for them.

Add the connector, then call `claude_setup(action="status")` through it. That proves the connection works and records it.
If your tool discovery shows fewer tools than the server has, call them by name anyway (see "Stale tool list" in Part B).

## A7. Verify

`claude_setup(action="verify", live=true)` (or `./claude-projects setup verify --live`). It checks that:
- the services are running and start at boot, with linger on and no sleep expected (process mode: the supervisor runs);
- `/healthz` answers (when the HTTP server is on) and the secret files are chmod 600;
- `viewer_ports` is set;
- 25 MCP tools load, and the skill's sha256 matches;
- the app is reachable and signed in, with no dialogs, no viewer and a free lock;
- Projects access is there and the project list loads;
- one Project's primary thread reads;
- that Project has a cloud environment (cloud threads need one);
- a send dry run produces the `[sent by Grok Bot]` header. Nothing is typed or sent.

Show the user what passed, and anything that warned or failed with its fix. A user with no Projects yet ends at
"ready, nothing to read". Offer to create a Project only if they ask.

## A8. Ready

Tell the user it's set up, which computer it runs on, and how they can come back to it (just ask you to re-check). If
they have a **real** first task for a Project, sending it is the final proof (and of cloud threads, when that Project
uses a cloud environment). Never invent one.

## Later: re-check, reconfigure, tidy

| need | run |
|---|---|
| Where things stand | `claude_setup(action="status")` / `claude-projects setup status` |
| Health check | `claude_setup(action="verify", live=true)` / `claude-projects setup verify --live` |
| Did the account get Projects yet? | `claude_setup(action="check_beta")` / `claude-projects setup check-beta` |
| Clean up leftovers | `claude_setup(action="tidy")` (dry run), then `apply=true` / `claude-projects setup tidy [--apply]` |
| Rename the bot, change the time zone, display or screen size | `claude-projects setup reconfigure --bot-name ... --timezone ...` |
| Sign in again or switch account | `claude-projects setup signin start [--switch]`, then `claude-projects setup signin wait` |
| Open or close the viewport | `claude_setup(action="viewport_on")` or `"viewport_off"` / `claude-projects setup viewport on` or `off` |
| Move to another computer | `claude-projects setup move` (prints the steps) |
| Remove it | `claude-projects setup uninstall` (keeps config and sign-in); `--purge` after the user confirms |

`tidy` only touches the plugin's own leftovers: the staging sweep, a stale lock record, old cache copies, stray file
pickers, the sign-in browser, and a viewport left on. `reconfigure` restarts only the services whose settings changed.
It never restarts the Claude app unless the user agrees and you pass `--restart-app`. Neither ever restarts a Claude
Code session.

# Part B: Using it

This plugin drives the real Claude desktop app on a dedicated Linux host (its own headless X display): deep links to
open Projects and threads, the accessibility tree to read them, X keystrokes to type. It never reads tokens or
cookies. One UI action runs at a time; callers queue on a shared lock.

**Read the POLICY section below before you act.** It is the user's, it overrides your defaults, and it exists so you
do not have to ask "is it OK to send?" for things the user already allowed. Get the live copy with `claude_skill_get`
(skill + effective policy) or `claude_policy_get` (policy only); the user may have changed it since this file was
written. If the policy and this skill disagree, the policy wins.

## Ids and addresses (stable)

| thing | id | where you get it |
|---|---|---|
| Project | `chan_...` | `claude_projects_list` |
| Primary thread (the Project's orchestrator chat) | `cse_...` | `claude_projects_list` (`primary_thread_id`) |
| Child thread (delegated / named) | `cmsg_...` | `claude_project_get` (`threads[].thread_id`) |
| Message | `index` within a thread | `claude_thread_read` (`messages[].index`) |
| Attachment | `<message index>.<n>` | `claude_thread_read` (`messages[].attachments[].id`) |

Every read and status result carries `address` = `<chan_>/<cse_|cmsg_>`. Use it to refer to a thread in your notes,
in hand-offs to other agents, and as the source of a forward. `project` accepts a name (fuzzy), `chan_` or `cse_`;
`thread` accepts omitted/`primary`, a `cmsg_` id, or an exact title. A child that cannot be found is a `NotFound`
error; the tools never fall back to the primary silently.

## Default to the primary thread

- New work goes to the Project's **primary thread** (omit `thread`). It delegates to children itself.
- Create a child thread (`claude_thread_create`) only when the user asked for a new or named thread. Never make a new
  thread a precondition for anything (new env vars apply to new sessions, including the primary).
- Continue an existing child only when that child is already the workstream or the user named it; pass its `cmsg_`.

## Check every thread, not just the primary

Sending defaults to the primary; **watching does not**. Claude asks for things in child threads too, and a child that
needs the user sits in the sidebar's **Waiting on you** group while the primary looks idle. So:

1. Check all threads of a Project, never only the primary. `claude_attention()` lists, across every Project, each thread
   (primary and child) in *Waiting on you* with its latest ask; `claude_thread_status` and `claude_project_get` return the
   same list for one Project as `waiting_threads`. These are cached reads (no UI); `claude_attention(refresh=true)`
   live-reads the waiting threads whose cached ask is stale (`ask_stale`), a few at a time, within about 45 s.
2. Surface every Waiting-on-you child thread to the user, and read its latest ask first (`latest_ask`, or
   `claude_thread_read(project, thread=<cmsg_>)` when `ask_stale` is true).
3. `ask_kind` says what Claude wants: `approval` (a line in the user's own words, an approval line, an exact reply),
   `permission` (a card or a permission stall, see below), `question`, or `action` (something for the user to do).
4. **Approval asks.** Relay `requested_line` to the user **exactly as Claude wrote it**, and say which Project and thread
   asked. Post it in the thread on the user's behalf only when the user has already given an explicit standing approval
   that covers that exact action; otherwise ask the user and wait. When `requested_line` is null, Claude described the line
   without giving it: ask the user for their words. **Never invent, complete or reword the user's words**, and never present a
   line you wrote as theirs.

## Tools

| tool | what it does | UI? |
|---|---|---|
| `claude_projects_list(refresh=false)` | Projects: name, `chan_`, primary `cse_`, last activity, unread | only with refresh |
| `claude_project_get(project)` | primary transcript + child threads with per-thread state | yes (refresh=false: cache) |
| `claude_thread_read(project, thread)` | transcript, `current_state`, `permission_cards`, `machine`, `address` | yes (refresh=false: cache) |
| `claude_thread_status(project, thread, refresh=false)` | compact triage from cache, plus `ask` and `waiting_threads` (every thread waiting on the user); refresh=true for live | only with refresh |
| `claude_attention(projects, refresh=false, max_reads=2)` | every thread in *Waiting on you* across Projects (child threads too) with its latest ask, `approval_asks` with the exact `requested_line`, and `permission_cards` | only with refresh |
| `claude_thread_wait(project, thread)` | poll until Claude replies (prefer polling `claude_thread_read`) | yes |
| `claude_thread_send(project, message, thread, dry_run, attachments, attach_test, idempotency_key, job_id, clear_leftovers)` | type + Send + verify; answers within ~45 s (`pending` + `job_id` while it still runs: call again, it never sends twice; a repeat within 30 min says `joined_existing_send`, a new `idempotency_key` sends again on purpose). Refuses when the box holds attachment chips it didn't add (`composer_chips`); `clear_leftovers=true` removes them first. `attach_test="open"` (dry run) attaches each file and removes it again, never Send | yes |
| `claude_thread_forward(source_project, target_project, ..., dry_run=true)` | relay one message verbatim (+ files) | yes |
| `claude_thread_permissions(project, thread)` | READ-ONLY list of permission cards | yes |
| `claude_thread_permission(project, option, thread, card_id, dry_run=true, only_apps, job_id)` | press a card's button by label; answers within ~45 s (`pending` + `job_id` while queued); real click + verification | yes |
| `claude_thread_attachment_get(project, thread, attachment_id, as)` | fetch an image / file / output into staging | yes |
| `claude_thread_create(project, title, instructions, dry_run)` | ask the primary to start a child thread | yes |
| `claude_staging(action='info'|'sweep', dry_run=true)` | the managed file directory: paths, sizes, cleanup | no |
| `claude_skill_get()` / `claude_policy_get()` / `claude_policy_propose(change, rationale)` | this skill, the policy, proposals | no |
| `claude_refresh(projects)` | refresh the list and re-read changed Projects | yes |
| `claude_project_create`, `claude_project_pin`, `claude_project_env`, `claude_project_set_env`, `claude_project_add_repo`, `claude_project_remove_repo` | Project admin (visible changes) | yes |
| `claude_app_status()` | page, dialogs, viewers, lock holder, idle | no |
| `claude_setup(action, live, apply)` | setup state, verify, Projects-beta check, tidy, viewport (Part A) | only verify/check_beta/tidy |

**Stale tool list.** Some MCP clients keep the tool list they saw when the connector was first added; Grok Bot's tool
discovery (`GetDynamicTools`) can still show an older, shorter tool list with old descriptions even after the server
was updated and the connector re-added. The server serves all 25 tools regardless: call any of them by name (in Grok Bot,
`CallDynamicTool` on the connector's namespace) with the arguments in the table above. Tools that older connector lists often miss:
`claude_thread_permissions`, `claude_thread_permission`, `claude_thread_forward`, `claude_staging`, `claude_skill_get`,
`claude_policy_get`, `claude_policy_propose`, `claude_project_add_repo` and `claude_project_remove_repo` (1.0.0), and
`claude_setup` (1.0.2), and `claude_attention` (1.0.3). A missing tool in
discovery is not a missing tool on the server; `claude_skill_get` returning this file proves the connection is current.

## Reading and reporting

1. Report `current_state` and the **latest** message (`is_latest=true`) with its local time and `age_hours`. Older
   messages marked `historical` / `superseded` are history, not current plans.
2. `current_state.status`: `working` | `waiting_on_user` | `paused` | `done` | `idle`.
   `waiting_on`: `claude` | `user` | `permission` | `background_watcher` | null.
   With `legacy_field_aliases` on, results also carry the pre-1.0 names next to the new ones: `status_legacy`
   (`waiting_on_<operator first name>` instead of `waiting_on_user`), `waiting_on_legacy` (`<operator first name>` for
   `user` and `permission`), and `*_ct` / `path_on_home_lab` copies of `*_local` / `path_on_host`. New code should read
   the new fields; the legacy ones exist so older prompts and scripts keep working.
3. A user message with `sent_by_bot: true` was typed by this plugin for an agent, not by the user. Its `text` starts with
   the origin bracket; its `summary` (and `current_state.summary`) is the request without the bracket. Do not quote a
   bot-sent message as something the user said.
4. Where a thread runs = `machine.resolved`. If it is unknown, say "unknown (not shown in the app)". Never guess.
5. Every read carries `freshness` (`source`, `read_at`, `age_s`, `stale`); a `stale_warning` means messages after `read_at` are missing. Refresh before treating anything older than ~15 minutes as fact.
6. A send is posted only when `sent` is true and `verified_in_transcript` is true. `unverified`, `not_posted` or `failed` means read the thread before doing anything; never resend blind.
7. When `sidebar_disagrees` is true, report the live status, not the sidebar label.
8. `current_state.ask` (and `ask` on status results) classifies the latest reply; an `approval` ask carries `requested_line`.
   Report waiting child threads (`waiting_threads`) along with the primary; see "Check every thread" above.

## Auto permission mode and permission cards

Projects always run in **Auto** permission mode and that cannot be changed from here. Threads still stop and wait:

- on a **permission card**: the app shows a prompt with buttons, e.g. `Claude would like to add this folder to the
  session: <path>` with `Add folder` / `Not now`; adding a repository; allowing a machine; running a command.
  Reads report these in `permission_cards` (`card_id`, `type`, `target`, `prompt`, `options`) and set
  `current_state.waiting_on = "permission"`, `permission_pending = true`;
- or **in prose**: Claude ends its turn asking for an OK ("should I proceed?", "I need your approval to ..."). Reads
  set `waiting_on = "permission"` and `permission_hint` to the phrase they matched.

You are the overseer. Unblock what is clearly inside the task the user gave; surface only what is questionable:

1. Read the card (`claude_thread_permissions` or the read result). Match it against the POLICY and the user's task.
2. In scope and pre-approved: press the affirmative option with `claude_thread_permission(option=..., dry_run=false)`
   (run it with `dry_run=true` first if you are unsure which card or button it will hit). For prose stalls, reply in
   the thread with the go-ahead (that reply is a send; the policy decides whether you may).
3. Questionable (outside the task, touches secrets, destructive or irreversible, a machine / repo / folder the user
   never mentioned, spending money, contacting people): do not press anything. Tell the user exactly what the card asks
   (type, target, options) and wait for their answer. Pressing `Not now` / deny also needs a reason from the policy or
   the user, because it can derail the thread.
4. Record repeat decisions as a proposal (`claude_policy_propose`) so the user can pre-approve them next time.

### Computer use (screen control)

Claude reaches a computer's screen through the Claude desktop app **on that computer** (Settings -> Computer use). Two
cards can show in the thread:

- **The enable step** (`computer_use_kind: "probe"`): "Claude wants to use your computer", no apps, "Check that computer
  use is turned on in the Claude desktop app on `<device>`" [Deny] [Continue]. `Continue` tells Claude the user says it
  is on there; `Deny` makes Claude carry on without the screen for that turn. Press `Continue` only when the user said
  computer use is on (or the policy pre-approves it).
- **The apps card** (`computer_use_kind: "apps"`): "Claude wants to use: Sketchpad All ... Notepad All" [Deny] [Allow for
  this session]. The plugin presses it with a real pointer click at the button's centre, then verifies. AT-SPI
  `do_action(0)` can only focus this button, not click it: the card stays pending and can cause new requests to decline
  instantly. Do not assume an Allow needs to be pressed on the controlled computer: a real click from the plugin host
  has been proven to grant laptop screen access. The laptop UI Automation path is also kept (`hosts/windows/computer-permission.ps1`
  snapshot -> `claude-projects computer-card decide --only-apps ...` -> press request; dry run first).
- Under a standing approval for named apps, pass them as `only_apps` (e.g. `["Sketchpad", "Notepad"]`): an Allow on a
  card that requests anything else is refused (`apps_not_allowed`). Count a press as done only when `card_gone` is true
  and the thread goes on with the screen.
- **Why a thread says "turn on computer use" when it is on:** the app writes Claude's note about computer use from the
  desktop the message was *typed on*. Messages sent through this plugin are typed on its host, where computer use is
  usually off, so Claude reads "Computer use is available on device X but not turned on yet" on every such turn, even
  though it is on where it matters. Tell the user; don't conclude that it is off. The fix is a message in the thread
  (sent only with the user's OK) saying computer use is on in the Claude app on that device and asking Claude to call
  `computer_resolve_access` for it, then `computer_request_access` with the apps it needs.
- `claude_thread_permission` always answers within about 45 s: while the app is busy it returns `pending: true`, a
  `job_id`, `queue_position` and `eta_s`; call it again with `job_id`. Each job's timeline is in `diagnostics_file`.

## Sending and forwarding

- Every typed message starts with the origin bracket `[sent by Grok Bot]` (or `[sent by Grok Bot · <bot name>]`), added
  by the tool. Never type it yourself, never strip it.
- `dry_run=true` first when you are unsure about the target; `dry_run=true, attach_test=true` checks attachments by
  opening the picker, typing each path, and cancelling (nothing uploaded, nothing sent). `attach_test="open"` goes one
  step further: it attaches each file and removes it again, and still never types or sends.
- Prefer full Unicode; spell names as they are written.
- **Forward** (`claude_thread_forward`): relays one message verbatim. The target sees the bracket, then
  `Forwarded from Project "<name>" > thread "<title>" (<chan_>/<cse_|cmsg_>), message #N by Claude, <time>:`, a rule,
  then the content. `include_attachments=true` fetches the message's images / files into staging and attaches them.
  `dry_run` defaults to true; a real forward is a send.
- The tool refuses when the composer holds an unsent draft (never overwrite the user's typing) or a VNC viewer is
  connected (a person is watching; retry later, `force=true` only if the user said so).

## Files (one managed staging directory)

All files that move into or out of threads live under the plugin's staging root (`claude_staging(action='info')`
prints the absolute paths):

- `incoming/` - attachments fetched from threads (`claude_thread_attachment_get`, forwards). Results give `path_on_host`.
- `outgoing/<stamp>-<id>/<filename>` - files staged for a send: base64 uploads and forwarded files. One unique folder per
  send, so same-named files never collide and the app still shows the original name.
- `attach-test/` - OCR crops from attach dry runs. `msg/` - temporary message files.

Rules: attach files that already exist on the app host by `{"path": ...}`; send anything else as
`{"base64": ..., "filename": ...}` and let the plugin stage it. Do not copy files around the host yourself, do not
leave copies in home or tmp directories, and quote the absolute paths the tools return. Old entries are swept
automatically (TTL per folder plus a size cap); `claude_staging(action='sweep', dry_run=false)` cleans up on demand.

## Etiquette (always, regardless of policy)

- No test messages, test threads or test Projects. Use `dry_run`.
- Never press Pause, Restart Claude, Archive, "Import Claude Code CLI sessions", re-verify, or "Get more usage".
- Never force past a connected VNC viewer and never overwrite an unsent draft.
- Never restart or kill the Claude desktop app or any Claude Code session. Code changes to this plugin load by restarting
  only its own MCP service, and only when the user allows it.
- Do not change a thread's model or a Project's settings unless the user asked for that exact change.
- `Busy` (code 3) means another agent holds the UI lock: retry (the server already retries with backoff).
- Secrets go in by file path on the app host (chmod 600), never as tool arguments or chat text.

## POLICY (user-editable)

This section is the default. The user owns the effective policy: if `<config dir>/policy.md` exists
(`claude_policy_get` shows the path), it replaces everything between the markers below. Agents may suggest changes
with `claude_policy_propose`; only the user applies them.

<!-- POLICY:BEGIN -->
### Pre-approved (act without asking)
- Reads of any kind: list, get, read, status, wait, attachment fetch, permission listing, staging info.
- Dry runs of any tool (`dry_run=true`, `attach_test=true`).
- Sending to a Project's primary thread when the user asked for that work or dictated the message, including follow-ups needed to finish the same task.
- Replying "go ahead" to a thread that stalled asking for an OK on a step that is clearly part of the task the user gave.
- Permission cards whose target is clearly part of the task: adding a folder or repository the user named or that belongs to the Project's own repo, running the commands the task needs.
- Forwarding a message between threads when the user asked for the hand-off.
- Staging cleanup (`claude_staging` sweep).
- Setup upkeep: `claude_setup` status, verify (including `live=true`), check_beta and tidy (also with `apply=true`).

### Surface to the user first
- Anything outside the task the user gave, or a target (folder, repo, machine, domain) the user never mentioned.
- Permission cards for allowing a new machine, network access to unfamiliar hosts, or anything touching credentials or secrets.
- Destructive or irreversible actions: deleting data, force-pushing, publishing, releasing, spending money, contacting people.
- Creating a child thread or a Project, changing Project settings, environments, repos or pins.
- Changing the plugin's host setup: reconfigure, sign-in switch, viewport on, sudo commands, sleep settings, uninstall.
- Denying a card (`Not now`, `Deny`) when the user did not say to.
- Posting a line Claude asked for in the user's own words (an approval ask), unless the user gave an explicit standing approval covering that exact action: relay the exact `requested_line` and ask.

### Never
- Test messages, test threads or test Projects.
- Pause, Restart Claude, Archive, Import CLI sessions, re-verify, Get more usage.
- Forcing past a connected VNC viewer or overwriting an unsent draft.
- Changing a thread's model.
- Inventing, completing or rewording the user's words, or presenting a line you wrote as theirs.
<!-- POLICY:END -->
