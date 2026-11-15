"""Mock tests for the send-hang patch (no X, no UI). Run: venv/bin/python -m pytest -q test_send_hang.py"""
import sys, pathlib, pytest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import conftest  # noqa: F401  (isolated config)
from claude_projects import core as c

class FakeX:
    def __init__(self, open_wids, escapes_to_close=2):
        self.open = list(open_wids); self.esc = {w: 0 for w in open_wids}; self.need = escapes_to_close; self.calls = []; self.active = None
    def __call__(self, *a, inp=None):
        self.calls.append(a)
        if a[0] == 'search': return '\n'.join(self.open)
        if a[0] == 'getwindowname': return 'Open Files' if a[1] in self.open else 'Claude'
        if a[0] == 'windowactivate': self.active = a[-1]; return ''
        if a[0] == 'key' and a[-1] == 'Escape' and self.active in self.open:
            self.esc[self.active] += 1
            if self.esc[self.active] >= self.need: self.open.remove(self.active)
        return ''

@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(c, 'log', lambda m: None); monkeypatch.setattr(c.time, 'sleep', lambda s: None)

def test_wids_and_dismiss_two_escapes(monkeypatch):
    fx = FakeX(['111']); monkeypatch.setattr(c, 'xdo', fx)
    assert c._open_files_wids() == ['111']
    assert c._dismiss_open_files('t') is True
    assert not any(x[0] == 'click' for x in fx.calls)  # never clicks Open/anything when Escape works

def test_dismiss_falls_back_to_geometry_cancel(monkeypatch):
    fx = FakeX(['222'], escapes_to_close=99); monkeypatch.setattr(c, 'xdo', fx)
    monkeypatch.setattr(c, '_file_chooser_root', lambda: None)
    monkeypatch.setattr(c, '_xdo_geom', lambda w: {'X': 0, 'Y': 201, 'WIDTH': 719, 'HEIGHT': 822})
    monkeypatch.setattr(c, '_screen_wh', lambda: (540, 1170))
    clicks = []; monkeypatch.setattr(c, '_click_xy', lambda x, y, **k: (clicks.append((x, y)), fx.open.clear()))
    assert c._dismiss_open_files('t') is True
    assert ('windowmove', '222', str(540 - 719), '201') in fx.calls
    assert clicks and clicks[0] == (719 - 140, 201 + 822 - 46)

def test_attach_failure_always_dismisses(monkeypatch):
    called = []
    monkeypatch.setattr(c, '_attach_files_impl', lambda f: (_ for _ in ()).throw(c.AppError("folder 'opt' not found")))
    monkeypatch.setattr(c, '_dismiss_open_files', lambda reason='': called.append(reason) or True)
    with pytest.raises(c.AppError): c.attach_files([{'path': '/x', 'name': 'x'}])
    assert called[-1] == 'attach failed'

def test_home_fallback_is_y40(monkeypatch):
    monkeypatch.setattr(c, '_chooser_home', lambda root: None)
    monkeypatch.setattr(c, 'xdo', lambda *a, inp=None: '')
    monkeypatch.setattr(c, '_xdo_geom', lambda w: {'X': 0, 'Y': 201, 'WIDTH': 719, 'HEIGHT': 822})
    clicks = []; monkeypatch.setattr(c, '_click_xy', lambda x, y, **k: clicks.append((x, y)))
    c._browse_chooser_to_dir(None, str(pathlib.Path.home()))
    assert clicks == [(60, 241)]

def test_patched_source_markers():
    src = (pathlib.Path(__file__).resolve().parent.parent / "claude_projects/core.py").read_text()
    assert "_click_xy(g['X'] + 70, g['Y'] + 120)" not in src
    assert "if _open_files_wids():" in src and "_attach_files_impl(files)" in src
