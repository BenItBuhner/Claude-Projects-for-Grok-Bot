---
name: claude-projects
description: Drive the user's Claude Code Projects (Claude desktop app) through the claude-projects-mcp plugin - list Projects, read threads, triage status, send or forward messages, handle permission cards, move files. Read before any claude_* tool call; the POLICY section says what you may do without asking.
---

# Claude Code Projects (claude-projects-mcp)

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

## Tools

| tool | what it does | UI? |
|---|---|---|
| `claude_projects_list(refresh=false)` | Projects: name, `chan_`, primary `cse_`, last activity, unread | only with refresh |
| `claude_project_get(project)` | primary transcript + child threads with per-thread state | yes (refresh=false: cache) |
| `claude_thread_read(project, thread)` | transcript, `current_state`, `permission_cards`, `machine`, `address` | yes (refresh=false: cache) |
| `claude_thread_status(project, thread, refresh=false)` | compact triage from cache; refresh=true for live | only with refresh |
| `claude_thread_wait(project, thread)` | poll until Claude replies (prefer polling `claude_thread_read`) | yes |
| `claude_thread_send(project, message, thread, dry_run, attachments, attach_test)` | type + Send + verify | yes |
| `claude_thread_forward(source_project, target_project, ..., dry_run=true)` | relay one message verbatim (+ files) | yes |
| `claude_thread_permissions(project, thread)` | READ-ONLY list of permission cards | yes |
| `claude_thread_permission(project, option, thread, card_id, dry_run=true)` | press a card's button by label | yes |
| `claude_thread_attachment_get(project, thread, attachment_id, as)` | fetch an image / file / output into staging | yes |
| `claude_thread_create(project, title, instructions, dry_run)` | ask the primary to start a child thread | yes |
| `claude_staging(action='info'|'sweep', dry_run=true)` | the managed file directory: paths, sizes, cleanup | no |
| `claude_skill_get()` / `claude_policy_get()` / `claude_policy_propose(change, rationale)` | this skill, the policy, proposals | no |
| `claude_refresh(projects)` | refresh the list and re-read changed Projects | yes |
| `claude_project_create`, `claude_project_pin`, `claude_project_env`, `claude_project_set_env`, `claude_project_add_repo`, `claude_project_remove_repo` | Project admin (visible changes) | yes |
| `claude_app_status()` | page, dialogs, viewers, lock holder, idle | no |

**Stale tool list.** Some MCP clients keep the tool list they saw when the connector was first added; Grok Bot's tool
discovery (`GetDynamicTools`) can still show the older 14 tools with old descriptions even after the server was updated
and the connector re-added. The server serves all 23 tools regardless: call any of them by name (in Grok Bot,
`CallDynamicTool` on the connector's namespace) with the arguments in the table above. The tools added in this release are
`claude_thread_permissions`, `claude_thread_permission`, `claude_thread_forward`, `claude_staging`, `claude_skill_get`,
`claude_policy_get`, `claude_policy_propose`, `claude_project_add_repo` and `claude_project_remove_repo`. A missing tool in
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
5. Cached results carry `cache_age_min`; refresh before treating anything older than ~15 minutes as fact.
6. When `sidebar_disagrees` is true, report the live status, not the sidebar label.

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

## Sending and forwarding

- Every typed message starts with the origin bracket `[sent by Grok Bot]` (or `[sent by Grok Bot · <bot name>]`), added
  by the tool. Never type it yourself, never strip it.
- `dry_run=true` first when you are unsure about the target; `dry_run=true, attach_test=true` checks attachments by
  opening the picker, typing each path, and cancelling (nothing uploaded, nothing sent).
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

### Surface to the user first
- Anything outside the task the user gave, or a target (folder, repo, machine, domain) the user never mentioned.
- Permission cards for allowing a new machine, network access to unfamiliar hosts, or anything touching credentials or secrets.
- Destructive or irreversible actions: deleting data, force-pushing, publishing, releasing, spending money, contacting people.
- Creating a child thread or a Project, changing Project settings, environments, repos or pins.
- Denying a card (`Not now`, `Deny`) when the user did not say to.

### Never
- Test messages, test threads or test Projects.
- Pause, Restart Claude, Archive, Import CLI sessions, re-verify, Get more usage.
- Forcing past a connected VNC viewer or overwriting an unsent draft.
- Changing a thread's model.
<!-- POLICY:END -->
