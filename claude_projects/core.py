#!/usr/bin/env python3
"""claude-projects - Claude Code Projects tool for a Claude desktop app running on its own X display (see claude_projects/config.py).

Reads projects, primary threads and child threads; sends messages exactly as typed in the app; waits for replies.
How it works (no tokens or cookies are ever read or exported):
  * project list  : a COPY of the app's IndexedDB react-query cache ("cc-project-channels"), refreshed by opening
                    the Projects view in the app.
  * reading       : the app is navigated with claude:// deep links (claude://code/project/<chan_id>[?thread=<cmsg_id>])
                    and the rendered transcript is read through the accessibility tree (AT-SPI).
  * sending       : the composer is focused through AT-SPI, the text is typed with X keystrokes (xdotool) on that display,
                    then the app's own Send button is pressed and the new message is read back from the transcript.
Every message typed into a thread starts with the origin bracket "[sent by Grok Bot]" (see claude_projects/origin.py).
UI work is serialized with a lock file, refused while a VNC viewer is connected (unless
--force), and the app is returned to its home screen afterwards (unless --stay).
"""
import os, sys, re, json, time, fcntl, argparse, pathlib, subprocess, datetime, hashlib, unicodedata, warnings, contextlib, threading, shutil
warnings.filterwarnings('ignore', category=DeprecationWarning)
H = pathlib.Path.home()
_PKG = pathlib.Path(__file__).resolve().parent   # run as a file (python claude_projects/<module>.py): import from the checkout,
sys.path[:] = [p for p in sys.path if pathlib.Path(p or '.').resolve() != _PKG]   # not from inside the package
sys.path.insert(0, str(_PKG.parent))
from claude_projects.config import CFG, tool as _cfg_tool
from claude_projects.origin import add_origin, strip_origin, has_origin, is_bot_sent, origin_header, origin_body, ORIGIN_PREFIX
from claude_projects import staging as staging_mod, permissions as PERM
STAGE = staging_mod.from_config(CFG)
ROOT = pathlib.Path(CFG['state_dir']); CACHE = ROOT/'cache'; LOGD = ROOT/'logs'
LOCKP = pathlib.Path(CFG['ui_lock_file'])
LAUNCH = CFG['launcher']
XDO = _cfg_tool(CFG, 'xdotool'); EXTRA_LIB = CFG.get('extra_lib_path') or ''
IDB_PY = CFG['idb_python']
OPERATOR = CFG.get('operator_name') or 'the user'; HOST_LABEL = CFG.get('host_label') or 'the Claude Code host'
for d in (CACHE, LOGD): d.mkdir(parents=True, exist_ok=True)
U = os.getuid()
from claude_projects import config as cfg_mod; cfg_mod.apply_runtime_env(CFG)   # display + session bus (process mode: the supervisor's private bus)
if CFG.get('gi_typelib_path'): os.environ.setdefault('GI_TYPELIB_PATH', CFG['gi_typelib_path'])
def _tool_env():
    env = dict(os.environ)
    if EXTRA_LIB: env['LD_LIBRARY_PATH'] = EXTRA_LIB + ((':' + env['LD_LIBRARY_PATH']) if env.get('LD_LIBRARY_PATH') else '')
    return env

class CPGError(Exception):
    code = 2
class Refused(CPGError): code = 5
class Busy(CPGError): code = 3
class NotFound(CPGError): code = 2
class AppError(CPGError): code = 6
CCPError = CPGError   # the old name, for code that imports it

def now(): return datetime.datetime.now(datetime.timezone.utc)
def log(msg):
    line = f"{now().strftime('%Y-%m-%dT%H:%M:%SZ')} [{os.getpid()}] {msg}"
    try:
        p = LOGD/'claude-projects.log'
        if p.exists() and p.stat().st_size > 2_000_000: p.replace(LOGD/'claude-projects.log.1')
        with open(p, 'a', encoding='utf-8') as f: f.write(line + '\n')
    except Exception: pass
    if os.environ.get('CLAUDE_PROJECTS_VERBOSE'): print(line, file=sys.stderr)
class StepTimer:
    """Wall time per step of one UI operation, logged as one line ("timing attachment X: lock 0.0, deeplink 0.9, ...").
    A step's time is what passed since the previous mark, so nothing between two marks goes uncounted."""
    def __init__(self, label): self.label = label; self.t0 = self.last = time.time(); self.steps = {}
    def mark(self, step):
        t = time.time(); self.steps[step] = round(self.steps.get(step, 0) + (t - self.last), 2); self.last = t
    def as_dict(self): return dict(self.steps, total=round(time.time() - self.t0, 2))
    def log(self):
        d = self.as_dict(); log(f'timing {self.label}: ' + ', '.join(f'{k} {v:.1f}' for k, v in d.items())); return d
_TIMER = None
def _mark(step):
    if _TIMER is not None: _TIMER.mark(step)
@contextlib.contextmanager
def step_timer(label):
    """with step_timer('send X') as t: ... -> every _mark() inside lands on t; the summary is logged on exit."""
    global _TIMER
    prev = _TIMER; _TIMER = t = StepTimer(label)
    try: yield t
    finally:
        _TIMER = prev
        try: t.log()
        except Exception: pass
def _zone(name):
    """Configured IANA zone, or this machine's local zone when unset."""
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception: pass
    return datetime.datetime.now().astimezone().tzinfo
DISPLAY_TZ = _zone(CFG.get('timezone')); APP_TZ = _zone(CFG.get('app_timezone'))
def ct(ts):
    """ISO/epoch -> 'Nov 04 05:59 PM <zone>' in the configured display zone (CLAUDE_PROJECTS_TIMEZONE)."""
    if not ts: return None
    try:
        d = datetime.datetime.fromisoformat(str(ts).replace('Z', '+00:00'))
        if d.tzinfo is None: d = d.replace(tzinfo=datetime.timezone.utc)
        return d.astimezone(DISPLAY_TZ).strftime('%b %d %I:%M %p %Z')
    except Exception: return str(ts)
def norm(s): return re.sub(r'\s+', ' ', unicodedata.normalize('NFC', s or '')).strip()
def slug(s): return re.sub(r'[^a-z0-9]+', '-', unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode().lower()).strip('-') or 'x'

# ---------------------------------------------------------------- machine / environment labels
# UI button text "Running on …" is a Claude Code *environment label*, not a hostname. Map labels and IndexedDB
# environment ids to human names in config.json ("environment_labels" / "environment_ids"); unknown stays unknown.
# The Claude desktop app itself always runs on the configured host/display; a thread may execute elsewhere.
_ENV_LABELS = dict(CFG.get('environment_labels') or {})
_ENV_IDS = dict(CFG.get('environment_ids') or {})

def _label_from_running_on(raw):
    """Parse AT-SPI 'Running on X' / 'Running on X, which is offline' into parts."""
    if not raw: return None, False, None
    s = raw.strip()
    offline = bool(re.search(r'\boffline\b', s, re.I))
    m = re.match(r'^Running on\s+(.+?)(?:,\s*which is offline)?\.?$', s, re.I)
    label = (m.group(1).strip() if m else s)
    label = re.sub(r',\s*which is offline$', '', label, flags=re.I).strip()
    return label or None, offline, s

def resolve_environment(raw_ui=None, env_id=None, connection_status=None, source='live', read_at=None):
    """Build a structured machine/environment block. Never guess when raw is null."""
    read_at = read_at or now().isoformat()
    label, offline_ui, raw = _label_from_running_on(raw_ui) if raw_ui else (None, False, None)
    meta = None
    if label and label in _ENV_LABELS: meta = dict(_ENV_LABELS[label])
    if not meta and env_id:
        if env_id in _ENV_IDS: meta = dict(_ENV_IDS[env_id])
        else:
            for pref, v in _ENV_IDS.items():
                if env_id.startswith(pref): meta = dict(v); break
    offline = offline_ui
    if connection_status and str(connection_status).lower() in ('disconnected', 'offline'): offline = True
    if connection_status and str(connection_status).lower() in ('connected', 'online'): 
        if not offline_ui: offline = False
    if raw_ui is None and env_id is None:
        return {
            'environment_raw': None,
            'machine': {'raw': None, 'resolved': 'unknown (not shown in the UI)', 'kind': None, 'detail': None},
            'offline': None,
            'connection_status': connection_status,
            'environment_id': env_id,
            'source': source,
            'read_at': read_at,
            'read_at_local': ct(read_at),
            'app_host': f"{HOST_LABEL} (Claude desktop on DISPLAY {os.environ.get('DISPLAY')})",
            'note': 'Primary threads often show no environment button; do not infer a machine from a missing label.',
        }
    resolved = (meta or {}).get('resolved') or (f'environment labeled {label!r}' if label else 'unknown (not shown in the UI)')
    return {
        'environment_raw': raw or raw_ui,
        'environment_label': label,
        'machine': {
            'raw': label,
            'resolved': resolved,
            'kind': (meta or {}).get('machine_kind'),
            'detail': (meta or {}).get('detail'),
        },
        'offline': offline,
        'connection_status': connection_status,
        'environment_id': env_id,
        'source': source,
        'read_at': read_at,
        'read_at_local': ct(read_at),
        'app_host': f"{HOST_LABEL} (Claude desktop on DISPLAY {os.environ.get('DISPLAY')})",
        'note': None if meta else ('Unrecognized environment label; report the raw string, do not guess.' if label else None),
    }

def idb_session_for_thread(thread_id):
    """Best-effort match of a cmsg_/session_/cse_ id to an IndexedDB sessions_api row."""
    if not thread_id: return None
    try: snap = json.loads((CACHE/'idb.json').read_text())
    except Exception:
        try: snap = idb_snapshot()
        except Exception: return None
    want = str(thread_id)
    alts = {want}
    if want.startswith('cmsg_'):
        alts.add('session_' + want[5:])
        alts.add('cse_' + want[5:])
    if want.startswith('session_'):
        alts.add('cmsg_' + want[8:])
        alts.add('cse_' + want[8:])
    if want.startswith('cse_'):
        alts.add('session_' + want[4:])
        alts.add('cmsg_' + want[4:])
    sessions = snap.get('sessions') or []
    for s in sessions:
        sid = s.get('id') or ''
        if sid in alts or sid == want: return s
    # Suffix match: cse_/session_/cmsg_ sometimes share the same public-id body.
    bodies = set()
    for a in list(alts) + [want]:
        for pref in ('cmsg_', 'session_', 'cse_'):
            if a.startswith(pref): bodies.add(a[len(pref):]); break
        else:
            bodies.add(a)
    bodies = {b for b in bodies if len(b) >= 16}
    if bodies:
        for s in sessions:
            sid = s.get('id') or ''
            for pref in ('cmsg_', 'session_', 'cse_'):
                if sid.startswith(pref) and sid[len(pref):] in bodies:
                    return s
    return None

def idb_env_fields_for_thread(thread_id):
    """Return (environment_id or None, connection_status or None) from idb when known."""
    sess = idb_session_for_thread(thread_id)
    if not sess: return None, None
    eid = sess.get('environment_id') or None
    if isinstance(eid, str) and not eid.strip(): eid = None
    return eid, sess.get('connection_status')

STATUS_WORDS = ('Paused', 'Unread', 'Working', 'Idle', 'Resolved', 'Failed', 'Queued', 'Needs attention', 'Waiting on you')
def clean_thread_title(title):
    if not title: return title
    t = title.strip()
    # strip trailing status tokens the sidebar concatenates into the accessible name
    changed = True
    while changed:
        changed = False
        for w in STATUS_WORDS:
            if t.endswith(' ' + w):
                t = t[:-(len(w)+1)].rstrip(); changed = True
    return t or title

def parse_msg_time(m):
    """Return timezone-aware UTC datetime for a message, or None.

    time_utc (ISO) is preferred. time_local like 'Nov 04 10:02 PM CST' has no year in the
    app string; strptime would default to 1900 and explode age_hours (~1e6). Pin the year
    to the current year in the display zone, or the previous year when that date would be in
    the future relative to now.
    """
    tu = m.get('time_utc')
    if tu:
        try:
            d = datetime.datetime.fromisoformat(str(tu).replace('Z', '+00:00'))
            if d.tzinfo is None: d = d.replace(tzinfo=datetime.timezone.utc)
            return d
        except Exception: pass
    tc = m.get('time_local')
    if tc:
        try:
            chi = DISPLAY_TZ
            raw = str(tc).strip()
            # Drop trailing zone token; we always pin the display zone.
            raw_noz = re.sub(r'\s+(CST|CDT|UTC|GMT|[A-Z]{2,5})$', '', raw).strip()
            parsed = None
            for fmt in ('%b %d %I:%M %p', '%b %d %I:%M%p', '%B %d %I:%M %p'):
                try:
                    parsed = datetime.datetime.strptime(f'2000 {raw_noz}', '%Y ' + fmt)  # leap year placeholder; real year pinned below
                    break
                except Exception:
                    continue
            if parsed is None:
                return None
            ref_chi = now().astimezone(chi)
            y = ref_chi.year
            candidate = parsed.replace(year=y, tzinfo=chi)
            # If that lands more than ~1 day in the future, it was last year
            # (e.g. reading 'Dec 30' on Jan 2).
            if candidate > ref_chi + datetime.timedelta(days=1):
                candidate = parsed.replace(year=y - 1, tzinfo=chi)
            return candidate.astimezone(datetime.timezone.utc)
        except Exception:
            pass
    return None
def age_hours_of(m, ref=None):
    ref = ref or now(); dt = parse_msg_time(m)
    if not dt: return None
    return round((ref - dt).total_seconds() / 3600.0, 2)

_PLANISH = re.compile(r'\b(to-?do|todo|plan|hourly (check|routine)|next steps?|still open|action items?)\b', re.I)

def annotate_messages_staleness(msgs, ref=None):
    """Add is_latest, age_hours, historical, superseded and sent_by_bot to each message. Newest last.
    sent_by_bot: a user message whose text starts with the origin bracket ([sent by Grok Bot...]) was typed by this
    plugin on behalf of a bot; any other user message was typed by a person."""
    ref = ref or now()
    if not msgs: return msgs
    latest_idx = msgs[-1]['index']
    for m in msgs:
        if m.get('role') == 'user':
            m['sent_by_bot'] = is_bot_sent(m.get('text'))
            if m['sent_by_bot']: _summary_without_origin(m)
        m['is_latest'] = (m['index'] == latest_idx)
        m['age_hours'] = age_hours_of(m, ref)
        if m['is_latest']:
            m['historical'] = False
            m['superseded'] = False
        else:
            m['historical'] = True
            # Flag older to-dos/plans that a later message replaced
            m['superseded'] = bool(_PLANISH.search(m.get('text') or '') or _PLANISH.search(m.get('summary') or ''))
    return msgs

def _summary_without_origin(m):
    """A bot-sent message's app heading starts with the origin bracket, so its summary would be just
    '[sent by Grok Bot]'. Summaries describe the request itself; the bracket stays in `text` and `sent_by_bot`."""
    sm = m.get('summary')
    if sm is None or is_bot_sent(sm):
        body = origin_body(m.get('text'))
        if not body.strip(): body = origin_body(sm)
        first = next((ln.strip() for ln in body.split('\n') if ln.strip()), '')
        m['summary'] = first[:300] or None
    return m

# Releases before 1.0 named the person in the vocabulary (status 'waiting_on_<first name>', waiting_on '<first name>').
# Their on-disk caches still hold those words; map them to the current ones when serving cached rows.
_STATUSES = ('working', 'waiting_on_user', 'paused', 'done', 'idle')
_WAITING_ON = ('claude', 'user', 'permission', 'background_watcher', None)

def normalize_state(cs):
    """Rewrite a current_state dict read from an old cache to the current vocabulary, in place, so one result never mixes
    'waiting_on_<name>' (old) and 'waiting_on_user' (new)."""
    if not isinstance(cs, dict): return cs
    for k in ('status', 'sidebar_suggested_status'):
        v = cs.get(k)
        if isinstance(v, str) and v.startswith('waiting_on_') and v not in _STATUSES: cs[k] = 'waiting_on_user'
    if 'waiting_on' in cs and cs['waiting_on'] not in _WAITING_ON: cs['waiting_on'] = 'user'
    return cs

def normalize_thread_rows(rows):
    for t in rows or []:
        if isinstance(t, dict): normalize_state(t.get('current_state'))
    return rows

def build_current_state(msgs, busy=False, thread_meta=None, info=None, ref=None, permission_cards=None):
    """Prefer live signals over the sidebar group.

    status: working | waiting_on_user | paused | done | idle
    waiting_on: 'claude' | 'user' | 'background_watcher' | None
    """
    ref = ref or now()
    meta = thread_meta or {}
    group = (meta.get('group') or '') or ''
    status = (meta.get('status') or '') or ''
    combined = f'{group} {status}'.lower()
    latest = msgs[-1] if msgs else None
    latest_role = (latest.get('role') if latest else None) or None
    text_l = ''
    if latest:
        text_l = ((latest.get('text') or '') + '\n' + (latest.get('summary') or '')).lower()

    def sidebar_status():
        if 'waiting on you' in combined or 'needs attention' in combined:
            return 'waiting_on_user'
        if 'paused' in combined or 'failed' in combined:
            return 'paused'
        if 'resolved' in combined or group.lower() == 'done' or status.lower() == 'done':
            return 'done'
        if 'working' in combined:
            return 'working'
        if 'idle' in combined or 'queued' in combined:
            return 'idle'
        return None

    sidebar_suggested = sidebar_status()

    asks_user = bool(latest_role == 'assistant' and (
        sidebar_suggested == 'waiting_on_user'
        or any(p in text_l for p in (
            'waiting on you', 'needs your', 'need you to', 'can you confirm',
            'please confirm', 'let me know', 'your call', 'what should i',
            'should i proceed', 'do you want', 'want me to', 'please reply',
            'awaiting your', 'over to you',
        ))
    ))
    mentions_watcher = bool(latest_role == 'assistant' and any(p in text_l for p in (
        'watcher', 'background poller', 'poller', 'wakes this thread',
        'wakes us', 'wake this thread', 'automatic watcher',
        'without spending tokens while nothing happens',
        'queue is now watched', 'watched without spending',
    )))

    # Live-signal preference
    if busy:
        st = 'working'
    elif latest_role == 'assistant':
        if asks_user:
            st = 'waiting_on_user'
        elif sidebar_suggested == 'done':
            st = 'done'
        elif sidebar_suggested == 'paused':
            st = 'paused'
        else:
            # Assistant finished a turn and is not typing -> idle even if sidebar still says Working
            st = 'idle'
    elif latest_role == 'user':
        # User spoke; Claude has not answered. Not live-working yet.
        if sidebar_suggested == 'waiting_on_user':
            st = 'waiting_on_user'
        elif sidebar_suggested in ('done', 'paused'):
            st = sidebar_suggested
        else:
            st = 'idle'
    else:
        st = sidebar_suggested or 'idle'

    # waiting_on: who/what must move next
    if busy or (latest_role == 'user' and st not in ('waiting_on_user', 'done', 'paused')):
        waiting_on = 'claude'
    elif st == 'waiting_on_user' or asks_user:
        waiting_on = 'user'
    elif mentions_watcher and st in ('idle', 'paused'):
        waiting_on = 'background_watcher'
    else:
        waiting_on = None

    # Auto permission mode: a visible permission card (or a reply that stalls asking for an OK) blocks the thread.
    perm_hint = PERM.stalled_on_permission_text(text_l) if (latest_role == 'assistant' and not busy) else None
    if permission_cards:
        st = 'waiting_on_user'; waiting_on = 'permission'
    elif perm_hint:
        st = 'waiting_on_user'; waiting_on = 'permission'

    summary = None; t_local = None; age_h = None
    if latest:
        summary = (latest.get('summary') or (latest.get('text') or '').split('\n')[0])[:240]
        t_local = latest.get('time_local'); age_h = latest.get('age_hours')
        if age_h is None: age_h = age_hours_of(latest, ref)

    note = 'Report current_state and the latest live message only; older messages are historical/superseded.'
    disagree = bool(sidebar_suggested and sidebar_suggested != st)
    if disagree:
        note = (note + f' Sidebar group/status still say {sidebar_suggested!r} (group={group!r}, status={status!r}), '
                f'but live signals (claude_working={bool(busy)}, latest_role={latest_role!r}) say {st!r}.')

    out = {
        'status': st,
        'summary': summary,
        'latest_message_index': latest['index'] if latest else None,
        'latest_role': latest_role,
        'latest_sent_by_bot': bool(latest_role == 'user' and is_bot_sent((latest or {}).get('text'))),
        'time_local': t_local,
        'age_hours': age_h,
        'claude_working': bool(busy),
        'waiting_on': waiting_on,
        'thread_group': meta.get('group'),
        'thread_status': meta.get('status'),
        'sidebar_group': group or None,
        'note': note,
    }
    if disagree:
        out['sidebar_disagrees'] = True
        out['sidebar_suggested_status'] = sidebar_suggested
    out['permission_pending'] = bool(permission_cards or perm_hint)
    if permission_cards:
        out['permission_cards'] = PERM.public(permission_cards)
    if perm_hint and not permission_cards:
        out['permission_hint'] = perm_hint
    if out['permission_pending']:
        out['note'] += (' The thread is blocked on a permission decision (Projects always run in Auto mode, but cards and '
                        '"may I proceed?" replies still stop it). Follow the user policy (claude_policy_get): approve what is '
                        'clearly inside the task with claude_thread_permission (cards) or a reply (prose); surface anything questionable.')
    return out
def vnc_viewers():
    ports = [int(x) for x in (CFG.get('viewer_ports') or [])]
    if not ports: return 0
    flt = '( ' + ' or '.join(f'sport = :{x}' for x in ports) + ' )'
    try: o = subprocess.run(['ss', '-Htn', 'state', 'established', flt], capture_output=True, text=True, timeout=10).stdout
    except Exception: return 0
    return len([l for l in o.splitlines() if l.strip()])
def x_idle_min():
    import ctypes, ctypes.util
    try:
        x11 = ctypes.CDLL(ctypes.util.find_library('X11') or 'libX11.so.6'); xss = ctypes.CDLL(ctypes.util.find_library('Xss') or 'libXss.so.1')
        class Info(ctypes.Structure): _fields_ = [('window', ctypes.c_ulong), ('state', ctypes.c_int), ('kind', ctypes.c_int), ('til_or_since', ctypes.c_ulong), ('idle', ctypes.c_ulong), ('eventMask', ctypes.c_ulong)]
        x11.XOpenDisplay.restype = ctypes.c_void_p; x11.XDefaultRootWindow.restype = ctypes.c_ulong; x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        xss.XScreenSaverAllocInfo.restype = ctypes.POINTER(Info); xss.XScreenSaverQueryInfo.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(Info)]
        dpy = x11.XOpenDisplay(os.environ['DISPLAY'].encode())
        if not dpy: return None
        i = xss.XScreenSaverAllocInfo()
        if not xss.XScreenSaverQueryInfo(dpy, x11.XDefaultRootWindow(dpy), i): return None
        v = i.contents.idle / 60000.0; x11.XCloseDisplay(ctypes.c_void_p(dpy)); return round(v, 1)
    except Exception: return None
def _note_own_input(kind='xdo'):
    """Record that claude-projects itself just drove the display (so background idle guard can ignore it)."""
    try:
        (CACHE/'last_own_input.json').write_text(json.dumps({'at': now().isoformat(), 'kind': kind, 'pid': os.getpid()}))
    except Exception: pass

def xdo(*args, inp=None):
    env = _tool_env()
    r = subprocess.run([str(XDO), *args], capture_output=True, text=True, env=env, timeout=120, input=inp)
    # Key/type/click/mousemove reset XScreenSaver idle; mark them as our own so refresh does not skip.
    if args and args[0] in ('key', 'type', 'click', 'mousemove', 'mousedown', 'mouseup', 'windowactivate'):
        _note_own_input(args[0])
    return r.stdout.strip()

def last_own_input_age_min():
    try:
        d = json.loads((CACHE/'last_own_input.json').read_text())
        at = datetime.datetime.fromisoformat(d['at'])
        if at.tzinfo is None: at = at.replace(tzinfo=datetime.timezone.utc)
        return (now() - at).total_seconds() / 60.0
    except Exception: return None

def effective_user_idle_min():
    """X idle minutes, but if the most recent input was claude-projects's own xdotool, treat the user as idle enough.
    Prevents background refresh from skipping for 5 min after every claude-projects read."""
    idle = x_idle_min()
    if idle is None: return None
    own = last_own_input_age_min()
    if own is None: return idle
    # If X idle ≈ time since our last input, that input was ours (nobody has touched the display since).
    if abs(idle - own) <= 1.0 or idle >= max(0.0, own - 0.25):
        return max(idle, own, 5.0)  # report as >=5 so the <5 guard does not fire
    return idle

# ---------------------------------------------------------------- UI lock (one UI action at a time)
_tlock = threading.Lock()
UI_LOCK_DEFAULT_TIMEOUT = int(os.environ.get('CLAUDE_PROJECTS_UI_LOCK_TIMEOUT', '600'))

def _read_ui_holder():
    try: return json.loads((ROOT/'ui.holder').read_text())
    except Exception: return None

def ui_lock_holder():
    """The live holder of the UI lock, or None (a holder file left by a killed process doesn't count)."""
    h = _read_ui_holder()
    if not h: return None
    try: os.kill(int(h.get('pid')), 0)
    except ProcessLookupError: return None
    except Exception: pass
    return h
def deploy_busy():
    """{'busy', 'lock_holder', 'jobs_running'}: busy while the UI lock is held or any background job runs. Deploys wait for busy=false."""
    h = ui_lock_holder()
    try: jr = jobs_running()
    except Exception as ex: jr = []; log(f'jobs_running failed: {ex!r}')
    return {'busy': bool(h) or bool(jr), 'lock_holder': h, 'jobs_running': jr or []}
def _busy_detail(wait_s, prefix):
    """Busy message with holder pid/op/since + wait_s so callers can queue/diagnose."""
    h = _read_ui_holder(); bits = [f'{prefix} for >{wait_s:.0f}s']
    if h:
        bits.append(f"holder={{pid={h.get('pid')}, op={h.get('op')!r}, since={h.get('since')}}}")
    else:
        bits.append('holder=none')
    bits.append(f'wait_s={wait_s:.1f}')
    bits.append('retry later')
    return '; '.join(bits)

@contextlib.contextmanager
def ui_lock(op, force=False, timeout=None):
    """Serialize UI work through CFG['ui_lock_file'] (holders unlink the file on exit, so re-check the inode).
    Default timeout 600s (override via timeout= or CLAUDE_PROJECTS_UI_LOCK_TIMEOUT) so multi-bot callers queue behind long send/settle."""
    if timeout is None: timeout = UI_LOCK_DEFAULT_TIMEOUT
    if vnc_viewers() and not force:
        raise Refused(f'a VNC/noVNC viewer is connected to {os.environ.get("DISPLAY")} ({OPERATOR} may be using the app); retry later or pass force=true')
    t0 = time.time(); logged_wait = False
    if not _tlock.acquire(timeout=timeout):
        raise Busy(_busy_detail(time.time() - t0, 'another claude-projects operation in this process is running'))
    f = None
    try:
        while True:
            waited = time.time() - t0
            if waited > 2 and not logged_wait:
                h = _read_ui_holder()
                log(f'lock wait >2s for {op!r} waiter_pid={os.getpid()} holder={h}')
                logged_wait = True
            f = open(LOCKP, 'a+')
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try: same = os.stat(LOCKP).st_ino == os.fstat(f.fileno()).st_ino
                except FileNotFoundError: same = False
                if same: break
                fcntl.flock(f, fcntl.LOCK_UN)
            except OSError: pass
            f.close(); f = None
            if time.time() - t0 > timeout:
                raise Busy(_busy_detail(time.time() - t0, 'the app is busy with another claude-projects run'))
            time.sleep(1)
        (ROOT/'ui.holder').write_text(json.dumps({'pid': os.getpid(), 'op': op, 'since': now().isoformat()}))
        log(f'lock acquired for {op} (waited {time.time()-t0:.1f}s)')
        yield
    finally:
        if f:
            try: (ROOT/'ui.holder').unlink()
            except Exception: pass
            try: LOCKP.unlink()
            except Exception: pass
            try: fcntl.flock(f, fcntl.LOCK_UN); f.close()
            except Exception: pass
        _tlock.release()

# ---------------------------------------------------------------- IndexedDB snapshot (no UI)
IDB_RESOLVE_MAX_AGE = float(os.environ.get('CLAUDE_PROJECTS_IDB_RESOLVE_MAX_AGE', '30'))

def _idb_cached(newer_than):
    """The last snapshot if it was written at or after `newer_than` (epoch seconds), else None."""
    p = CACHE/'idb.json'
    try:
        if p.stat().st_mtime >= newer_than: return json.loads(p.read_text())
    except Exception: pass
    return None

def idb_snapshot(max_age=0.0):
    """Read the app's IndexedDB (copy + full deserialize in a child process: the heaviest memory step in claude-projects).
    One reader at a time across processes: callers that queued behind a reader reuse its result instead of starting
    their own, since it is at least as fresh as what they asked for. max_age > 0 also accepts a snapshot that recent."""
    t_req = time.time()
    if max_age > 0 and (d := _idb_cached(t_req - max_age)) is not None: return d
    CACHE.mkdir(parents=True, exist_ok=True)
    with open(CACHE/'idb.lock', 'a+') as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        # another process finished a read while we waited for the lock: use it
        if (d := _idb_cached(t_req - max(0.0, max_age))) is not None: return d
        r = subprocess.run(cfg_mod.module_argv('claude_projects.idb', IDB_PY), capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            time.sleep(3); r = subprocess.run(cfg_mod.module_argv('claude_projects.idb', IDB_PY), capture_output=True, text=True, timeout=120)
        if r.returncode != 0: raise AppError('could not read the app cache: ' + (r.stderr.strip().splitlines() or ['?'])[-1][:200])
        d = json.loads(r.stdout)
        t = CACHE/'idb.json.tmp'; t.write_text(json.dumps(d, ensure_ascii=False)); t.replace(CACHE/'idb.json')
        return d
def project_rows(snap, include_archived=False):
    rows = []
    for c in snap.get('channels') or []:
        if not c.get('id', '').startswith('chan_'): continue
        archived = bool(c.get('archived_at')); disabled = bool(c.get('disabled_at'))
        if (archived or disabled) and not include_archived: continue
        cache = read_cache(c['id'])
        rows.append({'name': c.get('name'), 'id': c['id'], 'primary_thread_id': c.get('overview_session_id'),
                     'last_activity': c.get('last_message_at'), 'last_activity_local': ct(c.get('last_message_at')),
                     'status': 'archived' if archived else 'disabled' if disabled else ('unread' if c.get('has_unread') else 'active'),
                     'has_unread': bool(c.get('has_unread')), 'created_at': c.get('created_at'), 'visibility': c.get('visibility'),
                     'icon': c.get('icon'), 'color': c.get('color'),
                     'threads_waiting_on_you': (cache or {}).get('threads_waiting'), 'last_read_local': ct((cache or {}).get('read_at'))})
    rows.sort(key=lambda r: r['last_activity'] or '', reverse=True); return rows
def resolve_project(q, snap=None):
    if snap is None:
        # A recent snapshot is enough to map a name or id to a project; a miss falls back to a fresh read
        # (a project created or renamed in the last few seconds).
        try: return _resolve_in(q, idb_snapshot(max_age=IDB_RESOLVE_MAX_AGE))
        except NotFound: snap = idb_snapshot()
    return _resolve_in(q, snap)
def _resolve_in(q, snap):
    rows = project_rows(snap, include_archived=True); qq = norm(q).casefold()
    for r in rows:
        if r['primary_thread_id']: _OVERVIEW[r['id']] = r['primary_thread_id']
    for test in (lambda r: r['id'] == q or r['primary_thread_id'] == q,
                 lambda r: norm(r['name']).casefold() == qq,
                 lambda r: slug(r['name']) == slug(q),
                 lambda r: norm(r['name']).casefold().startswith(qq),
                 lambda r: qq in norm(r['name']).casefold()):
        hit = [r for r in rows if test(r)]
        if len(hit) == 1: return hit[0]
        if len(hit) > 1: raise NotFound(f'project {q!r} is ambiguous: ' + ', '.join(r['name'] for r in hit[:8]))
    raise NotFound(f'no project matches {q!r}; known: ' + ', '.join(r['name'] for r in rows[:30]))
def cache_key(chan, thread=None): return CACHE/(chan + (('.' + thread) if thread else '') + '.json')
def read_cache(chan, thread=None):
    try: return json.loads(cache_key(chan, thread).read_text(encoding='utf-8'))
    except Exception: return None
def write_cache(chan, thread, data):
    p = cache_key(chan, thread); t = p.with_suffix('.tmp'); t.write_text(json.dumps(data, ensure_ascii=False, indent=1)); t.replace(p)

# ---------------------------------------------------------------- AT-SPI
_A = None
def A():
    global _A
    if _A is None:
        import gi; gi.require_version('Atspi', '2.0'); from gi.repository import Atspi; _A = Atspi
    return _A
def nm(o):
    try: return o.get_name() or ''
    except Exception: return ''
def role(o):
    try: return o.get_role_name()
    except Exception: return ''
def kids(o):
    try: n = o.get_child_count()
    except Exception: return []
    out = []
    for i in range(min(n, 2000)):
        try: c = o.get_child_at_index(i)
        except Exception: c = None
        if c is not None: out.append(c)
    return out
def has_state(o, st):
    try: return o.get_state_set().contains(getattr(A().StateType, st))
    except Exception: return False
def text_of(o):
    x = nm(o)
    if x: return x
    try:
        n = A().Text.get_character_count(o); return A().Text.get_text(o, 0, min(n, 20000)) if n else ''
    except Exception: return ''
def win():
    for app in kids(A().get_desktop(0)):
        if nm(app) == 'claude-desktop':
            for f in kids(app):
                if role(f) == 'frame': return f
    raise AppError('Claude desktop window not found (is the claude-projects-desktop service running? claude-projects setup status)')
def walk(o, maxn=60000):
    st = [o]; n = 0
    while st and n < maxn:
        e = st.pop(); n += 1; yield e
        st.extend(reversed(kids(e)))
def main_doc():
    for e in walk(win()):
        if role(e) == 'document web':
            try: u = e.get_document_iface().get_document_attribute_value('URI') or ''
            except Exception: u = ''
            if u.startswith('https://'): return e, u
    return None, ''
def route():
    import urllib.parse
    _, u = main_doc(); p = urllib.parse.urlsplit(u); return p.path + (('?' + p.query) if p.query else '')
def find(root, r=None, name=None, pred=None):
    for e in walk(root):
        if (r is None or role(e) == r) and (name is None or nm(e) == name) and (pred is None or pred(e)): return e
def find_all(root, r=None, pred=None): return [e for e in walk(root) if (r is None or role(e) == r) and (pred is None or pred(e))]
def act(e, what=''):
    try:
        ac = e.get_action_iface()
        if ac and ac.get_n_actions() > 0: ac.do_action(0); return True
    except Exception: pass
    raise AppError(f'cannot press {role(e)} {nm(e)!r} {what}')
def deeplink(link):
    if not link.startswith('claude://'): raise ValueError(link)
    # the launcher reads the config file through python twice unless these are set; pass them so a deep link costs one exec
    env = dict(os.environ, CLAUDE_PROJECTS_DISPLAY=str(CFG.get('display') or os.environ.get('DISPLAY') or ':99'), CLAUDE_PROJECTS_CLAUDE_BINARY=str(CFG.get('claude_binary') or 'claude-desktop'))
    r = subprocess.run([str(LAUNCH), link], capture_output=True, text=True, timeout=90, env=env); log(f'deeplink {link} rc={r.returncode}')
    _mark('deeplink')
def wait_route(pred, secs=25):
    t0 = time.time()
    while time.time() - t0 < secs:
        u = route()
        if pred(u): _mark('route'); return u
        time.sleep(.2)
    u = route(); _mark('route'); return u
def dialogs():
    d, _ = main_doc()
    return [text_of(e)[:120] or nm(e) for e in find_all(d, pred=lambda e: role(e) in ('dialog', 'alert', 'alert dialog'))] if d else []
def go_home():
    if re.fullmatch(r'/epitaxy/?(\?.*)?', route() or ''): return True
    deeplink('claude://code/new?source=desktop_action')
    ok = re.fullmatch(r'/epitaxy/?(\?.*)?', wait_route(lambda u: re.fullmatch(r'/epitaxy/?(\?.*)?', u or '') is not None, 20) or '') is not None
    _mark('go_home'); return ok
def screenshot(path):
    from PIL import ImageGrab
    im = ImageGrab.grab(xdisplay=os.environ['DISPLAY']).convert('RGB'); p = pathlib.Path(path).expanduser(); p.parent.mkdir(parents=True, exist_ok=True)
    im.save(p, quality=75); return str(p)

# ---------------------------------------------------------------- navigation + transcript parsing
def is_primary_route(u, chan):
    u = u or ''; path, _, q = u.partition('?')
    if path.rstrip('/') != f'/epitaxy/project/{chan}': return False
    m = re.search(r'thread=([\w]+)', q)
    return not m or m.group(1).startswith(('session_', 'cse_'))
_OVERVIEW = {}
def open_target(chan, thread=None):
    if thread:
        deeplink(f'claude://code/project/{chan}?thread={thread}')
        u = wait_route(lambda u: f'thread={thread}' in (u or ''), 25); ok = f'thread={thread}' in (u or '')
        if not ok:
            # Never silently fall back to the primary thread when a child/id was requested.
            raise NotFound(f'thread {thread!r} was not found or did not open in project {chan} (route is {u!r}). '
                           f'Pass a live cmsg_ id or exact title from claude_project_get; there is no fallback to primary.')
        # Defensive: if we asked for a cmsg_ child, refuse a primary (cse_/session_) route even if the query string matched oddly.
        if str(thread).startswith('cmsg_') and is_primary_route(u, chan):
            raise NotFound(f'thread {thread!r} resolved to the primary thread (route {u!r}); refusing silent fallback')
        # The app keeps ?thread=<id> in the URL even for unknown ids — verify against the sidebar list.
        if str(thread).startswith(('cmsg_', 'session_')):
            known = list((read_cache(chan) or {}).get('threads') or [])
            if not any(x.get('thread_id') == thread for x in known) and _thread_view_real(thread):
                # not in the cached sidebar (a new thread), but the pane shows it with a real title: accept it without
                # toggling and parsing the Threads sidebar, which cost 10-20 s on a cold open
                log(f'thread {thread} not in the cached sidebar; the open pane shows it, accepted'); _mark('verify_thread')
                return u
            if not any(x.get('thread_id') == thread for x in known):
                try:
                    settle(thread, 8); live = parse_threads(chan, True, True) or []; ensure_threads_panel(False)
                    if live:
                        known = live
                        c = read_cache(chan) or {}; c['threads'] = known
                        if c.get('project'): write_cache(chan, None, c)
                except Exception as ex:
                    log(f'thread verify sidebar: {ex}')
            if known and not any(x.get('thread_id') == thread for x in known):
                raise NotFound(f'thread {thread!r} is not in project {chan} (not in the Threads sidebar). '
                               f'No fallback to primary. Known: ' + ', '.join((x.get("title") or x.get("thread_id") or "?")[:40] for x in known[:8]))
            if not known:
                info = pane_info(thread)
                if (info.get('thread_title') or '').strip() in ('', 'Thread') and not msg_panel(thread):
                    raise NotFound(f'thread {thread!r} did not open a real thread view (title={info.get("thread_title")!r})')
            _mark('verify_thread')
    else:
        ov = _OVERVIEW.get(chan)
        # the app re-opens the last viewed thread; ?thread=<overview cse_ id> forces the primary thread
        deeplink(f'claude://code/project/{chan}' + (f'?thread={ov}' if ov else ''))
        u = wait_route(lambda u: is_primary_route(u, chan), 25); ok = is_primary_route(u, chan)
        if not ok: raise AppError(f'the app did not open primary of {chan} (route is {u!r}; signed out or project removed?)')
    return u
def _thread_view_real(thread, secs=6):
    """The child-thread pane is open with a real title and a transcript panel (an unknown id shows neither)."""
    t0 = time.time()
    while time.time() - t0 < secs:
        try:
            if msg_panel(thread) is not None and (pane_info(thread).get('thread_title') or '').strip() not in ('', 'Thread'): return True
        except Exception as ex: log(f'thread view check: {ex!r}'); return False
        time.sleep(.4)
    return False
PANEL = {None: 'Chat messages', 'thread': 'Thread messages'}
def msg_panel(thread):
    d, _ = main_doc()
    want = 'Thread messages' if thread else 'Chat messages'
    return find(d, 'panel', want) if d else None
SETTLE_POLL_S = float(os.environ.get('CLAUDE_PROJECTS_SETTLE_POLL_S', '0.4'))
SETTLE_STABLE_S = float(os.environ.get('CLAUDE_PROJECTS_SETTLE_STABLE_S', '0.8'))
def settle(thread, maxs=25):
    """wait until the transcript panel exists, 'Loading...' is gone and the message count is stable"""
    t0 = time.time(); prev = None; since = t0; seen = 0
    while time.time() - t0 < maxs:
        p = msg_panel(thread)
        if p:
            arts = [a for a in kids_deep_articles(p)]; loading = any(nm(e) == 'Loading...' or text_of(e) == 'Loading...' for e in kids(p))
            sig = (len(arts), nm(arts[-1]) if arts else '')
            if arts and not loading and sig == prev:
                seen += 1
                # stable = the same count and last message seen on 2 more polls, at least SETTLE_STABLE_S apart in all
                if seen >= 2 and time.time() - since >= SETTLE_STABLE_S: _mark('settle'); return True
            else: prev, since, seen = sig, time.time(), 0
        time.sleep(SETTLE_POLL_S)
    ok = msg_panel(thread) is not None; _mark('settle'); return ok
def kids_deep_articles(p): return [e for e in walk(p, 20000) if role(e) == 'article' and re.match(r'^Message \d+$', nm(e))]
TIME_RE = re.compile(r'^(\d{1,2}:\d{2}\s?[AP]M|\d+\s?[smhdw]( ago)?|\d+ (sec|min|hour|day|week)s? ago|just now|now|yesterday|[A-Z][a-z]+day, [A-Z][a-z]+ \d{1,2}, \d{4}( at \d{1,2}:\d{2}\s?[AP]M)?)$')
CHROME = re.compile(r'^(Show message actions for .*|Load the rest of this message|Show more|Show less|Add reaction|Reply|Copy message|Edit message|More options|Retry|Download .*|Use the up and down arrow keys.*|Output list available.*|Quick reply and approval controls.*|Arrow keys move the tile.*|\d+ outputs? added by Claude)$')
def parse_article(a, expand=False, include_images=False, markdown=False):
    if expand:
        for b in find_all(a, 'push button', lambda e: nm(e) in ('Load the rest of this message', 'Show more')):
            try: act(b)
            except Exception: pass
        if expand: time.sleep(.6)
    groups = []; times = []; heading = ''; truncated = False; links = []
    def rec(o, parent_key, depth):
        nonlocal heading, truncated
        r = role(o); ch = kids(o)
        if r == 'heading' and not heading: heading = norm(text_of(o))
        if r == 'push button' and nm(o) in ('Load the rest of this message', 'Show more'): truncated = True; return
        if r == 'link':
            try:
                u = o.get_hyperlink().get_uri(0)
                if u and u.startswith('http') and u not in links: links.append(u)
            except Exception: pass
        if r in ('static', 'text') and not ch:
            x = text_of(o)
            if not x.strip(): return
            if TIME_RE.match(x.strip()): times.append(x.strip()); return
            if CHROME.match(x.strip()): return
            if groups and groups[-1][0] == parent_key: groups[-1][1].append(x)
            else: groups.append([parent_key, [x]])
            return
        if r == 'push button' and not ch:
            x = nm(o).strip()
            if x and not CHROME.match(x): groups.append([('btn', id(o)), ['[' + x + ']']])
            return
        if r == 'heading': return  # heading text duplicates the body
        for i, c in enumerate(ch): rec(c, (parent_key, i) if r not in ('static', 'link', 'push button') else parent_key, depth + 1)
    for i, c in enumerate(kids(a)): rec(c, (i,), 0)
    body = ''
    for g in groups:
        t = norm(''.join(g[1]))
        if not t: continue
        if not body: body = t
        elif re.search(r'[.!?:;)\]]$', body) or t.startswith(('-', '*', '•', '#')) or re.match(r'^\d+[.)] ', t): body += '\n' + t
        else: body += ('' if t[0] in '.,;:)' else ' ') + t
    if heading.startswith('You said:'): who = 'user'
    elif heading.startswith('Claude responded:'): who = 'assistant'
    else:
        m0 = re.match(r'^(You|Claude)\b[\s:]*', body)
        who = 'user' if (m0 and m0.group(1) == 'You') else 'assistant'
        if m0: body = body[m0.end():]
        if 'Forwarded from project chat' in body: body = body.replace('Forwarded from project chat', '').strip() + '\n(forwarded from the project chat)'
    mm = re.match(r'^Message (\d+)$', nm(a) or '')
    if not mm: raise AppError(f'article name not Message N: {nm(a)!r}')
    n = int(mm.group(1))
    dt = None
    for e in walk(a, 4000):
        if _tag(e) == 'time':
            dt = _attrs(e).get('datetime') or None
            if dt: break
    m = {'index': n, 'role': who, 'text': body[:12000], 'summary': re.sub(r'^(You said|Claude responded): ', '', heading)[:300] or None,
         'time_app': ' / '.join(dict.fromkeys(times)) or None, 'time_local': ct(dt) if dt else app_time_local(times), 'time_utc': dt, 'truncated_in_app': truncated, 'links': links[:20] or None}
    try:
        R, md = rich_of(a, n)
        m['attachments'] = R.atts or None; m['blocks'] = R.blocks or None
        if markdown: m['markdown'] = md[:20000]
        if include_images:
            for att, el in zip(R.atts, R.elems):
                if att['kind'] == 'image' and el is not None:
                    tb = thumb_b64(el)
                    if tb: att['thumbnail'] = tb
    except Exception as ex: log(f'rich parse error {ex}')
    return m
def app_time_local(times):
    """The app renders times in its host's zone (CLAUDE_PROJECTS_APP_TIMEZONE). Convert the fullest one to the display zone."""
    for t in times:
        m = re.match(r'^[A-Z][a-z]+day, ([A-Z][a-z]+ \d{1,2}, \d{4}) at (\d{1,2}:\d{2}\s?[AP]M)$', t)
        if m:
            try: return ct(datetime.datetime.strptime(m.group(1) + ' ' + m.group(2).replace(' ', ''), '%B %d, %Y %I:%M%p').replace(tzinfo=APP_TZ).isoformat())
            except Exception: pass
    for t in times:
        m = re.match(r'^(\d{1,2}:\d{2})\s?([AP]M)$', t); dm = next((re.match(r'^[A-Z][a-z]+day, ([A-Z][a-z]+ \d{1,2}, \d{4})$', x) for x in times if re.match(r'^[A-Z][a-z]+day, [A-Z][a-z]+ \d{1,2}, \d{4}$', x)), None)
        if m and dm:
            try: return ct(datetime.datetime.strptime(dm.group(1) + ' ' + m.group(1) + m.group(2), '%B %d, %Y %I:%M%p').replace(tzinfo=APP_TZ).isoformat())
            except Exception: pass
    return None
def busy_state(thread=None):
    """True if Claude is visibly still responding in the open transcript (Stop button, or a working indicator after the last message)."""
    d, _ = main_doc()
    if not d: return False
    for e in walk(d, 20000):
        if role(e) == 'push button' and re.match(r'^(Stop|Stop response|Stop generating|Interrupt|Cancel response)$', nm(e)): return True
    p = msg_panel(thread)
    if not p: return False
    arts = kids_deep_articles(p); tail = []
    started = not arts
    for e in walk(p, 20000):
        if arts and e == arts[-1]: started = True; continue
        if started and role(e) in ('static', 'status bar', 'progress bar'):
            tail.append(text_of(e).strip())
    return any(re.match(r'^(Thinking|Working|Claude is (thinking|working)|Responding|Running .*)…?\.{0,3}$', t) for t in tail)
def pane_info(thread):
    d, _ = main_doc(); info = {}
    if not d: return info
    for e in walk(d, 20000):
        x = nm(e); r = role(e)
        if (r in ('push button', 'static', 'text', 'label', 'status bar')
                and x and (x.startswith('Running on ') or re.match(r'^Running on .+offline', x, re.I))):
            info['environment'] = x
        # some builds expose offline as a separate status string nearby
        if r in ('static', 'text', 'push button', 'status bar') and re.search(r'\boffline\b', x or '', re.I) and 'environment' in info:
            if 'offline' not in info['environment'].lower():
                info['environment'] = info['environment'].rstrip('.') + ', which is offline'
        if r == 'toggle button' and x.startswith('Threads'): info['threads_button'] = x
        if r == 'push button' and x in ('Resolve', 'Reopen', 'Resolved'): info['resolve_button'] = x
        if r == 'tool bar' and x.endswith(' controls') and thread: info.setdefault('thread_title', clean_thread_title(x[:-9]))
    m = re.search(r'(\d+) threads? waiting on you', info.get('threads_button', ''))
    info['threads_waiting'] = int(m.group(1)) if m else (0 if 'threads_button' in info else None)
    return info
def read_messages(thread, max_messages=20, history_pages=0, expand=False, include_images=False, markdown=False):
    p = msg_panel(thread)
    if not p: raise AppError('transcript panel not found (page still loading or layout changed)')
    seen = {}
    def grab():
        for a in kids_deep_articles(msg_panel(thread) or p):
            try: m = parse_article(a, expand, include_images, markdown)
            except Exception as ex: log(f'parse error {ex}'); continue
            seen[m['index']] = m
    grab()
    for _ in range(max(0, history_pages)):
        arts = kids_deep_articles(msg_panel(thread) or p)
        if not arts: break
        first = min(arts, key=lambda a: int(nm(a).split()[1]))
        try: first.get_component_iface().grab_focus()
        except Exception: pass
        time.sleep(.3); before = min(seen) if seen else 0
        xdo('key', '--repeat', '8', '--delay', '60', 'Up'); time.sleep(1.5); grab()
        if (min(seen) if seen else 0) >= before: break
    msgs = [seen[k] for k in sorted(seen)]
    return msgs[-max_messages:] if max_messages else msgs
def composer():
    d, _ = main_doc()
    if not d: return None
    ents = find_all(d, 'entry', lambda e: has_state(e, 'EDITABLE'))
    pref = [e for e in ents if re.match(r'^(Write your (prompt|reply) to Claude|Type / for commands|Reply.*|Message .*|Ask Claude.*|Describe a task.*)', nm(e))]
    return (pref or ents or [None])[-1]
def composer_text(e):
    """Text in the ProseMirror composer: one paragraph per line (the entry itself only exposes U+FFFC embeds)."""
    if e is None: return ''
    paras = []
    for k in kids(e):
        parts = [text_of(x) for x in walk(k, 2000) if role(x) in ('static', 'text') and not kids(x)]
        if not parts and role(k) in ('static', 'text'): parts = [text_of(k)]
        paras.append(''.join(p for p in parts if p != '\n').replace('\ufffc', ''))
    if not paras:
        try:
            n = A().Text.get_character_count(e); paras = [(A().Text.get_text(e, 0, n) if n else '').replace('\ufffc', '')]
        except Exception: pass
    return '\n'.join(paras).strip('\n')

# ---------------------------------------------------------------- threads panel
def ensure_threads_panel(open_=True):
    d, _ = main_doc(); tb = find(d, 'toggle button', pred=lambda e: nm(e).startswith('Threads'))
    if not tb: return False
    on = has_state(tb, 'CHECKED') or has_state(tb, 'PRESSED') or find(d, 'landmark', 'Overview') is not None
    if on != open_: act(tb); time.sleep(2)
    return True
def parse_threads(chan, include_resolved=False, show_all=False):
    if not ensure_threads_panel(True): return []
    d, _ = main_doc(); ov = find(d, 'landmark', 'Overview') or d
    if include_resolved or show_all:
        for b in find_all(ov, 'push button', lambda e: (show_all and nm(e) == 'Show more') or (include_resolved and re.match(r'^Resolved · \d+$', nm(e)))):
            try: act(b); time.sleep(1.5)
            except Exception: pass
        ov = find(main_doc()[0], 'landmark', 'Overview') or ov
    out = []; group = None; cur = None
    for e in walk(ov, 30000):
        r = role(e); x = nm(e)
        if r in ('heading', 'push button', 'toggle button'):
            m = re.match(r'^(.+?) · (\d+)$', x)
            if m: group = m.group(1); cur = None; continue
        if r == 'check box' and x.startswith('Select '):
            cur = {'title': clean_thread_title(x[7:]), 'thread_id': None, 'group': group, 'status': None, 'age': None, 'pull_requests': []}; out.append(cur); continue
        if cur is None: continue
        if r == 'link':
            try: u = e.get_hyperlink().get_uri(0) or ''
            except Exception: u = ''
            m = re.search(r'[?&]thread=((?:cmsg|session|cse)_\w+)', u)
            if re.match(r'^\d+ pull requests?, ', x): cur['pull_requests'].append(x)
            elif m and not cur['thread_id']: cur['thread_id'] = m.group(1); cur['title'] = clean_thread_title(x or cur['title'])
        elif r == 'static' and not kids(e):
            t = text_of(e).strip()
            if t in ('Needs attention', 'Working', 'Idle', 'Resolved', 'Waiting on you', 'Failed', 'Queued', 'Paused') and not cur['status']: cur['status'] = t
            elif re.match(r'^\d+[smhdw]$|^now$', t): cur['age'] = t
    for t in out:
        if not t['pull_requests']: t['pull_requests'] = None
    return [t for t in out if t['thread_id']]
def resolve_thread(chan, q, threads=None):
    if not q or q in ('primary', 'main', 'overview'): return None
    if re.match(r'^(cmsg|session|cse)_\w+$', q): return q
    threads = threads or (read_cache(chan) or {}).get('threads') or []
    qq = norm(clean_thread_title(q)).casefold()
    cleaned = [{'title': clean_thread_title(t.get('title')), 'thread_id': t.get('thread_id'), **{k:v for k,v in t.items() if k not in ('title','thread_id')}} for t in threads]
    for test in (lambda t: norm(t['title']).casefold() == qq, lambda t: norm(t['title']).casefold().startswith(qq), lambda t: qq in norm(t['title']).casefold()):
        hit = [t for t in cleaned if t.get('title') and test(t)]
        if len(hit) == 1: return hit[0]['thread_id']
        if len(hit) > 1: raise NotFound(f'thread {q!r} is ambiguous: ' + '; '.join(t['title'] for t in hit[:8]))
    return 'LOOKUP:' + q

# ---------------------------------------------------------------- rich content: attachments, blocks, markdown
import struct, base64, mimetypes, io
HTTPC = pathlib.Path(CFG['claude_config_dir'])/'Cache/Cache_Data'          # Chromium "simple" HTTP cache of the app (read-only, copied bytes)
ATT_DIR = pathlib.Path(CFG['attachments_dir'])          # fetched attachments, named by sha256 (managed staging 'incoming')
ATT_FILE_CAP = 100 * 2**20; ATT_TOTAL_CAP = int(os.environ.get('CLAUDE_PROJECTS_ATTACH_CAP_MB', '500')) * 2**20
IMG_MAX_PX = 1600; IMG_MAX_BYTES = 1_000_000
for _m, _e in (('text/markdown', '.md'), ('application/vnd.android.package-archive', '.apk'), ('image/webp', '.webp'), ('text/x-python', '.py'),
               ('application/typescript', '.ts'), ('text/x-typescript', '.tsx'), ('application/x-yaml', '.yml'), ('application/x-yaml', '.yaml'), ('application/toml', '.toml')):
    mimetypes.add_type(_m, _e)
TEXT_EXT = set('.md .markdown .txt .log .csv .tsv .json .jsonl .yaml .yml .toml .ini .cfg .py .js .jsx .ts .tsx .mjs .cjs .go .rs .rb .java .kt .kts .swift .c .h .cc .cpp .hpp .cs .sh .bash .zsh .ps1 .sql .html .htm .css .scss .xml .svg .env .gradle .lua .php .r .tex .diff .patch .gitignore .dockerfile'.split())
def guess_mime(name):
    n = (name or '').lower(); ext = os.path.splitext(n)[1]
    if ext in ('.md', '.markdown'): return 'text/markdown'
    m = mimetypes.guess_type(n)[0]
    if m: return m
    return 'text/plain' if ext in TEXT_EXT else None
def is_image_mime(m): return bool(m) and m.startswith('image/') and m != 'image/svg+xml'
def is_text_mime(m, name=''):
    return bool(m) and (m.startswith('text/') or m in ('application/json', 'application/xml', 'application/x-yaml', 'application/toml', 'application/javascript', 'application/typescript', 'image/svg+xml')) or os.path.splitext((name or '').lower())[1] in TEXT_EXT
def _tag(o):
    try: return (o.get_attributes() or {}).get('tag', '')
    except Exception: return ''
def _attrs(o):
    try: return o.get_attributes() or {}
    except Exception: return {}
def _uri(o):
    try: return o.get_hyperlink().get_uri(0) or ''
    except Exception: return ''
def extents(o):
    try: x = o.get_component_iface().get_extents(0); return (x.x, x.y, x.width, x.height)
    except Exception: return None
def strip_q(u): return re.sub(r'[?#].*$', '', u or '')
def safe_url(u):
    """never return query strings (they can carry signed tokens) or the org id"""
    return re.sub(r'/api/[0-9a-f-]{36}/', '/api/<org>/', re.sub(r'/organizations/[0-9a-f-]{36}/', '/organizations/<org>/', strip_q(u)))

BADGE_RE = re.compile(r'^(?P<badge>[A-Z0-9]{2,5}) (?P<name>\S.*\.(?P<ext>[A-Za-z0-9]{1,10}))$')
BADGE_ALIASES = {'JPEG': ('jpg', 'jpeg'), 'JPG': ('jpg', 'jpeg'), 'TIFF': ('tif', 'tiff'), 'YAML': ('yml', 'yaml'), 'MARKDOWN': ('md',)}
def tile_file_name(label, statics=()):
    """A file tile's accessible name carries the app's type badge ("PNG tablet-light.png"): -> the file name.
    The tile's static children are the badge and the name; without them, a leading badge that matches the extension is dropped."""
    st = [x.strip() for x in statics if x and x.strip()]
    if len(st) >= 2 and re.fullmatch(r'[A-Z0-9]{2,5}', st[0]) and ' '.join(st) == label.strip() and UPLOAD_RE.match(st[-1]): return st[-1]
    m = BADGE_RE.match(label.strip())
    if m and (m['badge'].lower() == m['ext'].lower() or m['ext'].lower() in BADGE_ALIASES.get(m['badge'], ())): return m['name']
    return label
REF_RE = re.compile(r'^(?P<name>[^\s—][^—\n]*?\.[A-Za-z0-9]{1,10}) — (?P<path>\S.*)$')
UPLOAD_RE = re.compile(r'^(?!(Download|Copy|Open|View|Save|Reload|Show|Close|Remove|Delete) )[^/\\—\n\[\]]{1,200}\.[A-Za-z0-9]{1,10}$')
OUT_CHIP_RE = re.compile(r'^(?P<type>[A-Z0-9]{2,6}) (?P<name>.+\.[A-Za-z0-9]{1,10})$')
OUTPUTS_RE = re.compile(r'^\d+ outputs? added by Claude$')
PR_RE = re.compile(r'^https://github\.com/([^/]+/[^/]+)/pull/(\d+)')

class Rich:
    def __init__(self, idx=None):
        self.idx = idx; self.atts = []; self.elems = []; self.blocks = []; self.inline_code = []; self.seen_links = set()
    def add(self, a, elem=None):
        a = {k: v for k, v in a.items() if v is not None}
        a['id'] = f'{self.idx}.{len(self.atts) + 1}' if self.idx is not None else str(len(self.atts) + 1)
        self.atts.append(a); self.elems.append(elem); return a

def md_walk(o, R, ctx=None, depth=0):
    """Accessibility subtree -> markdown, collecting attachments (R.atts) and structural blocks (R.blocks)."""
    ctx = ctx or {}
    if depth > 60: return ''
    r = role(o); ch = kids(o); name = nm(o)
    if r == 'heading' and re.match(r'^(You said|Claude responded)[:\s]', name): return ''
    if r == 'landmark' and name in ('On this page',): return ''
    t = _tag(o)
    if t == 'time': return ''
    if r == 'push button' or r == 'toggle button':
        x = name.strip()
        if not x or CHROME.match(x) or x.startswith(('Show message actions', 'Zoom image')): return ''
        m = REF_RE.match(x)
        if m:
            a = R.add({'kind': 'file_ref', 'name': m.group('name').strip(), 'path_in_project': m.group('path').strip(), 'mime': guess_mime(m.group('name')),
                       'source': 'transcript: file link (opens in the app\'s file pane)'}, o)
            return f"[{a['name']}]({a['path_in_project']})"
        m = OUT_CHIP_RE.match(x)
        if ctx.get('outputs') and m:
            a = R.add({'kind': 'output_file', 'name': m.group('name'), 'type_label': m.group('type'), 'mime': guess_mime(m.group('name')),
                       'source': 'transcript: output added by Claude (opens in the output pane)'}, o)
            return f"\n[output file: {a['name']}]\n"
        if UPLOAD_RE.match(x):
            fn = tile_file_name(x, [nm(c) for c in ch if role(c) in ('static', 'text', 'label')])
            mi = guess_mime(fn); ex = extents(o)
            a = R.add({'kind': 'image' if is_image_mime(mi) or (ex and ex[3] > 60 and not mi) else 'file', 'name': fn, 'mime': mi,
                       'source': 'transcript: attachment tile (uploaded with the message)', **({'tile_label': x} if fn != x else {})}, o)
            return f"\n[attached {a['kind']}: {fn}]\n"
        return f'[{x}]' if not ch or all(role(c) in ('static', 'text', 'paragraph', 'section') for c in ch) else ''.join(md_walk(c, R, ctx, depth + 1) for c in ch)
    if r == 'link':
        u = _uri(o); txt = norm(''.join(md_walk(c, R, ctx, depth + 1) for c in ch) or name)
        if u.startswith('http') and u not in R.seen_links:
            R.seen_links.add(u); m = PR_RE.match(u)
            if m: R.add({'kind': 'pull_request', 'name': txt or f'#{m.group(2)}', 'url': strip_q(u), 'repo': m.group(1), 'number': int(m.group(2)), 'source': 'transcript: link'})
            else:
                fn = os.path.basename(strip_q(u))
                R.add({'kind': 'link', 'name': txt or u, 'url': strip_q(u), 'mime': guess_mime(fn) if '.' in fn else None, 'source': 'transcript: link'})
        return f'[{txt}]({strip_q(u)})' if u else txt
    if r in ('static', 'text') and not ch:
        x = text_of(o)
        if t == '::marker': return ''
        if CHROME.match(x.strip()) or TIME_RE.match(x.strip()) or re.match(r'^[A-Z][a-z]+day, [A-Z][a-z]+ \d{1,2}, \d{4}$', x.strip()): return ''
        if t == 'code': R.inline_code.append(x); return f'`{x}`'
        if t in ('strong', 'b'): return f'**{x}**'
        if t in ('em', 'i'): return f'*{x}*'
        return x
    if t == 'pre' or (r == 'section' and 'code-block' in _attrs(o).get('class', '')):
        lang = ''
        for e in walk(o, 3000):
            at = _attrs(e); cls = at.get('class', '') + ' ' + at.get('data-language', '') + ' ' + at.get('language', '')
            mm = re.search(r'(?:language|lang)-([\w+#.-]+)', cls) or (re.match(r'^\s*([\w+#.-]+)\s*$', at.get('data-language', '')) if at.get('data-language') else None)
            if mm: lang = mm.group(1); break
        code = ''.join(text_of(e) for e in walk(o, 20000) if role(e) in ('static', 'text') and not kids(e))
        if not lang:
            # a language label rendered just before the block (e.g. "python") or a "Copy" header row
            prev = ctx.get('prev_label')
            if prev and re.fullmatch(r'[\w+#.-]{1,20}', prev): lang = prev
        R.blocks.append({'type': 'code', 'language': lang or None, 'text': code[:20000], 'lines': code.count('\n') + 1})
        return f'\n```{lang}\n{code.rstrip()}\n```\n'
    if r == 'table' or t == 'table':
        rows = []
        for e in walk(o, 20000):
            if role(e) == 'table row':
                cells = [norm(''.join(md_walk(c, R, {}, depth + 2) for c in kids(cc)) or text_of(cc)) for cc in kids(e) if role(cc) in ('table cell', 'column header', 'row header', 'table column header', 'table row header', 'cell')]
                if cells: rows.append(cells)
        if not rows: return ''
        w = max(len(x) for x in rows); rows = [x + [''] * (w - len(x)) for x in rows]
        R.blocks.append({'type': 'table', 'rows': rows[:200], 'columns': w})
        esc = lambda s: s.replace('|', '\\|')
        out = ['| ' + ' | '.join(esc(c) for c in rows[0]) + ' |', '|' + '---|' * w] + ['| ' + ' | '.join(esc(c) for c in x) + ' |' for x in rows[1:]]
        return '\n' + '\n'.join(out) + '\n\n'
    if t in ('ul', 'ol') or r == 'list':
        ordered = t == 'ol'; items = []; ind = '  ' * ctx.get('list_depth', 0)
        for i, li in enumerate([c for c in ch if role(c) == 'list item'] or ch):
            body = ''.join(md_walk(c, R, dict(ctx, list_depth=ctx.get('list_depth', 0) + 1), depth + 1) for c in kids(li)).strip('\n')
            if not body.strip(): continue
            items.append(f"{ind}{(str(i + 1) + '.') if ordered else '-'} {body}")
        if ctx.get('list_depth', 0) == 0: R.blocks.append({'type': 'list', 'ordered': ordered, 'items': len(items)})
        return ('\n' if ctx.get('list_depth') else '\n') + '\n'.join(items) + ('\n' if ctx.get('list_depth') else '\n\n')
    if r == 'heading':
        lv = int(t[1]) if re.fullmatch(r'h[1-6]', t or '') else 3
        txt = norm(''.join(md_walk(c, R, ctx, depth + 1) for c in ch) or name)
        R.blocks.append({'type': 'heading', 'level': lv, 'text': txt[:300]})
        return f"\n{'#' * lv} {txt}\n\n"
    if t == 'blockquote':
        inner = ''.join(md_walk(c, R, ctx, depth + 1) for c in ch).strip()
        R.blocks.append({'type': 'quote', 'text': inner[:2000]})
        return '\n' + '\n'.join('> ' + l for l in inner.splitlines()) + '\n\n'
    if r == 'image':
        src = _attrs(o).get('src', ''); R.blocks.append({'type': 'image', 'alt': name or None, 'src': safe_url(src) if src.startswith('http') else ('blob' if src.startswith('blob:') else None)})
        return f'![{name}]()'
    if r in ('static', 'text') and t in ('strong', 'b', 'em', 'i', 'code', 'del', 's'):
        inner = ''.join(md_walk(c, R, ctx, depth + 1) for c in ch)
        if not inner.strip(): return inner
        if t == 'code': R.inline_code.append(inner); return f'`{inner}`'
        w = {'strong': '**', 'b': '**', 'em': '*', 'i': '*', 'del': '~~', 's': '~~'}[t]
        return f'{w}{inner}{w}'
    sub = dict(ctx)
    if r == 'panel' and OUTPUTS_RE.match(name): sub['outputs'] = True
    out = []; prev_label = None
    for c in ch:
        sub['prev_label'] = prev_label
        s = md_walk(c, R, sub, depth + 1); out.append(s)
        prev_label = norm(s) if s and '\n' not in s.strip() else None
    s = ''.join(out)
    if r == 'paragraph' or t == 'p': return s.strip() + '\n\n'
    return s
def finish_md(s): return re.sub(r'\n{3,}', '\n\n', re.sub(r'[ \t]+\n', '\n', s or '')).strip()
def rich_of(a, idx=None):
    R = Rich(idx); md = finish_md(''.join(md_walk(c, R, {}, 0) for c in kids(a)))
    if R.inline_code: R.blocks.append({'type': 'inline_code', 'items': list(dict.fromkeys(R.inline_code))[:50]})
    return R, md

# ---- Chromium simple-cache reader (the app's own HTTP cache; bytes only, never headers with cookies)
_EOF_MAGIC = 0xf4fa6f45970d41d8; _INIT_MAGIC = 0xfcfb6d1ba7725c30
def _cache_entry(path):
    b = path.read_bytes()
    if len(b) < 64 or struct.unpack('<Q', b[:8])[0] != _INIT_MAGIC: return None
    kl = struct.unpack('<I', b[12:16])[0]; key = b[24:24 + kl]
    # SimpleFileEOF = magic u64, flags u32, crc u32, stream_size u32 (+4 padding) = 24 bytes; layout: hdr key stream1 EOF1 stream0 [sha256] EOF0
    if len(b) < 24 + kl + 48: return None
    m0, flags, _crc, s0 = struct.unpack('<QIII', b[-24:-4])
    if m0 != _EOF_MAGIC: return None
    end0 = len(b) - 24 - (32 if flags & 2 else 0); st0 = end0 - s0
    if st0 - 24 < 24 + kl or struct.unpack('<Q', b[st0 - 24:st0 - 16])[0] != _EOF_MAGIC: return None
    body = b[24 + kl:st0 - 24]; hdr = b[st0:end0]
    def h(n):
        mm = re.search(rb'(?i)' + n + rb':\s*([^\x00\r\n]{1,300})', hdr); return mm.group(1).decode('latin-1').strip() if mm else None
    return {'key': key.decode('latin-1', 'replace'), 'body': body, 'content_type': h(rb'content-type'), 'content_encoding': h(rb'content-encoding'),
            'content_disposition': h(rb'content-disposition'), 'content_length': h(rb'content-length'), 'status': (re.search(rb'HTTP/[\d.]+ (\d{3})', hdr) or [None, b''])[1].decode() or None}
def _decode_body(e):
    b = e['body']; enc = (e.get('content_encoding') or '').lower()
    if enc == 'br':
        import brotli; b = brotli.decompress(b)
    elif enc in ('gzip', 'x-gzip'):
        import gzip; b = gzip.decompress(b)
    elif enc == 'deflate':
        import zlib; b = zlib.decompress(b)
    elif enc == 'zstd':
        try: import zstandard; b = zstandard.ZstdDecompressor().decompressobj().decompress(b)
        except ImportError: return None
    return b
def http_cache_get(path_sub, wait=0.0, newer_than=None):
    """Newest cached response whose URL (query ignored) contains path_sub -> dict(body=bytes, content_type, filename) or None."""
    t0 = time.time(); want = path_sub.encode()
    while True:
        hits = []
        try: files = list(HTTPC.glob('*_0'))
        except Exception: files = []
        for f in files:
            try:
                with open(f, 'rb') as fh: head = fh.read(2048)
            except Exception: continue
            if want not in head: continue
            try: mt = f.stat().st_mtime
            except Exception: continue
            hits.append((mt, f))
        for mt, f in sorted(hits, reverse=True):
            try: e = _cache_entry(f)
            except Exception: e = None
            if not e or want not in strip_q(e['key']).encode('latin-1', 'replace'): continue
            if e.get('status') and e['status'] not in ('200', '203'): continue
            body = _decode_body(e)
            if not body: continue
            if e.get('content_length') and not e.get('content_encoding') and e['content_length'].isdigit() and int(e['content_length']) != len(body): continue
            fn = None
            if e.get('content_disposition'):
                mm = re.search(r"filename\*=utf-8''([^;]+)|filename=\"?([^\";]+)", e['content_disposition'], re.I)
                if mm:
                    import urllib.parse; fn = urllib.parse.unquote(mm.group(1) or mm.group(2))
            return {'body': body, 'content_type': (e.get('content_type') or '').split(';')[0] or None, 'filename': fn, 'url': safe_url(e['key'].split('/', 2)[-1] if e['key'][:2] == '1/' else e['key']), 'cached_at': datetime.datetime.fromtimestamp(mt, datetime.timezone.utc).isoformat()}
        if time.time() - t0 >= wait: return None
        time.sleep(1)

# ---- extracted-file store
def sniff_mime(b):
    if b[:8] == b'\x89PNG\r\n\x1a\n': return 'image/png'
    if b[:3] == b'\xff\xd8\xff': return 'image/jpeg'
    if b[:4] == b'RIFF' and b[8:12] == b'WEBP': return 'image/webp'
    if b[:6] in (b'GIF87a', b'GIF89a'): return 'image/gif'
    if b[:5] == b'%PDF-': return 'application/pdf'
    if b[:2] == b'PK': return 'application/zip'
    return None
def store_bytes(data, name, mime, meta):
    if len(data) > ATT_FILE_CAP: return None, None
    ATT_DIR.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256(data).hexdigest()
    ext = os.path.splitext(name or '')[1].lower() or mimetypes.guess_extension(mime or '') or '.bin'
    if mime and is_image_mime(mime) and ext not in ('.png', '.jpg', '.jpeg', '.webp', '.gif'): ext = mimetypes.guess_extension(mime) or ext
    p = ATT_DIR/f'{sha[:32]}{ext}'
    if not p.exists():
        t = p.with_suffix(p.suffix + '.tmp'); t.write_bytes(data); t.replace(p)
    (ATT_DIR/f'{sha[:32]}.json').write_text(json.dumps(dict(meta, sha256=sha, path_on_host=str(p), size=len(data), stored_at=now().isoformat()), ensure_ascii=False))
    _att_index_put(meta, sha, str(p)); _att_prune(); return str(p), sha
def _att_index():
    try: return json.loads((ATT_DIR/'index.json').read_text())
    except Exception: return {}
def _att_key(chan, thread, idx, name): return f"{chan}|{thread or 'primary'}|{idx}|{name}"
def _att_index_put(meta, sha, path):
    if not meta.get('project_id'): return
    ix = _att_index(); ix[_att_key(meta['project_id'], meta.get('thread_id'), meta.get('message_index'), meta.get('name'))] = {'sha256': sha, 'path_on_host': path, 'source': meta.get('source'), 'mime': meta.get('mime'), 'size': meta.get('size')}
    t = ATT_DIR/'index.tmp'; t.write_text(json.dumps(ix)); t.replace(ATT_DIR/'index.json')
def _att_prune():
    fs = [f for f in ATT_DIR.iterdir() if f.is_file() and f.name not in ('index.json',) and not f.name.endswith('.json')]
    tot = sum(f.stat().st_size for f in fs)
    for f in sorted(fs, key=lambda f: f.stat().st_mtime):
        if tot <= ATT_TOTAL_CAP: break
        tot -= f.stat().st_size; f.unlink(missing_ok=True); (ATT_DIR/(f.stem + '.json')).unlink(missing_ok=True)
def annotate_known(msgs, chan, thread):
    ix = _att_index()
    for m in msgs:
        for a in m.get('attachments') or []:
            k = ix.get(_att_key(chan, thread, m['index'], a.get('name')))
            if k and os.path.exists(k['path_on_host']): a.update({'path_on_host': k['path_on_host'], 'sha256': k['sha256'], 'size': a.get('size') or k.get('size')})

# ---- images
def image_info(data):
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(data)); return im.width, im.height, Image.MIME.get(im.format)
    except Exception: return None, None, None
def image_for_mcp(data, full=False, max_px=IMG_MAX_PX, max_bytes=IMG_MAX_BYTES):
    """-> (bytes, mime, w, h, downscaled). Downscale to max_px and ~max_bytes unless full (then up to 10 MB as-is)."""
    from PIL import Image
    im = Image.open(io.BytesIO(data)); fmt = Image.MIME.get(im.format) or 'image/png'
    if full and len(data) <= 10 * 2**20 and fmt in ('image/png', 'image/jpeg', 'image/webp', 'image/gif'): return data, fmt, im.width, im.height, False
    if not full and max(im.size) <= max_px and len(data) <= max_bytes and fmt in ('image/png', 'image/jpeg', 'image/webp', 'image/gif'): return data, fmt, im.width, im.height, False
    im.load()
    if im.mode in ('P', 'LA', 'RGBA'):
        bg = Image.new('RGB', im.size, (255, 255, 255)); im2 = im.convert('RGBA'); bg.paste(im2, mask=im2.split()[-1]); im = bg
    else: im = im.convert('RGB')
    lim = 4096 if full else max_px
    if max(im.size) > lim: im.thumbnail((lim, lim))
    for q in (88, 80, 70, 60, 50):
        buf = io.BytesIO(); im.save(buf, 'JPEG', quality=q, optimize=True)
        if buf.tell() <= (8 * 2**20 if full else max_bytes): break
        if q == 60 and not full: im.thumbnail((int(im.width * .75), int(im.height * .75)))
    return buf.getvalue(), 'image/jpeg', im.width, im.height, True
def screen_crop(ext, pad=0):
    from PIL import ImageGrab
    full = ImageGrab.grab(xdisplay=os.environ['DISPLAY']); W, Hh = full.size
    x, y, w, h = ext; box = (max(0, x - pad), max(0, y - pad), min(W, x + w + pad), min(Hh, y + h + pad))
    if box[2] - box[0] < 4 or box[3] - box[1] < 4: return None
    buf = io.BytesIO(); full.crop(box).convert('RGB').save(buf, 'PNG'); return buf.getvalue()
def thumb_b64(elem, max_px=360):
    try: elem.get_component_iface().scroll_to(0); time.sleep(.5)
    except Exception: pass
    ex = extents(elem)
    if not ex: return None
    png = screen_crop(ex)
    if not png: return None
    from PIL import Image
    im = Image.open(io.BytesIO(png)).convert('RGB'); im.thumbnail((max_px, max_px)); buf = io.BytesIO(); im.save(buf, 'JPEG', quality=80)
    return {'mime': 'image/jpeg', 'base64': base64.b64encode(buf.getvalue()).decode(), 'width': im.width, 'height': im.height, 'source': 'screen crop of the attachment tile (thumbnail)'}
def pdf_text(path):
    for exe in ('pdftotext',):
        try:
            r = subprocess.run([exe, '-layout', path, '-'], capture_output=True, text=True, timeout=60)
            if r.returncode == 0: return r.stdout
        except Exception: continue
    try:
        from pypdf import PdfReader
        return '\n\n'.join((pg.extract_text() or '') for pg in PdfReader(path).pages)
    except Exception:
        return None

# ---------------------------------------------------------------- rich content operations
def _resolve_tid(p, chan, thread):
    tid = resolve_thread(chan, thread)
    if tid and str(tid).startswith('LOOKUP:'):
        open_target(chan, None); settle(None, 15); ths = parse_threads(chan, True, True); ensure_threads_panel(False)
        tid = resolve_thread(chan, tid[7:], ths)
        if str(tid).startswith('LOOKUP:'): raise NotFound(f'no thread titled {thread!r} in {p["name"]}')
    return tid
def find_article(thread, idx, max_pages=15):
    for _ in range(max_pages + 1):
        arts = kids_deep_articles(msg_panel(thread) or win())
        hit = [a for a in arts if nm(a) == f'Message {idx}']
        if hit: _mark('find_article'); return hit[0]
        if not arts: break
        nums = [int(nm(a).split()[1]) for a in arts]
        if idx > max(nums): raise NotFound(f'message {idx} not found (latest rendered is {max(nums)})')
        first = arts[nums.index(min(nums))]
        try: first.get_component_iface().grab_focus()
        except Exception: pass
        time.sleep(.15); xdo('key', '--repeat', '8', '--delay', '60', 'Up')
        lo = min(nums)   # wait for older messages to render (up to 1.5 s) instead of always sleeping 1.5 s
        _wait_until(lambda: min([int(nm(a).split()[1]) for a in kids_deep_articles(msg_panel(thread) or win())] or [lo]) < lo, 1.5, .3)
    raise NotFound(f'message {idx} is not rendered (scrolled {max_pages} pages back)')
# ---- attachment viewers: the Image preview dialog, the file/output side pane, and the Overview document viewer.
# Only these controls are ever pressed while fetching: the tile itself, the viewer's "Close", and its "Download file".
# Anything that could post, share, delete or open more menus is refused by name, whatever finds it.
NEVER_PRESS_RE = re.compile(r'^(Add a pinned comment\b|Add a comment\b|More actions$|More options\b|Send\b|Reply\b|Comment\b|Share\b|Delete\b|Remove\b|Rename\b|Pin\b|Unpin\b)', re.I)
def _press(e, what=''):
    if NEVER_PRESS_RE.match(nm(e)): raise AppError(f'refusing to press {nm(e)!r} {what}'.strip())
    return act(e, what)
def overview_viewer(d=None):
    """The app's "Overview" document viewer (it replaces the transcript when a file tile is opened):
    -> dict(region, close, download, image, section) or None. Read-only."""
    if d is None: d, _ = main_doc()
    if not d: return None
    reg = find(d, 'landmark', 'Overview')
    if reg is None:
        tb = find(d, 'tool bar', 'Overview controls')
        reg = tb and next((e for e in walk(d) if role(e) == 'landmark' and find(e, 'tool bar', 'Overview controls') is not None), None)
    if reg is None or find(reg, 'tool bar', pred=lambda e: nm(e) in ('Overview controls', 'Document navigation and actions')) is None: return None
    ctl = find(reg, 'tool bar', 'Overview controls'); nav = find(reg, 'tool bar', 'Document navigation and actions')
    sec = find(nav, 'section', pred=lambda e: bool(nm(e).strip())) if nav else None   # the named one; it sits inside an unnamed wrapper
    imgs = [e for e in walk(reg) if role(e) == 'image']
    img = max(imgs, key=lambda e: (extents(e) or (0, 0, 0, 0))[2] * (extents(e) or (0, 0, 0, 0))[3]) if imgs else None
    return {'region': reg, 'close': find(ctl, 'push button', 'Close') if ctl else None,
            'download': find(nav, 'push button', 'Download file') if nav else None, 'image': img, 'section': nm(sec) if sec else ''}
def _close_att_overlays():
    """Close what an attachment fetch opened: the Image preview dialog, a file/output side pane, or the Overview viewer
    (Escape, then the viewer's or pane's own Close button). Returns True once none is open. Presses nothing else."""
    for _ in range(3):
        u = route() or ''; d, _ = main_doc()
        dlg = find(d, 'dialog', 'Image preview') if d else None; ov = overview_viewer(d)
        if not dlg and not ov and 'sidePane=' not in u: return True
        if ov and ov['close'] is not None:
            try: _press(ov['close'], 'Close (Overview viewer)')
            except Exception: xdo('key', 'Escape')
            _wait_until(lambda: not overview_viewer(), 1.5, .25); continue
        xdo('key', 'Escape'); time.sleep(1)
        u = route() or ''
        if 'sidePane=' in u:
            d, _ = main_doc(); reg = find(d, 'tool bar', pred=lambda e: nm(e).endswith(' controls') and find(e, 'push button', 'Close') is not None) if d else None
            c = find(reg, 'push button', 'Close') if reg else None
            if c:
                try: _press(c, 'Close (side pane)')
                except Exception: pass
                time.sleep(1)
    d, _ = main_doc()
    return 'sidePane=' not in (route() or '') and not (d and find(d, 'dialog', 'Image preview')) and not overview_viewer(d)
def http_cache_by_name(name):
    """Original bytes of an uploaded file from the app's HTTP cache, found by its Content-Disposition file name, with no
    viewer open. Only an unambiguous hit counts: every cached /files/<id>/contents response with that name has the same bytes."""
    if not name: return None
    hits = {}
    try: files = list(HTTPC.glob('*_0'))
    except Exception: files = []
    for f in files:
        try:
            with open(f, 'rb') as fh: head = fh.read(2048)
        except Exception: continue
        if b'/files/' not in head or b'/contents' not in head: continue
        try: e = _cache_entry(f)
        except Exception: e = None
        if not e or not e.get('content_disposition') or (e.get('status') and e['status'] not in ('200', '203')): continue
        mm = re.search(r"filename\*=utf-8''([^;]+)|filename=\"?([^\";]+)", e['content_disposition'], re.I)
        if not mm: continue
        import urllib.parse
        if urllib.parse.unquote(mm.group(1) or mm.group(2)) != name: continue
        body = _decode_body(e)
        if body: hits[hashlib.sha256(body).hexdigest()] = {'body': body, 'content_type': (e.get('content_type') or '').split(';')[0] or None, 'filename': name, 'url': safe_url(e['key'].split('/', 2)[-1] if e['key'][:2] == '1/' else e['key'])}
    return next(iter(hits.values())) if len(hits) == 1 else None
def _viewer_text(reg):
    """Rendered text of a viewer region (tool bars and the pinned-comment button left out), or ''."""
    parts = []
    for k in kids(reg):
        if role(k) == 'tool bar' or find(k, 'tool bar') is not None: continue
        parts.extend(kids(k) if NEVER_PRESS_RE.match(nm(k)) else [k])   # the pinned-comment button wraps the content
    R = Rich(); return finish_md(''.join(md_walk(k, R, {}, 0) for k in parts))
def _fetch_upload(att, elem):
    """Uploaded-file tile (the user's or Claude's). The app opens one of two viewers:
    - the Image preview dialog: file id from the <img> src, bytes from the app's HTTP cache;
    - the Overview document viewer (src is a blob:): the original through its "Download file" button and the Save as dialog.
    Fallbacks: the HTTP cache by file name (needs no viewer), then a screen crop or the viewer's text.
    -> (data, name, mime, source, extra); extra['download_path'] is set when the original was saved to disk."""
    steps = []; name = att['name']
    try: elem.get_component_iface().scroll_to(0); time.sleep(.3)
    except Exception: pass
    act(elem, name); dlg = img = ov = None; t0 = time.time()   # the tile itself (its name is the file's)
    while time.time() - t0 < 8:
        d, _ = main_doc(); dlg = find(d, 'dialog', 'Image preview') if d else None
        img = find(dlg, 'image') if dlg else None
        if img: break
        ov = overview_viewer(d)
        if ov and (ov['download'] is not None or ov['image'] is not None): break
        time.sleep(.25)
    if ov and not img:
        try: return _fetch_from_overview(att, ov, steps)
        finally: _close_att_overlays()
    src = _attrs(img).get('src', '') if img else ''
    m = re.search(r'/files/([0-9a-f-]{36})/(contents|preview)', src)
    got = None; how = None
    if m:
        for sub, lab in ((f'/files/{m.group(1)}/contents', 'original file'), (f'/files/{m.group(1)}/preview', 'preview rendition')):
            got = http_cache_get(sub, wait=8 if lab == 'original file' else 1)
            if got: how = f"app HTTP cache ({lab}, {got['url']})"; break
        if not got: steps.append('HTTP cache by file id: not cached')
    elif dlg: steps.append(f'Image preview: no /files/ URL in the image ({src.split(":", 1)[0] or "no src"})')
    else: steps.append('opening the tile: neither the Image preview nor the Overview viewer appeared within 8 s')
    if not got:
        got = http_cache_by_name(name)
        if got: how = f"app HTTP cache, found by file name ({got['url']})"
        else: steps.append('HTTP cache by file name: no single match')
    crop = None
    if not got and img:
        try: img.get_component_iface().scroll_to(0); time.sleep(.4)
        except Exception: pass
        ex = extents(img); crop = screen_crop(ex) if ex else None
        if not crop: steps.append('screen crop of the preview: empty')
    if not got and not crop and not dlg:
        ex = extents(elem); crop = screen_crop(ex) if ex else None
        if crop: how = 'screen crop of the tile (no preview dialog)'
        else: steps.append('screen crop of the tile: the tile is no longer on screen')
    _close_att_overlays()
    if got: return got['body'], (got['filename'] or name), sniff_mime(got['body']) or got['content_type'] or att.get('mime'), how, {'file_id': m.group(1) if m else None}
    if crop: return crop, os.path.splitext(name)[0] + '.png', 'image/png', how or 'screen crop of the app\'s image preview (not in the HTTP cache)', {'file_id': m.group(1) if m else None}
    return None, name, att.get('mime'), 'not available: ' + '; '.join(steps), {'failed_steps': steps}
def _fetch_from_overview(att, ov, steps):
    name = att['name']; mi = att.get('mime') or guess_mime(name)
    if ov['section'] and tile_file_name(ov['section']) != tile_file_name(name):
        steps.append(f'the Overview viewer opened {ov["section"]!r}, not {name!r}')
        return None, name, mi, 'not available: ' + '; '.join(steps), {'failed_steps': steps, 'viewer': 'overview'}
    extra = {'viewer': 'overview'}
    src = _attrs(ov['image']).get('src', '') if ov['image'] is not None else ''
    m = re.search(r'/files/([0-9a-f-]{36})/(contents|preview)', src)
    if m:
        got = http_cache_get(f'/files/{m.group(1)}/contents', wait=3)
        if got: return got['body'], got['filename'] or name, sniff_mime(got['body']) or got['content_type'] or mi, f"app HTTP cache (original file, {got['url']})", dict(extra, file_id=m.group(1), original=True)
    if ov['download'] is not None:
        try: path = _save_with_button(ov['download'], name, dialog_timeout=OV_DIALOG_TIMEOUT)   # the viewer already holds the bytes
        except Exception as ex: path = None; log(f'overview download failed for {name}: {ex!r}')
        if path: return None, name, mi or 'application/octet-stream', "the Overview viewer's Download file button (original file)", dict(extra, original=True, download_path=path)
        steps.append('Overview "Download file": the Save as dialog did not finish')
    else: steps.append('Overview viewer: no "Download file" button')
    got = http_cache_by_name(name)
    if got: return got['body'], name, sniff_mime(got['body']) or got['content_type'] or mi, f"app HTTP cache, found by file name ({got['url']})", dict(extra, original=True)
    steps.append('HTTP cache by file name: no single match')
    im = ov['image']
    if im is not None:
        try: im.get_component_iface().scroll_to(0); time.sleep(.4)
        except Exception: pass
        ex = extents(im); crop = screen_crop(ex) if ex else None
        if crop:
            return crop, os.path.splitext(name)[0] + '.png', 'image/png', f'screen crop of the Overview viewer image ({ex[2]}x{ex[3]} px; the download failed, see failed_steps)', dict(extra, original=False, rendered_size=[ex[2], ex[3]], failed_steps=steps)
        steps.append('screen crop of the Overview image: empty')
    else:
        txt = _viewer_text(ov['region'])
        if txt and not is_loading_text(txt):
            return txt.encode(), name if is_text_mime(mi, name) else os.path.splitext(name)[0] + '.txt', mi if is_text_mime(mi, name) else 'text/plain', 'Overview viewer, rendered text read through the accessibility tree (the download failed, see failed_steps)', dict(extra, original=False, failed_steps=steps)
        steps.append('Overview viewer: no image and no readable text')
    return None, name, mi, 'not available: ' + '; '.join(steps), dict(extra, failed_steps=steps)
def _fetch_pane(att, elem):
    """Claude output chip or inline file link: open it in the app's side pane and read it (text -> markdown from the accessibility tree,
    images -> HTTP cache if the src is a URL, else a screen crop)."""
    elem.get_component_iface().scroll_to(0); time.sleep(.6); act(elem, att['name'])
    base = os.path.basename(att.get('path_in_project') or att['name'])
    reg = None; t0 = time.time()
    while time.time() - t0 < 10:
        if 'sidePane=' in (route() or ''):
            d, _ = main_doc(); reg = find(d, 'landmark', pred=lambda e: nm(e) in (base, att['name'])) if d else None
            if reg: break
        time.sleep(.5)
    if not reg: _close_att_overlays(); return None, att['name'], att.get('mime'), 'not available (the file pane did not open)', {}
    time.sleep(1.5)
    extra = {}
    cp = find(reg, 'push button', pred=lambda e: nm(e).startswith('Copy path '))
    if cp: extra['path_in_project'] = nm(cp)[len('Copy path '):]
    tb = find(reg, 'tool bar'); kidsr = kids(reg)
    content = [k for k in kidsr if not (tb and find(k, 'tool bar') is not None)] or kidsr
    imgs = [e for k in content for e in walk(k, 5000) if role(e) == 'image']
    vids = [e for k in content for e in walk(k, 5000) if role(e) in ('video', 'embedded', 'internal frame') or _tag(e) == 'video']
    mi = att.get('mime') or guess_mime(base)
    data = None; how = None; name = base
    if imgs and (is_image_mime(mi) or not is_text_mime(mi, base)):
        im = max(imgs, key=lambda e: (extents(e) or (0, 0, 0, 0))[2] * (extents(e) or (0, 0, 0, 0))[3]); src = _attrs(im).get('src', '')
        if src.startswith('http'):
            got = http_cache_get(strip_q(src).split('claude.ai', 1)[-1] if 'claude.ai' in src else strip_q(src), wait=5)
            if got: data, how, mi = got['body'], f"app HTTP cache ({got['url']})", sniff_mime(got['body']) or got['content_type'] or mi
        if data is None:
            try: im.get_component_iface().scroll_to(0); time.sleep(.5)
            except Exception: pass
            ex = extents(im)
            if ex:
                data = screen_crop(ex); mi = 'image/png'; name = os.path.splitext(base)[0] + '.png'
                how = f'screen crop of the app\'s file pane ({ex[2]}x{ex[3]} px on the 540x1170 display; the original is not in the HTTP cache)'
                extra['rendered_size'] = [ex[2], ex[3]]
    elif vids:
        v = max(vids, key=lambda e: (extents(e) or (0, 0, 0, 0))[2] * (extents(e) or (0, 0, 0, 0))[3]); src = _attrs(v).get('src', '')
        extra['is_video'] = True; extra['original_mime'] = mi
        got = http_cache_get(strip_q(src).split('://', 1)[-1].split('/', 1)[-1], wait=2) if src.startswith('http') else None
        if got and (got['content_type'] or '').startswith('video/'):
            data, mi, how = got['body'], got['content_type'], f"app HTTP cache (the video file, {got['url']})"
        else:
            try: v.get_component_iface().scroll_to(0); time.sleep(.4)
            except Exception: pass
            ex = extents(v) if (extents(v) or (0, 0, 0, 0))[2] > 20 else extents(reg)
            data = screen_crop(ex) if ex else None; mi = 'image/png'; name = os.path.splitext(base)[0] + '-frame.png'
            how = 'screen crop of the app\'s video player (current frame only; the video file is not in the HTTP cache)'
    else:
        R = Rich(); md = finish_md(''.join(md_walk(k, R, {}, 0) for k in content)); t0 = time.time()
        while is_loading_text(md) and time.time() - t0 < 20:
            time.sleep(1); R = Rich(); md = finish_md(''.join(md_walk(k, R, {}, 0) for k in kids(reg) if not (tb and find(k, 'tool bar') is not None)))
        if is_loading_text(md):
            _close_att_overlays(); return None, base, mi, 'not available (the file pane never finished loading)', {}
        if md:
            data = md.encode(); how = 'app file pane, rendered text read through the accessibility tree' + (' (markdown reconstructed from the rendering)' if mi == 'text/markdown' else '')
            if not is_text_mime(mi, base): mi = 'text/plain'
            extra['blocks'] = R.blocks[:50] or None
        else:
            ex = extents(reg); data = screen_crop(ex) if ex else None; mi = 'image/png'; name = os.path.splitext(base)[0] + '-pane.png'
            how = 'screen crop of the app\'s file pane (no readable text; binary file)'
    _close_att_overlays()
    return data, name, mi, how, extra
# ---- originals through the app's own Download control (output chips / file pane), never the HTTP cache
SAVE_TITLES = ('Save as', 'Save As', 'Save File')
DL_DIALOG_TIMEOUT = int(os.environ.get('CLAUDE_PROJECTS_DL_DIALOG_TIMEOUT', '900')); OV_DIALOG_TIMEOUT = int(os.environ.get('CLAUDE_PROJECTS_OV_DIALOG_TIMEOUT', '90')); DL_WRITE_TIMEOUT = int(os.environ.get('CLAUDE_PROJECTS_DL_WRITE_TIMEOUT', '900'))
LOADING_RE = re.compile(r'^\s*(Loading( file)?|Loading\u2026|Loading\.\.\.)\s*$', re.I)
def is_loading_text(s): return bool(s) and bool(LOADING_RE.match(s))
def _save_dialog_wid():
    for w in (xdo('search', '--onlyvisible', '--name', '.') or '').split():
        try:
            if (xdo('getwindowname', w) or '').strip() in SAVE_TITLES: return w
        except Exception: pass
    return None
def _press_save(wid):
    """Press the Save as dialog's Save button (Enter in the Name field does not confirm it on a narrow virtual display)."""
    for _ in range(3):
        if not _save_dialog_wid(): return True
        root = next((f for app in kids(A().get_desktop(0)) for f in kids(app) if nm(f) in SAVE_TITLES), None)
        btn = find(root, 'push button', 'Save') if root else None
        if btn is not None:
            try: act(btn, 'Save')
            except Exception: pass
            if _wait_until(lambda: not _save_dialog_wid(), 2): return True
        try: _ensure_chooser_open_visible(wid)
        except Exception: pass
        g = _xdo_geom(wid); xdo('windowactivate', '--sync', wid)
        _click_xy(g['X'] + g['WIDTH'] - 55, g['Y'] + g['HEIGHT'] - 32)
        if _wait_until(lambda: not _save_dialog_wid(), 2): return True
    return not _save_dialog_wid()
def _wait_until(fn, secs, step=.2):
    """Poll fn() until it returns something truthy or secs pass -> that value, or None."""
    t0 = time.time()
    while True:
        v = fn()
        if v: return v
        if time.time() - t0 >= secs: return None
        time.sleep(step)
def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''): h.update(b)
    return h.hexdigest()
def save_target(dest_dir, name):
    """ASCII-only file name for the Save as dialog (xdotool types ASCII only)."""
    return pathlib.Path(dest_dir)/(re.sub(r'[^A-Za-z0-9._-]', '_', name or '') or 'download.bin')
def _download_via_app(att, article, elem):
    """Original bytes via the app's Download control -> Save as dialog -> file under ATT_DIR/incoming. Returns a path or None.
    Output chips carry 'Download <name>'; file refs open the pane, whose toolbar has 'Download file'. Nothing is sent."""
    name = att['name']; btn = None
    if att['kind'] == 'output_file': btn = find(article, 'push button', f'Download {name}')
    if btn is None:
        elem.get_component_iface().scroll_to(0); time.sleep(.6); act(elem, name); t0 = time.time()
        base = os.path.basename(att.get('path_in_project') or name)
        while time.time() - t0 < 15 and btn is None:
            d, _ = main_doc(); tb = find(d, 'tool bar', pred=lambda e: nm(e) in (f'{base} controls', f'{name} controls')) if d else None
            btn = find(tb, 'push button', 'Download file') if tb else None
            if btn is None: time.sleep(.5)
    if btn is None: return None
    return _save_with_button(btn, name)
def _save_with_button(btn, name, dialog_timeout=None):
    """Press a Download control and save through the Save as dialog into a fresh incoming slot -> path or None."""
    if _save_dialog_wid(): return None
    dest = STAGE.new_slot('incoming')   # managed staging; the file is then moved into ATT_DIR by store_file
    target = save_target(dest, name)
    btn.get_component_iface().scroll_to(0); time.sleep(.2); _press(btn, f'Download {name}')
    wid = _wait_until(_save_dialog_wid, dialog_timeout or DL_DIALOG_TIMEOUT, .25)   # the app fetches the whole file before the dialog shows
    if not wid: return None
    try:
        xdo('windowactivate', '--sync', wid); time.sleep(.3)
        if _active_win_name() not in SAVE_TITLES: return None
        xdo('key', '--clearmodifiers', 'ctrl+a'); time.sleep(.2)
        xdo('type', '--delay', '10', '--clearmodifiers', str(target)); time.sleep(.8)
        if not _press_save(wid): return None
        # Chromium writes <name>.crdownload and renames it when done: the target plus no .crdownload is the finished file
        t0 = time.time(); last = -1; stable = 0
        while time.time() - t0 < DL_WRITE_TIMEOUT:
            if target.exists():
                sz = target.stat().st_size; stable = stable + 1 if sz == last and sz > 0 else 0; last = sz
                if stable >= 2 and not list(dest.glob('*.crdownload')): return str(target)
            time.sleep(.3)
        return None
    finally:
        if _save_dialog_wid(): xdo('key', 'Escape'); time.sleep(.5)
def store_file(src, name, mime, meta):
    """Like store_bytes but moves an on-disk file (no size cap, never loaded into memory)."""
    ATT_DIR.mkdir(parents=True, exist_ok=True); sha = _sha256_file(src)
    ext = os.path.splitext(name or '')[1].lower() or mimetypes.guess_extension(mime or '') or '.bin'
    p = ATT_DIR/f'{sha[:32]}{ext}'
    if p.exists(): os.unlink(src)
    else: os.replace(src, p)
    try: os.rmdir(os.path.dirname(src))
    except OSError: pass
    size = p.stat().st_size
    (ATT_DIR/f'{sha[:32]}.json').write_text(json.dumps(dict(meta, sha256=sha, path_on_host=str(p), size=size, stored_at=now().isoformat()), ensure_ascii=False))
    _att_index_put(dict(meta, size=size), sha, str(p)); _att_prune(); return str(p), sha, size
# ---- background jobs (attachment fetches, sends): a lock file held while one runs, a JSON result/state file.
# A job runs in a detached worker so a tool call can answer within its budget; the same call again joins it.
class Interrupted(BaseException):
    """SIGTERM or the watchdog: not an AppError, so no fallback path catches it; cleanup in finally blocks still runs."""
def job_lock(dirp, key):
    """-> an open file holding the job's exclusive lock, or None when another process holds it."""
    dirp.mkdir(parents=True, exist_ok=True); os.chmod(dirp, 0o700)
    f = open(dirp/f'{key}.lock', 'a+')
    try: fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB); return f
    except OSError: f.close(); return None
def job_unlock(f):
    try: fcntl.flock(f, fcntl.LOCK_UN)
    except Exception: pass
    f.close()
def job_running(dirp, key):
    if not (dirp/f'{key}.lock').exists(): return False
    f = job_lock(dirp, key)
    if f is None: return True
    job_unlock(f); return False
def job_read(dirp, key):
    try: return json.loads((dirp/f'{key}.json').read_text())
    except Exception: return None
def job_write(dirp, key, data):
    dirp.mkdir(parents=True, exist_ok=True); t = dirp/f'{key}.json.tmp'
    t.write_text(json.dumps(data, ensure_ascii=False, default=str)); t.replace(dirp/f'{key}.json')
def jobs_running():
    """Running background jobs (for status and the deploy busy check)."""
    out = []
    for kind, dirp in (('attachment', ATT_JOBS), ('send', SEND_JOBS)):
        try: locks = list(dirp.glob('*.lock'))
        except Exception: locks = []
        for lk in locks:
            key = lk.name[:-5]
            if not job_running(dirp, key): continue
            st = job_read(dirp, key) or {}
            out.append({'kind': kind, 'job_id': key, 'state': st.get('state') or 'running', 'step': st.get('step'),
                        'started_at_local': ct(datetime.datetime.fromtimestamp(st['started_ts'], datetime.timezone.utc).isoformat()) if st.get('started_ts') else None})
    return out
def _use_systemd_scope():
    rd = os.environ.get('XDG_RUNTIME_DIR') or ''
    return bool(shutil.which('systemd-run')) and bool(rd) and os.path.exists(os.path.join(rd, 'systemd', 'private')) and not os.environ.get('CLAUDE_PROJECTS_NO_SCOPE')
def spawn_job(argv, name):
    """Start a detached worker. Under a systemd user manager it runs in its own transient scope, so restarting the MCP
    service (a deploy) does not kill it mid-job; elsewhere it gets its own session."""
    LOGD.mkdir(parents=True, exist_ok=True)
    with open(LOGD/'jobs.log', 'a') as lf:
        if _use_systemd_scope():
            unit = f'claude-projects-job-{name}-{int(time.time())}'
            pr = subprocess.Popen(['systemd-run', '--user', '--scope', '--quiet', '--collect', f'--unit={unit}', '--'] + argv,
                                  stdin=subprocess.DEVNULL, stdout=lf, stderr=lf, start_new_session=True, env=dict(os.environ))
            time.sleep(.3)
            if pr.poll() in (None, 0): return 'scope'
            log(f'systemd-run for {name} failed (rc={pr.returncode}); starting it as a plain process')
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=lf, stderr=lf, start_new_session=True, env=dict(os.environ))
        return 'process'
ATT_JOBS = CACHE/'attachment-jobs'
SEND_JOBS = CACHE/'send-jobs'
ATT_BUDGET = float(os.environ.get('CLAUDE_PROJECTS_ATTACH_BUDGET', '40'))   # seconds a budgeted call (the MCP tool) waits before answering "pending"
def _att_parse(attachment_id, message_index, n):
    if attachment_id:
        mm = re.match(r'^(\d+)[.:/a-z]+(\d+)$', str(attachment_id).strip())
        if not mm: raise CPGError('attachment_id looks like "<message_index>.<n>" (from the attachments list of claude_thread_read)')
        message_index, n = int(mm.group(1)), int(mm.group(2))
    if message_index is None: raise CPGError('pass attachment_id or message_index (+ n)')
    return int(message_index), int(n)
def att_job_key(chan, thread, message_index, n):
    """One fetch per project / thread / message / attachment: retries and concurrent calls share it."""
    t = norm(str(thread)).casefold() if thread else 'primary'
    return hashlib.sha256(f'{chan}|{t}|{int(message_index)}|{int(n)}'.encode()).hexdigest()[:24]
def _att_job_result(key): return job_read(ATT_JOBS, key)
def _att_identity_ok(meta):
    """A cached file is served only while the on-disk transcript cache still shows the same attachment at that number:
    the app's "Message N" numbering shifts, so N.n alone can point at another message later. No UI, no lock."""
    idt = meta.get('identity') or {}
    if not idt.get('name'): return False
    th = meta.get('thread_id'); c = read_cache(meta.get('project_id'), None if th in (None, 'primary') else th)
    m = next((x for x in (c or {}).get('messages') or [] if x.get('index') == meta.get('message_index')), None)
    if not m: return False
    atts = m.get('attachments') or []; n = int(idt.get('n') or 0)
    if not 1 <= n <= len(atts) or tile_file_name(atts[n - 1].get('name') or '') != tile_file_name(idt['name']): return False
    for k, v in (('role', m.get('role')), ('time_utc', m.get('time_utc')), ('text_sha', _text_sha(m.get('text')))):
        if idt.get(k) and v and idt[k] != v: return False
    return True
def _text_sha(t): return hashlib.sha256((t or '').encode()).hexdigest()[:16] if t else None
def _att_cached(key, since=None, fresh=False):
    """A finished result for key: an available file still on disk whose identity the transcript cache confirms, or
    (since=t) any result written after t (the fetch this call started or joined). fresh=True: only the latter."""
    r = _att_job_result(key)
    if not r: return None
    if since is not None and (r.get('finished_ts') or 0) >= since: return r
    m = r.get('meta') or {}
    if not fresh and m.get('available') and m.get('path_on_host') and pathlib.Path(m['path_on_host']).is_file() and _att_identity_ok(m): return r
    return None
def _att_job_running(key): return job_running(ATT_JOBS, key)
def _att_run_job(key, job):
    """Run one fetch for key under its job lock and write the result file. Returns the result, or None when another
    process already holds the lock (that one writes the result; the caller waits for it)."""
    f = job_lock(ATT_JOBS, key)
    if f is None: return None
    try:
        r = _att_cached(key, since=job.get('started_ts'), fresh=bool(job.get('refresh')))
        if r: return r                                 # finished while this one waited for the lock
        t0 = time.time()
        try: r = {'status': 'done', 'meta': _attachment_fetch(**{k: job[k] for k in ('project', 'thread', 'message_index', 'n', 'force', 'stay', 'shot')})}
        except CPGError as ex: r = {'status': 'failed', 'error': str(ex), 'error_type': type(ex).__name__, 'code': getattr(ex, 'code', 1)}
        except Exception as ex: r = {'status': 'failed', 'error': repr(ex), 'error_type': 'AppError', 'code': 6}
        except Interrupted as ex: r = {'status': 'failed', 'error': f'interrupted ({ex})', 'error_type': 'AppError', 'code': 6}
        r.update(job_id=key, finished_ts=time.time(), seconds=round(time.time() - t0, 1))
        job_write(ATT_JOBS, key, r)
        return r
    finally: job_unlock(f)
def _att_spawn_worker(key, job):
    """Detached `claude-projects attachment get ... --job` that does the fetch; it outlives this call."""
    argv = [*cfg_mod.module_argv('claude_projects'), '--json'] + (['--force'] if job.get('force') else []) + \
           ['attachment', 'get', job['chan'], f"{job['message_index']}.{job['n']}", '--job'] + (['--thread', job['thread']] if job.get('thread') else []) + \
           (['--refresh'] if job.get('refresh') else [])
    how = spawn_job(argv, f'att-{key[:8]}')
    log(f'attachment job {key} started in the background ({job["message_index"]}.{job["n"]}, {how})')
def _att_answer(r, as_, full, cached=False):
    if r.get('status') == 'failed':
        cls = {c.__name__: c for c in (NotFound, AppError, CPGError)}.get(r.get('error_type'), AppError)
        raise cls(r.get('error') or 'attachment fetch failed')
    out = present_attachment(r['meta'], as_, full); out['job_id'] = r.get('job_id')
    if cached: out['cached'] = True
    return out
def op_attachment_get(project, thread=None, attachment_id=None, message_index=None, n=1, as_='auto', full=False, force=False, stay=False, shot=None,
                      budget=None, refresh=False, job=False):
    """budget=None: fetch now and wait for it (CLI, forward). budget=seconds (the MCP tool): answer within that time; a fetch
    still running then comes back as pending with a job_id, and the same call again returns the file. Either way a
    finished file is answered from the cache, and a fetch already running for the same attachment is joined, never repeated."""
    message_index, n = _att_parse(attachment_id, message_index, n)
    as_ = (as_ or 'auto').lower()
    if as_ not in ('auto', 'image', 'text', 'base64', 'path'): raise CPGError('as must be auto | image | text | base64 | path')
    p = resolve_project(project); chan = p['id']; key = att_job_key(chan, thread, message_index, n); t_start = time.time()
    if not refresh:
        r = _att_cached(key)
        if r: return _att_answer(r, as_, full, cached=True)
    spec = dict(project=chan, chan=chan, thread=thread, message_index=message_index, n=n, force=force, stay=stay, shot=shot, refresh=refresh, started_ts=t_start)
    if job or budget is None:
        r = _att_run_job(key, spec)
        if r is None and job: return {'available': False, 'pending': True, 'job_id': key, 'note': 'a fetch of this attachment was already running'}
        if r is None:                                  # another fetch of this attachment is running: wait for its result
            t0 = time.time()
            while time.time() - t0 < (budget or 900) and r is None:
                time.sleep(.5); r = _att_cached(key, since=t_start, fresh=refresh)
                if r is None and not _att_job_running(key): r = _att_run_job(key, spec)   # it ended without a result
            if r is None:
                return {'available': False, 'pending': True, 'job_id': key, 'project': p['name'], 'message_index': message_index, 'n': n,
                        'note': 'another fetch of this attachment is still running; call again with the same arguments for the file'}
        return _att_answer(r, as_, full)
    spawned = False
    while time.time() - t_start < budget:
        r = _att_cached(key, since=t_start, fresh=refresh)
        if r: return _att_answer(r, as_, full)
        if not spawned and not _att_job_running(key): _att_spawn_worker(key, spec); spawned = True   # else join the running fetch
        time.sleep(.25)
    return {'available': False, 'pending': True, 'job_id': key, 'project': p['name'], 'project_id': chan, 'thread_id': thread or 'primary',
            'message_index': message_index, 'n': n, 'retry_after_s': 15,
            'note': f'still fetching in the app after {int(budget)} s; call again with the same arguments to get the file '
                    '(the call joins this fetch and never starts a second one)'}
def _attachment_fetch(project, thread, message_index, n, force=False, stay=False, shot=None):
    """Open the attachment in the app and store it. -> metadata (path_on_host, sha256, mime, size, ...), no payload."""
    p = resolve_project(project); chan = p['id']
    with step_timer(f'attachment {p["name"]} {thread or "primary"} {message_index}.{n}') as tmr, ui_lock(f'attachment {p["name"]} {thread or "primary"} {message_index}.{n}', force):
        try:
            _mark('lock'); tid = _resolve_tid(p, chan, thread); _mark('resolve_thread')
            open_target_settled(chan, tid)
            a = find_article(tid, int(message_index))
            R, _ = rich_of(a, int(message_index)); _mark('read_message')
            if not R.atts: raise NotFound(f'message {message_index} has no attachments, files or links')
            if not 1 <= int(n) <= len(R.atts): raise NotFound(f'message {message_index} has {len(R.atts)} attachment(s); n must be 1..{len(R.atts)}')
            att = dict(R.atts[int(n) - 1]); el = R.elems[int(n) - 1]
            meta = dict(att, project=p['name'], project_id=chan, thread_id=tid or 'primary', message_index=int(message_index))
            try:
                pm = parse_article(a)
                meta['identity'] = {'name': tile_file_name(att.get('name') or ''), 'n': int(n), 'role': pm.get('role'), 'time_utc': pm.get('time_utc'), 'text_sha': _text_sha(pm.get('text'))}
            except Exception as ex: log(f'attachment identity not read: {ex!r}')
            if att['kind'] in ('link', 'pull_request'):
                meta.update(note='a link: nothing to download in the app; open the URL yourself', text=att.get('url'))
                return meta
            dl = None
            if att['kind'] in ('image', 'file'):
                data, name, mime, how, extra = _fetch_upload(att, el); dl = extra.pop('download_path', None)
            else:
                try: dl = _download_via_app(att, a, el)
                except Exception as ex: log(f'download via app failed for {att["name"]}: {ex!r}')
                try: _close_att_overlays()
                except Exception: pass
                if dl:
                    data, name, mime, extra = None, att['name'], att.get('mime') or guess_mime(att['name']) or 'application/octet-stream', {'original': True}
                    how = "the app's Download control (original file)"
                else:
                    a = find_article(tid, int(message_index)); R, _ = rich_of(a, int(message_index)); el = R.elems[int(n) - 1]
                    data, name, mime, how, extra = _fetch_pane(att, el); extra['original'] = False
            _mark('fetch')
            if shot: screenshot(shot)
        finally:
            try: _close_att_overlays()
            except Exception: pass
            if not stay: go_home()
    meta.update({k: v for k, v in extra.items() if v is not None}); meta['source'] = how; meta['fetched_at_local'] = ct(now().isoformat())
    meta['timing'] = tmr.as_dict()
    if dl:
        path, sha, size = store_file(dl, name, mime, dict(meta, mime=mime))
        meta.update(available=True, mime=mime, size=size, saved_name=name, path_on_host=path, sha256=sha)
    elif data is None:
        meta['available'] = False; return meta
    else:
        meta.update(available=True, mime=mime, size=len(data), saved_name=name)
        path, sha = store_bytes(data, name, mime, meta)
        meta['path_on_host'] = path; meta['sha256'] = sha or hashlib.sha256(data).hexdigest()
        if not path: meta['note'] = f'larger than {ATT_FILE_CAP >> 20} MB: not stored'
    if is_image_mime(meta['mime']) and meta.get('path_on_host') and meta['size'] <= 50 * 2**20:
        try: w, h, _ = image_info(pathlib.Path(meta['path_on_host']).read_bytes()); meta['width'], meta['height'] = w, h
        except Exception: pass
    return meta
def present_attachment(meta, as_='auto', full=False):
    """Stored attachment metadata -> the tool's answer: image content, text, base64 or just the path (as_)."""
    meta = dict(meta); path = meta.get('path_on_host')
    if not meta.get('available'): return meta
    if not path or not pathlib.Path(path).is_file(): meta['returned_as'] = 'path'; return meta
    mime = meta.get('mime'); name = meta.get('saved_name') or meta.get('name') or ''; size = pathlib.Path(path).stat().st_size
    text = None
    if is_text_mime(mime, name) and size <= 2 * 2**20: text = pathlib.Path(path).read_text('utf-8', 'replace')
    elif mime == 'application/pdf': text = pdf_text(path)
    if text: meta['preview_text'] = text[:400]
    mode = as_
    if mode == 'auto': mode = 'image' if is_image_mime(mime) and size <= 50 * 2**20 else ('text' if text else 'path')
    if mode == 'image':
        if not is_image_mime(mime): meta['note'] = f'not an image ({mime}); returned metadata and path'
        elif size > 50 * 2**20: meta['note'] = 'image larger than 50 MB; use the path'; mode = 'path'
        else:
            b, m2, w, h, ds = image_for_mcp(pathlib.Path(path).read_bytes(), full)
            meta['image'] = {'mime': m2, 'base64': base64.b64encode(b).decode(), 'width': w, 'height': h, 'bytes': len(b), 'downscaled': ds}
    elif mode == 'text':
        if text is None: meta['note'] = f'no text form for {mime}; returned metadata and path'
        else: meta['text'] = text[:400000]; meta['text_truncated'] = len(text) > 400000
    elif mode == 'base64':
        if size > 10 * 2**20: meta['note'] = 'larger than 10 MB; use the path'
        else: meta['base64'] = base64.b64encode(pathlib.Path(path).read_bytes()).decode()
    meta['returned_as'] = mode
    return meta

def locate_attach_control():
    """Find the composer's attach control and list its menu (opening the menu uploads nothing), then close it."""
    d, _ = main_doc(); b = None
    e = composer()
    adds = find_all(d, 'push button', lambda x: nm(x) in ('Add', 'Add files', 'Attach', 'Add content', 'Add attachment')) if d else []
    if adds and e:
        ce = extents(e) or (0, 0, 0, 0)
        b = min(adds, key=lambda x: abs((extents(x) or (0, 0, 0, 0))[1] - ce[1]))
    if not b: return {'attach_button_found': False}
    act(b, 'Add'); time.sleep(1.2); d, _ = main_doc()
    items = [nm(x) for x in walk(d, 40000) if role(x) in ('menu item', 'check menu item', 'radio menu item')]
    xdo('key', 'Escape'); time.sleep(.8)
    return {'attach_button_found': True, 'attach_button': nm(b), 'menu_items': items, 'attach_item': next((i for i in items if re.match(r'^(Add|Upload|Attach) (files|photos|file)', i)), None)}
def prepare_attachment_files(attachments):
    """attachments: list of {"path": host path} or {"base64": ..., "filename": ...} -> list of local paths (validated, not uploaded)."""
    out = []
    for i, a in enumerate(attachments or []):
        if isinstance(a, str): a = {'path': a}
        if a.get('path'):
            pth = pathlib.Path(a['path']).expanduser()
            if not pth.is_file(): raise CPGError(f'attachment {i + 1}: {a["path"]} is not a file on the Claude Code host (pass base64 + filename for files that live elsewhere)')
        elif a.get('base64'):
            fn = STAGE.safe_name(a.get('filename'), f'attachment-{i + 1}.bin')
            pth = STAGE.write_bytes('outgoing', fn, base64.b64decode(a['base64']))   # unique slot per call, original name kept
        else: raise CPGError(f'attachment {i + 1}: needs path or base64 + filename')
        if pth.stat().st_size > 30 * 2**20: raise CPGError(f'attachment {i + 1}: larger than 30 MB')
        out.append({'path': str(pth.resolve()), 'name': pth.name, 'size': pth.stat().st_size, 'mime': guess_mime(pth.name), 'staged': STAGE.contains(pth)})
    return out
def _screen_wh():
    from PIL import ImageGrab
    return ImageGrab.grab(xdisplay=os.environ['DISPLAY']).size
def _active_win_name():
    return (xdo('getactivewindow', 'getwindowname') or '').strip()
def _xdo_geom(wid):
    """Parse `xdotool getwindowgeometry --shell` into ints (X/Y/WIDTH/HEIGHT)."""
    raw = xdo('getwindowgeometry', '--shell', str(wid)) or ''
    g = {}
    for ln in raw.splitlines():
        if '=' not in ln: continue
        k, v = ln.split('=', 1)
        try: g[k] = int(v)
        except ValueError: g[k] = v
    if not all(k in g for k in ('X', 'Y', 'WIDTH', 'HEIGHT')):
        raise AppError(f'could not read Open Files geometry ({raw!r}); nothing attached')
    return g
def _open_files_wids():
    """X window ids of any GTK 'Open Files' chooser on the app display (mapped or not)."""
    out = []
    for w in (xdo('search', '--name', '^Open Files?$') or '').split():
        w = w.strip()
        if w and (xdo('getwindowname', w) or '').strip() in ('Open Files', 'Open File'): out.append(w)
    return out
def _dismiss_open_files(reason=''):
    """Cancel any GTK Open Files chooser (nothing is attached, uploaded or sent).
    A chooser left open after a failed attach is modal and keeps keyboard focus, so every later
    ctrl+v / xdotool type lands in it and the composer reads back 0 chars. One Escape only closes the
    chooser's search bar, so: Escape x2, then the AT-SPI Cancel button, then slide the dialog on-screen
    and click Cancel by geometry (bottom-right, left of Open). Never presses Open. True when none is left."""
    tries = 0
    for _ in range(3):
        wids = _open_files_wids()
        if not wids: break
        for w in wids:
            tries += 1
            try:
                xdo('windowactivate', '--sync', w); time.sleep(.2)
                for _k in range(2):
                    if w not in _open_files_wids(): break
                    xdo('key', '--clearmodifiers', 'Escape'); time.sleep(.5)
                if w not in _open_files_wids(): continue
                root = _file_chooser_root()
                btn = _chooser_named(root, 'Cancel', roles=('push button',)) if root else None
                if btn is not None:
                    try: act(btn)
                    except Exception: _activate_or_click(btn)
                    time.sleep(.6)
                    if w not in _open_files_wids(): continue
                g = _xdo_geom(w); sw, sh = _screen_wh()
                if g['X'] + g['WIDTH'] > sw - 4:
                    xdo('windowmove', str(w), str(int(sw - g['WIDTH'])), str(int(g['Y']))); time.sleep(.35); g = _xdo_geom(w)
                _click_xy(g['X'] + g['WIDTH'] - 140, g['Y'] + g['HEIGHT'] - 46); time.sleep(.6)
            except Exception as ex: log(f'dismiss Open Files {w} failed: {ex!r}')
    left = _open_files_wids()
    if tries or left: log(f'dismissed stray Open Files chooser ({reason}): tries={tries} left={len(left)}')
    return not left
def _chooser_home(root):
    """Sidebar 'Home' in the chooser. _file_chooser_root() can return None on some hosts, so also scan every
    a11y app for a frame that has an 'Other Locations' place (that frame is the chooser)."""
    e = _chooser_named(root, 'Home') if root else None
    if e is not None: return e
    try:
        for app in kids(A().get_desktop(0)):
            for f in kids(app):
                if role(f) == 'frame' and nm(f) == 'Claude': continue
                if find(f, pred=lambda x: nm(x) == 'Other Locations') is not None:
                    h = _chooser_named(f, 'Home')
                    if h is not None: return h
    except Exception as ex: log(f'chooser Home lookup failed: {ex!r}')
    return None
def _wait_open_files(secs=15):
    t0 = time.time()
    while time.time() - t0 < secs:
        if _active_win_name() == 'Open Files':
            return xdo('getactivewindow')
        time.sleep(.25)
    return None
def _click_xy(x, y, *, double=False, ctrl=False, shift=False):
    if ctrl: xdo('keydown', 'ctrl')
    if shift: xdo('keydown', 'shift')
    xdo('mousemove', '--sync', str(int(x)), str(int(y)))
    if double: xdo('click', '--repeat', '2', '--delay', '100', '1')
    else: xdo('click', '1')
    if shift: xdo('keyup', 'shift')
    if ctrl: xdo('keyup', 'ctrl')
    time.sleep(.15)
def _ensure_chooser_left_visible(wid):
    """Keep the sidebar + file list on-screen while browsing (Open may still be off-screen)."""
    g = _xdo_geom(wid)
    if g['X'] < -10:
        xdo('windowmove', str(wid), '0', str(g['Y'])); time.sleep(.3)
def _ensure_chooser_open_visible(wid):
    """Slide the dialog so the Open button (bottom-right) is on-screen.
    Call AFTER browse/select — moving first can push the list off a narrow display.
    When the chooser is wider than the screen this yields a negative X (≈-280 for 814-wide on 540)."""
    g = _xdo_geom(wid); sw, sh = _screen_wh()
    if g['X'] + g['WIDTH'] <= sw - 4: return
    new_x = sw - g['WIDTH']
    new_y = max(0, min(g['Y'], max(0, sh - min(g['HEIGHT'], sh))))
    xdo('windowmove', str(wid), str(int(new_x)), str(int(new_y))); time.sleep(.35)
def _file_chooser_root():
    """AT-SPI root for the GTK Open Files window, if exposed (Electron sometimes leaves the frame unnamed)."""
    desk = A().get_desktop(0)
    for app in kids(desk):
        for f in kids(app):
            n = nm(f)
            if n in ('Open Files', 'Open File'): return f
            if role(f) in ('dialog', 'file chooser', 'file chooser dialog') and re.search(r'\bopen\b', n, re.I):
                return f
    for app in kids(desk):
        if nm(app) != 'claude-desktop': continue
        for f in kids(app):
            if role(f) != 'frame' or nm(f) == 'Claude': continue
            if find(f, 'push button', 'Open') or find(f, pred=lambda e: nm(e) == 'Home'):
                return f
    return None
def _chooser_named(root, name, roles=None):
    """Find an a11y node whose name equals `name` (exact). Prefer interactive roles."""
    if root is None: return None
    prefer = ('push button', 'toggle button', 'list item', 'table cell', 'tree item', 'menu item', 'label', 'static', 'text', 'paragraph')
    for r in prefer:
        if roles and r not in roles: continue
        e = find(root, r, name)
        if e is not None: return e
    return find(root, pred=lambda e: nm(e) == name and (not roles or role(e) in roles))
def _activate_or_click(e, *, double=False, ctrl=False, shift=False):
    ex = extents(e)
    if ex and ex[2] > 0 and ex[3] > 0:
        _click_xy(ex[0] + ex[2] // 2, ex[1] + ex[3] // 2, double=double, ctrl=ctrl, shift=shift)
        return True
    if not (double or ctrl or shift):
        try: return bool(act(e))
        except Exception: return False
    return False
def _chooser_focus_list(wid):
    g = _xdo_geom(wid)
    # File list sits to the right of the places sidebar (~160px) and below the header/toolbar.
    _click_xy(g['X'] + min(max(g['WIDTH'] // 2, 200), max(g['WIDTH'] - 40, 200)), g['Y'] + min(200, max(g['HEIGHT'] // 3, 120)))
def _browse_chooser_to_dir(root, directory):
    """Browse to `directory` via sidebar Home + double-click each path component.
    Do NOT Ctrl+L / type a path / Enter — that closes the dialog without attaching (code 6)."""
    directory = str(pathlib.Path(directory).expanduser().resolve())
    home = str(pathlib.Path.home().resolve())
    if directory != home and not directory.startswith(home + os.sep):
        raise AppError(f'attachment folder {directory} is outside $HOME; copy it under the home directory (browse+Open attach supports Home/... only); nothing attached')
    rel = os.path.relpath(directory, home)
    parts = [] if rel == '.' else rel.split(os.sep)
    wid = xdo('getactivewindow')
    home_btn = _chooser_home(root)
    if home_btn: _activate_or_click(home_btn)
    else:
        g = _xdo_geom(wid)
        # Places sidebar (540x1170 display): Recent ~Y+10, Home ~Y+40, Desktop ~Y+75, Other Locations ~Y+115 (719x822 chooser).
        # The old Y+120 clicked "Other Locations", so every folder lookup failed and the chooser was orphaned.
        log('chooser Home not found via AT-SPI; geometry fallback Y+40')
        _click_xy(g['X'] + 60, g['Y'] + 40)
    time.sleep(.9)
    for part in parts:
        row = None; t0 = time.time()
        while time.time() - t0 < 10 and row is None:
            root = _file_chooser_root() or root
            row = _chooser_named(root, part) if root else None
            if row is None:
                wid = xdo('getactivewindow'); _chooser_focus_list(wid)
                xdo('type', '--delay', '20', '--clearmodifiers', '--file', '-', inp=part); time.sleep(.7)
                root = _file_chooser_root() or root
                row = _chooser_named(root, part) if root else None
            if row is None: time.sleep(.35)
        if row is None:
            raise AppError(f'folder {part!r} not found in the Open Files list; nothing attached')
        if not _activate_or_click(row, double=True):
            # type-ahead already selected it — Return opens a folder without confirming the dialog
            xdo('key', 'Return')
        time.sleep(1.0)
def _select_chooser_files(root, names):
    """Select exactly these basenames in the current folder (click, then ctrl-click). Never grab siblings.
    Returns True if every file was multi-selected in this open chooser; False if a11y could not
    ctrl-select a later file (caller should fall back to one-file-per-picker passes)."""
    if not names: return True
    for i, name in enumerate(names):
        row = None; t0 = time.time()
        while time.time() - t0 < 10 and row is None:
            root = _file_chooser_root() or root
            row = _chooser_named(root, name) if root else None
            if row is None:
                wid = xdo('getactivewindow'); _chooser_focus_list(wid)
                # Full basename (incl. extension) so settings.jpg wins over settings.png.
                xdo('type', '--delay', '18', '--clearmodifiers', '--file', '-', inp=name); time.sleep(.6)
                root = _file_chooser_root() or root
                row = _chooser_named(root, name) if root else None
            if row is None: time.sleep(.35)
        if row is None:
            raise AppError(f'file {name!r} not found in the Open Files list; nothing attached')
        ok = _activate_or_click(row, ctrl=(i > 0))
        if not ok and i > 0:
            return False
        time.sleep(.2)
    return True
def _click_chooser_open(root, wid):
    _ensure_chooser_open_visible(wid)
    root = _file_chooser_root() or root
    btn = _chooser_named(root, 'Open', roles=('push button',)) if root else None
    if btn is not None:
        if _activate_or_click(btn):
            _wait_until(lambda: not _open_files_wids(), 1.0, .2); return
        try: act(btn); _wait_until(lambda: not _open_files_wids(), 1.0, .2); return
        except Exception: pass
    g = _xdo_geom(wid)
    # Open is the right-hand action button on the dialog footer.
    _click_xy(g['X'] + g['WIDTH'] - 55, g['Y'] + g['HEIGHT'] - 32)
    time.sleep(1.0)
ATTACH_BUTTON_NAMES = ('Add', 'Add files', 'Attach', 'Add content', 'Add attachment')
ATTACH_FILE_TIMEOUT = float(os.environ.get('CLAUDE_PROJECTS_ATTACH_FILE_TIMEOUT', '20'))   # one file: picker, path, Open, chip
class AttachError(AppError):
    """One file failed at one step; names both."""
    def __init__(self, name, step, detail=''):
        self.file, self.step = name, step
        super().__init__(f"{name}: {step}{(' (' + detail + ')') if detail else ''}; nothing was sent")
class Deadline:
    """The whole send's time limit, enforced inside the job (not by an outside SIGTERM)."""
    def __init__(self, secs=None): self.secs = secs; self.t = (time.time() + secs) if secs else None
    def left(self, cap=None):
        x = float('inf') if self.t is None else max(0.0, self.t - time.time())
        return min(x, cap) if cap is not None else x
    def check(self, what):
        if self.t is not None and time.time() >= self.t: raise AppError(f'send took longer than {int(self.secs)} s ({what}); nothing was sent')
def _find_rev(root, r=None, name=None, maxn=40000):
    """find() that walks children last-first: popups and menus are appended at the end of the document."""
    st = [root] if root is not None else []; n = 0
    while st and n < maxn:
        e = st.pop(); n += 1
        if (r is None or role(e) == r) and (name is None or nm(e) == name): return e
        st.extend(kids(e))
    return None
def _composer_scope(e=None, levels=8):
    """The smallest ancestor of the composer that also holds its Add button: chips and the Add control live there,
    so polling it is cheap (a whole-document walk over AT-SPI takes seconds on a long transcript)."""
    e = e or composer(); node = e
    for _ in range(levels):
        try: par = node.get_parent() if node is not None else None
        except Exception: par = None
        if par is None: break
        node = par
        if find(node, 'push button', pred=lambda x: nm(x) in ATTACH_BUTTON_NAMES) is not None: return node
    return node
def _chip_is(label, name):
    """label (a chip's text, or its "Remove <x>" button name without "Remove ") is the chip for file `name`.
    The name must end at a word boundary, so a.js never matches a chip for a.json."""
    return bool(label) and bool(name) and re.match(re.escape(name) + r'(?![\w.])', label) is not None
def _chip_pred(name): return lambda e: nm(e) == name or (nm(e).startswith('Remove ') and _chip_is(nm(e)[7:], name)) or _chip_is(nm(e), name)
CHIP_NAME_RE = re.compile(r'^[^/\\\n]{0,200}[^/\\\s]\.[A-Za-z0-9]{1,10}$')
def composer_chips(scope=None):
    """Names of the attachment chips in the unsent message, one entry per chip (duplicates kept), read from each
    chip's own "Remove <name>" button inside the composer."""
    root = scope if scope is not None else _composer_scope()
    if root is None: return []
    # only file-like names (name.ext): other "Remove ..." controls near the composer are not attachments
    return [nm(b)[7:].strip() for b in find_all(root, 'push button', lambda e: nm(e).startswith('Remove ') and CHIP_NAME_RE.match(nm(e)[7:].strip()) is not None)]
def _chip_count(name, scope=None): return sum(1 for c in composer_chips(scope) if _chip_is(c, name))
def _extra_chips(chips, ours):
    """-> (chips that are not ours, our names with no chip); each of our names accounts for one chip."""
    left = list(ours or []); extra = []
    for c in chips or []:
        m = next((o for o in left if _chip_is(c, o)), None)
        if m is None: extra.append(c)
        else: left.remove(m)
    return extra, left
def _composer_has_chip(name, scope=None):
    root = scope if scope is not None else main_doc()[0]
    if root is None: return False
    return bool(find(root, 'push button', pred=_chip_pred(name)))
def _remove_chip(name, scope=None):
    """Press the chip's own "Remove <name>" (it only takes the file out of the unsent message). True when it's gone."""
    root = scope if scope is not None else main_doc()[0]
    b = find(root, 'push button', pred=lambda e: nm(e).startswith('Remove ') and _chip_is(nm(e)[7:], name)) if root is not None else None
    if b is None: return not _composer_has_chip(name)
    n0 = _chip_count(name, scope)
    try: act(b, f'Remove {name}')
    except Exception: return False
    if n0 > 1:   # the same file twice: one press takes out one chip, so wait for the count to drop
        return bool(_wait_until(lambda: _chip_count(name, scope) < n0, 3, .25))
    return bool(_wait_until(lambda: not _composer_has_chip(name, scope) and not _composer_has_chip(name), 3, .25))
def _clear_leftover_chips(chips, scope=None):
    """Take chips this send did not add out of the unsent message, each with its own Remove button. -> names still there."""
    for c in list(chips):
        try:
            if not _remove_chip(c, scope): log(f'leftover chip {c!r} did not go away')
        except Exception as ex: log(f'removing leftover chip {c!r} failed: {ex!r}')
    return composer_chips(scope)
# ---- location-bar attach (primary). The GTK chooser is NOT exposed over AT-SPI in this Electron build
# (Chromium owns the ATK root; the only extra claude-desktop frame is an unnamed hidden window), so
# browsing by row name can never find a folder/file. Typing the absolute path into the location bar
# needs no AT-SPI at all; OCR of the chooser header is used only as evidence for the attach-only test.
class AttachCtx:
    """What a send learns once and reuses for every file: the Add button and menu item names, the composer scope."""
    button = item = scope = None
    def ensure(self):
        if self.item is None:
            info = locate_attach_control()
            if not info.get('attach_item'): raise AppError('attach menu item not found; nothing attached')
            self.button, self.item = info['attach_button'], info['attach_item']; self.scope = _composer_scope(); _mark('locate_attach_control')
        return self
def _open_attach_picker(ctx=None, limit=15):
    ctx = (ctx or AttachCtx()).ensure(); t0 = time.time()
    b = find(ctx.scope, 'push button', ctx.button) if ctx.scope is not None else None
    if b is None: ctx.scope = _composer_scope(); b = find(ctx.scope, 'push button', ctx.button) or find(main_doc()[0], 'push button', ctx.button)
    if b is None: raise AppError(f'the {ctx.button!r} button is gone from the composer; nothing attached')
    act(b)
    it = _wait_until(lambda: _find_rev(main_doc()[0], 'menu item', ctx.item), min(5, limit), .15)
    if it is None: xdo('key', 'Escape'); raise AppError(f'the {ctx.item!r} menu item did not show; nothing attached')
    act(it)
    wid = _wait_open_files(max(1, limit - (time.time() - t0)))
    if not wid: raise AppError('Open Files dialog did not appear; nothing attached')
    _ensure_chooser_left_visible(wid)
    return wid
def _chooser_type_location(wid, path):
    """Open the location bar (ctrl+L), replace its text with the absolute path. Never presses Return/Open."""
    xdo('windowactivate', '--sync', str(wid)); time.sleep(.15)
    xdo('key', '--clearmodifiers', 'ctrl+l'); time.sleep(.35)
    xdo('key', '--clearmodifiers', 'ctrl+a'); time.sleep(.1)
    if _has_non_ascii(path):
        if not _paste_via_clipboard(path): raise AppError('could not paste the (non-ASCII) path into Open Files; nothing attached')
    else:
        xdo('type', '--delay', '8', '--clearmodifiers', '--file', '-', inp=path)
    time.sleep(.5)
    if _active_win_name() not in ('Open Files', 'Open File'):
        raise AppError(f'Open Files lost focus while typing the path (active={_active_win_name()!r}); nothing attached')
def _alnum(t): return re.sub(r'[^a-z0-9]', '', (t or '').lower())
def _ocr_contains(text, needle, ratio=0.75):
    import difflib
    a, b = _alnum(text), _alnum(needle)
    if not b: return False
    if b in a: return True
    n = len(b)
    return any(difflib.SequenceMatcher(None, a[i:i + n], b).ratio() >= ratio for i in range(0, max(1, len(a) - n + 1)))
def _chooser_ocr(wid, save=None, needle=None):
    """OCR the chooser's location bar. The chooser (719px) is wider than the 540px screen, so the
    entry's tail (the filename) is off-screen until the dialog is slid left; do that first, then OCR
    only the entry band, upscaled 3x and thresholded. Returns text ('' if tesseract is missing)."""
    from PIL import ImageGrab, ImageOps
    _ensure_chooser_open_visible(wid); time.sleep(.3)
    g = _xdo_geom(wid); sw, sh = _screen_wh()
    im = ImageGrab.grab(xdisplay=os.environ['DISPLAY'])
    if save: im.convert('RGB').save(save, quality=70)
    x0 = max(0, g['X']); x1 = min(sw, g['X'] + g['WIDTH'])
    # Same pipeline that read the path cleanly offline: JPEG round-trip (smooths subpixel-AA text), LANCZOS 3x.
    import io; buf = io.BytesIO(); im.convert('RGB').save(buf, 'JPEG', quality=70); buf.seek(0)
    from PIL import Image as _Img
    gray = ImageOps.grayscale(_Img.open(buf))
    tmp = CACHE/'chooser-ocr.png'; out = []
    # The location entry is a ~30px band just under the title; slide a single-line window over the
    # top of the client area (psm 7). Tuned on the 14:29Z attach-test screenshots.
    # Bands are swept from above to well below the reported Y: xdotool's Y can be the frame or the client
    # origin depending on the WM, and windowmove shifts it. Stop at the first band that shows the needle.
    for off in range(-24, 88, 4):
        y0 = max(0, g['Y'] + off); y1 = min(sh, y0 + 30)
        if y1 <= y0: break
        c = gray.crop((x0, y0, x1, y1)); c = c.resize((c.width * 3, c.height * 3), _Img.LANCZOS).point(lambda v: 255 if v > 170 else 0)
        c.save(tmp)
        try:
            r = subprocess.run(['tesseract', str(tmp), '-', '--psm', '7'], capture_output=True, text=True, timeout=30)
            out.append(r.stdout.strip())
            if needle and _ocr_contains(out[-1], needle): return out[-1]
        except Exception as ex:
            log(f'chooser OCR unavailable: {ex!r}'); break
    return chr(10).join(out)
def _attach_one_via_location(f, test_only=False, shot_dir=None, ctx=None, deadline=None, per_file=None):
    path = str(pathlib.Path(f['path']).expanduser().resolve())
    if not os.path.isfile(path) or not os.access(path, os.R_OK): raise AppError(f'{path} is not a readable file; nothing attached')
    deadline = deadline or Deadline(); limit = deadline.left(per_file or ATTACH_FILE_TIMEOUT); t0 = time.time(); tm = {}
    def left(): return limit - (time.time() - t0)
    try: wid = _open_attach_picker(ctx, limit=max(1, left()))
    except AppError as ex: raise AttachError(f['name'], 'opening the file picker', str(ex).replace('; nothing attached', ''))
    tm['picker'] = time.time() - t0
    _chooser_type_location(wid, path); tm['path'] = time.time() - t0 - sum(tm.values())
    shot = str(pathlib.Path(shot_dir)/f"attach-test-{_alnum(f['name'])[:40]}.jpg") if (test_only and shot_dir) else None
    txt = _chooser_ocr(wid, save=shot, needle=f['name']) if test_only else ''
    if test_only:
        ok = _ocr_contains(txt, f['name'])
        closed = _dismiss_open_files('attach-test cancel')
        log(f"attach-test {f['name']}: typed path, ocr_match={ok}, cancelled={closed}")
        return {'name': f['name'], 'path': path, 'size': os.path.getsize(path), 'typed_in_location_bar': True,
                'ocr_saw_filename': ok, 'ocr_text': ' '.join(txt.split())[:200], 'screenshot': shot, 'cancelled': closed}
    if left() <= 0: raise AttachError(f['name'], f'typing the path took longer than {int(limit)} s')
    _click_chooser_open(None, wid); tm['open'] = time.time() - t0 - sum(tm.values())
    scope = getattr(ctx, 'scope', None); polls = 0
    while True:
        polls += 1
        # the composer scope is cheap to poll; every 5th poll checks the whole document in case the chip sits elsewhere
        if _composer_has_chip(f['name'], scope if (scope is not None and polls % 5) else None):
            tm['chip'] = time.time() - t0 - sum(tm.values())
            log(f"attached {f['name']} via location bar in {time.time() - t0:.1f} s (" + ', '.join(f'{k} {v:.1f}' for k, v in tm.items()) + ')')
            return {'name': f['name'], 'attached': True, 'seconds': round(time.time() - t0, 1), 'timing': {k: round(v, 1) for k, v in tm.items()}}
        if left() <= 0: break
        time.sleep(.3)
    if _open_files_wids(): _dismiss_open_files('attach timeout')
    log(f"attach {f['name']} timed out after {time.time() - t0:.1f} s (" + ', '.join(f'{k} {v:.1f}' for k, v in tm.items()) + ')')
    raise AttachError(f['name'], f'did not appear in the composer within {int(limit)} s after Open')
def attach_files(files, test_only=False, shot_dir=None, done=None, deadline=None, per_file=None, progress=None):
    """Primary: one picker pass per file, absolute path typed into the GTK location bar, each file bounded by per_file
    seconds (CLAUDE_PROJECTS_ATTACH_FILE_TIMEOUT) and the whole send by deadline. done (a list) collects the names attached so far,
    so a caller can take them out again when a later file fails.
    Fallback (only if the path step fails before Open is pressed): legacy browse-by-name.
    test_only: type the path, OCR + screenshot, then Cancel; never presses Open; returns per-file results."""
    if not files: return []
    results = []; done = done if done is not None else []; deadline = deadline or Deadline()
    try:
        ctx = AttachCtx()
        for i, f in enumerate(files):
            deadline.check(f"before attaching {f['name']} ({i + 1}/{len(files)})")
            if progress: progress('attaching', attached=i, total=len(files), file=f['name'])
            try:
                results.append(_attach_one_via_location(f, test_only=test_only, shot_dir=shot_dir, ctx=ctx, deadline=deadline, per_file=per_file))
                if not test_only: done.append(f['name'])
            except AppError as ex:
                if isinstance(ex, AttachError) and ex.step != 'opening the file picker': raise
                if 'after Open' in str(ex) or test_only: raise
                log(f"location-bar attach failed for {f['name']}: {ex}; falling back to browse")
                _dismiss_open_files('location fallback'); deadline.check(f"falling back to browse for {f['name']}")
                _attach_files_impl([f]); results.append({'name': f['name'], 'attached': True, 'via': 'browse'}); done.append(f['name'])
        if progress and not test_only: progress('attached', attached=len(files), total=len(files))
        return results
    except BaseException:
        try: _dismiss_open_files('attach failed')
        except Exception as ex: log(f'dismiss after attach failure failed: {ex!r}')
        raise
def _attach_files_legacy_wrapper(files):
    """Wrapper: on ANY failure, cancel a chooser we opened so it cannot swallow later keystrokes."""
    try:
        return _attach_files_impl(files)
    except BaseException:
        try: _dismiss_open_files('attach failed')
        except Exception as ex: log(f'dismiss after attach failure failed: {ex!r}')
        raise
def _attach_files_impl(files):
    """REAL attach (only reached when not dry_run): Add -> 'Add files or photos' -> GTK Open Files
    -> browse the folder in the file list -> select the requested rows -> click Open, then wait for
    attachment chips in the composer.

    Why not Ctrl+L + type path + Enter (the old finish step): that closes the dialog but no file ever
    reaches the composer (code 6). The GTK chooser used by claude-desktop only accepts files when you
    browse to the folder, select rows, and press the Open button. On a narrow display (e.g. 540px wide) the
    dialog is often wider than the screen, so after selecting, move it so Open is visible, then click.
    """
    if not files: return
    # One picker pass per directory (multi-select within a folder; never confirm early between folders).
    groups, order = {}, []
    for f in files:
        parent = str(pathlib.Path(f['path']).expanduser().resolve().parent)
        if parent not in groups:
            groups[parent] = []; order.append(parent)
        groups[parent].append(f)
    for directory in order:
        group = groups[directory]
        info = locate_attach_control()
        if not info.get('attach_item'): raise AppError('attach menu item not found; nothing attached')
        d, _ = main_doc(); b = find(d, 'push button', info['attach_button']); act(b); time.sleep(1.2)
        it = find(main_doc()[0], 'menu item', info['attach_item']); act(it); time.sleep(2.0)
        wid = _wait_open_files(15)
        if not wid: raise AppError('Open Files dialog did not appear; nothing attached')
        _ensure_chooser_left_visible(wid)
        names = [f['name'] for f in group]
        # Prefer one multi-select pass. If ctrl-select fails (no row extents), cancel and
        # attach each file with its own picker pass so we never grab sibling files.
        root = _file_chooser_root()
        _browse_chooser_to_dir(root, directory)
        root = _file_chooser_root() or root
        multi_ok = _select_chooser_files(root, names)
        if not multi_ok:
            if _active_win_name() == 'Open Files':
                xdo('key', 'Escape'); time.sleep(.6)
            for f in group:
                info2 = locate_attach_control()
                if not info2.get('attach_item'): raise AppError('attach menu item not found; nothing attached')
                d, _ = main_doc(); b = find(d, 'push button', info2['attach_button']); act(b); time.sleep(1.2)
                it = find(main_doc()[0], 'menu item', info2['attach_item']); act(it); time.sleep(2.0)
                wid = _wait_open_files(15)
                if not wid: raise AppError('Open Files dialog did not appear; nothing attached')
                _ensure_chooser_left_visible(wid)
                root = _file_chooser_root()
                _browse_chooser_to_dir(root, directory)
                root = _file_chooser_root() or root
                if not _select_chooser_files(root, [f['name']]):
                    raise AppError(f"could not select {f['name']!r} in Open Files; nothing attached")
                if _active_win_name() != 'Open Files':
                    wids = [w for w in (xdo('search', '--onlyvisible', '--name', 'Open Files') or '').split() if w.strip()]
                    wids = [w for w in wids if (xdo('getwindowname', w) or '').strip() == 'Open Files']
                    if not wids: raise AppError('Open Files dialog disappeared before Open; nothing attached')
                    wid = wids[0]; xdo('windowactivate', '--sync', wid); time.sleep(.3)
                else:
                    wid = xdo('getactivewindow')
                _click_chooser_open(root, wid)
                t0 = time.time(); ok = False
                while time.time() - t0 < 30:
                    if _composer_has_chip(f['name']): ok = True; break
                    time.sleep(1)
                if not ok:
                    if _active_win_name() == 'Open Files':
                        xdo('key', 'Escape'); time.sleep(.5)
                    raise AppError(f"{f['name']} did not appear in the composer; nothing was sent")
            continue
        if _active_win_name() != 'Open Files':
            wids = [w for w in (xdo('search', '--onlyvisible', '--name', 'Open Files') or '').split() if w.strip()]
            wids = [w for w in wids if (xdo('getwindowname', w) or '').strip() == 'Open Files']
            if not wids: raise AppError('Open Files dialog disappeared before Open; nothing attached')
            wid = wids[0]; xdo('windowactivate', '--sync', wid); time.sleep(.3)
        else:
            wid = xdo('getactivewindow')
        _click_chooser_open(root, wid)
        t0 = time.time(); missing = {f['name'] for f in group}
        while time.time() - t0 < 30 and missing:
            missing = {n for n in missing if not _composer_has_chip(n)}
            if missing: time.sleep(1)
        if missing:
            if _active_win_name() == 'Open Files':
                xdo('key', 'Escape'); time.sleep(.5)
            raise AppError(f'{sorted(missing)[0]} did not appear in the composer; nothing was sent')


# ---------------------------------------------------------------- operations
# ---------------------------------------------------------------- permission cards (generic)
def _parent(e):
    try: return e.get_parent()
    except Exception: return None
def _snapshot_subtree(root, maxn=3000):
    """Plain-dict snapshot of an AT-SPI subtree for perm_mod.find_cards. Returns (nodes, objs)."""
    nodes = []; objs = {}; st = [(root, None)]; n = 0
    while st and n < maxn:
        e, par = st.pop(); nid = n; n += 1
        r = role(e); name = nm(e); t = ''
        if r in ('static', 'text', 'label', 'heading', 'paragraph', 'code', 'link') and not name:
            t = text_of(e)[:2000]
        nodes.append({'id': nid, 'parent': par, 'role': r, 'name': name[:500], 'text': t}); objs[nid] = e
        for c in reversed(kids(e)): st.append((c, nid))
    return nodes, objs
def detect_permission_cards(max_up=6, with_objs=False):
    """Read-only: find permission cards in the open page. Cheap when there are none (one role/name pass)."""
    d, _ = main_doc()
    if not d: return ([], {}) if with_objs else []
    cand = [e for e in walk(d, 30000) if role(e) == 'push button' and PERM.OPTION_RE.match(nm(e).strip())]
    if not cand: return ([], {}) if with_objs else []
    tops = []
    for b in cand:
        top = b
        for _ in range(max_up):
            pp = _parent(top)
            if pp is None or pp == d: break
            top = pp
        if not any(t == top for t in tops): tops.append(top)
    cards = []; objs_all = {}; seen = set()
    for i, top in enumerate(tops[:8]):
        nodes, objs = _snapshot_subtree(top)
        for c in PERM.find_cards(nodes, max_up=max_up):
            if c['card_id'] in seen: continue
            seen.add(c['card_id']); c['_objs'] = [objs[x] for x in c['_option_ids']]; cards.append(c)
    return (cards, objs_all) if with_objs else cards
def _safe_detect_cards():
    try: return detect_permission_cards()
    except Exception as ex: log(f'permission detect error {ex}'); return []
def _press_diagnostics(btn):
    """When a pressed card stays: the button's states (busy / held / disabled) and any notice the app showed (toasts,
    status lines), so the caller can tell 'held by the app', 'refused with a toast' and 'ignored' apart."""
    out = {}
    try:
        ss = btn.get_state_set()
        out['button'] = {'name': nm(btn), **{k: ss.contains(getattr(A().StateType, k.upper())) for k in ('busy', 'enabled', 'sensitive', 'showing')}}
    except Exception as ex: out['button_error'] = str(ex)[:120]
    try:
        d, _ = main_doc(); seen = []
        for e in (walk(d, 40000) if d else []):
            if role(e) in ('alert', 'notification', 'status bar', 'log', 'alert dialog'):
                t = ' '.join(text_of(x) for x in walk(e, 50) if text_of(x)).strip()
                if t and t not in seen and 'Choose an option to respond' not in t: seen.append(t[:300])
        out['notices'] = seen[:8]
    except Exception as ex: out['notices_error'] = str(ex)[:120]
    return out
CU_INTRO_TITLE = re.compile(r'background computer use', re.I)
def _finish_computer_use_allow(wait_s=6.0):
    """After Allow on a computer-use card: press 'Continue' in the app's 'Background computer use' intro dialog when it
    opens (it does once per computer). Touches no other dialog or button. Returns the labels pressed."""
    t0 = time.time(); pressed = []
    while time.time() - t0 < wait_s:
        d, _ = main_doc()
        for dl in ([e for e in walk(d, 40000) if role(e) in ('dialog', 'alert dialog')] if d else []):
            txt = ' '.join(text_of(e) for e in walk(dl, 400) if role(e) in ('heading', 'static', 'paragraph', 'label', 'dialog'))
            if not CU_INTRO_TITLE.search(txt + ' ' + nm(dl)): continue
            b = find(dl, 'push button', 'Continue')
            if b:
                act(b, 'computer-use intro Continue'); log('computer-use intro: pressed Continue')
                pressed.append('Continue (Background computer use intro)'); return pressed
        time.sleep(0.5)
    return pressed
def thread_address(chan, tid, primary=None):
    """Stable address of an agent/thread: <project chan_>/<thread id> (primary = cse_, child = cmsg_)."""
    t = tid if tid and tid != 'primary' else (primary or 'primary')
    return f'{chan}/{t}'
def op_permission(project, option, thread=None, card_id=None, dry_run=True, force=False, stay=False, shot=None):
    """Approve / deny a permission card by option label. dry_run=True (default) only reads the card and the button it would press."""
    if not option or not str(option).strip(): raise CPGError('option is required (the exact button label, e.g. "Add folder" or "Not now")')
    p = resolve_project(project); chan = p['id']
    with ui_lock(f'permission {p["name"]} {thread or "primary"}', force):
        try:
            tid = _resolve_tid(p, chan, thread)
            open_target_settled(chan, tid)
            cards = detect_permission_cards()
            if not cards: raise NotFound('no permission card is showing in this thread')
            if card_id:
                card = next((c for c in cards if c['card_id'] == card_id), None)
                if not card: raise NotFound(f'card {card_id!r} is not showing (cards now: {[c["card_id"] for c in cards]}); re-read and retry')
            elif len(cards) == 1: card = cards[0]
            else: raise CPGError(f'{len(cards)} cards are showing; pass card_id. Cards: ' + json.dumps(PERM.public(cards), ensure_ascii=False)[:1500])
            try: label, idx = PERM.match_option(card, option)
            except ValueError as ex: raise CPGError(str(ex))
            base = {'project': p['name'], 'project_id': chan, 'thread_id': tid or 'primary', 'address': thread_address(chan, tid, p.get('primary_thread_id')),
                    'card': PERM.public([card])[0], 'option': label}
            if dry_run:
                if shot: screenshot(shot)
                return dict(base, dry_run=True, would_press=label, note='read-only: the card was found and nothing was pressed')
            act(card['_objs'][idx], f'permission option {label!r}')
            log(f'permission {p["name"]} {tid or "primary"} {card["card_id"]} {card["type"]} -> {label!r}')
            follow = []; affirm = bool(PERM.AFFIRM_RE.match(label))
            if card['type'] == 'computer_use' and affirm:
                # The app answers the first computer-use Allow on a computer with a "Background computer use (Beta)"
                # intro dialog [Not now] [Continue]; the grant is only sent on Continue. Press Continue in that dialog
                # (and nothing else), then wait for the card to go.
                follow = _finish_computer_use_allow()
            still, after = True, []
            for _ in range(12 if card['type'] == 'computer_use' else 1):
                time.sleep(1.5)
                after = detect_permission_cards()
                still = any(c['card_id'] == card['card_id'] for c in after)
                if not still: break
            busy = None
            try: busy = bool(busy_state(tid))
            except Exception: pass
            if shot: screenshot(shot)
            out = dict(base, dry_run=False, pressed=label, card_gone=not still, cards_now=PERM.public(after) or None,
                       pressed_at_local=ct(now().isoformat()), claude_working=busy)
            if follow: out['also_pressed'] = follow
            if still:
                out['diagnostics'] = _press_diagnostics(card['_objs'][idx])
                try: dl = dialogs()
                except Exception: dl = []
                out['warning'] = ('the card is still showing after the press: the app did not take the answer. '
                                  + ('Open dialogs: ' + json.dumps(dl, ensure_ascii=False)[:400] if dl else 'No dialog is open.'))
            log(f'permission {card["card_id"]} result card_gone={not still} also={follow} working={busy}')
            return out
        finally:
            if not stay: go_home()

# ---------------------------------------------------------------- forwarding
FORWARD_RULE = '\u2500' * 24
def forward_header(src_project, src_thread_title, src_chan, src_tid, msg):
    who = 'Claude' if msg.get('role') == 'assistant' else ('a bot' if is_bot_sent(msg.get('text')) else OPERATOR)
    where = f'Project "{src_project}" \u203a ' + (f'thread "{src_thread_title}"' if src_thread_title and src_thread_title != '(primary thread)' else 'primary thread')
    when = f', {msg["time_local"]}' if msg.get('time_local') else ''
    return f'Forwarded from {where} ({src_chan}/{src_tid}), message #{msg.get("index")} by {who}{when}:'
def forward_text(header, content, note=None, extra_refs=None, limit=19000):
    parts = []
    if note and note.strip(): parts.append(note.strip())
    parts.append(header); parts.append(FORWARD_RULE); body = content or ''
    tail = ''
    if extra_refs: tail = '\n' + FORWARD_RULE + '\nNot attached (links / references):\n' + '\n'.join(f'- {r}' for r in extra_refs)
    head = '\n'.join(parts) + '\n'
    room = limit - len(head) - len(tail)
    truncated = len(body) > room
    if truncated: body = body[:max(0, room - 40)] + '\n[... truncated by the forwarder ...]'
    return head + body + tail, truncated
FORWARDABLE_KINDS = ('image', 'file', 'output_file')
def op_forward(source_project, target_project, source_thread=None, target_thread=None, message_index=None, role='assistant',
               note=None, include_attachments=False, refresh_source=True, dry_run=True, bot_name=None, force=False, stay=False):
    """Relay one message verbatim from a source thread to a target thread, under a 'Forwarded from ...' header.
    The origin bracket ([sent by Grok Bot]) is added above everything by op_send, exactly like a normal send."""
    STAGE.maybe_sweep()
    src = op_read(source_project, source_thread, bool(refresh_source), max_messages=30, force=force, stay=True)
    msgs = src.get('messages') or []
    if message_index is not None:
        msg = next((m for m in msgs if m.get('index') == int(message_index)), None)
        if not msg: raise NotFound(f'message #{message_index} is not among the {len(msgs)} rendered messages of the source; read with history_pages to load older ones')
    else:
        pool = [m for m in msgs if (role in (None, '', 'any') or m.get('role') == role)]
        if not pool: raise NotFound(f'no {role} message in the source thread')
        msg = pool[-1]
    src_tid = src.get('thread_id') if src.get('thread_id') not in (None, 'primary') else (src.get('primary_thread_id') or 'primary')
    hdr = forward_header(src.get('project'), src.get('thread_title'), src.get('project_id'), src_tid, msg)
    atts = msg.get('attachments') or []
    fwd = [a for a in atts if a.get('kind') in FORWARDABLE_KINDS] if include_attachments else []
    refs = [(a.get('url') or a.get('name') or a.get('id')) for a in atts if a not in fwd and (a.get('url') or a.get('name'))]
    text, truncated = forward_text(hdr, msg.get('text') or '', note, refs or None)
    source = {'project': src.get('project'), 'project_id': src.get('project_id'), 'thread_id': src_tid,
              'thread_title': src.get('thread_title'), 'address': f'{src.get("project_id")}/{src_tid}', 'message_index': msg.get('index'),
              'role': msg.get('role'), 'time_local': msg.get('time_local'), 'from_cache': bool(src.get('from_cache'))}
    staged = []
    if dry_run:
        r = op_send(target_project, text, target_thread, dry_run=True, force=force, stay=stay, bot_name=bot_name)
        return {'dry_run': True, 'source': source, 'target': {k: r.get(k) for k in ('project', 'project_id', 'thread_id', 'route', 'composer_empty', 'send_button_found')},
                'would_send_chars': r.get('would_send_chars'), 'origin_header': r.get('origin_header'), 'forward_header': hdr,
                'would_send_preview': (r.get('origin_header') or '') + '\n' + text[:600], 'truncated': truncated,
                'would_attach': [{'id': a.get('id'), 'kind': a.get('kind'), 'name': a.get('name')} for a in fwd] or None,
                'note': 'nothing fetched, typed or sent'}
    slot = STAGE.new_slot('outgoing') if fwd else None
    for a in fwd:
        g = op_attachment_get(src.get('project_id'), source_thread, attachment_id=a.get('id'), as_='path', force=force, stay=True)
        pth = g.get('path_on_host') or g.get('path')
        if not pth or not pathlib.Path(pth).is_file(): raise AppError(f'could not fetch attachment {a.get("id")} ({a.get("name")}); nothing was sent')
        dst = slot/STAGE.safe_name(g.get('saved_name') or a.get('name') or pathlib.Path(pth).name)
        shutil.copy2(pth, dst); staged.append({'path': str(dst), 'source_id': a.get('id')})
    r = op_send(target_project, text, target_thread, force=force, stay=stay, bot_name=bot_name,
                attachments=[{'path': x['path']} for x in staged] or None)
    return dict(r, forwarded=True, source=source, forward_header=hdr, truncated=truncated, staged_files=staged or None)

# ---------------------------------------------------------------- staging tool
def op_staging(action='info', dry_run=True, older_than_hours=None, kinds=None):
    if action == 'info': return dict(STAGE.info(), attachments_dir=str(ATT_DIR))
    if action in ('sweep', 'cleanup'): return STAGE.sweep(dry_run=dry_run, older_than_hours=older_than_hours, kinds=kinds)
    raise CPGError("action is 'info' or 'sweep'")
def op_projects(refresh=False, include_archived=False, force=False, stay=False):
    before = None
    if refresh:
        try: before = json.loads((CACHE/'idb.json').read_text()).get('channels_updated')
        except Exception: before = None
        with ui_lock('projects-refresh', force):
            try:
                d, _ = main_doc()
                if not route().startswith('/epitaxy/projects/browse'):
                    if not find(d, 'push button', pred=lambda e: nm(e) in ('Projects Beta', 'Projects')):
                        sb = find(d, 'push button', 'Show sidebar')
                        if sb: act(sb); time.sleep(1.5)
                    b = find(main_doc()[0], 'push button', pred=lambda e: nm(e) in ('Projects Beta', 'Projects'))
                    if b: act(b)
                wait_route(lambda u: (u or '').startswith('/epitaxy/projects/browse'), 15); time.sleep(5)
            finally:
                if not stay: go_home()
    snap = idb_snapshot(); rows = project_rows(snap, include_archived)
    return {'projects': rows, 'count': len(rows), 'list_fetched_by_app_at': snap.get('channels_updated'), 'list_fetched_by_app_local': ct(snap.get('channels_updated')),
            'previous_fetch_local': ct(before) if refresh else None, 'source': 'app cache (IndexedDB copy)' + (' after opening the Projects view' if refresh else '')}
def _read(chan, thread, max_messages, history_pages, expand, with_threads=False, include_resolved=False, show_all=False, include_images=False, markdown=False):
    open_target(chan, thread); ok = settle(thread)
    msgs = read_messages(thread, max_messages, history_pages, expand, include_images, markdown) if ok else []
    try: annotate_known(msgs, chan, thread)
    except Exception: pass
    info = pane_info(thread); threads = None
    if with_threads and not thread: threads = parse_threads(chan, include_resolved, show_all); ensure_threads_panel(False)
    return msgs, info, threads, dialogs()
def op_read(project, thread=None, refresh=True, max_messages=20, history_pages=0, expand=False, force=False, stay=False, shot=None, with_threads=False, include_resolved=False, show_all=False, include_images=False, markdown=False):
    p = resolve_project(project); chan = p['id']
    tid = resolve_thread(chan, thread)
    cached = read_cache(chan, tid if tid and not str(tid).startswith('LOOKUP:') else None)
    if not refresh:
        if not cached: raise NotFound('nothing cached yet for this target; call again with refresh=true')
        cached = dict(cached); cached['from_cache'] = True
        msgs = list(cached.get('messages') or [])
        annotate_messages_staleness(msgs, now()); cached['messages'] = msgs
        # Re-resolve machine: primaries often lack UI label; pick up env_id from idb when present.
        look_id = (None if (cached.get('thread_id') in (None, 'primary')) else cached.get('thread_id')) or p.get('primary_thread_id')
        eid, cst = idb_env_fields_for_thread(look_id)
        ei_prev = cached.get('environment_info') or {}
        if not eid: eid = ei_prev.get('environment_id') or None
        if not cst: cst = ei_prev.get('connection_status')
        cached['environment_info'] = resolve_environment(
            raw_ui=cached.get('environment') or ei_prev.get('environment_raw'),
            env_id=eid, connection_status=cst, source='cached', read_at=cached.get('read_at'))
        cached['machine'] = cached['environment_info']['machine']
        th_meta = None
        if cached.get('thread_id') and cached.get('thread_id') != 'primary':
            th_meta = next((t for t in (cached.get('threads') or []) if t.get('thread_id') == cached.get('thread_id')), None)
        cached['current_state'] = build_current_state(msgs, cached.get('claude_working'), th_meta, None, now(), permission_cards=cached.get('permission_cards'))
        cached['address'] = thread_address(chan, cached.get('thread_id'), p.get('primary_thread_id'))
        try:
            cached['cache_age_min'] = round((now() - datetime.datetime.fromisoformat(str(cached.get('read_at','')).replace('Z','+00:00'))).total_seconds()/60.0, 1)
        except Exception: cached['cache_age_min'] = None
        if cached.get('threads'):
            cached['threads'] = normalize_thread_rows([dict(t) for t in cached['threads']])
            for trow in cached['threads']:
                trow['title'] = clean_thread_title(trow.get('title'))
                if not trow.get('current_state'):
                    trow['current_state'] = build_current_state([], False, trow, None)
                    trow['current_state']['summary'] = None
                    trow['current_state']['note'] = 'Sidebar-only status from cache; call claude_thread_read/thread-status for live latest-message summary.'
        return cached
    with ui_lock(f'read {p["name"]} {thread or "primary"}', force):
        try:
            if tid and str(tid).startswith('LOOKUP:'):
                open_target(chan, None); settle(None, 15); ths = parse_threads(chan, True, True); ensure_threads_panel(False)
                c = read_cache(chan) or {}; c['threads'] = ths; write_cache(chan, None, c) if c.get('project') else None
                tid = resolve_thread(chan, tid[7:], ths)
                if str(tid).startswith('LOOKUP:'): raise NotFound(f'no thread titled {tid[7:]!r} in {p["name"]}; see claude_project_get threads')
            msgs, info, threads, dl = _read(chan, tid, max_messages, history_pages, expand, with_threads and not tid, include_resolved, show_all, include_images, markdown)
            if tid and str(tid).startswith('cmsg_'):
                title = (info.get('thread_title') or '').strip()
                if title in ('', 'Thread') and not msgs:
                    raise NotFound(f'thread {tid!r} opened no transcript (title={title!r}); not falling back to primary')
            busy = busy_state(tid)
            pcards = _safe_detect_cards()
            if shot: screenshot(shot)
            read_ts = now().isoformat()
            annotate_messages_staleness(msgs, now())
            # Match thread sidebar row (for status) when we have threads on a project get, else from cache
            th_meta = None
            th_list = threads if threads is not None else ((cached or {}).get('threads') or [])
            if tid and not th_list:
                th_list = (read_cache(chan) or {}).get('threads') or []
            if tid and th_list:
                th_meta = next((t for t in th_list if t.get('thread_id') == tid), None)
            # Prefer UI "Running on …"; else env_id from IndexedDB (primaries often have no UI button).
            look_id = tid or p.get('primary_thread_id')
            eid, cst = idb_env_fields_for_thread(look_id)
            if not eid and p.get('primary_thread_id') and look_id != p.get('primary_thread_id'):
                eid2, cst2 = idb_env_fields_for_thread(p.get('primary_thread_id'))
                eid = eid or eid2; cst = cst or cst2
            env_block = resolve_environment(
                raw_ui=info.get('environment'),
                env_id=eid,
                connection_status=cst,
                source='live',
                read_at=read_ts,
            )
            # Keep legacy string for back-compat, but prefer machine/current_state
            res = {'project': p['name'], 'project_id': chan, 'primary_thread_id': p['primary_thread_id'], 'thread_id': tid or 'primary',
                   'thread_title': (info.get('thread_title') if tid else '(primary thread)'), 'route': route(), 'read_at': read_ts, 'read_at_local': ct(read_ts),
                   'previous_read_local': ct((cached or {}).get('read_at')), 'claude_working': busy,
                   'environment': info.get('environment'),  # legacy raw UI string (may be null on primary)
                   'machine': env_block['machine'], 'environment_info': env_block,
                   'threads_waiting_on_you': info.get('threads_waiting'), 'dialogs': dl or None,
                   'messages': msgs, 'message_count_returned': len(msgs), 'latest_message_index': msgs[-1]['index'] if msgs else None,
                   'current_state': build_current_state(msgs, busy, th_meta, info, now(), permission_cards=pcards),
                   'address': thread_address(chan, tid, p.get('primary_thread_id')),
                   'permission_cards': PERM.public(pcards) or None,
                   'note': 'Only the messages the app has rendered are returned (newest last); pass history_pages>0 to scroll further back. '
                           'Use current_state + the latest message (is_latest=true) as live fact; older messages are historical/superseded. '
                           f'Use machine.resolved (not environment alone) for where the thread runs; the app UI always runs on {HOST_LABEL}.'}
            if threads is not None:
                for t in threads:
                    t['title'] = clean_thread_title(t.get('title'))
                    # per-thread state from sidebar (full transcript current_state comes from claude_thread_read)
                    t['current_state'] = build_current_state([], busy=False, thread_meta=t, info=None)
                    t['current_state']['summary'] = None
                    t['current_state']['note'] = 'Sidebar-only status; call claude_thread_read for live latest-message summary.'
                res['threads'] = threads
            elif not tid and cached and cached.get('threads'): res['threads_cached'] = normalize_thread_rows([dict(t) for t in cached['threads']])
            res['threads_waiting'] = info.get('threads_waiting')
            if include_images:  # keep thumbnails out of the on-disk cache
                import copy; cres = copy.deepcopy(res)
                for m in cres['messages']:
                    for at in m.get('attachments') or []: at.pop('thumbnail', None)
                write_cache(chan, tid, cres)
            else: write_cache(chan, tid, res)
            return res
        finally:
            if not stay: go_home()
def op_project_get(project, refresh=True, include_resolved=False, show_all=False, max_messages=10, force=False, stay=False, shot=None, include_images=False, markdown=False):
    r = op_read(project, None, refresh, max_messages, 0, False, force, stay, shot, with_threads=True, include_resolved=include_resolved, show_all=show_all, include_images=include_images, markdown=markdown)
    return r
def _verify_sent(thread, text, before_max, secs=40):
    t0 = time.time(); want = norm(text)[:120].casefold()
    while time.time() - t0 < secs:
        try:
            for m in read_messages(thread, 0):
                if m['index'] > before_max and m['role'] == 'user' and want[:60] in norm(m['text']).casefold(): return m
        except Exception: pass
        time.sleep(2)
    return None
def _has_non_ascii(s):
    return any(ord(c) > 127 for c in (s or ''))

def normalize_for_send_compare(s):
    """NFC + CRLF→LF + per-line rstrip of trailing spaces/tabs; blank lines KEPT.

    Do NOT use norm() here — collapsing all whitespace hides NBSP→space and newline loss.
    """
    s = unicodedata.normalize('NFC', s or '')
    s = s.replace('\r\n', '\n').replace('\r', '\n')
    return '\n'.join(ln.rstrip(' \t') for ln in s.split('\n'))

def send_text_diff(intended, got):
    """Return (ok, unified_diff_or_None)."""
    import difflib
    a = normalize_for_send_compare(intended)
    b = normalize_for_send_compare(got)
    if a == b: return True, None
    diff = ''.join(difflib.unified_diff(
        a.splitlines(keepends=True), b.splitlines(keepends=True),
        fromfile='intended', tofile='got', n=2))
    return False, (diff or f'length intended={len(a)} got={len(b)}')[:2000]

def _xclip_bin():
    p = pathlib.Path(_cfg_tool(CFG, 'xclip'))
    return str(p) if p.is_file() and os.access(p, os.X_OK) else None

def clipboard_get():
    """Read CLIPBOARD on the app display. Returns str or None on failure."""
    exe = _xclip_bin()
    if not exe: return None
    env = dict(os.environ, DISPLAY=os.environ.get('DISPLAY', CFG['display']))
    try:
        r = subprocess.run([exe, '-selection', 'clipboard', '-o'], capture_output=True, env=env, timeout=5)
        if r.returncode != 0: return None
        return r.stdout.decode('utf-8', errors='replace')
    except Exception:
        return None

def clipboard_set(text):
    """Set CLIPBOARD on the app display via xclip (preferred for non-ASCII). Returns True on success.
    Important: do not capture xclip stdout/stderr on set — a tty-less xclip with pipes stays foreground and hangs;
    with DEVNULL it daemonizes a child that holds the selection."""
    exe = _xclip_bin()
    if not exe: return False
    env = dict(os.environ, DISPLAY=os.environ.get('DISPLAY', CFG['display']))
    try:
        r = subprocess.run([exe, '-selection', 'clipboard'], input=text.encode('utf-8'), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, timeout=5)
        if r.returncode not in (0, None):
            log(f'clipboard_set xclip rc={r.returncode}'); return False
        time.sleep(0.05)
        r2 = subprocess.run([exe, '-selection', 'clipboard', '-o'], capture_output=True, env=env, timeout=5)
        got = r2.stdout.decode('utf-8') if r2.stdout else ''
        if r2.returncode != 0 or got != text:
            log(f'clipboard_set verify mismatch got={len(got)} want={len(text)}'); return False
        return True
    except Exception as ex:
        log(f'clipboard_set failed: {ex}'); return False

def _clear_composer_keys():
    xdo('key', '--clearmodifiers', 'ctrl+a'); time.sleep(0.05)
    xdo('key', '--clearmodifiers', 'BackSpace'); time.sleep(0.2)

def _focus_composer(e):
    last_err = None
    for attempt in range(1, 4):
        try:
            e.get_component_iface().grab_focus()
            last_err = None
            break
        except Exception as ex:
            last_err = ex; time.sleep(0.4 * attempt)
    time.sleep(.2)
    wname = xdo('getactivewindow', 'getwindowname')
    if (wname or '').strip() != 'Claude':
        try:
            wid = next((w for w in (xdo('search', '--onlyvisible', '--name', '^Claude$') or '').split('\n') if w.strip() and (xdo('getwindowname', w) or '').strip() == 'Claude'), '')
        except Exception as ex:
            wid = ''; last_err = last_err or ex
        if wid:
            xdo('windowactivate', '--sync', wid); time.sleep(.3)
            for attempt in range(1, 4):
                try:
                    e.get_component_iface().grab_focus(); last_err = None; break
                except Exception as ex:
                    last_err = ex; time.sleep(0.4 * attempt)
        else:
            raise AppError(f'could not focus the Claude window to type (active={wname!r}); retry the send')
    if last_err:
        log(f'type_text focus warning: {last_err}')
    return last_err

def atspi_insert_text(e, text):
    """First-choice: AT-SPI EditableText set_text_contents / insert_text. Returns True on apparent success."""
    text = unicodedata.normalize('NFC', text or '')
    try:
        # Prefer set_text_contents (replaces all). Fall back to insert at 0 after clear.
        try:
            ok = e.set_text_contents(text)
            if ok is False:
                raise RuntimeError('set_text_contents returned False')
            log(f'atspi_insert_text set_text_contents ({len(text)} chars)')
            time.sleep(0.2)
            return True
        except Exception as ex1:
            try:
                # Clear via editable delete if possible, then insert
                try:
                    n = A().Text.get_character_count(e)
                    if n: e.delete_text(0, n)
                except Exception:
                    pass
                ok = e.insert_text(0, text, len(text))
                if ok is False:
                    raise RuntimeError(f'insert_text returned False ({ex1})')
                log(f'atspi_insert_text insert_text ({len(text)} chars)')
                time.sleep(0.2)
                return True
            except Exception as ex2:
                log(f'atspi_insert_text failed: set={ex1!r} insert={ex2!r}')
                return False
    except Exception as ex:
        log(f'atspi_insert_text fatal: {ex!r}'); return False

def _paste_via_clipboard(text):
    """Save/restore user clipboard around a ctrl+v paste. Returns True if paste keys were sent."""
    prev = clipboard_get()
    try:
        if not clipboard_set(text):
            return False
        xdo('key', '--clearmodifiers', 'ctrl+v')
        time.sleep(0.5)
        return True
    finally:
        try:
            if prev is None:
                # best-effort clear
                clipboard_set('')
            else:
                clipboard_set(prev)
        except Exception:
            pass

def _type_ascii_chunks(text, chunk_size=800, timeout_s=120):
    """xdotool-type ASCII (and safe newlines via shift+Return). Raises AppError on timeout; caller clears."""
    t0 = time.time()
    lines = text.split('\n')
    for i, line in enumerate(lines):
        if time.time() - t0 > timeout_s:
            raise AppError(f'typing timed out after {timeout_s}s; box should be cleared')
        # chunk long lines
        pos = 0
        while pos < len(line):
            if time.time() - t0 > timeout_s:
                raise AppError(f'typing timed out after {timeout_s}s; box should be cleared')
            piece = line[pos:pos + chunk_size]
            xdo('type', '--delay', '12', '--clearmodifiers', '--file', '-', inp=piece)
            pos += chunk_size
        if i < len(lines) - 1:
            xdo('key', '--clearmodifiers', 'shift+Return')
    time.sleep(0.4)


def _click_composer(e):
    """Click the center of the composer using AT-SPI screen extents."""
    ext = e.get_component_iface().get_extents(0)
    if ext.width < 2 or ext.height < 2:
        raise RuntimeError(f'composer extents too small: {ext.width}x{ext.height}')
    x = int(ext.x + ext.width // 2)
    y = int(ext.y + ext.height // 2)
    xdo('mousemove', '--sync', str(x), str(y))
    time.sleep(0.05)
    xdo('click', '1')
    time.sleep(0.2)

def _wait_composer_match(intended, e=None, timeout_s=2.5, poll_s=0.15):
    """Poll composer_text until it matches `intended` (send-normalize) or timeout.

    Re-finds the composer each poll because ProseMirror may replace the AT-SPI node
    after a paste. Returns (ok, last_got).
    """
    t0 = time.time()
    last = ''
    while True:
        cur = composer() or e
        last = composer_text(cur) if cur else ''
        ok, _ = send_text_diff(intended, last)
        if ok:
            return True, last
        if time.time() - t0 >= timeout_s:
            return False, last
        time.sleep(poll_s)

def type_text(e, text, read_back=None):
    """Enter text into the composer (or any editable AT-SPI entry).

    Order: AT-SPI EditableText -> clipboard paste (save/restore) -> click+paste retry
    -> per-line xdotool type. Each insert is verified by polling composer_text;
    a method that reports success but leaves the box empty falls through instead of
    returning. Refuses Tab before keystroke paths. Converts CR/CRLF to LF. NFC on the way in.
    read_back: optional callable () -> str for verification helpers; unused here.
    """
    if text is None:
        text = ''
    # Refuse Tab before any keystroke path (steals focus). Convert CR/CRLF to LF.
    if '\t' in text:
        raise Refused('message contains a Tab character; refusing (Tab steals focus from the composer). Convert tabs to spaces first.')
    text_nfc = unicodedata.normalize('NFC', text).replace('\r\n', '\n').replace('\r', '\n')

    _focus_composer(e)

    # 1) AT-SPI EditableText
    if atspi_insert_text(e, text_nfc):
        ok, got = _wait_composer_match(text_nfc, e)
        if ok:
            return
        log(f'atspi_insert_text readback mismatch (got {len(got)} chars); falling through')

    # 2) Clipboard paste (works for non-ASCII; also fine for ASCII when AT-SPI failed)
    if _paste_via_clipboard(text_nfc):
        log(f'type_text clipboard paste ({len(text_nfc)} chars)')
        ok, got = _wait_composer_match(text_nfc, e)
        if ok:
            return
        log(f'clipboard paste readback mismatch (got {len(got)} chars); click and retry')
        try:
            _click_composer(composer() or e)
        except Exception as ex:
            log(f'composer click failed: {ex!r}')
        if _paste_via_clipboard(text_nfc):
            ok, got = _wait_composer_match(text_nfc, e, timeout_s=3.0)
            if ok:
                return
            log(f'clipboard paste retry still mismatched (got {len(got)} chars)')
            if _open_files_wids():
                raise AppError('keystrokes went to a GTK Open Files chooser, not the composer; nothing was sent')

    # 3) Keystroke fallback - ASCII-safe path; refuse leftover non-ASCII
    if _has_non_ascii(text_nfc):
        raise AppError('could not enter non-ASCII text (AT-SPI and clipboard both failed); nothing left in the box')

    try:
        _type_ascii_chunks(text_nfc)
        ok, got = _wait_composer_match(text_nfc, e)
        if ok:
            return
        raise AppError(f'xdotool type readback mismatch (got {len(got)} chars); box should be cleared')
    except Exception:
        try:
            _clear_composer_keys()
        except Exception:
            pass
        raise

def open_target_settled(chan, tid, maxs=25):
    """Open a target and require settle() True. On False: go_home, reopen once; still False → Busy (retryable)."""
    open_target(chan, tid)
    if settle(tid, maxs):
        return True
    log(f'settle failed for chan={chan} tid={tid}; go_home and retry once')
    try: go_home()
    except Exception as ex: log(f'go_home after settle fail: {ex!r}')
    time.sleep(1.0)
    open_target(chan, tid)
    if settle(tid, maxs):
        return True
    raise Busy(f'transcript panel did not settle for {tid or "primary"} after reopen; retry')

def _cleanup_composer(names, scope=None):
    """After a failed send: close a file picker, take our chips out of the unsent message, clear typed text.
    -> report of what was done and what is still there. Never presses Send."""
    rep = {'open_files_closed': None, 'chips_removed': [], 'chips_left': [], 'composer_empty': None}
    try: rep['open_files_closed'] = _dismiss_open_files('send cleanup')
    except BaseException as ex: log(f'cleanup: dismiss failed {ex!r}')
    for nme in names or []:
        try: (rep['chips_removed'] if _remove_chip(nme, scope) else rep['chips_left']).append(nme)
        except BaseException as ex: rep['chips_left'].append(nme); log(f'cleanup: remove {nme} failed {ex!r}')
    try:
        e = composer()
        if e is not None and norm(composer_text(e)): _clear_composer_keys(); time.sleep(.3)
        e = composer(); rep['composer_empty'] = bool(e is not None and not norm(composer_text(e)))
    except BaseException as ex: log(f'cleanup: clear failed {ex!r}')
    log(f'send cleanup: {rep}')
    return rep
def _attach_test_open(p, tid, files, chips0, e, dl, tmr):
    """Dry run that really attaches: per file press Open, wait for the chip, then take it out with its own
    "Remove <name>" button. The message is never typed and Send is never pressed. The file does upload to claude.ai
    with the chip (that is what Open does); removing the chip leaves it out of any message."""
    if chips0: raise Refused(f'the message box already holds attachment(s) {chips0}; the open test needs an empty box. Nothing was attached.')
    ctx = AttachCtx(); res = []; attached = []
    try:
        for i, f in enumerate(files):
            dl.check(f"before attaching {f['name']} ({i + 1}/{len(files)})")
            t0 = time.time()
            r = _attach_one_via_location(f, test_only=False, ctx=ctx, deadline=dl); attached.append(f['name']); _mark('attach')
            r['removed'] = bool(_remove_chip(f['name'], ctx.scope)); _mark('remove')
            if r['removed']: attached.remove(f['name'])
            r['total_seconds'] = round(time.time() - t0, 1); res.append(r)
            log(f"attach-test open {f['name']}: chip after {r.get('seconds')} s, removed={r['removed']}")
    except BaseException as ex:
        rep = _cleanup_composer(attached)
        try: ex.cleanup = rep
        except Exception: pass
        raise
    chips = composer_chips(); text_left = norm(composer_text(composer() or e))
    return {'dry_run': True, 'attach_test': 'open', 'project': p['name'], 'thread_id': tid or 'primary', 'files': res,
            'all_attached': all(r.get('attached') for r in res), 'all_removed': all(r.get('removed') for r in res),
            'composer_chips': chips, 'composer_empty': not text_left and not chips, 'open_files_left': len(_open_files_wids()),
            'timing': tmr.as_dict(),
            'note': 'Open pressed per file, chip seen, then removed with its own Remove button; the message was not typed and Send was NOT pressed'}
_PROGRESS = None   # set by a send job's worker: progress(state, **detail)
def _progress(state, **kw):
    if _PROGRESS:
        try: _PROGRESS(state, **kw)
        except Exception as ex: log(f'progress hook failed: {ex!r}')
def op_send(project, message, thread=None, wait=False, timeout=600, poll=10, force=False, stay=False, shot=None, dry_run=False, type_test=False, attachments=None, bot_name=None, attach_test=False, deadline_s=None, prepared_files=None, clear_leftovers=False):
    """attach_test (dry runs only): True/'cancel' types each path in the picker and Cancels; 'open' presses Open, waits
    for the chip, removes it with its own Remove button and checks the box is empty. Neither types the message or Sends.
    clear_leftovers (real sends): chips already in the box that this send didn't add are removed first (never sent);
    without it such a send is refused."""
    STAGE.maybe_sweep()
    attach_mode = 'open' if attach_test == 'open' else ('cancel' if attach_test else None)
    files = prepared_files if prepared_files is not None else (prepare_attachment_files(attachments) if attachments else [])
    dl = Deadline(deadline_s)
    if (not message or not message.strip()) and not files: raise CPGError('message is empty')
    # Origin bracket: every message this plugin types starts with "[sent by Grok Bot]" (never doubled).
    message = add_origin(message or '', bot_name if bot_name is not None else CFG.get('bot_name'))
    if len(message) > 20000: raise CPGError('message longer than 20000 characters')
    p = resolve_project(project); chan = p['id']; tid = resolve_thread(chan, thread)
    with step_timer(f'send {p["name"]} {thread or "primary"}') as tmr, ui_lock(f'send {p["name"]} {thread or "primary"}', force, timeout=max(UI_LOCK_DEFAULT_TIMEOUT, int(timeout or 0))):
        try:
            _mark('lock')
            if tid and str(tid).startswith('LOOKUP:'):
                open_target(chan, None); settle(None, 15); ths = parse_threads(chan, True, True); ensure_threads_panel(False)
                tid = resolve_thread(chan, tid[7:], ths)
                if str(tid).startswith('LOOKUP:'): raise NotFound(f'no thread titled {thread!r} in {p["name"]}')
            _progress('opening'); open_target(chan, tid); settle(tid)
            dlg_open = dialogs()
            if dlg_open: raise AppError(f'a dialog is open in the app, not sending: {dlg_open}')
            if _open_files_wids():
                if dry_run and not type_test:
                    raise AppError('a GTK Open Files chooser is open on the app display (left by a failed attach); a real send would dismiss it first. Nothing typed.')
                if not _dismiss_open_files('pre-send'):
                    raise AppError('a GTK Open Files chooser is open on the app display and could not be closed; not sending')
            before = read_messages(tid, 0); bmax = max([m['index'] for m in before], default=0)
            e = composer()
            if not e: raise AppError('composer (message box) not found on this page — the thread may still be loading, or a dialog is covering it; retry')
            draft = composer_text(e)
            if norm(draft): raise Refused(f'the message box already holds an unsent draft (maybe typed by {OPERATOR}); not overwriting it')
            chips0 = composer_chips()      # attachments already in the box: a real send never sends them along
            d, _ = main_doc()
            sb = [b for b in find_all(d, 'push button', lambda b: nm(b) in ('Send', 'Send message', 'Submit'))]
            att_info = locate_attach_control() if (files and dry_run) else None
            if dry_run and attach_mode == 'open':
                if not files: raise CPGError('--attach-test=open needs --attach files')
                return _attach_test_open(p, tid, files, chips0, e, dl, tmr)
            if dry_run and attach_mode:
                if not files: raise CPGError('--attach-test needs --attach files')
                sd = STAGE.new_slot('attach-test')
                res = attach_files(files, test_only=True, shot_dir=str(sd))
                leftover = [f['name'] for f in files if _composer_has_chip(f['name'])]
                chips = composer_chips()
                return {'dry_run': True, 'attach_test': 'cancel', 'project': p['name'], 'thread_id': tid or 'primary', 'files': res,
                        'all_found': all(r.get('ocr_saw_filename') for r in res), 'all_cancelled': all(r.get('cancelled') for r in res),
                        'open_files_left': len(_open_files_wids()), 'composer_chips_left': leftover or None, 'composer_chips': chips,
                        'composer_empty': not norm(composer_text(composer() or e)) and not chips, 'active_window': _active_win_name(), 'ocr_dir': str(sd),
                        'timing': tmr.as_dict(), 'note': 'picker opened per file, path typed, then Cancelled; Open and Send were NOT pressed'}
            if dry_run and not type_test:
                if shot: screenshot(shot)
                r = {'dry_run': True, 'project': p['name'], 'project_id': chan, 'thread_id': tid or 'primary', 'route': route(),
                     'composer': nm(e), 'composer_empty': not norm(composer_text(composer() or e)) and not chips0, 'composer_chips': chips0, 'send_button_found': bool(sb), 'latest_message_index': bmax,
                     'would_send_chars': len(message), 'origin_header': message.split(chr(10), 1)[0], 'would_send_preview': message[:200], 'note': 'nothing typed, nothing attached, nothing sent'}
                if files: r.update(would_attach=files, attach_control=att_info, attach_note='dry run stops at locating the attach control: attaching uploads the file to claude.ai')
                if chips0: r['leftover_note'] = (f'the box already holds {len(chips0)} attachment(s); a real send refuses unless clear_leftovers=true, '
                                                 'which removes them first (they are never sent)')
                r['timing'] = tmr.as_dict()
                return r
            if dry_run and files: raise CPGError('type_test with attachments is not supported (attaching uploads the file); use plain --dry-run')
            cleared = None
            if chips0 and not dry_run:
                if not clear_leftovers:
                    raise Refused(f'the message box already holds attachment(s) this send did not add: {chips0}. Nothing was typed or sent. '
                                  'Pass clear_leftovers=true to remove them first (they are never sent).')
                still = _clear_leftover_chips(chips0)
                if still: raise AppError(f'could not remove leftover attachment(s) {still} from the message box; nothing was typed or sent')
                cleared = chips0; log(f'removed leftover chips before sending: {chips0}'); _mark('clear_leftovers')
            attached = []; send_pressed = False
            try:
                if files: attach_files(files, done=attached, deadline=dl, progress=_progress); e = composer() or e
                dl.check('before typing'); _progress('typing', attached=len(attached), total=len(files))
                if message: type_text(e, message)
                dl.check('after typing')
                got = composer_text(composer() or e)
                if dry_run:
                    typed_ok, diff = send_text_diff(message, got)
                    if shot: screenshot(shot)
                    _clear_composer_keys(); time.sleep(.5)
                    left = composer_text(composer() or e)
                    out = {'dry_run': True, 'typed_and_cleared': True, 'project': p['name'], 'thread_id': tid or 'primary', 'route': route(),
                            'composer': nm(e), 'typed_text_matched': typed_ok, 'composer_empty_after_clear': not bool(normalize_for_send_compare(left)),
                            'send_button_found': bool(sb), 'latest_message_index': bmax, 'note': 'Send was NOT pressed'}
                    if not typed_ok: out['typed_diff'] = diff
                    return out
                ok_match, diff = send_text_diff(message, got)
                if not ok_match:
                    _clear_composer_keys()
                    raise AppError('typed text did not match; cleared the box, nothing was sent' + chr(10) + (diff or ''))
                # last look before Send: exactly our chips, nothing else (a stray chip would go out with the message)
                extra, missing = _extra_chips(composer_chips(), attached)
                if extra: raise AppError(f'the message box holds attachment(s) this send did not add: {extra}; stopped before Send, nothing was sent')
                if missing: raise AppError(f'attached file(s) {missing} are no longer in the message box; stopped before Send, nothing was sent')
                d, _ = main_doc()
                sb = [b for b in find_all(d, 'push button', lambda b: nm(b) in ('Send', 'Send message', 'Submit'))]
                _progress('sending', attached=len(attached), total=len(files)); send_pressed = True
                if sb: act(sb[-1], 'Send')
                else: xdo('key', '--clearmodifiers', 'Return')
                log(f'sent {len(message)} chars to {p["name"]} {tid or "primary"}: {message[:80]!r}')
                _progress('verifying', attached=len(attached), total=len(files))
                m = _verify_sent(tid, message, bmax)
            except BaseException as ex:
                if send_pressed: raise                     # it went out: never clear or retry after Send
                rep = _cleanup_composer(attached)
                try: ex.cleanup = rep
                except Exception: pass
                log(f'send to {p["name"]} {tid or "primary"} stopped before Send ({type(ex).__name__}: {ex}); cleanup {rep}')
                raise
            res = {'project': p['name'], 'project_id': chan, 'thread_id': tid or 'primary', 'sent': True, 'verified_in_transcript': bool(m), 'attached': [f['name'] for f in files] or None,
                   'message_index': m['index'] if m else None, 'origin_header': message.split(chr(10), 1)[0], 'sent_at_local': ct(now().isoformat()), 'route': route(),
                   'cleared_leftovers': cleared, 'timing': tmr.as_dict()}
            if shot: screenshot(shot)
        finally:
            if not (stay or wait): go_home()
    if wait:
        w = op_wait(p['id'], tid, after_index=res['message_index'] or bmax, timeout=timeout, poll=poll, force=force, stay=stay, shot=shot)
        res['reply'] = w
    return res
# ---- sends as background jobs: the MCP tool answers within its budget; a retry joins the job and never sends twice
SEND_BUDGET = float(os.environ.get('CLAUDE_PROJECTS_SEND_BUDGET', '40'))
SEND_DEADLINE = float(os.environ.get('CLAUDE_PROJECTS_SEND_DEADLINE', '420'))          # a whole send, enforced inside the job
SEND_DEDUPE_WINDOW = float(os.environ.get('CLAUDE_PROJECTS_SEND_DEDUPE_S', '1800'))    # the same send again within this window joins the old job
SEND_FINAL = ('sent', 'failed')
def send_job_key(chan, thread, message, files, idempotency_key=None):
    if idempotency_key: base = f'key|{chan}|{idempotency_key}'
    else:
        fs = '|'.join(f"{f['name']}:{f['size']}" for f in files or [])
        base = f"{chan}|{norm(str(thread)).casefold() if thread else 'primary'}|{message}|{fs}"
    return 's' + hashlib.sha256(base.encode()).hexdigest()[:23]
def _send_state_public(st):
    out = {k: st.get(k) for k in ('job_id', 'state', 'step', 'attached', 'total', 'file', 'error', 'error_type', 'cleanup', 'project', 'thread_id') if st.get(k) is not None}
    if st.get('started_ts'): out['started_at_local'] = ct(datetime.datetime.fromtimestamp(st['started_ts'], datetime.timezone.utc).isoformat())
    if st.get('finished_ts'): out['finished_at_local'] = ct(datetime.datetime.fromtimestamp(st['finished_ts'], datetime.timezone.utc).isoformat())
    if st.get('state') == 'attaching' and st.get('total'): out['progress'] = f"attaching {min(st.get('attached', 0) + 1, st['total'])}/{st['total']}"
    return out
def _send_answer(st, joined=False):
    if st.get('state') == 'sent':
        r = dict(st.get('result') or {}); r['job_id'] = st['job_id']
        if joined:
            r['joined_existing_send'] = True
            r['note'] = (f'this send already went out (same message and files within {int(SEND_DEDUPE_WINDOW // 60)} min, or the same idempotency_key); '
                         'nothing was sent again. To send the same text again on purpose, pass a new idempotency_key.')
        return r
    if st.get('state') == 'failed':
        out = dict(_send_state_public(st), sent=False, failed=True)
        out['note'] = 'nothing was sent' + ('; ' + st['cleanup_note'] if st.get('cleanup_note') else '') + '. Calling again starts a new attempt.'
        return out
    return dict(_send_state_public(st), pending=True, sent=False, retry_after_s=15,
                note='still sending in the app; call claude_thread_send again with the same arguments (or job_id) for the result. '
                     'It joins this send and never types or sends the message twice.')
def op_send_job(project, message, thread=None, force=False, attachments=None, bot_name=None, budget=None, idempotency_key=None, job_id=None, clear_leftovers=False):
    """A send that answers within `budget` seconds: the send itself runs in a detached worker."""
    budget = SEND_BUDGET if budget is None else budget
    if job_id:
        st = job_read(SEND_JOBS, job_id)
        if not st: raise NotFound(f'no send job {job_id}')
        if st.get('state') not in SEND_FINAL and not job_running(SEND_JOBS, job_id):
            st = dict(st, state='failed', error='the send job ended without a result (killed?)', error_type='AppError'); job_write(SEND_JOBS, job_id, st)
        return _send_answer(st)
    STAGE.maybe_sweep()
    files = prepare_attachment_files(attachments) if attachments else []
    if (not message or not message.strip()) and not files: raise CPGError('message is empty')
    full = add_origin(message or '', bot_name if bot_name is not None else CFG.get('bot_name'))
    if len(full) > 20000: raise CPGError('message longer than 20000 characters')
    p = resolve_project(project); chan = p['id']; key = send_job_key(chan, thread, full, files, idempotency_key)
    st = job_read(SEND_JOBS, key); t_start = time.time()
    if st and job_running(SEND_JOBS, key): joined = True                     # a retry while the send runs: join it
    elif st and st.get('state') == 'sent' and time.time() - (st.get('finished_ts') or 0) < SEND_DEDUPE_WINDOW: return _send_answer(st, joined=True)
    else:
        joined = False
        spec = {'project': chan, 'thread': thread, 'message': message, 'bot_name': bot_name, 'force': force, 'files': files, 'deadline_s': SEND_DEADLINE,
                'clear_leftovers': bool(clear_leftovers)}
        job_write(SEND_JOBS, key, {'job_id': key, 'state': 'queued', 'project': p['name'], 'thread_id': thread or 'primary', 'started_ts': t_start,
                                   'total': len(files), 'attached': 0, 'spec': spec})
        how = spawn_job([*cfg_mod.module_argv('claude_projects'), '--json'] + (['--force'] if force else []) + ['send-job', key], f'send-{key[:8]}')
        log(f'send job {key} started for {p["name"]} {thread or "primary"} ({len(files)} file(s), {how})')
    while time.time() - t_start < budget:
        st = job_read(SEND_JOBS, key) or {}
        if st.get('state') in SEND_FINAL: return _send_answer(st, joined=joined)
        time.sleep(.3)
    return _send_answer(job_read(SEND_JOBS, key) or {'job_id': key, 'state': 'queued'})
def run_send_job(key):
    """The worker: one send for key, under its job lock, with progress written to the job file. A SIGTERM or the
    deadline stops it cleanly: the composer is cleaned up, the lock released, the app sent home, the reason recorded."""
    global _PROGRESS
    f = job_lock(SEND_JOBS, key)
    if f is None: return {'job_id': key, 'note': 'this send job is already running'}
    st = job_read(SEND_JOBS, key) or {}
    try:
        if st.get('state') in SEND_FINAL: return _send_answer(st)
        spec = st.get('spec') or {}
        def prog(state, **kw):
            st.update(kw, state=state, step=state, updated_ts=time.time()); job_write(SEND_JOBS, key, st)
        _PROGRESS = prog; prog('starting')
        try:
            r = op_send(spec['project'], spec.get('message') or '', spec.get('thread'), force=spec.get('force', False), bot_name=spec.get('bot_name'),
                        prepared_files=spec.get('files') or [], deadline_s=spec.get('deadline_s'), clear_leftovers=bool(spec.get('clear_leftovers')))
            st.update(state='sent', step='sent', result=r)
        except BaseException as ex:
            rep = getattr(ex, 'cleanup', None)
            if st.get('state') in ('sending', 'verifying'):   # Send was pressed: it went out, so it must never be retried
                st.update(state='sent', step='sent', result={'project': st.get('project'), 'thread_id': st.get('thread_id'), 'sent': True, 'verified_in_transcript': False,
                                                              'note': f'Send was pressed, then {type(ex).__name__}: {ex}; check the thread before sending again'})
                st['finished_ts'] = time.time(); st.pop('spec', None); job_write(SEND_JOBS, key, st); log(f'send job {key}: sent, then {ex!r}')
                return _send_answer(st)
            st.update(state='failed', error=str(ex) or type(ex).__name__, error_type=type(ex).__name__, cleanup=rep,
                      cleanup_note=(None if not rep else ('chips still in the composer: ' + ', '.join(rep['chips_left']) if rep.get('chips_left') else 'the composer was left clean')))
            if isinstance(ex, (CPGError, Interrupted)): pass
            else: log(f'send job {key} crashed: {ex!r}')
        st['finished_ts'] = time.time(); st.pop('spec', None) if st.get('state') == 'sent' else None
        job_write(SEND_JOBS, key, st); log(f"send job {key}: {st['state']} ({st.get('error') or ''})")
        return _send_answer(st)
    finally:
        _PROGRESS = None; job_unlock(f)
def op_wait(project, thread=None, after_index=None, timeout=600, poll=10, force=False, stay=False, shot=None, quiet_polls=2):
    """Poll the transcript until Claude has replied after `after_index` and is no longer working."""
    p = resolve_project(project); chan = p['id']; tid = resolve_thread(chan, thread)
    if tid and str(tid).startswith('LOOKUP:'): raise NotFound('pass the thread id (cmsg_...) to wait')
    t0 = time.time(); last_sig = None; quiet = 0; polls = 0; msgs = []; busy = True
    if after_index is None:
        c = read_cache(chan, tid); after_index = (c or {}).get('latest_message_index') or 0
    while True:
        polls += 1
        with ui_lock(f'wait {p["name"]} {tid or "primary"}', force, timeout=max(UI_LOCK_DEFAULT_TIMEOUT, int(timeout or 0))):
            try:
                want = f'thread={tid}' if tid else None
                cur = route()
                if (want and want not in cur) or (not want and not is_primary_route(cur, chan)): open_target(chan, tid)
                settle(tid, 15); msgs = read_messages(tid, 0); busy = busy_state(tid)
                if shot: screenshot(shot)
            finally:
                done_now = False
                new = [m for m in msgs if m['index'] > after_index and m['role'] == 'assistant']
                sig = (len(msgs), msgs[-1]['index'] if msgs else 0, len(msgs[-1]['text']) if msgs else 0)
                quiet = quiet + 1 if (sig == last_sig and not busy) else 0; last_sig = sig
                done_now = bool(new) and quiet >= quiet_polls - 1 and not busy
                timed_out = time.time() - t0 > timeout
                if not stay and (done_now or timed_out): go_home()
        if done_now or timed_out: break
        time.sleep(poll)
    new_all = [m for m in msgs if m['index'] > after_index]
    res = {'project': p['name'], 'thread_id': tid or 'primary', 'done': done_now, 'timed_out': not done_now, 'claude_working': busy,
           'polls': polls, 'waited_s': round(time.time() - t0), 'after_index': after_index, 'new_messages': new_all,
           'reply_text': '\n\n'.join(m['text'] for m in new_all if m['role'] == 'assistant') or None, 'checked_at_local': ct(now().isoformat())}
    return res
CREATE_THREAD_NOTE = 'Keep the first line of this message (the [sent by Grok Bot] header) as the first line of the new thread\'s task.'
def create_thread_message(title, instructions, bot_name=None):
    """The delegation request typed into the primary thread, origin bracket first."""
    body = f"Start a new thread titled \"{title.strip()}\" for this:\n{instructions.strip()}\n\n{CREATE_THREAD_NOTE}"
    return add_origin(body, bot_name if bot_name is not None else CFG.get('bot_name'))
def op_create_thread(project, title, instructions, wait=False, timeout=900, poll=15, force=False, stay=False, shot=None, dry_run=False, bot_name=None):
    """Projects have no 'new thread' button: the primary thread (orchestrator) spawns threads. This sends a delegation
    request into the primary thread, then (wait=True) waits for the reply and returns any 'Sent to a thread' result."""
    msg = create_thread_message(title, instructions, bot_name)
    r = op_send(project, msg, None, wait, timeout, poll, force, stay, shot, dry_run, bot_name=bot_name)
    if not dry_run and wait:
        txt = (r.get('reply') or {}).get('reply_text') or ''
        r['new_thread_hint'] = re.findall(r'\[Sent to a thread: ([^\]]+)\]', txt) or None
    return r
def _norm_repo(repo):
    """'https://github.com/Owner/Name(.git)(/...)' | 'github.com/Owner/Name' | 'Owner/Name' -> 'Owner/Name'."""
    r = (repo or '').strip()
    r = re.sub(r'^(https?://)?(www\.)?github\.com/', '', r, flags=re.I)
    r = re.sub(r'\.git$', '', r.split('?')[0].split('#')[0]).strip('/')
    parts = r.split('/')
    if len(parts) < 2 or not parts[0] or not parts[1]: raise CPGError(f'repo {repo!r} is not owner/name or a github.com URL')
    return f'{parts[0]}/{parts[1]}'
def _dialog_open(title='New project'):
    d, _ = main_doc()
    return bool(d and find(d, 'dialog', title))
def _attach_repo_in_dialog(repo):
    """Inside the open New project dialog: Context -> Add context -> Add repository -> search -> pick exact owner/name."""
    d, _ = main_doc(); ac = find(d, 'push button', 'Add context')
    if not ac: raise AppError('"Add context" button not found in the New project dialog')
    act(ac, 'Add context'); time.sleep(1.5)
    mi = find(main_doc()[0], 'menu item', 'Add repository')
    if not mi: xdo('key', 'Escape'); raise AppError('"Add repository" not in the Add context menu')
    act(mi, 'Add repository'); time.sleep(2.5)
    want = repo.casefold(); short = repo.split('/', 1)[1]
    def items():
        return [e for e in walk(main_doc()[0], 30000) if role(e) == 'list item']
    s = find(main_doc()[0], 'combo box', pred=lambda e: nm(e).startswith('Search repositories'))
    if not s: raise AppError('repository search box did not open (GitHub not connected?)')
    try: s.get_component_iface().grab_focus(); time.sleep(.3)
    except Exception: pass
    xdo('type', '--delay', '20', '--clearmodifiers', short); time.sleep(2)
    hit = None
    for attempt in range(3):
        hit = next((e for e in items() if nm(e).casefold() == want), None)
        if hit: break
        if attempt == 0:
            rb = find(main_doc()[0], 'push button', 'Refresh list')
            if rb: act(rb, 'Refresh list'); time.sleep(4)
        else: time.sleep(3)
    if not hit:
        raise NotFound(f'repository {repo!r} is not in the app\'s GitHub repo list (check "Manage GitHub connection"); seen: ' + ', '.join(nm(e) for e in items()[:15]))
    act(hit, repo); time.sleep(2)
    t0 = time.time()
    while time.time() - t0 < 10:
        if find(main_doc()[0], 'push button', pred=lambda e: nm(e).casefold() == f'remove {want}'): return nm(hit)
        time.sleep(.5)
    raise AppError(f'repository {repo!r} chip did not appear after selecting it')
def _pin_toggle(name):
    """On the Projects browse page: the Pin/Unpin toggle that follows 'More options for <name>' in the row."""
    d, _ = main_doc(); seen = False
    for e in walk(d, 40000):
        r = role(e); n = nm(e)
        if r == 'push button' and n.startswith('More options for '):
            if seen: return None
            seen = norm(n[len('More options for '):]).casefold() == norm(name).casefold()
        elif seen and r == 'toggle button' and n in ('Pin project', 'Unpin project'):
            return e
    return None
def _set_pin(name, pinned=True):
    """Assumes the lock is held. Returns (pinned_now, changed)."""
    op_projects_nav()
    t0 = time.time(); tg = None
    while time.time() - t0 < 20:
        tg = _pin_toggle(name)
        if tg: break
        time.sleep(1.5)
    if not tg: raise AppError(f'pin toggle for {name!r} not found on the Projects page')
    is_pinned = nm(tg) == 'Unpin project' or has_state(tg, 'PRESSED')
    if is_pinned == bool(pinned): return is_pinned, False
    act(tg, 'pin toggle'); time.sleep(2)
    t0 = time.time()
    while time.time() - t0 < 10:
        tg = _pin_toggle(name)
        if tg and (nm(tg) == 'Unpin project') == bool(pinned): return bool(pinned), True
        time.sleep(1)
    tg = _pin_toggle(name)
    return (nm(tg) == 'Unpin project') if tg else None, True
def _wait_primary(chan, secs=60):
    """Poll the app's project list cache until the new project's primary (overview) thread id appears. Never invents ids."""
    t0 = time.time(); row = None
    while time.time() - t0 < secs:
        try:
            snap = idb_snapshot(); row = next((r for r in project_rows(snap, True) if r['id'] == chan), None)
            if row and row.get('primary_thread_id'): return row
        except Exception as ex: log(f'wait_primary: {ex}')
        time.sleep(4)
    return row
def _project_repo_evidence(chan, repo):
    """Read-only: open the project and collect accessible text that mentions the repo (owner/name or name)."""
    try:
        deeplink(f'claude://code/project/{chan}'); wait_route(lambda u: (u or '').startswith(f'/epitaxy/project/{chan}'), 20); time.sleep(4)
        d, _ = main_doc(); short = repo.split('/', 1)[1].casefold(); hits = []
        for e in walk(d, 40000):
            t = nm(e)
            if t and (repo.casefold() in t.casefold() or (short in t.casefold() and len(t) < 160)) and t not in hits: hits.append(t[:160])
            if len(hits) >= 12: break
        return hits
    except Exception as ex:
        return [f'evidence read failed: {ex}']
def op_create_project(name, description=None, repo=None, pin=True, force=False, stay=False, shot=None):
    """Create a Claude Code Project through the app's New project dialog, optionally attaching a GitHub repository
    (Context -> Add repository) and pinning it on the Projects page. If a project with that name already exists it is
    not duplicated: its ids are returned and pin is ensured (repo cannot be added after create from here; reported)."""
    name = (name or '').strip()
    if not name: raise CPGError('name is empty')
    repo_n = _norm_repo(repo) if repo else None
    snap = idb_snapshot()
    existing = next((r for r in project_rows(snap, True) if norm(r['name']).casefold() == norm(name).casefold()), None)
    notes = []; res = {'name': name, 'repo_requested': repo_n}
    with ui_lock(f'create-project {name}', force):
        try:
            if existing:
                res.update(created=False, already_existed=True, project_id=existing['id'], primary_thread_id=existing.get('primary_thread_id'))
                notes.append('a project with this name already exists; not duplicated')
                chan = existing['id']; repo_shown = None
            else:
                op_projects_nav()
                d, _ = main_doc(); b = find(d, 'push button', 'New project')
                if not b: raise AppError('"New project" button not found')
                act(b); time.sleep(2)
                d, _ = main_doc(); e = find(d, 'entry', 'Name')
                if not e: raise AppError('New project dialog did not open')
                created = False
                try:
                    type_text(e, name)
                    if description:
                        e2 = find(main_doc()[0], 'entry', pred=lambda x: nm(x).startswith('What do you want to work on'))
                        if e2: type_text(e2, description)
                        else: notes.append('description box not found; description skipped')
                    repo_shown = _attach_repo_in_dialog(repo_n) if repo_n else None
                    if norm(composer_text(find(main_doc()[0], 'entry', 'Name'))) != norm(name): raise AppError('name did not type correctly; nothing created')
                    if repo_n and not find(main_doc()[0], 'push button', pred=lambda x: nm(x).casefold() == f'remove {repo_n.casefold()}'):
                        raise AppError('repository chip missing before Create; nothing created')
                    if shot: screenshot(shot.replace('.jpg', '-dialog.jpg'))
                    c = find(main_doc()[0], 'push button', 'Create project')
                    if not c or not has_state(c, 'ENABLED'): raise AppError('"Create project" is not enabled; nothing created')
                    act(c, 'Create project'); created = True
                finally:
                    if not created:
                        cb = find(main_doc()[0], 'push button', 'Cancel')
                        try:
                            if cb: act(cb)
                            else: xdo('key', 'Escape')
                        except Exception: xdo('key', 'Escape')
                        time.sleep(1)
                u = wait_route(lambda u: re.match(r'^/epitaxy/project/chan_\w+', u or '') is not None, 60)
                m = re.match(r'^/epitaxy/project/(chan_\w+)', u or '')
                time.sleep(3)
                if shot: screenshot(shot)
                if not m: raise AppError(f'project was not opened after Create (route {u!r}); check the app before retrying (it may have been created)')
                chan = m.group(1)
                if chan.startswith('chan_pending'):
                    # the app first shows a client-side placeholder id; wait for the server id (route or app list), never report the placeholder
                    u2 = wait_route(lambda u: re.match(r'^/epitaxy/project/chan_(?!pending)\w+', u or '') is not None, 30)
                    m2 = re.match(r'^/epitaxy/project/(chan_(?!pending)\w+)', u2 or '')
                    if m2: chan = m2.group(1); u = u2
                    else:
                        t1 = time.time(); real = None
                        while time.time() - t1 < 60 and not real:
                            try: real = next((r for r in project_rows(idb_snapshot(), True) if norm(r['name']).casefold() == norm(name).casefold() and not r['id'].startswith('chan_pending')), None)
                            except Exception as ex: log(f'create: idb poll {ex}')
                            if not real:
                                try: op_projects_nav()
                                except Exception: pass
                                time.sleep(4)
                        if not real: raise AppError(f'project was created (placeholder {chan}) but its real id did not appear; call claude_projects_list(refresh=true)')
                        chan = real['id']; res['primary_thread_id'] = real.get('primary_thread_id')
                log(f'created project {name!r} {chan} repo={repo_shown}')
                res.update(created=True, already_existed=False, project_id=chan, route=u, repo=repo_shown)
            if repo_n:
                ev = _project_repo_evidence(chan, repo_n); res['repo_evidence'] = ev
                if shot: screenshot(shot.replace('.jpg', '-project.jpg'))
                if existing: notes.append('repo attach on an existing project is not automated; repo_evidence shows what the project page lists')
            if pin is not None:
                try:
                    pinned, changed = _set_pin(name, bool(pin)); res['pinned'] = pinned; res['pin_changed'] = changed
                except CPGError as ex:
                    res['pinned'] = None; notes.append(f'pin failed: {ex}')
            row = _wait_primary(chan, 75 if not res.get('primary_thread_id') else 1)
            if row:
                res['primary_thread_id'] = row.get('primary_thread_id') or res.get('primary_thread_id')
            if not res.get('primary_thread_id'): notes.append('primary thread id not yet in the app list cache; call claude_projects_list(refresh=true) shortly')
            res['notes'] = notes; return res
        finally:
            if not stay: go_home()
def op_project_pin(project, pinned=True, force=False, stay=False, shot=None):
    """Pin or unpin a project with its row toggle on the Projects page (reversible; no usage)."""
    p = resolve_project(project)
    with ui_lock(f'pin {p["name"]} {pinned}', force):
        try:
            now_, changed = _set_pin(p['name'], bool(pinned))
            if shot: screenshot(shot)
            return {'project': p['name'], 'project_id': p['id'], 'pinned': now_, 'changed': changed}
        finally:
            if not stay: go_home()
def op_projects_nav():
    if route().startswith('/epitaxy/projects/browse'): return
    d, _ = main_doc()
    if not find(d, 'push button', pred=lambda e: nm(e) in ('Projects Beta', 'Projects')):
        sb = find(d, 'push button', 'Show sidebar')
        if sb: act(sb); time.sleep(1.5)
    b = find(main_doc()[0], 'push button', pred=lambda e: nm(e) in ('Projects Beta', 'Projects'))
    if not b: raise AppError('Projects button not found')
    act(b); wait_route(lambda u: (u or '').startswith('/epitaxy/projects/browse'), 15); time.sleep(2)
def op_project_menu(project, item=None, confirm=None, force=False, stay=False, shot=None):
    """List the project's '...' menu items, or press one (archive/delete need confirm == exact project name)."""
    p = resolve_project(project)
    if item and re.search(r'archive|delete|remove|leave', item, re.I) and confirm != p['name']:
        raise Refused(f'pressing {item!r} needs confirm={p["name"]!r} (exact project name)')
    with ui_lock(f'menu {p["name"]} {item}', force):
        try:
            op_projects_nav(); d, _ = main_doc()
            b = find(d, 'push button', f'More options for {p["name"]}')
            if not b: raise AppError('project options button not found')
            act(b); time.sleep(1.2)
            d, _ = main_doc()
            items = [e for e in walk(d, 30000) if role(e) in ('menu item', 'check menu item', 'radio menu item')]
            names = [nm(e) for e in items]
            if not item:
                xdo('key', 'Escape'); return {'project': p['name'], 'menu_items': names}
            hit = [e for e in items if nm(e).casefold() == item.casefold()] or [e for e in items if item.casefold() in nm(e).casefold()]
            if not hit: xdo('key', 'Escape'); raise NotFound(f'menu item {item!r} not found; items: {names}')
            act(hit[0]); time.sleep(1.5)
            d, _ = main_doc(); dl = [e for e in walk(d, 30000) if role(e) in ('dialog', 'alert dialog', 'alert')]
            confirmed = None
            if dl:
                btns = [e for e in walk(dl[0]) if role(e) == 'push button' and re.search(item.split()[0], nm(e), re.I)]
                if shot: screenshot(shot.replace('.jpg', '-confirm.jpg'))
                if btns: act(btns[-1]); confirmed = nm(btns[-1]); time.sleep(2)
            if shot: screenshot(shot)
            log(f'menu {p["name"]} -> {nm(hit[0])} confirmed={confirmed}')
            return {'project': p['name'], 'pressed': nm(hit[0]), 'confirmed_with': confirmed, 'menu_items': names}
        finally:
            if not stay: go_home()

def _close_overlays(n=5):
    for _ in range(n):
        d, _ = main_doc()
        if not d or not [e for e in walk(d, 30000) if role(e) in ('dialog', 'alert dialog', 'menu')]: return True
        xdo('key', 'Escape'); time.sleep(1)
    return False
def _open_project_settings(name, tab=None):
    """Assumes the lock is held. Projects page -> More options for <name> -> Settings [-> tab]."""
    op_projects_nav(); d, _ = main_doc()
    b = find(d, 'push button', f'More options for {name}')
    if not b: raise AppError(f'options button for {name!r} not found on the Projects page')
    act(b); time.sleep(1.2)
    it = find(main_doc()[0], 'menu item', 'Settings')
    if not it: xdo('key', 'Escape'); raise AppError('"Settings" not in the project menu')
    act(it); time.sleep(2.5)
    if not find(main_doc()[0], 'dialog', 'Project settings'): raise AppError('Project settings dialog did not open')
    if tab:
        t = find(main_doc()[0], 'page tab', tab)
        if not t: raise AppError(f'settings tab {tab!r} not found')
        act(t); time.sleep(2.5)
def op_project_env(project, force=False, stay=False, shot=None):
    """READ-ONLY: Project settings -> Environment. Returns project repositories, cloud environment name, network access /
    network secrets / setup-script summaries and the cloud environment's variable NAMES (values are never read out),
    local-device toggle and pre-approved folders. Nothing is changed; every dialog is closed with Escape/Cancel."""
    p = resolve_project(project); out = {'project': p['name'], 'project_id': p['id']}
    with ui_lock(f'env {p["name"]}', force):
        try:
            _open_project_settings(p['name'], 'Environment')
            dlg = find(main_doc()[0], 'dialog', 'Project settings')
            out['repositories'] = [nm(e)[len('Remove '):] for e in walk(dlg, 30000) if role(e) == 'push button' and nm(e).startswith('Remove ')]
            cb = find(dlg, 'combo box', pred=lambda e: nm(e).startswith('Cloud environment'))
            out['cloud_environment'] = (nm(cb)[len('Cloud environment'):].strip() or None) if cb else None
            tg = find(dlg, 'toggle button', 'Let Claude use your device'); out['local_device_allowed'] = has_state(tg, 'CHECKED') if tg else None
            tg = find(dlg, 'toggle button', 'Use worktrees'); out['use_worktrees'] = has_state(tg, 'CHECKED') if tg else None
            out['pre_approved_folders'] = [nm(e)[len('Options for '):] for e in walk(dlg, 30000) if role(e) == 'push button' and nm(e).startswith('Options for ')]
            if shot: screenshot(shot)
            eb = find(dlg, 'push button', 'Edit cloud environment')
            if eb:
                act(eb); time.sleep(2.5)
                ed = find(main_doc()[0], 'dialog', 'Edit cloud environment')
                if ed:
                    els = list(walk(ed, 20000)); summ = {}
                    for i, e in enumerate(els):
                        if role(e) == 'push button' and nm(e) in ('Network access', 'Network secrets', 'Setup script', 'Environment variables'):
                            nxt = next((nm(x) for x in els[i+1:i+4] if role(x) == 'static' and nm(x)), None); summ[nm(e)] = nxt
                    out['cloud_environment_summary'] = summ
                    vb = find(ed, 'push button', 'Environment variables')
                    if vb:
                        act(vb); time.sleep(2)
                        en = find(main_doc()[0], 'entry', 'Environment variables')
                        if en:
                            txt = composer_text(en) or ''
                            out['cloud_env_variable_names'] = [ln.split('=', 1)[0].strip() for ln in txt.splitlines() if '=' in ln and not ln.strip().startswith('#')]
                            txt = None
                        hint = find(main_doc()[0], 'static', pred=lambda e: 'don’t add secrets' in nm(e) or "don't add secrets" in nm(e))
                        if hint: out['app_warning'] = norm(nm(hint))
            out['shared_note'] = 'A cloud environment is account-level: every project that selects it shares its variables. It applies to cloud threads, not threads running on your own machines.'
            return out
        finally:
            _close_overlays()
            if not stay: go_home()

def op_project_add_repo(project, repo, force=False, stay=False, shot=None, dry_run=False):
    """Attach a GitHub repository to an EXISTING project: Project settings -> Environment -> Project repositories ->
    "Add repository" -> search -> (Refresh list if missing) -> pick exact owner/name -> verify the "Remove <repo>" chip.
    Touches only that combo; never Pause/Restart/Archive, cloud env or variables. Already attached -> no-op."""
    p = resolve_project(project); repo_n = _norm_repo(repo); want = repo_n.casefold(); short = repo_n.split('/', 1)[1]
    out = {'project': p['name'], 'project_id': p['id'], 'repo_requested': repo_n, 'dry_run': dry_run}
    def attached(dlg):
        return [nm(e)[len('Remove '):] for e in walk(dlg, 30000) if role(e) == 'push button' and nm(e).startswith('Remove ')]
    with ui_lock(f'add-repo {p["name"]}', force):
        try:
            _open_project_settings(p['name'], 'Environment')
            dlg = find(main_doc()[0], 'dialog', 'Project settings')
            before = attached(dlg); out['repositories_before'] = before
            if any(r.casefold() == want for r in before):
                out.update(added=False, already_attached=True, repositories_after=before); return out
            cb = find(dlg, 'combo box', pred=lambda e: nm(e).startswith('Add repository'))
            if not cb: raise AppError('"Add repository" combo not found in Project settings -> Environment')
            act(cb, 'Add repository'); time.sleep(2.5)
            s = find(main_doc()[0], 'combo box', pred=lambda e: nm(e).startswith('Search repositories'))
            if not s: raise AppError('repository search box did not open (GitHub not connected?)')
            try: s.get_component_iface().grab_focus(); time.sleep(.3)
            except Exception: pass
            xdo('type', '--delay', '20', '--clearmodifiers', short); time.sleep(2)
            def items(): return [e for e in walk(main_doc()[0], 30000) if role(e) == 'list item' and nm(e)]
            hit = None; refreshed = False
            for attempt in range(3):
                hit = next((e for e in items() if nm(e).casefold() == want), None)
                if hit: break
                if not refreshed:
                    rb = find(main_doc()[0], 'push button', 'Refresh list')
                    if rb: act(rb, 'Refresh list'); refreshed = True; time.sleep(5)
                else: time.sleep(3)
            out['refreshed_list'] = refreshed
            if not hit:
                raise NotFound(f'repository {repo_n!r} is not in the app\'s GitHub repo list even after Refresh list '
                               '(Claude GitHub app likely lacks access to it); seen: ' + ', '.join(nm(e) for e in items()[:20]))
            out['found_in_list'] = nm(hit)
            if dry_run:
                xdo('key', 'Escape'); time.sleep(1); out['added'] = False; return out
            act(hit, repo_n); time.sleep(2.5)
            t0 = time.time(); after = []
            while time.time() - t0 < 15:
                after = attached(find(main_doc()[0], 'dialog', 'Project settings') or main_doc()[0])
                if any(r.casefold() == want for r in after): break
                time.sleep(.7)
            out['repositories_after'] = after
            if not any(r.casefold() == want for r in after): raise AppError(f'"Remove {repo_n}" chip did not appear after selecting it')
            missing = [r for r in before if r not in after]
            if missing: raise AppError(f'previously attached repos disappeared: {missing}')
            out['added'] = True
            if shot: screenshot(shot)
            log(f'add-repo {p["name"]} {p["id"]} {repo_n} ok')
            return out
        finally:
            _close_overlays()
            if not stay: go_home()

def op_project_remove_repo(project, repo, force=False, stay=False, shot=None, dry_run=False):
    """Detach a GitHub repository from an EXISTING project via Project settings -> Environment -> its exact
    "Remove <owner/name>" button. Exact, case-sensitive match; refuses if zero-or-many controls match exactly or if a
    case-insensitive twin would be ambiguous. Verifies every other repo is still listed. Already gone -> no-op."""
    p = resolve_project(project); repo_n = _norm_repo(repo)
    out = {'project': p['name'], 'project_id': p['id'], 'repo_requested': repo_n, 'dry_run': dry_run}
    def buttons(root): return [e for e in walk(root, 30000) if role(e) == 'push button' and nm(e).startswith('Remove ')]
    def attached(root): return [nm(e)[len('Remove '):] for e in buttons(root)]
    with ui_lock(f'remove-repo {p["name"]}', force):
        try:
            _open_project_settings(p['name'], 'Environment')
            dlg = find(main_doc()[0], 'dialog', 'Project settings')
            before = attached(dlg); out['repositories_before'] = before
            exact = [e for e in buttons(dlg) if nm(e) == f'Remove {repo_n}']
            if not exact:
                twins = [r for r in before if r.casefold() == repo_n.casefold()]
                if twins: raise Refused(f'no exact (case-sensitive) match for {repo_n!r}; case-insensitive twins: {twins}')
                out.update(removed=False, already_absent=True, repositories_after=before); return out
            if len(exact) > 1: raise Refused(f'{len(exact)} controls named "Remove {repo_n}"; ambiguous, nothing removed')
            keep = [r for r in before if r != repo_n]; out['will_keep'] = keep
            if dry_run: out['removed'] = False; return out
            act(exact[0], f'Remove {repo_n}'); time.sleep(2)
            ad = next((e for e in walk(main_doc()[0], 30000) if role(e) == 'alert dialog'), None)
            if ad:
                btns = [nm(b) for b in walk(ad, 5000) if role(b) == 'push button']; out['confirm_dialog_buttons'] = btns
                cb = next((b for b in walk(ad, 5000) if role(b) == 'push button' and nm(b) in ('Remove', 'Remove repository', 'Confirm')), None)
                if not cb: xdo('key', 'Escape'); raise AppError(f'unexpected confirm dialog, buttons {btns}; cancelled')
                act(cb, nm(cb)); time.sleep(2)
            t0 = time.time(); after = before
            while time.time() - t0 < 15:
                after = attached(find(main_doc()[0], 'dialog', 'Project settings') or main_doc()[0])
                if repo_n not in after: break
                time.sleep(.7)
            out['repositories_after'] = after
            if repo_n in after: raise AppError(f'{repo_n!r} still listed after pressing Remove')
            lost = [r for r in keep if r not in after]
            if lost: raise AppError(f'other repos disappeared: {lost}')
            out['removed'] = True
            if shot: screenshot(shot)
            log(f'remove-repo {p["name"]} {p["id"]} {repo_n} ok')
            return out
        finally:
            _close_overlays()
            if not stay: go_home()

# ---------------------------------------------------------------- cloud environment variables (write)
_ENV_KEY = re.compile(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=')
def _top_dialog():
    d, _ = main_doc()
    dl = [e for e in walk(d, 40000) if role(e) in ('dialog', 'alert dialog') and nm(e) != 'Project settings'] if d else []
    return dl[-1] if dl else None
def _env_names(txt):
    out = []
    for ln in (txt or '').splitlines():
        m = _ENV_KEY.match(ln)
        if m and not ln.strip().startswith('#'): out.append(m.group(1))
    return out
def _merge_env(txt, new):
    """Replace KEY=... lines for keys in new, append the rest. Values never logged."""
    out, seen = [], set()
    for ln in (txt or '').splitlines():
        m = _ENV_KEY.match(ln)
        if m and m.group(1) in new:
            if m.group(1) in seen: continue
            out.append(f'{m.group(1)}={new[m.group(1)]}'); seen.add(m.group(1))
        elif ln.strip(): out.append(ln)
    for k, v in new.items():
        if k not in seen: out.append(f'{k}={v}')
    return '\n'.join(out)
def _replace_entry_text(en, text):
    """Clear a multi-line entry and type text (ASCII only: no clipboard, so secrets never touch CLIPBOARD)."""
    if _has_non_ascii(text): raise Refused('environment text has non-ASCII characters; refusing (would need the clipboard)')
    # Only the desktop app window (exact title 'Claude'); never a browser tab titled 'Claude - Google Chrome'.
    if (xdo('getactivewindow', 'getwindowname') or '').strip() != 'Claude':
        wids = [w for w in (xdo('search', '--onlyvisible', '--name', '^Claude$') or '').split('\n') if w.strip()]
        wids = [w for w in wids if (xdo('getwindowname', w) or '').strip() == 'Claude']
        if len(wids) != 1: raise AppError(f'cannot identify the Claude desktop window uniquely ({len(wids)} matches); nothing typed')
        xdo('windowactivate', '--sync', wids[0]); time.sleep(.3)
    if (xdo('getactivewindow', 'getwindowname') or '').strip() != 'Claude': raise AppError('Claude desktop window is not active; nothing typed')
    try: en.get_component_iface().grab_focus()
    except Exception as ex: log(f'set-env focus warning: {type(ex).__name__}')
    time.sleep(.4); xdo('key', '--clearmodifiers', 'ctrl+a'); time.sleep(.2); xdo('key', '--clearmodifiers', 'BackSpace'); time.sleep(.4)
    if norm(_entry_plain_text(en) or composer_text(en)): raise AppError('could not clear the Environment variables box; nothing saved')
    lines = text.split('\n'); trace = []
    for i, line in enumerate(lines):
        if (xdo('getactivewindow', 'getwindowname') or '').strip() != 'Claude': raise AppError('focus left the Claude desktop window while typing; stopped (dialog cancelled)')
        if line:
            env = _tool_env()
            r = subprocess.run([str(XDO), 'type', '--delay', '12', '--clearmodifiers', '--file', '-'], capture_output=True, text=True, env=env, timeout=120, input=line)
            _note_own_input('type')
            trace.append({'rc': r.returncode, 'stderr': (r.stderr or '')[:160], 'len_after': len(_entry_plain_text(en) or '')})
        if i < len(lines) - 1: xdo('key', '--clearmodifiers', 'shift+Return')
    time.sleep(.8)
    want = [l.strip() for l in text.splitlines() if l.strip()]
    t_end = time.time() + 20
    while time.time() < t_end:  # the app / AT-SPI text cache lags behind xdotool; poll until it catches up
        top = _top_dialog(); en2 = find(top, 'entry', 'Environment variables') if top else None
        for cand in (en, en2):
            if cand is None: continue
            got = [l.strip() for l in (_entry_plain_text(cand) or '').splitlines() if l.strip()]
            if got == want: got = None; return 'polled'
            got = None
        time.sleep(1)
    diag = {}
    top = _top_dialog(); en2 = find(top, 'entry', 'Environment variables') if top else None
    diag['top_dialog'] = nm(top) if top else None; diag['entry_refound'] = en2 is not None
    ne = find(top, 'entry', 'Name') if top else None
    if ne is not None: diag['name_entry_len'] = len(_entry_plain_text(ne) or '')
    for src, fn in (('text_iface', _entry_plain_text), ('composer', composer_text), ('refound', lambda _e: _entry_plain_text(en2) if en2 is not None else '')):
        got = [l.strip() for l in (fn(en) or '').splitlines() if l.strip()]
        if got == want: got = None; return src
        diag[src] = {'lines': len(got), 'names': _env_names('\n'.join(got)), 'line_len_delta': [len(g) - len(w) for g, w in zip(got, want)]}
        got = None
    diag['want'] = {'lines': len(want), 'names': _env_names('\n'.join(want))}
    diag['trace'] = trace; g = _entry_plain_text(en) or ''; diag['got_is_prefix'] = text.startswith(g); g = None
    raise AppError(f'Environment variables box did not read back as typed; nothing saved (dialog cancelled). diag={diag}')
def _entry_plain_text(e):
    try:
        n = A().Text.get_character_count(e); return (A().Text.get_text(e, 0, n) if n else '').replace('\ufffc', '')
    except Exception: return None
def _read_env_text_in(dlg_name_hint=None):
    vb = find(_top_dialog(), 'push button', 'Environment variables')
    if not vb: raise AppError('"Environment variables" button not found in the cloud environment dialog')
    act(vb); time.sleep(2)
    en = find(_top_dialog(), 'entry', 'Environment variables')
    if not en: raise AppError('Environment variables box not found')
    return en
def _summary_after(dlg, label):
    els = list(walk(dlg, 20000))
    for i, e in enumerate(els):
        if role(e) == 'push button' and nm(e) == label:
            return next((nm(x) for x in els[i+1:i+4] if role(x) == 'static' and nm(x)), None)
def _set_network_full(dlg):
    act(find(dlg, 'push button', 'Network access')); time.sleep(2)
    cb = find(_top_dialog(), 'combo box', 'Network access')
    if not cb: raise AppError('Network access selector not found')
    act(cb); time.sleep(1.5)
    opt = find(main_doc()[0], pred=lambda e: role(e) in ('list item', 'option', 'menu item', 'radio menu item') and nm(e).startswith('Full'))
    if not opt: xdo('key', 'Escape'); raise AppError('"Full" network option not found')
    act(opt); time.sleep(1.2)
    bk = find(_top_dialog(), 'push button', 'Back')
    if bk: act(bk); time.sleep(1.5)
def _read_secret_file(path):
    p = pathlib.Path(path).expanduser()
    if not p.is_file(): raise NotFound(f'secret file not found: {p}')
    st = p.stat()
    if st.st_mode & 0o077: raise Refused(f'secret file {p} must be chmod 600 (owner only)')
    v = p.read_text(encoding='utf-8').strip()
    if not v or '\n' in v: raise CPGError(f'secret file {p} must hold one non-empty line')
    return v
def op_project_set_env(project, values=None, secret_files=None, env_name=None, copy_from_current=True, allow_shared=False,
                       dry_run=False, force=False, stay=False):
    """Set cloud-environment variables for a project. values = {NAME: literal} (non-secret), secret_files = {NAME: path on
    the Claude Code host, chmod 600}. env_name: a dedicated cloud environment for this project (created if missing, copying the
    current environment's variables and Full network access, then selected on the project). env_name=None edits the
    project's CURRENT environment in place, which needs allow_shared=True when it is shared (e.g. "Default").
    Values are never printed, logged, screenshotted or returned; only names, lengths and flags."""
    p = resolve_project(project)
    new = dict(values or {})
    for k, path in (secret_files or {}).items(): new[k] = _read_secret_file(path)
    bad = [k for k in new if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', k)]
    if bad: raise CPGError(f'bad variable names: {bad}')
    if not new: raise CPGError('nothing to set')
    out = {'project': p['name'], 'project_id': p['id'], 'names_requested': list(new), 'lengths': {k: len(v) for k, v in new.items()}, 'dry_run': dry_run}
    with ui_lock(f'set-env {p["name"]} {list(new)}', force):
        try:
            _open_project_settings(p['name'], 'Environment')
            dlg = find(main_doc()[0], 'dialog', 'Project settings')
            cb = find(dlg, 'combo box', pred=lambda e: nm(e).startswith('Cloud environment'))
            if not cb: raise AppError('Cloud environment selector not found')
            cur = nm(cb)[len('Cloud environment'):].strip(); out['previous_cloud_environment'] = cur
            act(cb); time.sleep(1.5)
            items = [nm(e) for e in walk(main_doc()[0], 40000) if role(e) == 'list item' and nm(e).endswith(' Edit cloud environment')]
            envs = [x[:-len(' Edit cloud environment')] for x in items]; out['cloud_environments'] = envs
            xdo('key', 'Escape'); time.sleep(1)
            if not find(main_doc()[0], 'dialog', 'Project settings'): _open_project_settings(p['name'], 'Environment')
            # read the current environment's variables + network (in memory only)
            act(find(find(main_doc()[0], 'dialog', 'Project settings'), 'push button', 'Edit cloud environment')); time.sleep(2.5)
            ed = _top_dialog(); cur_net = _summary_after(ed, 'Network access') or ''
            en = _read_env_text_in(); cur_txt = _entry_plain_text(en) or composer_text(en) or ''
            out['previous_env_variable_names'] = _env_names(cur_txt)
            _close_overlays()
            target = env_name or cur
            out['cloud_environment'] = target; out['dedicated'] = bool(env_name) and env_name != 'Default'
            if target == 'Default' or (not env_name and not allow_shared):
                if not allow_shared: raise Refused(f'{target!r} is a shared cloud environment; pass env_name for a dedicated one or allow_shared=true')
                out['warning'] = f'{target!r} is shared: every project that selects it (and anyone using it) can see these values.'
            if dry_run:
                out['would'] = ('create' if target not in envs else 'edit') + f' {target!r} and select it on the project'; return out
            _open_project_settings(p['name'], 'Environment')
            dlg = find(main_doc()[0], 'dialog', 'Project settings')
            if target not in envs:
                base = cur_txt if copy_from_current else ''
                merged = _merge_env(base, new); base = None
                act(find(dlg, 'combo box', pred=lambda e: nm(e).startswith('Cloud environment'))); time.sleep(1.5)
                ab = find(main_doc()[0], 'push button', 'Add cloud environment')
                if not ab: raise AppError('"Add cloud environment" not found')
                act(ab); time.sleep(2.5)
                ad = _top_dialog()
                if not ad or nm(ad) != 'Add cloud environment': raise AppError('Add cloud environment dialog did not open')
                ne = find(ad, 'entry', 'Name'); type_text(ne, target)
                if norm(composer_text(ne)) != norm(target): raise AppError('environment name did not type correctly; nothing created')
                if copy_from_current and cur_net.startswith('Full'): _set_network_full(_top_dialog())
                out['network_access'] = _summary_after(_top_dialog(), 'Network access')
                en = _read_env_text_in(); _replace_entry_text(en, merged); merged = None
                bk = find(_top_dialog(), 'push button', 'Back')
                if bk: act(bk); time.sleep(1.5)
                ad = _top_dialog(); out['env_vars_summary'] = _summary_after(ad, 'Environment variables')
                go = find(ad, 'push button', 'Add environment')
                if not go or not has_state(go, 'ENABLED'): raise AppError('"Add environment" is not enabled; nothing created')
                act(go); time.sleep(3); out['created'] = True
                log(f'set-env: created cloud environment {target!r} names={_env_names(cur_txt) + [k for k in new if k not in _env_names(cur_txt)]}')
            else:
                if target != cur:
                    act(find(dlg, 'combo box', pred=lambda e: nm(e).startswith('Cloud environment'))); time.sleep(1.5)
                    it = find(main_doc()[0], 'list item', f'{target} Edit cloud environment'); act(it); time.sleep(2)
                    _close_overlays(); _open_project_settings(p['name'], 'Environment')
                dlg = find(main_doc()[0], 'dialog', 'Project settings')
                act(find(dlg, 'push button', 'Edit cloud environment')); time.sleep(2.5)
                en = _read_env_text_in(); merged = _merge_env(_entry_plain_text(en) or composer_text(en) or '', new)
                _replace_entry_text(en, merged); merged = None
                bk = find(_top_dialog(), 'push button', 'Back')
                if bk: act(bk); time.sleep(1.5)
                sv = find(_top_dialog(), 'push button', pred=lambda e: nm(e) in ('Save', 'Save changes', 'Save environment', 'Update environment', 'Done'))
                if not sv or not has_state(sv, 'ENABLED'): raise AppError('no enabled Save button in the cloud environment dialog; nothing saved')
                act(sv); time.sleep(3); out['created'] = False
            cur_txt = None; new = None
            # select it on the project and read back
            _close_overlays()
            _open_project_settings(p['name'], 'Environment')
            dlg = find(main_doc()[0], 'dialog', 'Project settings')
            cb = find(dlg, 'combo box', pred=lambda e: nm(e).startswith('Cloud environment'))
            if nm(cb)[len('Cloud environment'):].strip() != target:
                act(cb); time.sleep(1.5)
                it = find(main_doc()[0], 'list item', pred=lambda e: nm(e).startswith(f'{target} ') or nm(e) == target)
                if not it: raise AppError(f'{target!r} not in the cloud environment list after saving')
                act(it); time.sleep(2)
            _close_overlays()
        finally:
            _close_overlays()
    rb = op_project_env(p['id'], force=force, stay=stay)
    out['selected_cloud_environment'] = rb.get('cloud_environment')
    out['env_variable_names_now'] = rb.get('cloud_env_variable_names')
    out['network_access_now'] = (rb.get('cloud_environment_summary') or {}).get('Network access')
    out['names_set'] = [k for k in out['names_requested'] if k in (rb.get('cloud_env_variable_names') or [])]
    out['ok'] = out['selected_cloud_environment'] == out['cloud_environment'] and out['names_set'] == out['names_requested']
    out['note'] = 'Cloud environment variables reach cloud threads only; threads on your own machines use that machine\'s own environment. New sessions pick them up.'
    return out

def op_thread_status(project, thread=None, refresh=False, force=False, stay=True):
    """Compact triage: current_state, latest message, machine, what it is waiting on.
    refresh=False serves the on-disk cache (fast, stamped); refresh=True does a live UI read."""
    p = resolve_project(project); chan = p['id']
    tid = resolve_thread(chan, thread)
    if tid and str(tid).startswith('LOOKUP:'):
        if not refresh:
            raise NotFound(f'thread title {tid[7:]!r} is not in cache; call with refresh=true once, or pass a cmsg_ id')
        # live resolve via sidebar (no silent primary return)
        r = op_read(project, thread, True, max_messages=5, force=force, stay=stay)
        return _triage_from_read(r)
    cached = read_cache(chan, tid if tid else None)
    if not refresh:
        if not cached:
            raise NotFound('nothing cached yet; call claude_thread_read/claude_project_get once, or pass refresh=true')
        # Recompute staleness/current_state from cached messages so age_hours is fresh
        msgs = list(cached.get('messages') or [])
        annotate_messages_staleness(msgs, now())
        look_id = tid or p.get('primary_thread_id')
        eid, cst = idb_env_fields_for_thread(look_id)
        ei_prev = cached.get('environment_info') or {}
        if not eid: eid = ei_prev.get('environment_id') or None
        if not cst: cst = ei_prev.get('connection_status')
        env_block = resolve_environment(
            raw_ui=cached.get('environment') or ei_prev.get('environment_raw'),
            env_id=eid, connection_status=cst, source='cached', read_at=cached.get('read_at'))
        env_block = dict(env_block); env_block['source'] = 'cached'; env_block['read_at'] = cached.get('read_at'); env_block['read_at_local'] = ct(cached.get('read_at'))
        th_meta = None
        th_list = cached.get('threads') or (read_cache(chan) or {}).get('threads') or []
        for trow in th_list:
            if tid and trow.get('thread_id') == tid: th_meta = trow; break
        cs = build_current_state(msgs, cached.get('claude_working'), th_meta, None, now(), permission_cards=cached.get('permission_cards'))
        latest = msgs[-1] if msgs else None
        age_cache = None
        try:
            if cached.get('read_at'):
                age_cache = round((now() - datetime.datetime.fromisoformat(str(cached['read_at']).replace('Z','+00:00'))).total_seconds()/60.0, 1)
        except Exception: pass
        return {
            'project': cached.get('project') or p['name'], 'project_id': chan,
            'thread_id': cached.get('thread_id') or (tid or 'primary'),
            'thread_title': cached.get('thread_title'),
            'address': thread_address(chan, cached.get('thread_id') or tid, p.get('primary_thread_id')),
            'current_state': cs,
            'permission_cards': cached.get('permission_cards'),
            'latest_message': ({'index': latest['index'], 'role': latest.get('role'), 'summary': latest.get('summary') or (latest.get('text') or '')[:240],
                                'time_local': latest.get('time_local'), 'age_hours': latest.get('age_hours'), 'is_latest': True} if latest else None),
            'waiting_on': cs.get('waiting_on'),
            'machine': env_block.get('machine'), 'environment_info': env_block,
            'claude_working': cached.get('claude_working'),
            'from_cache': True, 'cache_age_min': age_cache,
            'cache_read_at_local': ct(cached.get('read_at')),
            'note': 'Cache triage (no UI). Pass refresh=true for a live read. Do not treat cache older than ~15 min as authoritative without refreshing.',
        }
    r = op_read(project, thread, True, max_messages=8, force=force, stay=stay)
    return _triage_from_read(r)

def _triage_from_read(r):
    msgs = r.get('messages') or []
    latest = msgs[-1] if msgs else None
    cs = r.get('current_state') or build_current_state(msgs, r.get('claude_working'))
    return {
        'project': r.get('project'), 'project_id': r.get('project_id'),
        'thread_id': r.get('thread_id'), 'thread_title': r.get('thread_title'),
        'address': r.get('address'),
        'current_state': cs,
        'permission_cards': r.get('permission_cards'),
        'latest_message': ({'index': latest['index'], 'role': latest.get('role'), 'summary': latest.get('summary') or (latest.get('text') or '')[:240],
                            'time_local': latest.get('time_local'), 'age_hours': latest.get('age_hours'), 'is_latest': True} if latest else None),
        'waiting_on': cs.get('waiting_on'),
        'machine': r.get('machine'), 'environment_info': r.get('environment_info'),
        'claude_working': r.get('claude_working'),
        'from_cache': False, 'cache_age_min': 0,
        'cache_read_at_local': r.get('read_at_local'),
        'note': 'Live triage from the app UI.',
    }

def op_refresh(projects=None, background=False, max_projects=3, force=False):
    """Refresh the project list (opens Projects view) and re-read the primary thread of changed / named projects."""
    if background:
        if vnc_viewers(): return {'skipped': 'VNC viewer connected'}
        idle = effective_user_idle_min()
        raw_idle = x_idle_min(); own = last_own_input_age_min()
        if idle is not None and idle < 5:
            return {'skipped': f'the app display was used {raw_idle} min ago (user input; own_input_age_min={None if own is None else round(own,2)})'}
    try:
        # stay=True: skip the expensive claude://code/new round-trip before we re-open each project
        res = op_projects(refresh=True, force=force, stay=True)
    except Busy as ex: return {'skipped': str(ex)}
    targets = list(projects or [])
    if not targets:
        for r in res['projects']:
            c = read_cache(r['id'])
            if r['last_activity'] and (not c or (c.get('last_activity_seen') or '') < r['last_activity']): targets.append(r['id'])
        targets = targets[:max_projects]
    done = []
    for i, t in enumerate(targets):
        try:
            # stay on all but the last to avoid home↔project thrash under one lock
            r = op_read(t, None, True, 15, force=force, with_threads=True, stay=(i < len(targets) - 1))
            c = read_cache(r['project_id']) or r; c['last_activity_seen'] = next((x['last_activity'] for x in res['projects'] if x['id'] == r['project_id']), None)
            write_cache(r['project_id'], None, c); done.append({'project': r['project'], 'latest_message_index': r['latest_message_index'], 'read_at_local': r['read_at_local'],
                                                                'current_state': r.get('current_state')})
        except Busy: break
        except Exception as ex: done.append({'project': t, 'error': str(ex)[:200]})
    return {'list_fetched_by_app_local': res['list_fetched_by_app_local'], 'previous_fetch_local': res['previous_fetch_local'], 'refreshed': done}
def _pkg_version(pkg='claude-desktop'):
    """Installed and candidate apt versions of the Claude desktop package (None when apt/dpkg is unavailable)."""
    out = {'installed': None, 'candidate': None}
    try:
        r = subprocess.run(['dpkg-query', '-W', '-f=${Version}', pkg], capture_output=True, text=True, timeout=10)
        out['installed'] = r.stdout.strip() or None
        r = subprocess.run(['apt-cache', 'policy', pkg], capture_output=True, text=True, timeout=20)
        m = re.search(r'Candidate:\s*(\S+)', r.stdout or '')
        out['candidate'] = m.group(1) if m and m.group(1) != '(none)' else None
    except Exception: pass
    return out
def op_probe_projects(force=False):
    """READ-ONLY: does the signed-in account have Claude Code Projects? Looks for the sidebar entry ('Projects Beta' or
    'Projects'), opens the Projects browse view to see whether the route sticks, and reads whether the app cached a
    project-list query. Presses nothing else, types nothing, and returns the app to its home screen."""
    out = {'signed_in': None, 'route': None, 'sidebar_entry': None, 'browse_route_ok': False, 'project_list_cached': False,
           'project_list_query_status': None, 'project_count': None, 'app_version': _pkg_version()}
    with ui_lock('probe-projects', force):
        try:
            d, _ = main_doc()
            if not d: raise AppError('the Claude app window is not reachable (is claude-projects-desktop running?)')
            out['route'] = route(); out['signed_in'] = not re.search(r'/login|/oauth', out['route'] or '')
            if out['signed_in']:
                is_entry = lambda e: nm(e) in ('Projects Beta', 'Projects')
                b = find(d, 'push button', pred=is_entry)
                if not b:
                    sb = find(d, 'push button', 'Show sidebar')
                    if sb: act(sb); time.sleep(1.5)
                    b = find(main_doc()[0], 'push button', pred=is_entry)
                if b:
                    out['sidebar_entry'] = nm(b); act(b)
                    u = wait_route(lambda u: (u or '').startswith('/epitaxy/projects/browse'), 15)
                    out['browse_route_ok'] = (u or '').startswith('/epitaxy/projects/browse'); time.sleep(3)
        finally:
            try: go_home()
            except Exception: pass
    try:
        snap = idb_snapshot()
        out['project_list_cached'] = bool(snap.get('channels_updated')); out['project_list_query_status'] = snap.get('channels_query_status')
        out['project_count'] = len(project_rows(snap)) if out['project_list_cached'] else None
    except CPGError as ex: out['cache_error'] = str(ex)[:200]
    return out
def op_status():
    holder = ui_lock_holder()
    st = {'vnc_viewers': vnc_viewers(), 'x_idle_min': x_idle_min(), 'effective_user_idle_min': effective_user_idle_min(), 'own_input_age_min': last_own_input_age_min(), 'lock_holder': holder}
    try: st['jobs_running'] = jobs_running() or None
    except Exception as ex: st['jobs_running'] = None; log(f'jobs_running failed: {ex!r}')
    st['busy'] = bool(holder) or bool(st['jobs_running'])   # deploys: restart only when this is false
    try: st['route'] = route(); st['dialogs'] = dialogs() or None; st['stray_open_files'] = len(_open_files_wids()) or None; st['signed_in'] = not re.search(r'/login|/oauth', st['route'] or '')
    except Exception as ex: st['app'] = f'not reachable: {ex}'
    try:
        c = json.loads((CACHE/'idb.json').read_text()); st['project_list_fetched_by_app_local'] = ct(c.get('channels_updated')); st['projects_cached'] = len(c.get('channels') or [])
    except Exception: pass
    return st

# ---------------------------------------------------------------- CLI
def human(res):
    if isinstance(res, dict) and 'projects' in res and isinstance(res['projects'], list):
        print(f"{'project':34} {'status':8} {'last activity':22} {'id':30} primary thread")
        for r in res['projects']: print(f"{(r['name'] or '')[:34]:34} {r['status']:8} {r['last_activity_local'] or '-':22} {r['id']:30} {r['primary_thread_id']}")
        print(f"({res['count']} projects; list fetched by the app {res['list_fetched_by_app_local']}" + (f", before refresh {res['previous_fetch_local']}" if res.get('previous_fetch_local') else '') + ')'); return
    if isinstance(res, dict) and 'messages' in res:
        print(f"# {res['project']} / {res.get('thread_title') or res['thread_id']}  [{res['thread_id']}]  read {res['read_at_local']}  working={res['claude_working']}" + (f"  {res['environment']}" if res.get('environment') else ''))
        for m in res['messages']:
            print(f"\n--- #{m['index']} {m['role']}" + (f"  ({m['time_local']})" if m.get('time_local') else (f"  ({m['time_app']})" if m.get('time_app') else '')) + ('  [truncated in app; use --expand]' if m['truncated_in_app'] else '') + f"\n{m.get('markdown') or m['text']}")
            if res.get('_show_atts'):
                for at in m.get('attachments') or []:
                    print(f"  [{at['id']}] {at['kind']:13} {at.get('name','')[:60]}" + (f"  {at['mime']}" if at.get('mime') else '') + (f"  path={at['path_in_project']}" if at.get('path_in_project') else '') + (f"  {at['url']}" if at.get('url') else '') + (f"  saved={at['path_on_host']}" if at.get('path_on_host') else ''))
                for b in m.get('blocks') or []:
                    if b['type'] in ('code', 'table'): print(f"  <{b['type']}" + (f" {b.get('language') or '?'} {b['lines']} lines" if b['type'] == 'code' else f" {len(b['rows'])}x{b['columns']}") + '>')
        th = res.get('threads') or res.get('threads_cached')
        if th:
            print(f"\n# threads{' (cached)' if 'threads_cached' in res else ''}:")
            for t in th: print(f"  [{t['group'] or '-'}] {t['title'][:70]}  {t['thread_id']}  {t['age'] or ''} {', '.join(t['pull_requests'] or [])}")
        return
    print(json.dumps(res, ensure_ascii=False, indent=1))
LEGACY_KEYS = {'path_on_host': 'path_on_home_lab'}
def _legacy_person():
    """The word pre-1.0 releases used for the person (<name> in waiting_on_<name>): the operator's first name, lowercased."""
    n = re.sub(r'^(the|a|an)\s+', '', str(CFG.get('operator_name') or 'user').strip().lower())
    w = re.sub(r'[^a-z0-9]+', '_', (n.split() or ['user'])[0]).strip('_')
    return w or 'user'
def legacy_aliases(o):
    """Add pre-1.0 field names next to the new ones (time_local -> time_ct, path_on_host -> path_on_home_lab, ...).
    current_state-like dicts (status + waiting_on) also get status_legacy / waiting_on_legacy in the old vocabulary
    (waiting_on_user -> waiting_on_<operator>, waiting_on user|permission -> <operator>); the new fields are unchanged."""
    if isinstance(o, list): return [legacy_aliases(x) for x in o]
    if not isinstance(o, dict): return o
    out = {}
    for k, v in o.items():
        v = legacy_aliases(v); out[k] = v
        if k.endswith('_local') and (k[:-6] + '_ct') not in o: out[k[:-6] + '_ct'] = v
        elif k in LEGACY_KEYS and LEGACY_KEYS[k] not in o: out[LEGACY_KEYS[k]] = v
    if 'waiting_on' in o:
        person = _legacy_person(); w = o.get('waiting_on')
        if 'waiting_on_legacy' not in o: out['waiting_on_legacy'] = person if w in ('user', 'permission') else w
        st = o.get('status')
        if 'status' in o and 'status_legacy' not in o and isinstance(st, str):
            out['status_legacy'] = f'waiting_on_{person}' if st == 'waiting_on_user' else st
    return out
def op_permissions(project, thread=None, force=False, stay=False):
    """Read-only listing of the permission cards in a thread (opens it in the app; never presses anything)."""
    p = resolve_project(project); chan = p['id']
    with ui_lock(f'permissions {p["name"]} {thread or "primary"}', force):
        try:
            tid = _resolve_tid(p, chan, thread)
            open_target_settled(chan, tid)
            cards = detect_permission_cards()
            c = read_cache(chan, tid)
            if c is not None:
                c['permission_cards'] = PERM.public(cards) or None; write_cache(chan, tid, c)
            return {'project': p['name'], 'project_id': chan, 'thread_id': tid or 'primary', 'address': thread_address(chan, tid, p.get('primary_thread_id')),
                    'permission_cards': PERM.public(cards), 'count': len(cards), 'read_at_local': ct(now().isoformat()),
                    'note': 'read-only; approve or deny with claude_thread_permission(option=<label>, dry_run=false) only when the user policy allows it'}
        finally:
            if not stay: go_home()
def _hoist_globals(argv):
    """Allow --json/--force/--stay/--shot before or after the subcommand (MCP appends --stay at the end)."""
    argv = list(argv); globals_out = []; rest = []; i = 0
    while i < len(argv):
        a = argv[i]
        if a in ('--json', '--force', '--stay'):
            globals_out.append(a); i += 1; continue
        if a == '--shot' and i + 1 < len(argv):
            globals_out.extend([a, argv[i+1]]); i += 2; continue
        if a.startswith('--shot='):
            globals_out.append(a); i += 1; continue
        rest.append(a); i += 1
    return globals_out + rest

def main(argv=None):
    argv = _hoist_globals(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog='claude-projects', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--json', action='store_true', help='machine-readable output'); ap.add_argument('--force', action='store_true', help='act even if a VNC viewer is connected')
    ap.add_argument('--stay', action='store_true', help='leave the app on the page (default: back to home)'); ap.add_argument('--shot', help='save a screenshot (jpg) at the end')
    sp = ap.add_subparsers(dest='cmd', required=True)
    s = sp.add_parser('projects', help='list projects'); s.add_argument('--refresh', action='store_true'); s.add_argument('--all', action='store_true', help='include archived')
    s = sp.add_parser('project', help='primary thread + threads of a project'); s.add_argument('project'); s.add_argument('--cached', action='store_true'); s.add_argument('--resolved', action='store_true'); s.add_argument('--all-threads', action='store_true'); s.add_argument('--max', type=int, default=10)
    s = sp.add_parser('threads', help='threads of a project'); s.add_argument('project'); s.add_argument('--resolved', action='store_true'); s.add_argument('--all-threads', action='store_true')
    s = sp.add_parser('read', help='read a thread (primary by default)'); s.add_argument('project'); s.add_argument('--thread'); s.add_argument('--cached', action='store_true'); s.add_argument('--max', type=int, default=20); s.add_argument('--history', type=int, default=0); s.add_argument('--expand', action='store_true'); s.add_argument('--attachments', action='store_true', help='list attachments/blocks per message'); s.add_argument('--images', action='store_true', help='inline image thumbnails (base64, JSON only)'); s.add_argument('--markdown', action='store_true', help='markdown rendering of each message')
    s = sp.add_parser('send', help='type + send a message'); s.add_argument('project'); s.add_argument('message', nargs='?'); s.add_argument('--file'); s.add_argument('--thread'); s.add_argument('--wait', action='store_true'); s.add_argument('--timeout', type=int, default=600); s.add_argument('--poll', type=int, default=10); s.add_argument('--dry-run', action='store_true', help='check targeting only; never presses Send'); s.add_argument('--type-test', action='store_true', help='with --dry-run: type the text, read it back, clear it'); s.add_argument('--attach-test', nargs='?', const='cancel', choices=['cancel', 'open'], default=None, help='with --dry-run and --attach: cancel (default) types each path in the picker, OCRs it and Cancels; --attach-test=open presses Open, waits for the chip, removes it again. Never types the message or presses Send'); s.add_argument('--clear-leftovers', action='store_true', help='remove attachment chips already in the message box (never sent) instead of refusing'); s.add_argument('--attach', action='append', help='file on the Claude Code host to attach (repeatable); --attach-json for base64')
    s.add_argument('--attach-json', help='JSON file: [{"path":...} | {"base64":...,"filename":...}]'); s.add_argument('--bot-name', help='name in the origin bracket: [sent by Grok Bot · NAME]')
    s.add_argument('--budget', type=float, help='answer within this many seconds; the send runs as a background job (call again, or --job-id, for the result)'); s.add_argument('--idempotency-key', help='with --budget: calls with the same key are one send'); s.add_argument('--job-id', help='the result of a send job (nothing is sent)')
    s = sp.add_parser('send-job', help=argparse.SUPPRESS); s.add_argument('key')
    sp.add_parser('jobs', help='busy check for deploys: the UI lock holder and running background jobs; exits 3 while busy, 0 when idle')
    s = sp.add_parser('attachment', help='get an attachment / file / image from a message'); s.add_argument('action', choices=['get', 'list']); s.add_argument('project'); s.add_argument('id', nargs='?', help='<message_index>.<n> (from read --attachments)')
    s.add_argument('--thread'); s.add_argument('--message', type=int); s.add_argument('--n', type=int, default=1); s.add_argument('--as', dest='as_', default='auto', choices=['auto', 'image', 'text', 'base64', 'path']); s.add_argument('--full', action='store_true', help='full-size image (default: max 1600 px / ~1 MB)'); s.add_argument('--out', help='also write the returned image/text/bytes to this file')
    s.add_argument('--budget', type=float, help='answer within this many seconds; a fetch still running comes back as pending with a job_id (call again for the file)'); s.add_argument('--refresh', action='store_true', help='fetch again even if this attachment was fetched before'); s.add_argument('--job', action='store_true', help=argparse.SUPPRESS)
    s = sp.add_parser('create-thread', help='ask the primary thread to start a new thread'); s.add_argument('project'); s.add_argument('title'); s.add_argument('instructions'); s.add_argument('--wait', action='store_true'); s.add_argument('--timeout', type=int, default=900); s.add_argument('--dry-run', action='store_true'); s.add_argument('--bot-name')
    s = sp.add_parser('wait', help='wait for a reply'); s.add_argument('project'); s.add_argument('--thread'); s.add_argument('--after', type=int); s.add_argument('--timeout', type=int, default=600); s.add_argument('--poll', type=int, default=10)
    s = sp.add_parser('create-project', help='create a project (optionally attach a GitHub repo, pin)'); s.add_argument('name'); s.add_argument('--description'); s.add_argument('--repo', help='owner/name or https://github.com/owner/name'); s.add_argument('--pin', dest='pin', action='store_true', default=True); s.add_argument('--no-pin', dest='pin', action='store_false')
    s = sp.add_parser('project-pin', help='pin / unpin a project'); s.add_argument('project'); s.add_argument('--unpin', action='store_true')
    s = sp.add_parser('project-env', help='READ-ONLY: project Environment settings (repos, cloud env, variable names only)'); s.add_argument('project')
    s = sp.add_parser('project-add-repo', help='attach a GitHub repo to an existing project (Settings -> Environment)'); s.add_argument('project'); s.add_argument('repo', help='owner/name or github URL'); s.add_argument('--dry-run', action='store_true')
    s = sp.add_parser('project-remove-repo', help='detach a GitHub repo from a project (exact, case-sensitive)'); s.add_argument('project'); s.add_argument('repo', help='owner/name or github URL'); s.add_argument('--dry-run', action='store_true')
    s = sp.add_parser('project-set-env', help="set cloud environment variables (values never printed); prefer --env-name for a dedicated env"); s.add_argument('project'); s.add_argument('--set', action='append', default=[], help='NAME=value (non-secret)'); s.add_argument('--secret-file', action='append', default=[], help='NAME=/path/on/host (chmod 600)'); s.add_argument('--env-name'); s.add_argument('--no-copy', action='store_true'); s.add_argument('--allow-shared', action='store_true'); s.add_argument('--dry-run', action='store_true')
    s = sp.add_parser('project-menu', help="list or press an item in a project's ... menu"); s.add_argument('project'); s.add_argument('--item'); s.add_argument('--confirm')
    s = sp.add_parser('refresh', help='refresh list + changed projects'); s.add_argument('projects', nargs='*'); s.add_argument('--background', action='store_true'); s.add_argument('--max', type=int, default=3)
    s = sp.add_parser('thread-status', help='compact triage: state, latest message, machine (cache by default)'); s.add_argument('project'); s.add_argument('--thread'); s.add_argument('--refresh', action='store_true', help='live UI read instead of cache')
    sp.add_parser('tidy-ui', help='cancel stray file-picker windows and return the app home (presses Cancel only)')
    sp.add_parser('probe-projects', help='READ-ONLY: does this account have Claude Code Projects (beta)?')
    sp.add_parser('status', help='app / lock / viewer status'); sp.add_parser('home', help='return the app to its home screen')
    s = sp.add_parser('origin', help='print the origin header (and the full text with --message) without touching the app'); s.add_argument('--message'); s.add_argument('--bot-name'); s.add_argument('--title'); s.add_argument('--instructions')
    sp.add_parser('config', help='print the effective configuration (paths only, no secrets)')
    s = sp.add_parser('permissions', help='READ-ONLY: list permission cards showing in a thread'); s.add_argument('project'); s.add_argument('--thread')
    s = sp.add_parser('permission', help='approve / deny a permission card by option label (dry run unless --apply)'); s.add_argument('project'); s.add_argument('option'); s.add_argument('--thread'); s.add_argument('--card'); s.add_argument('--apply', action='store_true', help='really press the button (default is a dry run)')
    s = sp.add_parser('forward', help='relay one message verbatim to another thread (dry run unless --apply)'); s.add_argument('source_project'); s.add_argument('target_project'); s.add_argument('--source-thread'); s.add_argument('--target-thread'); s.add_argument('--message', type=int); s.add_argument('--role', default='assistant', choices=['assistant', 'user', 'any']); s.add_argument('--note'); s.add_argument('--attachments', action='store_true'); s.add_argument('--cached-source', action='store_true'); s.add_argument('--bot-name'); s.add_argument('--apply', action='store_true')
    s = sp.add_parser('staging', help='managed staging dir: info, or sweep (dry run unless --apply)'); s.add_argument('action', choices=['info', 'sweep']); s.add_argument('--older-than-hours', type=float); s.add_argument('--kind', action='append'); s.add_argument('--apply', action='store_true')
    s = sp.add_parser('skill', help='print the canonical skill with the effective policy'); s.add_argument('--policy-only', action='store_true')
    s = sp.add_parser('screenshot', help='screenshot the app display'); s.add_argument('file')
    a = ap.parse_args(argv); F = dict(force=a.force)  # argv already hoisted
    import signal
    lim = int(os.environ.get('CLAUDE_PROJECTS_MAX_SECONDS', '0')) or ((getattr(a, 'timeout', 0) or 0) + 300 if a.cmd in ('send', 'wait') else 600)
    if a.cmd == 'send-job': lim = int(SEND_DEADLINE + 120)        # the job enforces its own deadline; this is the backstop
    def _alarm(*_): raise Interrupted(f'claude-projects watchdog: {a.cmd} exceeded {lim}s')
    def _term(*_): raise Interrupted('stopped by SIGTERM')    # finally blocks run: composer cleanup, lock release, go home
    signal.signal(signal.SIGALRM, _alarm); signal.alarm(lim); signal.signal(signal.SIGTERM, _term)
    rc = 0
    try:
        if a.cmd == 'projects': res = op_projects(a.refresh, a.all, **F, stay=a.stay)
        elif a.cmd == 'project': res = op_project_get(a.project, not a.cached, a.resolved, a.all_threads, a.max, **F, stay=a.stay, shot=a.shot)
        elif a.cmd == 'threads':
            r = op_project_get(a.project, True, a.resolved, a.all_threads, 1, **F, stay=a.stay, shot=a.shot); res = {'project': r['project'], 'project_id': r['project_id'], 'threads': r.get('threads')}
        elif a.cmd == 'read':
            res = op_read(a.project, a.thread, not a.cached, a.max, a.history, a.expand, **F, stay=a.stay, shot=a.shot, include_images=a.images, markdown=a.markdown)
            if a.attachments and not a.json: res['_show_atts'] = True
        elif a.cmd == 'attachment':
            if a.action == 'list':
                r = op_read(a.project, a.thread, True, 0 if a.message else 30, 0, False, **F, stay=a.stay)
                res = {'project': r['project'], 'thread_id': r['thread_id'], 'attachments': [dict(at, message_index=m['index']) for m in r['messages'] if (not a.message or m['index'] == a.message) for at in (m.get('attachments') or [])]}
            else:
                res = op_attachment_get(a.project, a.thread, a.id, a.message, a.n, a.as_, a.full, **F, stay=a.stay, shot=a.shot, budget=a.budget, refresh=a.refresh, job=a.job)
                if a.out:
                    o = pathlib.Path(a.out).expanduser(); o.parent.mkdir(parents=True, exist_ok=True)
                    if res.get('image'): o.write_bytes(base64.b64decode(res['image']['base64']))
                    elif res.get('base64'): o.write_bytes(base64.b64decode(res['base64']))
                    elif res.get('text') is not None and res.get('available'): o.write_text(res['text'], encoding='utf-8')
                    else: o = None
                    if o: res['written_to'] = str(o)
                if not a.json:
                    res = {k: (v if k not in ('image', 'base64') else (dict(v, base64=f"<{len(v['base64'])} base64 chars>") if isinstance(v, dict) else f'<{len(v)} base64 chars>')) for k, v in res.items()}
        elif a.cmd == 'send-job': res = run_send_job(a.key)
        elif a.cmd == 'jobs':
            res = deploy_busy(); rc = 3 if res['busy'] else 0   # non-zero while busy: `claude-projects jobs && restart` is safe
        elif a.cmd == 'send' and a.job_id: res = op_send_job(a.project, None, job_id=a.job_id)
        elif a.cmd == 'send' and a.budget is not None and not a.dry_run and not a.wait:
            msg = pathlib.Path(a.file).read_text(encoding='utf-8') if a.file else (a.message if a.message not in (None, '-') else sys.stdin.read())
            atts = list(a.attach or []) + (json.loads(pathlib.Path(a.attach_json).read_text()) if a.attach_json else [])
            res = op_send_job(a.project, msg.rstrip('\n'), a.thread, a.force, atts or None, a.bot_name, a.budget, a.idempotency_key, clear_leftovers=a.clear_leftovers)
        elif a.cmd == 'send':
            msg = pathlib.Path(a.file).read_text(encoding='utf-8') if a.file else (a.message if a.message not in (None, '-') else sys.stdin.read())
            atts = list(a.attach or []) + (json.loads(pathlib.Path(a.attach_json).read_text()) if a.attach_json else [])
            res = op_send(a.project, msg.rstrip('\n'), a.thread, a.wait, a.timeout, a.poll, **F, stay=a.stay, shot=a.shot, dry_run=a.dry_run, type_test=a.type_test, attachments=atts or None, bot_name=a.bot_name, attach_test=getattr(a, 'attach_test', None) or False, clear_leftovers=a.clear_leftovers)
        elif a.cmd == 'create-thread': res = op_create_thread(a.project, a.title, a.instructions, a.wait, a.timeout, 15, **F, stay=a.stay, shot=a.shot, dry_run=a.dry_run, bot_name=a.bot_name)
        elif a.cmd == 'wait': res = op_wait(a.project, a.thread, a.after, a.timeout, a.poll, **F, stay=a.stay, shot=a.shot)
        elif a.cmd == 'create-project': res = op_create_project(a.name, a.description, a.repo, a.pin, **F, stay=a.stay, shot=a.shot)
        elif a.cmd == 'project-pin': res = op_project_pin(a.project, not a.unpin, **F, stay=a.stay, shot=a.shot)
        elif a.cmd == 'project-env': res = op_project_env(a.project, **F, stay=a.stay, shot=a.shot)
        elif a.cmd == 'project-add-repo': res = op_project_add_repo(a.project, a.repo, **F, stay=a.stay, shot=a.shot, dry_run=a.dry_run)
        elif a.cmd == 'project-remove-repo': res = op_project_remove_repo(a.project, a.repo, **F, stay=a.stay, shot=a.shot, dry_run=a.dry_run)
        elif a.cmd == 'project-set-env': res = op_project_set_env(a.project, dict(x.split('=', 1) for x in a.set), dict(x.split('=', 1) for x in a.secret_file), a.env_name, not a.no_copy, a.allow_shared, a.dry_run, **F, stay=a.stay)
        elif a.cmd == 'project-menu': res = op_project_menu(a.project, a.item, a.confirm, **F, stay=a.stay, shot=a.shot)
        elif a.cmd == 'refresh': res = op_refresh(a.projects, a.background, a.max, **F)
        elif a.cmd == 'thread-status': res = op_thread_status(a.project, a.thread, a.refresh, **F, stay=a.stay)
        elif a.cmd == 'status': res = op_status()
        elif a.cmd == 'probe-projects': res = op_probe_projects(**F)
        elif a.cmd == 'tidy-ui':
            with ui_lock('tidy-ui', a.force): res = {'file_pickers_closed': _dismiss_open_files('tidy'), 'home': go_home()}
        elif a.cmd == 'origin':
            bn = a.bot_name if a.bot_name is not None else CFG.get('bot_name')
            text = create_thread_message(a.title, a.instructions or '', a.bot_name) if a.title else add_origin(a.message or '', bn)
            res = {'origin_prefix': ORIGIN_PREFIX, 'origin_header': origin_header(bn), 'text': text}
        elif a.cmd == 'config': res = dict(CFG, config_file=str(cfg_mod.CONFIG_FILE))
        elif a.cmd == 'permissions': res = op_permissions(a.project, a.thread, **F, stay=a.stay)
        elif a.cmd == 'permission': res = op_permission(a.project, a.option, a.thread, a.card, dry_run=not a.apply, **F, stay=a.stay, shot=a.shot)
        elif a.cmd == 'forward': res = op_forward(a.source_project, a.target_project, a.source_thread, a.target_thread, a.message, a.role, a.note, a.attachments, not a.cached_source, dry_run=not a.apply, bot_name=a.bot_name, **F, stay=a.stay)
        elif a.cmd == 'staging': res = op_staging(a.action, dry_run=not a.apply, older_than_hours=a.older_than_hours, kinds=a.kind)
        elif a.cmd == 'skill':
            from claude_projects import policy as policy_mod, config as _cc
            pol = policy_mod.Policy(CFG['skill_path'], _cc.CONFIG_DIR)
            res = pol.effective() if a.policy_only else pol.skill_with_policy()
        elif a.cmd == 'home':
            with ui_lock('home', a.force): res = {'home': go_home(), 'route': route()}
        elif a.cmd == 'screenshot': res = {'file': screenshot(a.file)}
    except Interrupted as ex:
        out = {'error': 'Interrupted', 'message': str(ex), 'code': 6}
        if getattr(ex, 'cleanup', None): out['cleanup'] = ex.cleanup
        print(json.dumps(out, ensure_ascii=False) if a.json else f'claude-projects: {ex}', file=sys.stdout if a.json else sys.stderr); log(f'{a.cmd} interrupted: {ex}'); return 6
    except CPGError as ex:
        out = {'error': type(ex).__name__, 'message': str(ex), 'code': ex.code}
        if getattr(ex, 'cleanup', None): out['cleanup'] = ex.cleanup
        if isinstance(ex, Busy):
            h = _read_ui_holder()
            if h: out['holder'] = h
        print(json.dumps(out, ensure_ascii=False) if a.json else f'claude-projects: {ex}', file=sys.stdout if a.json else sys.stderr); log(f'{a.cmd} error {ex}'); return ex.code
    if CFG.get('legacy_field_aliases'): res = legacy_aliases(res)
    if a.json: print(json.dumps(res, ensure_ascii=False, default=str))
    else: human(res)
    return rc
if __name__ == '__main__': sys.exit(main())
