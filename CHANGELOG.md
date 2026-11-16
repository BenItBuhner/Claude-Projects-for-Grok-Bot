# Changelog

## 1.0.6 (2026-11-15)

### Reads and sends stop walking message bodies
- Every accessibility walk now skips what it doesn't need. Finding messages, the last message number, settling a
  page, the Stop check, and the attachment finder never walk into a message body. Page lookups (thread title,
  "Running on", dialogs, Send/Add buttons, the message box) never walk into the transcripts. Before, each of these
  walked the whole transcript at 13-20 ms per node.
- One page snapshot (outside the transcripts) is read once per read and shared by pane info, dialogs, the Stop check
  and the permission-card scan. Before, each walked the page on its own.
- The transcript panel and the message box are remembered while they are alive on the same route.
- Reads parse only the messages they return. Finished messages are reused from the last read (everything before that
  read's latest message, unless the app cut it short). Expand, images and markdown always parse fresh.
- Permission cards on reads are looked for outside the transcript and in its last 2 messages. Card presses and the
  card watcher keep the full scan.
- Sends get the last message number without parsing anything. Verify and the post check parse only the messages after
  it.
- Measured live on a long transcript: a live read went from 120-145 s of UI time to 22 s (settle 4.5, messages 3.2,
  page snapshot 3.4, busy 2.7, cards 4.2). The last-index probe takes 2.5 s (it used to take 38.6 s).

### Bulk work steps aside faster
- The bulk cap is 10 s (was 20 s; `CLAUDE_PROJECTS_BULK_CAP_S`). It no longer disarms after one interrupt. If a
  broad `except` swallowed it, or one slow accessibility call (4 s per-call timeout) delayed it, it fires again a
  second later until the bulk op lets go.
- Settle polls every 0.25 s and needs 0.6 s of stability (it was 0.4 and 0.8 s). The polls are cheap now.

### Shared and measured reads
- `claude_project_get` reads coalesce too: a waiting caller answers from the shared pass when it left the threads in
  the cache.
- Every read answer logs `timing readsrc <live|coalesced|cache|cache_busy> ...: parsed N, reused N, total S`. Live
  reads log their steps (`lock`, `settle`, `messages`, `ui_snapshot`, `busy`, `cards`), so cache hits and live reads
  can be measured apart.

### Follow-up after the first live samples
- Identical reads from different bots share one UI pass even when they start at the same moment (a per-read
  single-flight file lock keyed by project, thread and read options). Joiners get the same answer, stamped
  `coalesced`, or the same error. Reads with different options never share.
- The thread list is found and parsed without walking the transcripts (it took 32.5 s on a long Project).
- The Stop check after the last message is one pruned pass (it took 14.3 s). The read's card scan reuses that pass
  rather than walking the transcript again (cards took 16.5 s on a thread read).
- A background read gives way to a waiting send or read as soon as its messages are in, rather than finishing threads
  and cards first. It does not give way to other bulk work.
- A preempted bulk op skips its trip back to the home page.
- An attachment fetch that keeps getting preempted gets one last try with a 60 s cap
  (`CLAUDE_PROJECTS_ATTACH_FINAL_CAP_S`). If it is preempted again it fails with `Busy` and can be retried later.
  Before, it could hold the app for minutes.

## 1.0.5 (2026-11-15)

### Sends: verified, or loudly not
- A send result reports `sent: true` only after the post is found in the transcript: role user, an index after the
  last one seen before Send, and matching text. Matching ignores whitespace the app adds on re-render (`(8:32` shows as
  `( 8:32`), which had made a posted message look unverified.
- When the post can't be confirmed, the result says `unverified: true` (or the job ends `not_posted`) with
  `posted: false`, a warning and the service-limit check result. The answer no longer says `sent: true` on its own.
- Send jobs that fail before Send are retried with backoff (10 s, then 30 s; tripled while a service limit shows),
  at most twice within 10 minutes. Refusals are not retried. Before any retry, and after an unverified Send, the job
  reads the thread for an existing post (rechecks at 10, 30 and 60 s), so a message is never posted twice.

### Root cause of the slow and lost sends (measured from 7 PM CT)
- Whole-document accessibility walks run at about 13-20 ms per node on a long transcript. The send path ran them
  dozens of times: once on every paste-verify poll (finding the message box), plus again for the Send button and the
  attach control. AT-SPI text insertion failed in all 38 sends and cost over a minute on a 10k-character message
  before every send fell back to clipboard paste. A 10k-character send spent 64 s between paste and Send.
- The message box is now remembered while it is alive on the same route, and searched from the end of the page
  otherwise. The Send button and the attach control are looked up next to the composer first. Paste is the first
  typing method (`CLAUDE_PROJECTS_ATSPI_INSERT=1` restores AT-SPI insertion). Timing marks now name each step
  (`before_index`, `composer`, `typed`, `pre_send_checks`, `send`, `verify`).
- Attachment sends failed with "typing the path took longer than 20 s". The time was really spent finding the attach
  control (25-94 s), and that was charged to the first file's 20 s clock. Finding the control now happens once per
  send, before the per-file clock starts, inside the composer's scope first. The error names its parts (picker s, path s).

### Fairness: nobody waits minutes behind bulk work
- Bulk hard cap: an attachment fetch, refresh or attention pass that holds the UI more than 20 s
  (`CLAUDE_PROJECTS_BULK_CAP_S`) while anyone waits is stopped and re-queued behind the waiter. After 3 preemptions,
  one last run goes to the end. Sends, reads, presses and waits are never preempted.
- "Not available" attachment results are cached for 6 h, so bots stop re-running 2-5 minute fetches for a file with no
  Download button. `refresh=true` checks again.
- The card watcher yields: while others wait it looks at most once per max(15 s, 2x its last look).
- Status shows `queue_depth`, `queue_oldest_wait_s`, `queue_eta_last_s` and `service_limit`.

### Reads: fresh, stamped and shared
- Every read carries `freshness` (`source`: live, coalesced, cache or cache_busy; `read_at`, `age_s`, `stale`). A
  cached answer older than 5 minutes (`CLAUDE_PROJECTS_READ_STALE_S`) has `stale: true` and a loud `stale_warning`.
- Duplicate reads are coalesced. A read of a thread that another bot is already reading (or is queued to read) waits
  for that pass and answers from the transcript it wrote, instead of queueing a second UI pass.
- The app keeps no transcripts in its IndexedDB (only lists, account and settings), so reads still come from the UI or
  the cache. No lock-free source for transcripts exists in the app's stored data.

### Service limits
- Limit and overload banners (rate limit, usage limit, 429, at capacity) are detected in alerts, dialogs and the
  composer area, never the transcript. They are recorded in `service-limit.json`, shown in status and lengthen send
  backoff. None were seen in the logs from this session.

## 1.0.4 (2026-11-15)

### Reliable computer-use presses and bounded jobs
- Root cause confirmed live: AT-SPI `do_action(0)` only focused "Allow for this session"; it did not click it. The
  pending card remained and later requests declined immediately. A real xdotool click from the home lab granted laptop
  screen access and cleared the card in 1 s at 5:56:15 PM CT. Cards now use a real click at the button centre, with
  `do_action` only as a fallback and a verification after either path. Intro Continue uses a real click too.
- Computer-use cards distinguish enable probes from apps lists. `only_apps` refuses cards requesting other apps;
  lasting grants, full-screen control and disabled buttons are refused. The laptop UI Automation path is retained.
- Permission jobs answer within about 40 s with a result or pending + job id, queue position and ETA. Each job saves
  diagnostics; repeating a call joins the same job rather than pressing twice.
- `card-watch` watches one thread for allowlisted apps cards and logs every decision/press. It takes a short urgent lock
  for each look, freeing the UI between checks.

### Latency and the UI queue
- Urgent (permission presses, card watcher), normal (reads/sends), and bulk (attachments/refreshes) lanes; FIFO in each.
  Bulk work steps aside for urgent work at checkpoints. Status exposes queue position and ETA.
- MCP reads answer cache-first while the app is busy; uncached reads queue at most about 35 s. Already-open targets
  skip the deep link. AT-SPI calls and tree walks are bounded; post-press verification watches the button, not repeated
  40000-node walks.

### The last old name
- The error class is `ClaudeProjectsError` (was a short-prefixed name, plus an alias for an even older one, both
  removed). Error results report `"error": "ClaudeProjectsError"`; the subclasses `Busy`, `NotFound`, `Refused` and
  `AppError` are unchanged.
- `tests/test_layout.py` now flags the old prefixes in any spelling or case, inside identifiers too, outside the
  migration code. Everything else in 1.0.3 (one package, 25 tools, threads waiting on you) is unchanged.

## 1.0.3 (2026-11-15)

### One package, plain names
- The code is one Python package, `claude_projects/` (`core`, `config`, `mcp_server`, `permissions`, `policy`,
  `staging`, `setup`, `origin`, `proc`, `idb`, `attention`, plus `__main__`). Nothing in the tree carries an internal short prefix
  any more, and the compatibility shims of 1.0.2 (the old CLI name, the old module files and host wrappers) are gone.
- One entry point: `claude-projects` (the checkout's wrapper, or `python -m claude_projects`; `pyproject.toml` also
  declares it as a console script). The MCP server is `python -m claude_projects.mcp_server` (`--stdio` for stdio);
  the CLI's background jobs run the same way, so `pgrep -fa claude_projects` finds everything the plugin runs.
- The services are `claude-projects-xvfb`, `-wm`, `-keyring`, `-desktop`, `-vnc`, `-novnc`, `-mcp`, and
  `-refresh` (+ `.timer`); the environment variables are `CLAUDE_PROJECTS_*`; the Windows distro's plugin user is
  `claude`.

### Threads waiting on you, in every thread
- Claude asks for things in child threads, not only in a Project's primary thread. `claude_thread_status` and
  `claude_project_get` now return `waiting_threads`: every thread in the sidebar's *Waiting on you* group, child threads
  included, each with its latest ask from its own cached read (`ask_stale` when that read predates the sidebar state).
- New tool `claude_attention` (25 tools): one pass across Projects listing every waiting thread with its latest ask,
  `approval_asks`, and the permission cards seen in any thread. Cached by default (no UI); `refresh=true` live-reads a
  few stale waiting threads, one short UI lock each, skipped while another caller holds the lock, within about 45 s.
  The background refresh does the same for the Projects it re-reads. CLI: `claude-projects attention [--refresh]`.
- Approval asks: when Claude asks for a line in the user's own words, an approval line or an exact reply, the ask is
  classified `approval` and `requested_line` is that line exactly as Claude wrote it. `current_state.ask` carries the
  classification (`approval` | `permission` | `question` | `action`), and an approval ask sets `waiting_on = "user"`
  rather than `permission`.
- The skill and the default policy: check every thread, surface every *Waiting on you* child thread with its latest
  ask, relay an approval ask's exact line to the user, post it on their behalf only under an explicit standing approval
  covering that exact action, and never invent the user's words.

### Migration
- `claude-projects setup install` / `install.sh` on an older install stops and removes the services of 1.0.2 and
  earlier, installs the `claude-projects-*` ones, deletes leftover shim files and a `~/.local/bin` link to the old CLI
  name, and keeps config, sign-in and state. The hand-built layout (`claude-projects-mcp.service` + `venv/`) is still
  recognized; point its `ExecStart` at `venv/bin/python -m claude_projects.mcp_server` with
  `Environment=PYTHONPATH=<checkout>` (see docs/deploy.md).
- Variables exported under the old short prefixes are still read when the `CLAUDE_PROJECTS_*` name is not set.
- `hosts/windows/setup.ps1` uses `claude` for fresh distros and keeps an existing distro's default user and home;
  on a Mac an existing VM under its old name keeps being used.

### Fixes
- Computer-use permission cards ("Claude wants to use: …", "Allow for this session" / "Allow once"): detected as type
  `computer_use` with the apps (`displayName`, `bundleId`, `tier`) parsed; an Allow presses the app's "Background
  computer use" Continue when it appears, then checks that the card is gone and reports diagnostics when it is not.

### Known issue
- A computer-use card is bound to the device that asked for it: the Claude app on another machine can only send Deny,
  so "Allow for this session" pressed from the plugin's host does not land when the request targets another computer
  (for example a laptop). Allow it in the Claude app on that computer. A fix that answers within about 45 s and saves
  diagnostics is planned for 1.0.4.

## 1.0.2 (2026-11-15)

Brings the guided setup to the public repo: one copy-paste prompt, an agent-driven
`claude-projects setup` flow, Mac and Windows hosts, the 24th tool `claude_setup`, and hosts without systemd.

### Attachment reliability
- Files the app opens in its Overview document viewer are saved as original bytes through its Download file button;
  the HTTP cache by file name and a crop or rendered text are fallbacks, and `source` says which was used.
- `claude_thread_attachment_get` and `claude_thread_send` answer within about 45 s; a call still running returns
  `pending` with a `job_id`, and calling again joins that job instead of fetching or sending twice. A cached file is
  served only while the transcript still shows the same attachment at that message.
- File names no longer carry the tile's type badge. A failed send removes the chips it attached and clears its text,
  each file has its own attach timeout, nothing that comments or posts is pressed, and `status` lists running jobs.

### Leftover chips, timing and the busy check
- A send never takes along attachment chips it didn't add. `composer_chips` lists the chips already in the message box
  (duplicates included), and `composer_empty` is false while any is there. A real send refuses and names them;
  `clear_leftovers=true` (`--clear-leftovers`) removes them first with their own Remove buttons. Right before Send it
  looks again and stops without sending if an extra chip shows up.
- A message whose attachment chips come before the `[sent by Grok Bot]` line in the transcript now counts as
  `sent_by_bot`.
- Opening a project or thread is faster, and every fetch and send logs and returns `timing` per step.
- `attach_test="open"` (`--attach-test=open`) is a dry run that presses Open per file, waits for the chip and removes it
  again. It never types the message or presses Send.
- `claude-projects jobs` exits 3 while the UI lock is held or a background job runs and 0 when idle. See
  [Updating a running install](docs/deploy.md).
- A repeat send that joins an earlier one says `joined_existing_send`; a new `idempotency_key` sends again on purpose.

### New names (the old ones keep working)
- The CLI is `claude-projects` (was `ccp`), the modules are `cpg_*.py` (were `ccp_*.py`), install.sh's services are
  `cpg-*` (were `ccp-*`), and the environment variables are `CPG_*` (were `CCP_*`). On a Mac the VM is
  `claude-projects` (was `ccp`), the wrappers are `hosts/macos/claude-projects` and `hosts\windows\claude-projects.cmd`,
  and the Windows distro's user is `cpg`.
- Shims keep every old name working: `ccp` runs `claude-projects`; each `ccp_*.py` is the `cpg_*` module when imported
  and runs it when started as a script (so `python ccp_mcp.py` and `ccp refresh` in existing units still work);
  `hosts/macos/ccp` and `hosts\windows\ccp.cmd` run the new wrappers; every `CCP_*` variable is read when its `CPG_*`
  name isn't set; a Mac VM called `ccp` is used when there is no `claude-projects` VM.
- Installs from before the rename keep running with their `ccp-*` services; `setup status` and `verify` recognize them,
  and the next `install.sh` run stops and removes them and starts the same services as `cpg-*`. The older hand-built
  layout (`claude-projects-mcp.service` and `venv/`) is recognized as before.
- `AGENTS.md` replaces `CLAUDE.md` as the origin-bracket note (same content); `CLAUDE.md` now only imports it
  (`@AGENTS.md`) for Claude Code. `install.sh --claude-md` copies `AGENTS.md`.

### Guided setup
- New `claude-projects setup` flow that an agent drives end to end. `status` prints the state, the next action, and `ask_user` (the
  exact question for the user, or null). The other commands are `preflight`, `choose`, `install`, `signin start|wait`,
  `check-beta`, `connect`, `verify [--live]`, `viewport on|off`, `tidy [--apply]`, `reconfigure`, `mark`, `move` and
  `uninstall [--purge]`. Progress is kept in `<state_dir>/setup.json` (no secrets), so an interrupted setup resumes.
- Projects-beta detection (`claude-projects probe-projects`, read-only): the sidebar entry, the Projects route, and the app's cached
  project-list query, plus the app's apt version, so an outdated app isn't mistaken for a missing beta. When the
  account has no Projects, setup flags it plainly and stays idle, with the background refresh off.
- noVNC sign-in as a step: `signin start` turns the viewport on and gives the user the link and the password-file path
  (never the password). `signin wait` waits for the sign-in and for the tab to close, closes the sign-in browser, and
  turns the viewport off again (`viewport_mode: on-demand`, the default for new installs; older installs keep `always`).
- New MCP tool `claude_setup` (24 tools): status, verify, check_beta, tidy, connect, viewport on/off.
- Hosts: always-on Linux (recommended), always-on Mac through a Lima VM (`hosts/macos/setup.sh`), always-on Windows PC
  through a dedicated WSL2 distro (`hosts/windows/setup.ps1`), laptops with a warning, and the agent's own box as a
  fallback with a warning.
- `install.sh --apt-only`: when sudo needs a password, the user pastes one line and the agent does the rest.
  `install.sh` also records `screen` and `viewport_bind` so `reconfigure` keeps them.
- Skill: new Part A (setup and maintenance) in front of the usage guide. The default policy pre-approves setup
  upkeep (status, verify, check_beta, tidy) and surfaces host changes.
- README rebuilt around one copy-paste prompt. The manual steps moved to `docs/manual-install.md`, and hosts are
  covered in `docs/hosts.md`.
- README: the download-only "Watch the demo" link and the 2.4 MB video it pointed at
  (`docs/assets/plugin-launch-v7-1080p.mp4`) are removed. A placeholder ("Demo: coming soon on X") marks where the demo
  post's link goes.

### Host scripts (review fixes)
- Windows (`hosts/windows/setup.ps1`):
  - The script now parses: values are sanitized outside the strings, and the bot name, operator name and time zone are
    checked against a safe character set.
  - It checks for WSL 2.4.4+ and updates WSL if needed. `-WebDownload`, and an automatic retry, cover PCs without the
    Microsoft Store.
  - It waits for systemd and retries `enable-linger`.
  - The distro user `cpg` has no sudo. Root steps run from Windows with `wsl -u root`.
  - The keep-alive task is a hidden PowerShell instead of `conhost --headless`. The at-sign-in trigger is documented,
    and `-AtStartup` adds an S4U at-boot task.
  - Sleep and battery checks warn instead of aborting.
  - The time-zone lookup uses .NET's Windows-to-IANA mapping.
  - `claude-projects.cmd` uses CRLF line endings.
  - Bash scripts reach the distro as base64 through `wsl.exe --exec` (`Invoke-WslScript`). Passed as plain text after
    `wsl.exe --`, wsl expanded `$name` against the guest environment, and Windows PowerShell 5.1 split embedded double
    quotes.
  - `wsl --version`, `--status` and `-l` run with stderr guarded (`Invoke-WslQuiet`), so PowerShell 5.1 with
    `ErrorActionPreference=Stop` can't abort on them.
- macOS (`hosts/macos/setup.sh`):
  - Homebrew's paths are on PATH (Apple silicon and Intel), with `brew shellenv`, and `"$LIMACTL"` is used everywhere.
  - Only an already-running VM is skipped. Real start failures stop the script, and it waits for `Running`.
  - `git` and `ca-certificates` are installed in the VM before the clone.
  - Linger is retried.
  - The LaunchAgent runs a helper that exits quietly when the VM is up, and logs go to `~/Library/Logs`.
  - A flag given without a value is an error.
- CI (`.github/workflows/checks.yml`) runs pytest, ShellCheck, the PowerShell parser and PSScriptAnalyzer
  (`tools/check-ps1.ps1`).

### Grok Bot box and other hosts without systemd

- Process mode: on a host without user systemd (a container whose PID 1 is tini, like the Grok Bot box) the installer
  runs the same unit files with `cpg_proc.py`, a small supervisor with its own D-Bus session bus, restarts on failure
  and the 10-minute refresh. The MCP server, `claude-projects setup status` and `claude-projects setup start` bring it up. The preflight no
  longer suggests `enable-linger` there.
- The supervisor holds its lock for its whole life and retries for a few seconds when a status probe is in the way,
  so a probe can't make a starting supervisor give up (it occasionally failed at start-up).
- The box is detected without `--host-kind box` (a container without systemd), installs stdio only (no HTTP server),
  and `--mcp-port N` / `--no-http` / `--http` set the HTTP server everywhere. A busy MCP port gets a suggested free one.
- Sign-in on the box: `signin start` asks before opening a temporary Cloudflare quick tunnel (`--tunnel`) or takes the
  user's own URL, and puts a one-time viewport password in the link, which `signin wait` replaces when done.
- Older hand-built installs (`claude-projects-mcp.service`, `venv/`) count as installed; `install` and `reconfigure`
  leave them alone unless `--migrate`.
- New installs keep the plugin's keyring in its own folder, so a desktop login keyring is never asked to unlock.
- `install` runs the preflight first (also with `--dry-run`) and stops on a failure; `viewport on` and `signin start`
  report units that didn't start instead of saying the viewport is on; `status` gives the right next step when a
  service file is missing, a service is down, or the app was started by hand.
- The sign-in steps say "Get started" (the app's current first screen). System packages install without Recommends
  (fonts and xauth listed explicitly), and the label says "as root" or "with sudo".

### Platform guidance in the skill

- `skill/SKILL.md` explains why the plugin needs a Linux host and how a Mac (Lima VM) or Windows PC (WSL2 distro)
  provides one, with a side-by-side table and the two pitfalls that cost the most time (quoting across the VM/WSL
  boundary, the runtime stopping after a restart).
- New `skill/references/macos.md` and `skill/references/windows.md`: every step the host scripts do, by hand, for an
  agent that only has a shell on the Mac or PC: install, VM/distro, systemd and linger, start at login, ports, running
  claude-projects inside, viewer link and password file, sleep, connecting, verify commands and common failures.
- Mac and Windows connect lines keep `~` and `$` for the Linux side (`bash -lc '...'`, `wsl.exe --exec`).

### Installing a pre-release from the host scripts
- `hosts/macos/setup.sh --ref REF` and `hosts/windows/setup.ps1 -Ref REF` (or `CPG_REPO_REF`; the flag wins) install a
  branch, a tag or a full commit sha instead of the default branch, e.g. `--ref release-1.0.2`. A fresh install clones
  and checks it out; a re-run fetches it from origin and checks it out, and refuses when the existing clone has local
  changes or local commits. The ref is validated strictly in the outer script before anything runs. Without a ref,
  nothing changes.

### Windows and install fixes (from runs on GitHub's Windows runners)
- `setup.ps1` on the old inbox WSL (Windows Server 2022 and other builds without `wsl --version`): it tries
  `wsl --update --web-download`, then `wsl --update`. If WSL 2.4.4+ still isn't there, or `wsl --status` or
  `wsl --set-default-version 2` fails, it stops with a readable message and next steps instead of a stack trace: turn
  on the two Windows features as admin, reboot, install the WSL package from github.com/microsoft/WSL/releases, re-run.
  It makes WSL 2 the default and converts an existing WSL 1 `ClaudeProjects` distro. Any other unexpected failure ends
  with one readable line.
- `setup.ps1` decodes `wsl.exe` output from its raw bytes: UTF-16LE (with or without a BOM, which the old wsl.exe prints
  whatever `WSL_UTF8` says) or UTF-8, with NULs and BOMs dropped, for `--version`, `-l -q`, `-l -v` and `--status`.
- `install.sh` installs a sign-in browser deb only when apt has a candidate for it (`firefox-esr` or `chromium` on
  Debian). On Ubuntu, which has neither, it goes straight to the chromium snap with no apt error.
- `setup verify` failed `signin_browser` ("no usable browser") with the chromium snap installed, because /snap/bin
  wasn't on PATH. `bin/claude-signin-browser` now adds /snap/bin itself and counts a snap as a browser when
  `/snap/bin/<name>` runs and `snap list <name>` lists it; the Ubuntu `chromium-browser` deb stub counts only when its
  snap is there. The CLI, the process supervisor and the desktop, MCP and refresh units put /snap/bin on PATH, and a
  failed check now says why (deb stub only, a snap snapd doesn't list, or no browser).
- Preflight on a Windows distro no longer warns that sudo needs a password: the `cpg` user has no sudo by design, and
  the check now says root steps run from Windows with `wsl -u root`.

## 1.0.1 (2026-11-14)

### Fixed
- Fresh installs failed at the pip step with `ResolutionImpossible`: `ccl_chromium_reader` declares an unpinned git
  dependency on `ccl_simplesnappy` that pip won't combine with our pinned one. It's now installed with `--no-deps` from
  `requirements-nodeps.txt`, and its other dependency (`zstd`) is pinned in `requirements.txt`.

## 1.0.0 (2026-11-14)

### Compatibility
- With `legacy_field_aliases` on, `current_state` (and any dict with `waiting_on`) also carries `status_legacy` /
  `waiting_on_legacy` in the pre-1.0 vocabulary (`waiting_on_<operator first name>`, `<operator first name>`), next to
  the unchanged new fields, so older prompts and scripts keep working.
- Cached reads written by pre-1.0 releases are normalized to the new vocabulary (`waiting_on_user`, `user`), so one
  result never mixes old and new status words.
- A bot-sent message's `summary` (and `current_state.summary`) is the request without the `[sent by Grok Bot]` line;
  `text` stays verbatim and `sent_by_bot` marks it. New helper `ccp_origin.origin_body`.
- Skill: note that some MCP clients show a stale tool list; every tool can still be called by name.

### Added
- `claude_thread_permissions` (read-only) and `claude_thread_permission` (press an option by label, dry run by default):
  generic detection of the app's permission cards (add folder, add repository, allow machine, run command, ...), with a
  stable `card_id`, `type`, `target`, `prompt` and `options`.
- Reads and status report `permission_cards`, `current_state.waiting_on = "permission"` and `permission_pending`, also
  for replies that stall asking for an OK (`permission_hint`).
- `claude_thread_forward`: relays one message verbatim under a `Forwarded from Project "…" › thread "…" (<chan_>/<id>)`
  header, optionally with its images and files (fetched into staging and re-attached). Dry run by default.
- Stable `address` (`<chan_>/<cse_|cmsg_>`) on read and status results.
- Managed staging directory (`incoming/`, `outgoing/`, `attach-test/`, `msg/`) with per-folder TTLs, a size cap, an
  hourly automatic sweep and the `claude_staging` tool.
- Canonical agent skill at `skill/SKILL.md` with a user-editable POLICY section; `claude_skill_get`, `claude_policy_get`
  and `claude_policy_propose` (proposals go to a user-owned file and never apply themselves).
- `claude_thread_send(attach_test=true)` with `dry_run=true`: opens the file picker per file, types the path, confirms it
  by OCR and cancels. Nothing is uploaded or sent.
- Config keys `staging_dir`, `staging_ttl_hours`, `staging_max_mb`, `staging_sweep_interval_min`, `attachments_dir`,
  `skill_path`, `legacy_field_aliases`.
- `CHANGELOG.md`.

### Changed
- README: titled "Claude Projects for Grok Bot" (repository `BenItBuhner/Claude-Projects-for-Grok-Bot`), new hero
  image and a link to the launch demo (`docs/assets/`), all 23 tools listed (adds `claude_refresh`).
  The old `docs/images/banner.png` is removed.
- Attachments are chosen by typing each absolute path into the GTK file chooser's location bar (Ctrl+L) instead of
  browsing the Places sidebar.
- Base64 attachments are staged in a unique folder per send (`outgoing/<stamp>-<id>/<filename>`) instead of overwriting
  a shared file by name.
- The `ccp` wrapper also finds a `venv/` next to it.

### Fixed
- An orphaned GTK "Open Files" chooser left by a failed attach is cancelled before and after every attach, and sends refuse
  to type while one is open (it used to swallow every later send's keystrokes).
- Clipboard paste read-back is verified and retried once; non-ASCII text goes through the clipboard.
- README: `claude_project_create` and `claude_project_pin` have no dry run (the README said every write tool had one).
- `install.sh` writes `viewer_ports` (`5900+N`, `6080+N` for display `:N`) into the config, so the "pause while a viewer
  is connected" check is visible there. Ports set by hand are kept; default ports follow a `--display` change.
- Memory: every tool call that names a project used to copy and fully deserialize the app's IndexedDB in its own child
  process, so parallel calls from several bots stacked those reads up against the service's `MemoryMax`. Now one read
  runs at a time (callers queued behind it reuse its result) and project lookups accept a snapshot up to 30 s old
  (`CCP_IDB_RESOLVE_MAX_AGE`), falling back to a fresh read when the name or id isn't in it.

## 0.1.0 (2026-11-10)
- First private release: Projects, threads, sends with the `[sent by Grok Bot]` origin bracket, attachments, Project admin
  tools, installer, systemd units, stdio and HTTP transports.
