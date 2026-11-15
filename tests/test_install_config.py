"""install.sh's config step writes viewer_ports for the chosen display and keeps ports set by hand."""
import json, os, pathlib, re, subprocess, sys

REPO = pathlib.Path(__file__).resolve().parents[1]


def _config_step():
    src = (REPO / 'install.sh').read_text()
    m = re.search(r"<<'PY'\n(.*?)\nPY\n", src, re.S)
    assert m, 'config heredoc not found in install.sh'
    return m.group(1)


def _run(tmp_path, display, existing=None):
    cfg = tmp_path / 'config.json'
    if existing is not None: cfg.write_text(json.dumps(existing))
    n = int(display[1:])
    env = dict(os.environ, CLAUDE_PROJECTS_REPO=str(REPO), CLAUDE_PROJECTS_CFG=str(cfg), CLAUDE_PROJECTS_DISP=display, CLAUDE_PROJECTS_BN='', CLAUDE_PROJECTS_OP='', CLAUDE_PROJECTS_TZ='',
               CLAUDE_PROJECTS_VNC_PORT=str(5900 + n), CLAUDE_PROJECTS_NOVNC_PORT=str(6080 + n))
    subprocess.run([sys.executable, '-'], input=_config_step(), text=True, env=env, check=True)
    return json.loads(cfg.read_text())


def test_fresh_install_writes_viewer_ports(tmp_path):
    assert _run(tmp_path, ':99')['viewer_ports'] == [5999, 6179]


def test_display_change_moves_default_ports(tmp_path):
    c = _run(tmp_path, ':42', {'display': ':99', 'viewer_ports': [5999, 6179]})
    assert c['display'] == ':42' and c['viewer_ports'] == [5942, 6122]


def test_custom_ports_are_kept(tmp_path):
    c = _run(tmp_path, ':42', {'display': ':99', 'viewer_ports': [5901, 6081]})
    assert c['viewer_ports'] == [5901, 6081]
