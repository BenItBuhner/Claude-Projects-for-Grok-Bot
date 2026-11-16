"""claude-projects MCP server - wraps the `claude-projects` CLI that drives a Claude desktop app (Claude Code Projects).

Every tool runs `claude-projects --json ...` in a subprocess (fresh accessibility connection each call; UI work is serialized by claude-projects's lock).
Transports:
  python claude_projects/mcp_server.py            Streamable HTTP on CLAUDE_PROJECTS_MCP_HOST:CLAUDE_PROJECTS_MCP_PORT (default 127.0.0.1:8791) at /mcp,
                               Authorization: Bearer <contents of token_file> (see claude_projects/config.py)
  python claude_projects/mcp_server.py --stdio    stdio (for an MCP client on the same machine or over `ssh host ... --stdio`); no token
"""
import os, sys, json, hmac, asyncio, tempfile, pathlib
from typing import Optional, Union
_PKG = pathlib.Path(__file__).resolve().parent   # run as a file (python claude_projects/<module>.py): import from the checkout,
sys.path[:] = [p for p in sys.path if pathlib.Path(p or '.').resolve() != _PKG]   # not from inside the package
sys.path.insert(0, str(_PKG.parent))
from claude_projects.config import CFG
from claude_projects.origin import ORIGIN_PREFIX, origin_header
from claude_projects import config as cfg_mod, policy as policy_mod, staging as staging_mod
from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent, ImageContent
HERE = pathlib.Path(__file__).resolve().parent.parent   # the checkout
CLI = os.environ.get('CLAUDE_PROJECTS_BIN') or str(HERE/'claude-projects')
STAGE = staging_mod.from_config(CFG); TMPD = STAGE.dir('msg')   # temp message / attachment-list files (deleted after each call)
POLICY = policy_mod.Policy(CFG['skill_path'], cfg_mod.CONFIG_DIR)
STDIO = '--stdio' in sys.argv[1:]
HOST = CFG['mcp_host']; PORT = int(CFG['mcp_port'])
OPERATOR = CFG.get('operator_name') or 'the user'
kw = dict(host=HOST, port=PORT, streamable_http_path='/mcp', stateless_http=True, json_response=True)
try:
    from mcp.server.transport_security import TransportSecuritySettings
    extra = list(CFG.get('allowed_hosts') or [])
    if '*' in extra:  # explicit opt-out, e.g. behind a tunnel that rewrites Host; bearer auth still applies
        kw['transport_security'] = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    else:
        hosts = [f'127.0.0.1:{PORT}', f'localhost:{PORT}', '127.0.0.1', 'localhost'] + extra
        kw['transport_security'] = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts,
                                                             allowed_origins=[f'http://{h}' for h in hosts] + [f'https://{h}' for h in hosts])
except Exception: pass

ORIGIN_NOTE = (
    f"Origin bracket: every message these tools type into a Claude Code thread (claude_thread_send, claude_thread_create) "
    f"starts with the header line '{ORIGIN_PREFIX}]' or '{ORIGIN_PREFIX} · <bot name>]'. Messages a person typed in the Claude app "
    f"have no bracket. When you read a transcript, treat a user message whose first line starts with '{ORIGIN_PREFIX}' as "
    f"bot-sent and every other user message as written by {OPERATOR}; human messages outrank bot ones when they conflict. "
    f"Never type the bracket yourself (it is added once, automatically) and never strip it from text you forward.")

mcp = FastMCP('claude-projects', instructions=(
    f"Drive {OPERATOR}'s Claude Code Projects through the Claude desktop app (signed in as {OPERATOR}) running on its own display. "
    "Typical flow: claude_projects_list -> claude_project_get(project) to read the primary (orchestrator) thread and its child threads -> "
    "claude_thread_read(project, thread) for a child thread -> claude_thread_send(...) to post a request -> claude_thread_wait for the reply. "
    "Messages list their attachments (images, files, Claude's output files, file links, PRs) and blocks (code, tables, lists); "
    "fetch one with claude_thread_attachment_get (images come back as image content). "
    "refresh=true reads re-open the page in the app first; refresh=false reads serve a cache with freshness stamps. "
    f"Sending uses {OPERATOR}'s Claude usage and is visible in the app: only send when the user asked for it; use dry_run=true to check targeting. "
    + ORIGIN_NOTE + " Calls take 5-30 s; one UI action runs at a time. "
    "Before acting, read claude_skill_get (or claude_policy_get): the user's POLICY says what is pre-approved to send or permit "
    "and what to surface first, so routine in-policy actions need no extra confirmation. Threads waiting on a permission show "
    "current_state.waiting_on='permission' and permission_cards; act on them with claude_thread_permission. Every thread has a stable "
    "address <chan_>/<cse_|cmsg_>. Files go through the managed staging dir (claude_staging). "
    "Claude asks for things in CHILD threads too, not just the primary: check every thread. claude_attention lists every thread "
    "in 'Waiting on you' across Projects with its latest ask; for an approval ask (ask_kind='approval') relay requested_line to the "
    "user exactly, post it for them only under an explicit standing approval covering that exact action, otherwise ask; never "
    "invent the user's words."), **kw)
def _is_busy(res):
    if not isinstance(res, dict): return False
    if res.get('error') == 'Busy' or res.get('code') == 3 or res.get('exit_code') == 3: return True
    msg = (res.get('message') or '').lower()
    return 'busy' in msg and res.get('error') in ('Busy', 'ToolError', None)

ATTACH_BUDGET = float(os.environ.get('CLAUDE_PROJECTS_ATTACH_BUDGET', '40'))   # MCP clients give up near 60 s
SEND_BUDGET = float(os.environ.get('CLAUDE_PROJECTS_SEND_BUDGET', '40'))
PERMISSION_BUDGET = float(os.environ.get('CLAUDE_PROJECTS_PERMISSION_BUDGET', '40'))
READ_LOCK_WAIT = float(os.environ.get('CLAUDE_PROJECTS_READ_LOCK_WAIT', '35'))   # a live read queues this long, then answers busy


async def run(args, timeout=900, msg=None, attach=None, busy_budget=None):
    """Run claude-projects --json. On Busy (code 3 / holder), auto-retry with 2..15s backoff until busy_budget
    (default 900s; wait tools pass their own timeout). Each attempt keeps the original subprocess timeout."""
    import time as _time
    if busy_budget is None: busy_budget = 900
    tmp = None; tmp2 = None
    try:
        if msg is not None:
            fd, tmp = tempfile.mkstemp(prefix='claude-projects-msg-', dir=str(TMPD)); os.write(fd, msg.encode()); os.close(fd)
            args = args + ['--file', tmp]
        if attach:
            fd, tmp2 = tempfile.mkstemp(prefix='claude-projects-att-', suffix='.json', dir=str(TMPD)); os.write(fd, json.dumps(attach).encode()); os.close(fd)
            args = args + ['--attach-json', tmp2]
        t0 = _time.monotonic(); delay = 2.0; attempt = 0; last = None; lock_waited_s = 0.0
        while True:
            attempt += 1
            p = await asyncio.create_subprocess_exec(CLI, '--json', *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try: out, err = await asyncio.wait_for(p.communicate(), timeout)
            except asyncio.TimeoutError:
                p.terminate(); return {'error': 'Timeout', 'message': f'claude-projects did not finish in {timeout}s', 'busy_retries': attempt - 1}
            raw = out.decode() if out else ''
            err_txt = err.decode() if err else ''
            for line in err_txt.splitlines():
                if 'lock acquired' in line and 'waited' in line:
                    try:
                        import re as _re
                        mm = _re.search(r'waited ([0-9.]+)s', line)
                        if mm: lock_waited_s += float(mm.group(1))
                    except Exception: pass
            if not raw.strip():
                last = {'error': 'ToolError', 'exit_code': p.returncode, 'message': (err_txt.strip().splitlines() or ['claude-projects produced no JSON (empty stdout)'])[-1][:500]}
            else:
                try: last = json.loads(raw)
                except Exception: last = {'error': 'ToolError', 'exit_code': p.returncode, 'message': (err_txt.strip().splitlines() or raw.splitlines() or ['?'])[-1][:500]}
            if p.returncode == 3 and isinstance(last, dict) and last.get('error') != 'Busy':
                last.setdefault('error', 'Busy'); last.setdefault('code', 3)
            if not _is_busy(last):
                if isinstance(last, dict) and (lock_waited_s > 0 or attempt > 1):
                    if lock_waited_s > 0: last['lock_waited_s'] = round(lock_waited_s, 1)
                    if attempt > 1: last['busy_retries'] = attempt - 1
                return last
            elapsed = _time.monotonic() - t0
            if elapsed >= busy_budget:
                if isinstance(last, dict):
                    last['busy_retries'] = attempt
                    last['busy_budget_s'] = busy_budget
                    last['message'] = (last.get('message') or 'Busy') + f' (gave up after {attempt} tries / {elapsed:.0f}s; holder={last.get("holder")})'
                return last
            await asyncio.sleep(min(15.0, delay))
            delay = min(15.0, delay * 1.7)
    finally:
        for t in (tmp, tmp2):
            if t:
                try: os.unlink(t)
                except Exception: pass
def flags(force): return ['--force'] if force else []
def with_images(res):
    """Move base64 thumbnails / images out of the JSON into MCP image content blocks."""
    if not isinstance(res, dict): return res
    imgs = []
    for m in res.get('messages') or []:
        for a in m.get('attachments') or []:
            tb = a.pop('thumbnail', None)
            if tb: imgs.append((f"thumbnail of attachment {a['id']} ({a.get('name')}) in message {m['index']}", tb))
    if res.get('image'):
        im = res.pop('image'); res['image'] = {k: v for k, v in im.items() if k != 'base64'}; imgs.append((None, im))
    out = [TextContent(type='text', text=json.dumps(res, ensure_ascii=False, default=str))]
    for label, im in imgs:
        if label: out.append(TextContent(type='text', text=label))
        out.append(ImageContent(type='image', data=im['base64'], mimeType=im['mime']))
    return out

@mcp.tool()
async def claude_projects_list(refresh: bool = False, include_archived: bool = False) -> dict:
    """List the user's Claude Code Projects: name, project id (chan_...), primary thread id (cse_...), last activity (UTC + local),
    status (active / unread / archived), unread flag. Fast (no UI) unless refresh=true, which opens the Projects view in the app first.
    Use the name or id as `project` in the other tools."""
    return await run(['projects'] + (['--refresh'] if refresh else []) + (['--all'] if include_archived else []), 180)

@mcp.tool(structured_output=False)
async def claude_project_get(project: str, refresh: bool = True, max_messages: int = 10, include_resolved: bool = False,
                             all_threads: bool = False, include_images: bool = False, markdown: bool = False, force: bool = False):
    """Open a project in the app and return its PRIMARY thread (the orchestrator chat: latest `max_messages` messages, newest last,
    each with index, role user/assistant, text, time_local) plus its child THREADS (title, thread_id cmsg_..., group such as
    'Waiting on you' / 'Working' / 'Idle', status, age, PRs). include_resolved / all_threads expand the collapsed groups.
    refresh=false returns the last cached read without touching the app. project = name (fuzzy), chan_ id or cse_ id.
    Messages carry `attachments` and `blocks` like claude_thread_read; include_images / markdown work the same.
    CACHE-FIRST WHEN BUSY: while another run holds the app, a refresh answers at once from the last read
    (served_from_cache_because_busy=true, busy.eta_s, cache_age_min); with nothing cached it queues up to ~35 s, then
    answers busy with the queue position."""
    a = ['project', project, '--max', str(max_messages)] + (['--cached'] if not refresh else ['--cache-if-busy', '--lock-wait', str(READ_LOCK_WAIT)]) + (['--resolved'] if include_resolved else []) + (['--all-threads'] if all_threads else []) + (['--images'] if include_images else []) + (['--markdown'] if markdown else []) + (['--stay'] if refresh else [])
    return with_images(await run(flags(force) + a, 300, busy_budget=0))

@mcp.tool(structured_output=False)
async def claude_thread_read(project: str, thread: Optional[str] = None, refresh: bool = True, max_messages: int = 20,
                             history_pages: int = 0, expand: bool = False, include_images: bool = False, markdown: bool = False, force: bool = False):
    """Read a thread's transcript. thread = None/'primary' for the project's primary thread, a thread id (cmsg_...) or a thread
    title (fuzzy; from claude_project_get). Returns messages newest last (each with is_latest/age_hours/historical/superseded), current_state
    (status/summary/time_ct/age_hours), claude_working, environment (raw UI) plus machine/environment_info
    (resolved name, offline flag, source). history_pages=N scrolls up N pages for older messages; expand=true clicks 'Load the rest of this message'.
    Each message also has `attachments` (id '<message>.<n>', kind image | file | output_file | file_ref | link | pull_request, name,
    mime, path_in_project, url; path_on_host + sha256 once fetched) and `blocks` (code with language, table rows, list, heading,
    quote, inline_code). Fetch an attachment's content with claude_thread_attachment_get. include_images=true adds small screen-crop
    thumbnails of image attachments as image content; markdown=true adds a markdown rendering per message.
    Each user message has sent_by_bot (true when its first line is the origin bracket) - see the server instructions.
    CACHE-FIRST WHEN BUSY: while another run holds the app, a refresh answers at once from the last read
    (served_from_cache_because_busy=true, busy.eta_s, cache_age_min); retry after eta_s for a live read. With nothing
    cached it queues (fair FIFO) up to ~35 s, then answers busy with the queue position."""
    a = ['read', project, '--max', str(max_messages), '--history', str(history_pages)] + (['--thread', thread] if thread else []) + (['--cached'] if not refresh else ['--cache-if-busy', '--lock-wait', str(READ_LOCK_WAIT)]) + (['--expand'] if expand else []) + (['--images'] if include_images else []) + (['--markdown'] if markdown else []) + (['--stay'] if refresh else [])
    return with_images(await run(flags(force) + a, 300, busy_budget=0))

@mcp.tool(structured_output=False)
async def claude_thread_attachment_get(project: str, thread: Optional[str] = None, attachment_id: Optional[str] = None,
                                       message_index: Optional[int] = None, n: int = 1, as_: str = 'auto', full_size: bool = False, force: bool = False,
                                       refresh: bool = False):
    """Get the content of an attachment shown in a thread (no message is sent; it only opens the preview / file pane in the app and
    closes it again). Pick it with attachment_id ('<message_index>.<n>' from claude_thread_read attachments) or message_index + n.
    as_: 'auto' (images as image content, text-like files as text, others as metadata + path), 'image', 'text', 'base64', 'path'.
    Images are downscaled to max 1600 px / ~1 MB unless full_size=true. Sources, best first: Claude's output files and file links are
    saved through the app's own Download button (original bytes, any size; original=true); uploaded images and files come from the app's
    HTTP cache, or, when the app opens its Overview document viewer, through that viewer's Download file button (viewer='overview');
    otherwise the app's file pane or viewer read through the accessibility tree (text files) or a screen crop of the preview
    (original=false). A pane still showing 'Loading file' is reported as not available; a not-available result lists each step
    that failed (failed_steps). Nothing that comments or posts is ever pressed. The file is kept on the Claude Code host (path_on_host,
    named by sha256). Links and PRs return just their URL.
    Always answers within about 45 s: a fetch still running then comes back as pending=true with a job_id; call again with the
    same arguments (after retry_after_s) to get the file. Calls for the same attachment share one fetch, and a fetched file is
    answered from the cache (cached=true); refresh=true fetches it again."""
    a = ['attachment', 'get', project] + ([attachment_id] if attachment_id else []) + (['--thread', thread] if thread else []) + (['--message', str(message_index), '--n', str(n)] if (message_index is not None and not attachment_id) else []) + ['--as', as_] + (['--full'] if full_size else []) + (['--refresh'] if refresh else []) + ['--budget', str(ATTACH_BUDGET)]
    return with_images(await run(flags(force) + a, 120))

@mcp.tool()
async def claude_thread_send(project: str, message: str, thread: Optional[str] = None, wait: bool = False,
                             timeout_s: int = 600, poll_s: int = 10, dry_run: bool = False, attachments: Optional[list[dict]] = None,
                             bot_name: Optional[str] = None, attach_test: Union[bool, str] = False, force: bool = False,
                             idempotency_key: Optional[str] = None, job_id: Optional[str] = None, clear_leftovers: bool = False) -> dict:
    """Type `message` into the composer of the project's primary thread (thread=None) or a child thread and press Send, exactly as
    the user would; then read the transcript back (verified_in_transcript, message_index). wait=true also waits for Claude's reply
    (see claude_thread_wait). Refuses if the box holds an unsent draft or a VNC viewer is connected (force=true overrides the viewer
    check). This spends the user's Claude usage and is visible to him: send only when asked. dry_run=true opens the target and checks
    the composer and Send button without typing or sending.
    attachments: optional list of {"path": "<file on the Claude Code host>"} or {"base64": "...", "filename": "x.png"} (for files on the box or
    elsewhere), attached through the app's own Add -> 'Add files or photos' flow before the text is typed. Attaching uploads the file
    to claude.ai, so dry_run=true only validates the files and locates the attach control (nothing is attached).
    dry_run=true + attach_test=true goes one step further without uploading: for each file it opens the app's file picker, types
    the absolute path into the location bar, confirms by OCR that the picker shows it, and presses Cancel (never Open, never Send).
    dry_run=true + attach_test="open" presses Open per file, waits for the chip (timing per file), takes the chip out again with
    its own "Remove <name>" button and checks the box is empty. It never types the message and never presses Send (the file
    does upload with the chip, then the chip is removed).
    LEFTOVER CHIPS: composer_chips lists attachments already in the box (duplicates included) and composer_empty is false while
    any is there. A real send refuses when the box holds chips it did not add, naming them; clear_leftovers=true removes them
    with their own Remove buttons first (they are never sent). Right before Send it checks again and stops if any extra chip shows.
    Base64 attachments are written to a unique folder under the managed staging dir (staging/outgoing/<stamp>-<id>/<filename>),
    so same-named files never overwrite each other; the result lists each file's absolute path. See claude_staging.
    ORIGIN BRACKET: the typed text always starts with the line '[sent by Grok Bot]' ('[sent by Grok Bot · <bot_name>]' when
    bot_name is passed or configured), so the Project can tell this request came from a bot, not from a person typing. Do not
    add it yourself; it is never doubled. The result's origin_header shows the exact line used.
    ALWAYS ANSWERS WITHIN ABOUT 45 S: a send (wait=false) runs as a background job. If it is still attaching or typing after
    about 40 s, the answer is pending=true with job_id, state (opening, attaching n/N, typing, sending, verifying) and
    retry_after_s; call claude_thread_send again with the same arguments, or with job_id, for the result. A repeat of the
    same send (same message and files, or the same idempotency_key, within 30 min) joins it and never sends twice: the answer
    then says joined_existing_send=true. To send the same text again on purpose, pass a new idempotency_key. A failed
    send says which file and step failed and that the composer was cleaned up; nothing was sent. With attachments,
    wait=true is not waited for: call claude_thread_wait after the send."""
    if job_id:
        return await run(flags(force) + ['send', project, '--job-id', job_id], 60)
    jobbed = not dry_run and (not wait or bool(attachments))
    if jobbed:
        a = ['send', project] + (['--thread', thread] if thread else []) + (['--bot-name', bot_name] if bot_name else []) + ['--budget', str(SEND_BUDGET)] + (['--idempotency-key', idempotency_key] if idempotency_key else []) + (['--clear-leftovers'] if clear_leftovers else [])
        r = await run(flags(force) + a, 120, msg=message, attach=attachments, busy_budget=60)
        if wait and isinstance(r, dict): r['wait_note'] = 'wait=true is not waited for on a send with attachments; call claude_thread_wait'
        return r
    a = ['send', project] + (['--thread', thread] if thread else []) + (['--bot-name', bot_name] if bot_name else []) + (['--wait', '--timeout', str(timeout_s), '--poll', str(poll_s)] if wait else []) + (['--dry-run'] if dry_run else []) + ([('--attach-test=open' if attach_test == 'open' else '--attach-test')] if (dry_run and attach_test) else []) + (['--clear-leftovers'] if clear_leftovers else [])
    return await run(flags(force) + a, (timeout_s + 400) if wait else 300, msg=message, attach=attachments, busy_budget=(timeout_s + 400) if wait else 900)

@mcp.tool()
async def claude_thread_wait(project: str, thread: Optional[str] = None, after_index: Optional[int] = None,
                             timeout_s: int = 600, poll_s: int = 15, force: bool = False) -> dict:
    """Poll a thread until Claude has replied after message `after_index` (use message_index from claude_thread_send) and has stopped
    working, or until timeout_s. Returns done/timed_out, new_messages and reply_text. The UI lock is released between polls."""
    a = ['wait', project, '--timeout', str(timeout_s), '--poll', str(poll_s)] + (['--thread', thread] if thread else []) + (['--after', str(after_index)] if after_index is not None else [])
    return await run(flags(force) + a, timeout_s + 400, busy_budget=timeout_s)

@mcp.tool()
async def claude_thread_create(project: str, title: str, instructions: str, wait: bool = False, timeout_s: int = 900,
                               dry_run: bool = False, bot_name: Optional[str] = None, force: bool = False) -> dict:
    """Start a new child thread. Projects have no 'new thread' button: the primary thread delegates work into threads. This sends
    '[sent by Grok Bot]' + 'Start a new thread titled "<title>" for this: <instructions>' into the primary thread (spends usage),
    asking the orchestrator to keep the bracket line at the top of the child thread's task so the child also knows the request is
    bot-sent. wait=true waits for the reply. Afterwards call claude_project_get to see the new thread and its id.
    dry_run=true checks targeting only. bot_name works as in claude_thread_send."""
    a = ['create-thread', project, title, instructions] + (['--bot-name', bot_name] if bot_name else []) + (['--wait', '--timeout', str(timeout_s)] if wait else []) + (['--dry-run'] if dry_run else [])
    return await run(flags(force) + a, timeout_s + 400, busy_budget=timeout_s + 400)

@mcp.tool()
async def claude_thread_status(project: str, thread: Optional[str] = None, refresh: bool = False, force: bool = False) -> dict:
    """Compact triage for a primary or child thread: current_state (working/waiting_on_user/paused/done/idle),
    latest message summary with local time and age, what it is waiting on, and machine/environment_info.
    refresh=false (default) is fast: serves the on-disk cache with freshness stamps (cache_age_min). refresh=true
    does a live UI read. Prefer this for 'what is going on' questions; use claude_thread_read for the full transcript.
    Also returns ask (approval | permission | question | action, with requested_line for an approval ask) and waiting_threads:
    EVERY thread of the project in the sidebar's 'Waiting on you' group, child threads included, each with its latest ask."""
    a = (flags(force) + ['thread-status', project] + (['--thread', thread] if thread else []) + (['--refresh'] if refresh else []) + (['--stay'] if refresh else []))
    return await run(a, 300 if refresh else 60)

@mcp.tool()
async def claude_attention(projects: Optional[list[str]] = None, refresh: bool = False, max_reads: int = 2, force: bool = False) -> dict:
    """What needs the user across Projects, in one pass: every thread (primary AND child threads) whose sidebar state is
    'Waiting on you', each with its latest ask and ask_kind; approval_asks lists the threads where Claude asked for a line in
    the user's own words, with requested_line exactly as Claude wrote it; permission_cards lists cards seen in any thread.
    refresh=false (default) uses cached reads only (fast, no UI). refresh=true also live-reads up to max_reads waiting threads
    whose cached ask is stale (ask_stale), one short UI lock each, and answers within about 45 s. Nothing is pressed or sent.
    Relay an approval ask's requested_line to the user; post it for them only under an explicit standing approval covering that
    exact action. Never invent the user's words."""
    a = flags(force) + ['attention'] + list(projects or []) + (['--refresh', '--max-reads', str(max_reads), '--budget', '35'] if refresh else [])
    return await run(a, 60)

@mcp.tool()
async def claude_refresh(projects: Optional[list[str]] = None, max_projects: int = 3, force: bool = False) -> dict:
    """Refresh now: re-read the project list and re-open the primary thread of the named projects (or of up to max_projects whose
    activity changed since the last read), updating the cache used by refresh=false reads. The same pass reads their child threads in
    'Waiting on you' (latest ask, permission cards); see claude_attention. A background timer does this every 10 min."""
    return await run(flags(force) + ['refresh', '--max', str(max_projects)] + list(projects or []), 900)

@mcp.tool()
async def claude_project_create(name: str, repo: Optional[str] = None, pin: bool = True, instructions: Optional[str] = None,
                                force: bool = False) -> dict:
    """Create a new Claude Code Project in the app (Projects -> New project). name = project name (Unicode ok).
    repo = optional GitHub repository attached during create via Context -> Add repository ('owner/name' or a github.com URL;
    must be in the app's connected GitHub repo list). pin = pin it on the Projects page (default true).
    instructions = optional 'What do you want to work on?' text. Spends no Claude chat usage but DOES add a Project visible to
    the user: only call when the user (or a relay he authorized) asked for that project. If a project with that name exists it is
    not duplicated; its ids are returned and pin is ensured. Returns created/already_existed, project_id (chan_),
    primary_thread_id (cse_, once the app lists it), pinned, repo, repo_evidence, notes. Takes 1-3 min."""
    a = ['create-project', name] + ([f'--description={instructions}'] if instructions else []) + ([f'--repo={repo}'] if repo else []) + (['--pin'] if pin else ['--no-pin'])
    return await run(flags(force) + a, 600)

@mcp.tool()
async def claude_project_pin(project: str, pinned: bool = True, force: bool = False) -> dict:
    """Pin (pinned=true) or unpin a project with its toggle on the app's Projects page. Reversible; no usage.
    project = name (fuzzy), chan_ id or cse_ id. Returns pinned (state read back) and changed."""
    return await run(flags(force) + ['project-pin', project] + ([] if pinned else ['--unpin']), 180)

@mcp.tool()
async def claude_project_env(project: str, force: bool = False) -> dict:
    """READ-ONLY view of a project's Settings -> Environment: attached GitHub repositories, selected cloud environment,
    its network access / network secrets / setup script summary and environment-variable NAMES (values are never returned),
    local-device toggle and pre-approved folders. Changes nothing. Note: cloud environments are account-level and shared by
    every project that selects them; the app warns not to put secrets in their environment variables."""
    return await run(flags(force) + ['project-env', project], 240)

@mcp.tool()
async def claude_project_set_env(project: str, values: Optional[dict[str, str]] = None, secret_files: Optional[dict[str, str]] = None,
                                 env_name: Optional[str] = None, copy_from_current: bool = True, allow_shared: bool = False,
                                 dry_run: bool = False, force: bool = False) -> dict:
    """WRITE a project's cloud-environment variables (Settings -> Environment -> cloud environment -> Environment variables).
    values = {NAME: value} for NON-secret values only. secret_files = {NAME: absolute path on the Claude Code host (chmod 600, one line)}
    for secrets: the value is read on the host and typed into the app, never returned, logged or screenshotted.
    env_name = a DEDICATED cloud environment for this project: created if missing (copies the current environment's variables
    and Full network access when copy_from_current), then selected on the project. Without env_name the project's current
    environment is edited in place; shared ones like "Default" need allow_shared=true (every project using it sees the values).
    Returns variable NAMES set, environment name, dedicated flag; values never. Cloud vars only reach cloud threads."""
    args = flags(force) + ['project-set-env', project]
    for k, v in (values or {}).items(): args += ['--set', f'{k}={v}']
    for k, v in (secret_files or {}).items(): args += ['--secret-file', f'{k}={v}']
    if env_name: args += ['--env-name', env_name]
    if not copy_from_current: args.append('--no-copy')
    if allow_shared: args.append('--allow-shared')
    if dry_run: args.append('--dry-run')
    return await run(args, 600)

@mcp.tool()
async def claude_project_add_repo(project: str, repo: str, dry_run: bool = False, force: bool = False) -> dict:
    """Attach a GitHub repository to an EXISTING project (Project settings -> Environment -> Add repository). repo = 'owner/name'
    or a github.com URL; it must be visible to the Claude GitHub app. Already attached -> no-op. Verifies every previously
    attached repo is still listed. dry_run=true finds the repo in the picker and closes it without selecting. Visible change:
    only call when the user asked for it."""
    return await run(flags(force) + ['project-add-repo', project, repo] + (['--dry-run'] if dry_run else []), 300)

@mcp.tool()
async def claude_project_remove_repo(project: str, repo: str, dry_run: bool = False, force: bool = False) -> dict:
    """Detach a GitHub repository from a project via its exact, case-sensitive 'Remove <owner/name>' control. Refuses on zero or
    ambiguous matches; verifies the other repos remain. Already gone -> no-op. dry_run=true locates the control only.
    Visible change: only call when the user asked for it."""
    return await run(flags(force) + ['project-remove-repo', project, repo] + (['--dry-run'] if dry_run else []), 300)

# ---------------------------------------------------------------- permission cards
@mcp.tool()
async def claude_thread_permissions(project: str, thread: Optional[str] = None, force: bool = False) -> dict:
    """READ-ONLY. List the permission cards showing in a thread right now (opens it in the app; never presses anything).
    Projects always run in Auto permission mode, yet threads still stop on dynamic cards such as 'Claude would like to add this
    folder to the session: <path>' [Add folder] [Not now], adding a repository, allowing a machine, or running a command.
    Each card: card_id (stable), type (add_folder | add_repository | allow_machine | run_command | network_access | edit_files |
    tool_use | other), target (path / repo / machine / command), prompt, options (button labels, in order), affirmative_options,
    negative_options. claude_thread_read / claude_thread_status also report cards in permission_cards and set
    current_state.waiting_on='permission'. Decide with the user policy (claude_policy_get), then act with claude_thread_permission."""
    return await run(flags(force) + ['permissions', project] + (['--thread', thread] if thread else []), 300)

@mcp.tool()
async def claude_thread_permission(project: str, option: str, thread: Optional[str] = None, card_id: Optional[str] = None,
                                   dry_run: bool = True, force: bool = False, only_apps: Optional[list[str]] = None,
                                   job_id: Optional[str] = None) -> dict:
    """Approve or deny a permission card by pressing one of its buttons, chosen by label (exact, case-insensitive; a unique prefix
    also works), e.g. option='Add folder' or option='Not now'. card_id picks the card when several are showing.
    dry_run=true (the DEFAULT) finds the card and the button and presses nothing; pass dry_run=false to really press it.
    Policy: press an affirmative option only when the user policy pre-approves it or the action is clearly inside the task the user
    gave; anything questionable (outside the task, secrets, destructive, new machines/repos the user did not mention) goes to the
    user first. The result says whether the card disappeared (card_gone).
    COMPUTER USE: only_apps=['Ephemeris','Notepad'] refuses an Allow on any card that requests other apps
    (apps_not_allowed); lasting grants ('Always allow') and full-screen control are refused. All card buttons are pressed
    with a real pointer click at their centre, then verified; AT-SPI do_action is a fallback (on computer-use cards it
    can focus the button without clicking it). An Allow can work from this host even for a laptop-targeted card.
    ALWAYS ANSWERS WITHIN ABOUT 45 S: the press runs as a background job. While it waits for the app the answer is
    pending=true with job_id, queue_position, eta_s and busy ('busy, queued as job X'); call again with job_id (or the same
    arguments) for the result; it never presses twice. Every job writes its timeline to diagnostics_file."""
    if job_id:
        return await run(flags(force) + ['permission', project, option or '-', '--job', job_id, '--budget', '20'], 60, busy_budget=0)
    a = ['permission', project, option] + (['--thread', thread] if thread else []) + (['--card', card_id] if card_id else []) + ([] if dry_run else ['--apply']) + (['--only-apps', ','.join(only_apps)] if only_apps is not None else []) + ['--budget', str(PERMISSION_BUDGET)]
    return await run(flags(force) + a, 60, busy_budget=0)

# ---------------------------------------------------------------- forwarding
@mcp.tool()
async def claude_thread_forward(source_project: str, target_project: str, source_thread: Optional[str] = None,
                                target_thread: Optional[str] = None, message_index: Optional[int] = None, role: str = 'assistant',
                                note: Optional[str] = None, include_attachments: bool = False, refresh_source: bool = True,
                                dry_run: bool = True, bot_name: Optional[str] = None, force: bool = False) -> dict:
    """Relay ONE message verbatim from a source thread to a target thread (any Project; primary by default on both sides).
    The typed text is: the origin bracket '[sent by Grok Bot]' (added automatically, as for every send), an optional `note`,
    a header 'Forwarded from Project "<name>" > thread "<title>" (<chan_>/<cse_|cmsg_>), message #N by Claude, <time>:', a rule,
    then the message text exactly as read from the transcript. Links and references that cannot be attached are listed under it.
    message_index picks the message (default: the latest message with `role`, 'assistant' | 'user' | 'any').
    include_attachments=true also forwards the message's images / files / output files: each is fetched with the same flow as
    claude_thread_attachment_get into the managed staging dir, then attached to the forward.
    dry_run=true (the DEFAULT) reads the source and checks the target composer; nothing is fetched, typed or sent. A real forward
    (dry_run=false) is a send: it spends the user's usage and posts in the target thread, so follow the user policy."""
    a = (['forward', source_project, target_project] + (['--source-thread', source_thread] if source_thread else []) +
         (['--target-thread', target_thread] if target_thread else []) + (['--message', str(message_index)] if message_index is not None else []) +
         ['--role', role or 'assistant'] + (['--note', note] if note else []) + (['--attachments'] if include_attachments else []) +
         ([] if refresh_source else ['--cached-source']) + (['--bot-name', bot_name] if bot_name else []) + ([] if dry_run else ['--apply']))
    return await run(flags(force) + a, 900 if include_attachments else 600)

# ---------------------------------------------------------------- files
@mcp.tool()
async def claude_staging(action: str = 'info', dry_run: bool = True, older_than_hours: Optional[float] = None,
                         kinds: Optional[list[str]] = None) -> dict:
    """The plugin's ONE managed directory for files moving into and out of threads (absolute paths in every result):
    incoming/ (attachments fetched from threads), outgoing/ (base64 uploads and forwarded files, one unique folder per send),
    attach-test/ (OCR crops from attach dry runs), msg/ (temporary message files). Put files you need to attach here or pass
    base64; do not scatter copies elsewhere. action='info' shows paths, sizes and TTLs. action='sweep' removes entries older than
    their TTL (or older_than_hours) and trims the oldest ones above the size cap; dry_run=true (default) only lists what it would
    remove. Sweeps also run automatically about once an hour. Never touches anything outside the staging root."""
    a = ['staging', action] + (['--older-than-hours', str(older_than_hours)] if older_than_hours is not None else []) + sum((['--kind', k] for k in (kinds or [])), []) + ([] if (dry_run or action == 'info') else ['--apply'])
    return await run(a, 120)

# ---------------------------------------------------------------- setup and upkeep
SETUP_ACTIONS = ('status', 'verify', 'check_beta', 'tidy', 'connect', 'viewport_on', 'viewport_off')
@mcp.tool()
async def claude_setup(action: str = 'status', live: bool = False, apply: bool = False) -> dict:
    """Guided setup and upkeep of this plugin (skill Part A). action='status' (default): where the setup stands, the next
    action and, when set, ask_user = the exact question to put to the user; calling it through this connector also records
    that the connector works. 'verify': read-only end-to-end checks (services, app, sign-in, Projects access, project read,
    MCP tools, skill sha256); live=true also refreshes, opens Project settings for the cloud environment and runs a send
    dry run (nothing is typed or sent). 'check_beta': does the signed-in account have Claude Code Projects? (read-only;
    verdict has_projects | no_projects | app_outdated | unknown, plus tell_user text). 'tidy': leftovers (staging, stale lock
    record, IndexedDB copies, file pickers, sign-in browser, viewport left on); dry run unless apply=true; never restarts the
    app. 'connect': connector options. 'viewport_on' / 'viewport_off': the noVNC viewport (off refuses while someone watches).
    Install, sign-in, reconfigure and uninstall need a shell on the host: run `claude-projects setup ...` there."""
    if action not in SETUP_ACTIONS:
        return {'error': 'ClaudeProjectsError', 'message': f'action must be one of {", ".join(SETUP_ACTIONS)}', 'code': 2}
    a = {'status': ['status'], 'verify': ['verify'] + (['--live'] if live else []), 'check_beta': ['check-beta'],
         'tidy': ['tidy'] + (['--apply'] if apply else []), 'connect': ['connect'], 'viewport_on': ['viewport', 'on'],
         'viewport_off': ['viewport', 'off']}[action]
    return await run(['setup', '--via-mcp'] + a, 900 if action == 'verify' else 300)

# ---------------------------------------------------------------- skill + policy (no UI)
@mcp.tool()
async def claude_skill_get(policy_only: bool = False) -> dict:
    """Return the canonical agent skill for this plugin (SKILL.md from the server's repo) with the user's EFFECTIVE policy merged
    into its POLICY section, plus its sha256 so you can tell when it changed. Read this before driving Projects and whenever you are
    unsure whether something is allowed: the POLICY says what is pre-approved to send or permit and what must be surfaced to the
    user, so you do not need to ask for routine actions. policy_only=true returns just the policy (same as claude_policy_get)."""
    return POLICY.effective() if policy_only else POLICY.skill_with_policy()

@mcp.tool()
async def claude_policy_get() -> dict:
    """The user's effective policy: text, source (the user-owned policy file when it exists, else the skill's default POLICY
    section), parsed rules {pre_approved, surface_first, never}, and the paths of the policy and proposals files."""
    return POLICY.effective()

@mcp.tool()
async def claude_policy_propose(change: str, rationale: str = '', proposed_by: str = '') -> dict:
    """Propose a policy change (for example a new pre-approved action the user keeps approving). The proposal is appended to the
    user-owned policy-proposals.md next to the policy file; it never takes effect until the user copies it into policy.md.
    Tell the user you proposed it. Returns proposal_id and the file path."""
    try: return POLICY.propose(change, rationale, proposed_by)
    except ValueError as ex: return {'error': 'ClaudeProjectsError', 'message': str(ex), 'code': 2}

@mcp.tool()
async def claude_app_status() -> dict:
    """Health check: current page in the app, open dialogs, connected VNC viewers (UI actions are refused while one is connected),
    who holds the UI lock, and when the app last fetched the project list."""
    return await run(['status'], 60)

class Bearer:
    def __init__(self, app, token): self.app = app; self.token = token
    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http':
            hdr = dict(scope.get('headers') or []).get(b'authorization', b'').decode()
            if scope.get('path') == '/healthz':
                await send({'type': 'http.response.start', 'status': 200, 'headers': [(b'content-type', b'text/plain')]}); await send({'type': 'http.response.body', 'body': b'ok'}); return
            if not (hdr.startswith('Bearer ') and hmac.compare_digest(hdr[7:].strip(), self.token)):
                await send({'type': 'http.response.start', 'status': 401, 'headers': [(b'content-type', b'application/json'), (b'www-authenticate', b'Bearer')]})
                await send({'type': 'http.response.body', 'body': b'{"error":"unauthorized"}'}); return
        await self.app(scope, receive, send)

def _read_token():
    p = pathlib.Path(CFG['token_file']).expanduser()
    if not p.is_file():
        sys.exit(f'claude-projects-mcp: token file {p} is missing; run install.sh (or create it: 32+ random chars, chmod 600)')
    if p.stat().st_mode & 0o077:
        sys.exit(f'claude-projects-mcp: token file {p} must be private (chmod 600)')
    t = p.read_text().strip()
    if len(t) < 24: sys.exit(f'claude-projects-mcp: token in {p} is too short (need 24+ characters)')
    return t

if __name__ == '__main__':
    if CFG.get('service_mode') == 'process':
        # no systemd on this host: nothing else brings the display and the app back after a restart of the box,
        # so the MCP server (started by the agent) starts the plugin's supervisor when it isn't running
        from claude_projects import proc as proc_mod; proc_mod.ensure_up()
    if STDIO:
        mcp.run('stdio')
    else:
        import uvicorn
        uvicorn.run(Bearer(mcp.streamable_http_app(), _read_token()), host=HOST, port=PORT, log_level='warning', proxy_headers=True)
