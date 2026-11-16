"""MCP surface: tool list, origin-bracket instructions in descriptions, bot_name pass-through (no app, no network)."""
import asyncio, pathlib, re
import pytest

mcp_mod = pytest.importorskip('mcp')
from claude_projects import mcp_server as M

EXPECTED = {
    'claude_projects_list', 'claude_project_get', 'claude_thread_read', 'claude_thread_attachment_get', 'claude_thread_send',
    'claude_thread_wait', 'claude_thread_create', 'claude_thread_status', 'claude_refresh', 'claude_project_create',
    'claude_project_pin', 'claude_project_env', 'claude_project_set_env', 'claude_project_add_repo',
    'claude_project_remove_repo', 'claude_app_status',
    # oss-prep additions
    'claude_thread_permissions', 'claude_thread_permission', 'claude_thread_forward', 'claude_staging',
    'claude_skill_get', 'claude_policy_get', 'claude_policy_propose',
    # 1.1: guided setup
    'claude_setup',
    # 1.0.3: threads waiting on the user across Projects
    'claude_attention',
}


def tools():
    return {t.name: t for t in asyncio.run(M.mcp.list_tools())}


def test_tool_list():
    assert set(tools()) == EXPECTED


def test_bracket_documented_for_agents():
    instr = M.mcp.instructions
    assert '[sent by Grok Bot]' in instr and 'bot-sent' in instr and 'no bracket' in instr
    t = tools()
    for name in ('claude_thread_send', 'claude_thread_create'):
        assert '[sent by Grok Bot' in t[name].description, name
        assert 'bot_name' in t[name].inputSchema['properties'], name
    assert 'sent_by_bot' in t['claude_thread_read'].description


def test_send_passes_bot_name(monkeypatch):
    seen = {}
    async def fake_run(args, timeout=900, msg=None, attach=None, busy_budget=None):
        seen.update(args=args, msg=msg); return {'ok': True}
    monkeypatch.setattr(M, 'run', fake_run)
    asyncio.run(M.claude_thread_send('Demo Project', 'hello', bot_name='Scout', dry_run=True))
    assert seen['args'][:2] == ['send', 'Demo Project'] and '--bot-name' in seen['args'] and 'Scout' in seen['args']
    assert '--dry-run' in seen['args'] and seen['msg'] == 'hello'   # the bracket is added once, in claude_projects.core
    asyncio.run(M.claude_thread_create('Demo Project', 'T', 'do it', dry_run=True, bot_name='Scout'))
    assert seen['args'][0] == 'create-thread' and 'Scout' in seen['args']


def test_systemd_templates_render(tmp_path):
    subs = {'DISPLAY': ':99', 'SCREEN': '540x1170', 'REPO': '/opt/x', 'CONFIG_DIR': '/c', 'CONFIG_FILE': '/c/config.json',
            'VNC_PORT': '5999', 'NOVNC_PORT': '6179', 'VIEWPORT_BIND': '127.0.0.1', 'NOVNC_WEB': '/usr/share/novnc',
            'MCP_HOST': '127.0.0.1', 'MCP_PORT': '8791', 'KEYRING_ENV': 'Environment=XDG_DATA_HOME=/k'}
    for f in (pathlib.Path(M.__file__).parent.parent / 'systemd').iterdir():
        s = f.read_text()
        for k, v in subs.items(): s = s.replace(f'@{k}@', v)
        assert not re.search(r'@[A-Z_]+@', s), f.name


def _capture(monkeypatch):
    seen = {}
    async def fake_run(args, timeout=900, msg=None, attach=None, busy_budget=None):
        seen.update(args=args, msg=msg, attach=attach); return {'ok': True}
    monkeypatch.setattr(M, 'run', fake_run)
    return seen


def test_write_tools_default_to_dry_run(monkeypatch):
    t = tools()
    for name in ('claude_thread_permission', 'claude_thread_forward', 'claude_staging'):
        assert t[name].inputSchema['properties']['dry_run'].get('default') is True, name
    seen = _capture(monkeypatch)
    asyncio.run(M.claude_thread_permission('Demo', 'Add folder'))
    assert seen['args'][:3] == ['permission', 'Demo', 'Add folder'] and '--apply' not in seen['args']
    asyncio.run(M.claude_thread_permission('Demo', 'Not now', thread='cmsg_X', card_id='perm_1', dry_run=False))
    assert '--apply' in seen['args'] and '--card' in seen['args'] and 'cmsg_X' in seen['args']
    asyncio.run(M.claude_thread_forward('Src', 'Dst', message_index=4, note='fyi'))
    a = seen['args']
    assert a[:3] == ['forward', 'Src', 'Dst'] and '--apply' not in a and a[a.index('--message') + 1] == '4' and 'fyi' in a
    asyncio.run(M.claude_thread_forward('Src', 'Dst', include_attachments=True, refresh_source=False, dry_run=False))
    assert '--apply' in seen['args'] and '--attachments' in seen['args'] and '--cached-source' in seen['args']
    asyncio.run(M.claude_staging('sweep'))
    assert seen['args'][:2] == ['staging', 'sweep'] and '--apply' not in seen['args']
    asyncio.run(M.claude_staging('sweep', dry_run=False, older_than_hours=1, kinds=['outgoing']))
    assert '--apply' in seen['args'] and seen['args'].count('--kind') == 1


def test_send_attach_test_only_with_dry_run(monkeypatch):
    seen = _capture(monkeypatch)
    asyncio.run(M.claude_thread_send('Demo', 'x', dry_run=True, attach_test=True, attachments=[{'path': '/tmp/a.png'}]))
    assert '--attach-test' in seen['args'] and '--dry-run' in seen['args']
    asyncio.run(M.claude_thread_send('Demo', 'x', dry_run=False, attach_test=True))
    assert '--attach-test' not in seen['args']


def test_skill_and_policy_tools():
    sk = asyncio.run(M.claude_skill_get())
    assert 'POLICY:BEGIN' in sk['text'] and 'claude_thread_permission' in sk['text'] and sk['sha256']
    pol = asyncio.run(M.claude_policy_get())
    assert pol['rules']['pre_approved'] and pol['rules']['surface_first'] and pol['rules']['never']
    assert 'POLICY' in M.mcp.instructions and 'claude_skill_get' in M.mcp.instructions


def test_skill_documents_every_tool():
    text = (pathlib.Path(M.__file__).parent.parent / 'skill' / 'SKILL.md').read_text()
    missing = [n for n in tools() if n not in text]
    assert not missing, missing


def test_send_attach_test_open_and_clear_leftovers(monkeypatch):
    seen = _capture(monkeypatch)
    asyncio.run(M.claude_thread_send('Demo', 'x', dry_run=True, attach_test='open', attachments=[{'path': '/tmp/a.png'}]))
    assert '--attach-test=open' in seen['args']
    asyncio.run(M.claude_thread_send('Demo', 'x', clear_leftovers=True))
    assert '--clear-leftovers' in seen['args'] and '--budget' in seen['args']
