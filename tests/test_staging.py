"""Managed staging dir: unique slots, TTL / size sweeps, base64 staging without overwrites."""
import base64, os, pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import conftest  # noqa: F401
import cpg_staging as S
import cpg_core as C


def test_unique_slots_keep_name(tmp_path):
    st = S.Staging(tmp_path/'stage')
    a = st.write_bytes('outgoing', 'shot.png', b'a'); b = st.write_bytes('outgoing', 'shot.png', b'b')
    assert a != b and a.name == b.name == 'shot.png' and a.read_bytes() == b'a' and b.read_bytes() == b'b'
    assert st.contains(a) and not st.contains(tmp_path/'elsewhere')
    assert S.Staging.safe_name('../../etc/passwd') == 'passwd' and S.Staging.safe_name('') == 'file.bin'


def test_sweep_ttl_and_dry_run(tmp_path):
    st = S.Staging(tmp_path/'stage', ttl_hours={'outgoing': 1})
    old = st.write_bytes('outgoing', 'old.txt', b'x' * 10); new = st.write_bytes('outgoing', 'new.txt', b'y')
    past = time.time() - 7200; os.utime(old.parent, (past, past))
    dry = st.sweep(dry_run=True)
    assert dry['removed_count'] == 1 and old.exists()
    real = st.sweep()
    assert real['removed_count'] == 1 and not old.exists() and new.exists()
    assert st.info()['kinds']['outgoing']['entries'] == 1


def test_sweep_size_cap_oldest_first(tmp_path):
    st = S.Staging(tmp_path/'stage', max_mb=0.001)  # ~1 KB
    a = st.write_bytes('incoming', 'a.bin', b'a' * 800); time.sleep(0.01)
    b = st.write_bytes('incoming', 'b.bin', b'b' * 800)
    os.utime(a.parent, (time.time() - 100, time.time() - 100))
    r = st.sweep()
    assert [x['reason'] for x in r['removed']] == ['over_size_cap'] and not a.exists() and b.exists()


def test_maybe_sweep_throttled(tmp_path):
    st = S.Staging(tmp_path/'stage', sweep_interval_min=60)
    assert st.maybe_sweep() is not None
    assert st.maybe_sweep() is None


def test_never_deletes_outside_root(tmp_path):
    st = S.Staging(tmp_path/'stage')
    outside = tmp_path/'keep.txt'; outside.write_text('keep')
    st.dir('outgoing'); (tmp_path/'stage'/'outgoing'/'link').symlink_to(outside)
    st.sweep(older_than_hours=0)
    assert outside.exists()


def test_base64_attachments_do_not_overwrite(monkeypatch, tmp_path):
    monkeypatch.setattr(C, 'STAGE', S.Staging(tmp_path/'stage'))
    one = C.prepare_attachment_files([{'base64': base64.b64encode(b'first').decode(), 'filename': 'clip.mp4'}])[0]
    two = C.prepare_attachment_files([{'base64': base64.b64encode(b'second').decode(), 'filename': 'clip.mp4'}])[0]
    assert one['path'] != two['path'] and one['name'] == two['name'] == 'clip.mp4'
    assert pathlib.Path(one['path']).read_bytes() == b'first' and one['staged'] and os.path.isabs(one['path'])


def test_op_staging(monkeypatch, tmp_path):
    monkeypatch.setattr(C, 'STAGE', S.Staging(tmp_path/'stage'))
    info = C.op_staging('info')
    assert info['root'] == str(tmp_path/'stage') and set(info['kinds']) == set(S.KINDS) and 'attachments_dir' in info
    assert C.op_staging('sweep')['dry_run'] is True
