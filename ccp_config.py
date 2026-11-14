"""Configuration for claude-projects-mcp.

Precedence (lowest to highest): built-in defaults < JSON config file < environment variables.

Config file: $CCP_CONFIG, else $XDG_CONFIG_HOME/claude-projects-mcp/config.json
(~/.config/claude-projects-mcp/config.json). install.sh writes it; every key is optional.
Nothing in this module reads a secret. `token_file` is only a path; ccp_mcp.py reads it at start-up.
"""
import os, sys, json, pathlib, shutil

APP = 'claude-projects-mcp'
HOME = pathlib.Path.home()
REPO_DIR = pathlib.Path(__file__).resolve().parent


def _xdg(var, fallback):
    v = os.environ.get(var)
    return pathlib.Path(v) if v else HOME / fallback


CONFIG_DIR = pathlib.Path(os.environ.get('CCP_CONFIG_DIR') or (_xdg('XDG_CONFIG_HOME', '.config') / APP))
CONFIG_FILE = pathlib.Path(os.environ.get('CCP_CONFIG') or (CONFIG_DIR / 'config.json'))

DEFAULTS = {
    # where ccp keeps its cache, logs, lock and fetched attachments
    'state_dir': str(_xdg('XDG_STATE_HOME', '.local/state') / APP),
    # the X display the Claude desktop app runs on (its own "viewport"; install.sh creates it with Xvfb)
    'display': ':99',
    # command that opens a claude:// deep link in the running app (single-instance hand-off)
    'launcher': str(REPO_DIR / 'bin' / 'claude-desktop-launch'),
    # Claude desktop app binary (official Linux build installs /usr/bin/claude-desktop)
    'claude_binary': 'claude-desktop',
    # the app's profile directory (IndexedDB + HTTP cache are read from a private COPY)
    'claude_config_dir': str(HOME / '.config' / 'Claude'),
    'xdotool': 'xdotool',
    'xclip': 'xclip',
    # optional extra library / GObject typelib dirs (only for user-space package installs)
    'extra_lib_path': '',
    'gi_typelib_path': '',
    # python that has ccl_chromium_reader installed (install.sh: the project venv)
    'idb_python': sys.executable,
    # lock file that serializes every UI action (share it with any other tool that drives the same display)
    'ui_lock_file': '',
    # TCP ports whose established connections mean "a person is watching the viewport" (UI actions refuse)
    'viewer_ports': [],
    # zone used for *_local timestamps in output; '' = this machine's local zone
    'timezone': '',
    # zone the app renders transcript times in; '' = this machine's local zone
    'app_timezone': '',
    # wording only: how responses refer to the person who owns the Claude account and the app host
    'operator_name': 'the user',
    'host_label': 'the Claude Code host',
    # optional name shown in the origin bracket: [sent by Grok Bot · <bot_name>]
    'bot_name': '',
    # MCP server
    'token_file': str(CONFIG_DIR / 'mcp.token'),
    'mcp_host': '127.0.0.1',
    'mcp_port': 8791,
    # extra Host headers allowed by DNS-rebinding protection (e.g. your tunnel hostname); ['*'] disables the check
    'allowed_hosts': [],
    # managed staging / cache for files moving into and out of threads (default: <state_dir>/staging)
    'staging_dir': '',
    # per-kind TTL in hours: incoming, outgoing, attach-test, msg (see ccp_staging.py)
    'staging_ttl_hours': {},
    'staging_max_mb': 2048,
    'staging_sweep_interval_min': 60,
    # content-addressed store of attachments fetched from threads (default: <staging>/incoming)
    'attachments_dir': '',
    # canonical agent skill; its POLICY section is the default policy (user override: <config dir>/policy.md)
    'skill_path': str(REPO_DIR / 'skill' / 'SKILL.md'),
    # also emit pre-1.0 field names (time_ct, path_on_home_lab, ...) next to the new ones, for older agent skills
    'legacy_field_aliases': False,
    # Claude Code environment label ("Running on <label>") -> {resolved, detail, machine_kind}
    'environment_labels': {},
    # IndexedDB environment_id (or prefix) -> {resolved, detail, machine_kind}
    'environment_ids': {},
}

ENV_KEYS = {
    'CCP_STATE_DIR': 'state_dir', 'CCP_DISPLAY': 'display', 'CCP_LAUNCHER': 'launcher',
    'CCP_CLAUDE_BINARY': 'claude_binary', 'CCP_CLAUDE_CONFIG_DIR': 'claude_config_dir',
    'CCP_XDOTOOL': 'xdotool', 'CCP_XCLIP': 'xclip', 'CCP_EXTRA_LIB_PATH': 'extra_lib_path',
    'CCP_GI_TYPELIB_PATH': 'gi_typelib_path', 'CCP_IDB_PYTHON': 'idb_python', 'CCP_UI_LOCK_FILE': 'ui_lock_file',
    'CCP_VIEWER_PORTS': 'viewer_ports', 'CCP_TIMEZONE': 'timezone', 'CCP_APP_TIMEZONE': 'app_timezone',
    'CCP_OPERATOR_NAME': 'operator_name', 'CCP_HOST_LABEL': 'host_label', 'CCP_BOT_NAME': 'bot_name',
    'CCP_TOKEN_FILE': 'token_file', 'CCP_MCP_HOST': 'mcp_host', 'CCP_MCP_PORT': 'mcp_port',
    'CCP_ALLOWED_HOSTS': 'allowed_hosts', 'CCP_STAGING_DIR': 'staging_dir', 'CCP_STAGING_MAX_MB': 'staging_max_mb',
    'CCP_ATTACHMENTS_DIR': 'attachments_dir', 'CCP_SKILL_PATH': 'skill_path',
}
_LIST_KEYS = {'viewer_ports', 'allowed_hosts'}


def _coerce(key, val):
    d = DEFAULTS[key]
    if key in _LIST_KEYS and isinstance(val, str):
        val = [x.strip() for x in val.split(',') if x.strip()]
        if key == 'viewer_ports': val = [int(x) for x in val]
    elif isinstance(d, int) and not isinstance(d, bool) and isinstance(val, str):
        val = int(val)
    return val


def load(path=None, environ=None):
    """Return the merged config dict. Unknown keys in the file are kept (forward compatible)."""
    environ = os.environ if environ is None else environ
    cfg = json.loads(json.dumps(DEFAULTS))
    p = pathlib.Path(path) if path else CONFIG_FILE
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding='utf-8'))
            if isinstance(data, dict): cfg.update(data)
        except Exception as ex:
            print(f'{APP}: ignoring unreadable config {p}: {ex}', file=sys.stderr)
    for env, key in ENV_KEYS.items():
        if environ.get(env) not in (None, ''):
            cfg[key] = _coerce(key, environ[env])
    for k in ('state_dir', 'launcher', 'claude_config_dir', 'token_file', 'ui_lock_file', 'staging_dir', 'attachments_dir', 'skill_path', 'xclip', 'xdotool', 'idb_python', 'extra_lib_path', 'gi_typelib_path'):
        if cfg.get(k): cfg[k] = os.path.expanduser(str(cfg[k]))
    if not cfg.get('ui_lock_file'):
        cfg['ui_lock_file'] = str(pathlib.Path(cfg['state_dir']) / 'ui.lock')
    if not cfg.get('staging_dir'):
        cfg['staging_dir'] = str(pathlib.Path(cfg['state_dir']) / 'staging')
    if not cfg.get('attachments_dir'):
        cfg['attachments_dir'] = str(pathlib.Path(cfg['staging_dir']) / 'incoming')
    if not cfg.get('viewer_ports'):
        cfg['viewer_ports'] = default_viewer_ports(cfg['display'])
    return cfg


def display_number(display):
    try: return int(str(display).split(':', 1)[1].split('.', 1)[0])
    except Exception: return 99


def default_viewer_ports(display):
    """x11vnc on 5900+N and noVNC on 6080+N, as install.sh sets them up."""
    n = display_number(display)
    return [5900 + n, 6080 + n]


def tool(cfg, key):
    """Resolve a binary from config (absolute path or a name on PATH)."""
    v = cfg.get(key) or key
    return v if os.path.isabs(v) else (shutil.which(v) or v)


CFG = load()
