"""Config precedence and the absence of deployment-specific literals."""
import json, os, pathlib, re, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import conftest  # noqa: F401
import cpg_config as K

REPO = pathlib.Path(__file__).resolve().parents[1]


def test_defaults_are_generic(tmp_path):
    c = K.load(tmp_path / 'missing.json', environ={})
    assert c['display'] == ':99'
    assert c['mcp_host'] == '127.0.0.1' and c['mcp_port'] == 8791
    assert c['viewer_ports'] == [5999, 6179]
    assert c['ui_lock_file'].endswith('ui.lock')
    assert c['environment_labels'] == {} and c['environment_ids'] == {}


def test_file_then_env(tmp_path):
    f = tmp_path / 'c.json'
    f.write_text(json.dumps({'display': ':42', 'bot_name': 'FromFile', 'mcp_port': 9000}))
    c = K.load(f, environ={})
    assert c['display'] == ':42' and c['bot_name'] == 'FromFile' and c['viewer_ports'] == [5942, 6122]
    c = K.load(f, environ={'CPG_DISPLAY': ':7', 'CPG_BOT_NAME': 'FromEnv', 'CPG_MCP_PORT': '9100', 'CPG_VIEWER_PORTS': '1,2'})
    assert c['display'] == ':7' and c['bot_name'] == 'FromEnv' and c['mcp_port'] == 9100 and c['viewer_ports'] == [1, 2]


# /home/cpg/ is allowed: it is the plugin's own user in its WSL distro, the same on every PC
GENERIC = (r'/home/(?!cpg/)[a-z][a-z0-9_-]*/|/Users/[A-Za-z]|env_01[0-9A-Za-z]{8}|chan_01[0-9A-Za-z]{8}|cse_01[0-9A-Za-z]{8}|'
           r'cmsg_01[0-9A-Za-z]{8}|\b100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+\b|Bearer [A-Za-z0-9_-]{24,}')
# Maintainers: put deployment-specific words (hostnames, user names, domains) one per line in a file outside the repo and
# point CPG_PRIVATE_PATTERNS at it, so the check covers them without publishing them here.
_extra = os.environ.get('CPG_PRIVATE_PATTERNS')
_words = [re.escape(w.strip()) for w in (open(_extra).read().splitlines() if _extra and os.path.isfile(_extra) else []) if w.strip()]
PRIVATE = re.compile('|'.join([GENERIC] + _words), re.I)


def test_no_deployment_literals_in_shipped_files():
    bad = []
    for f in (list(REPO.glob('*.py')) + list(REPO.glob('*.sh')) + list(REPO.glob('*.md')) + [REPO / 'claude-projects'] + list((REPO / 'bin').glob('*'))
              + list((REPO / 'systemd').glob('*')) + list((REPO / 'skill').rglob('*.md')) + list((REPO / 'docs').glob('*.md'))
              + [p for p in (REPO / 'hosts').rglob('*') if p.is_file()]):
        if f.name in ('LICENSE', 'test_config.py') or not f.is_file(): continue
        for n, line in enumerate(f.read_text(errors='ignore').splitlines(), 1):
            if PRIVATE.search(line): bad.append(f'{f.name}:{n}: {line.strip()[:100]}')
    assert not bad, '\n'.join(bad)
