#!/usr/bin/env python3
"""Unit tests for M0 hygiene: parse_msg_time/age, machine resolve, send-compare."""
import datetime
import sys
import unicodedata
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import conftest  # noqa: F401  (isolated config when run as a plain script)

from claude_projects import core as C

def approx(a, b, tol=1.0):
    return abs(a - b) <= tol

def test_parse_msg_time_year_pin():
    # Without year, must NOT land in 1900
    m = {"time_local": "Nov 04 10:02 PM EST"}
    dt = C.parse_msg_time(m)
    assert dt is not None, "parse_msg_time returned None"
    assert dt.year >= 2024, f"year was {dt.year}, expected current/recent (not 1900)"
    age = C.age_hours_of(m, ref=datetime.datetime(2026, 11, 6, 10, 0, tzinfo=datetime.timezone.utc))
    assert age is not None
    assert age < 10000, f"age_hours exploded: {age}"
    assert age > 0
    print("PASS parse_msg_time year pin", dt, "age", age)

def test_parse_msg_time_future_rolls_prev_year():
    # If today is early January and time_local says Dec 30, pin to previous year
    ref = datetime.datetime(2027, 1, 2, 12, 0, tzinfo=datetime.timezone.utc)
    # monkeypatch now()
    real_now = C.now
    C.now = lambda: ref
    try:
        m = {"time_local": "Dec 30 03:00 PM EST"}
        dt = C.parse_msg_time(m)
        assert dt.year == 2026, dt
        age = C.age_hours_of(m, ref=ref)
        assert 24 < age < 200, age
        print("PASS prev-year roll", dt, age)
    finally:
        C.now = real_now

def test_parse_prefers_time_utc():
    m = {
        "time_utc": "2026-11-04T22:08:59.049Z",
        "time_local": "Nov 04 04:08 PM EST",
    }
    dt = C.parse_msg_time(m)
    assert dt.year == 2026
    assert dt.month == 11 and dt.day == 4
    print("PASS time_utc preferred", dt)

def test_age_recomputed_not_stale_cache_value():
    msgs = [{
        "index": 1, "role": "user", "text": "hi",
        "time_utc": "2026-11-05T12:00:00Z",
        "time_local": "Nov 05 06:00 AM EST",
        "age_hours": 1_100_000,  # stale absurd cached value
    }]
    ref = datetime.datetime(2026, 11, 6, 12, 0, tzinfo=datetime.timezone.utc)
    C.annotate_messages_staleness(msgs, ref=ref)
    assert msgs[0]["age_hours"] < 100, msgs[0]["age_hours"]
    assert approx(msgs[0]["age_hours"], 24.0, tol=0.1)
    print("PASS age recomputed", msgs[0]["age_hours"])

def test_resolve_environment_from_env_id():
    # No UI label, but env_id known from config -> mapped machine
    b = C.resolve_environment(raw_ui=None, env_id="env_TEST_WORKSTATION_0001")
    assert b["machine"]["resolved"] == "the workstation", b
    b2 = C.resolve_environment(raw_ui=None, env_id="env_TEST_LAPTOP_0001")
    assert b2["machine"]["resolved"] == "the laptop", b2
    # prefix match
    b3 = C.resolve_environment(raw_ui=None, env_id="env_TESTPFX_ABCDEF")
    assert b3["machine"]["resolved"] == "the workstation", b3
    # truly unknown
    b4 = C.resolve_environment(raw_ui=None, env_id=None)
    assert "unknown" in b4["machine"]["resolved"]
    # UI label wins
    b5 = C.resolve_environment(raw_ui="Running on laptop-relay", env_id="env_TEST_WORKSTATION_0001")
    assert b5["machine"]["resolved"] == "the laptop", b5
    # unmapped label is reported raw, never guessed
    b6 = C.resolve_environment(raw_ui="Running on somewhere-else", env_id=None)
    assert "somewhere-else" in b6["machine"]["resolved"], b6
    print("PASS resolve_environment env_id/labels")

def test_send_compare_keeps_blank_lines_and_nfc():
    intended = "line1\n\nline3"
    got = "line1\n\nline3"
    ok, diff = C.send_text_diff(intended, got)
    assert ok and diff is None

    # NFC vs NFD e-acute
    nfc = unicodedata.normalize("NFC", "cafe\u0301")  # if given NFD-ish
    # actually cafe + combining → NFC is café
    nfd = "caf\u00e9"  # already NFC
    nfd2 = "cafe\u0301"
    ok, _ = C.send_text_diff(nfd2, unicodedata.normalize("NFC", nfd2))
    assert ok

    # NBSP vs space should NOT be equal under send compare (norm() would hide this)
    ok, diff = C.send_text_diff("a\u00a0b", "a b")
    assert not ok, "NBSP should not silently match space"
    assert diff and "intended" in diff
    print("PASS send_text_diff blank/NFC/NBSP")

def test_send_compare_crlf_and_trailing():
    ok, _ = C.send_text_diff("a\r\nb\r\n", "a\nb\n")
    assert ok
    ok, _ = C.send_text_diff("a  \nb  ", "a\nb")
    assert ok  # trailing spaces stripped per line
    print("PASS send_text_diff CRLF/trailing")

def test_build_current_state_basic():
    msgs = [{
        "index": 3, "role": "assistant", "text": "done",
        "time_utc": "2026-11-06T01:00:00Z", "time_local": "Nov 05 07:00 PM EST",
        "summary": "done",
    }]
    C.annotate_messages_staleness(msgs)
    cs = C.build_current_state(msgs, busy=False)
    assert cs.get("status") in ("idle", "done", "waiting_on_user", "paused", "working") or "status" in cs
    print("PASS build_current_state", cs.get("status"), "keys", sorted(cs)[:8])

if __name__ == "__main__":
    test_parse_msg_time_year_pin()
    test_parse_msg_time_future_rolls_prev_year()
    test_parse_prefers_time_utc()
    test_age_recomputed_not_stale_cache_value()
    test_resolve_environment_from_env_id()
    test_send_compare_keeps_blank_lines_and_nfc()
    test_send_compare_crlf_and_trailing()
    test_build_current_state_basic()
    print("ALL UNIT TESTS PASSED")

# fragment appended to test_m0_unit.py if missing

def test_wait_composer_match_polls_until_text_lands():
    """Paste can report success before AT-SPI exposes the text; wait until it appears."""
    from claude_projects import core as c
    calls = {'n': 0}
    sequence = ['', '', 'hello from typetest']

    def fake_composer():
        return object()

    def fake_text(_e=None):
        i = min(calls['n'], len(sequence) - 1)
        calls['n'] += 1
        return sequence[i]

    c.composer = fake_composer
    c.composer_text = fake_text
    ok, got = c._wait_composer_match('hello from typetest', timeout_s=2.0, poll_s=0.01)
    assert ok and got == 'hello from typetest'
    assert calls['n'] >= 3


def test_wait_composer_match_times_out_empty():
    from claude_projects import core as c
    c.composer = lambda: object()
    c.composer_text = lambda _e=None: ''
    ok, got = c._wait_composer_match('never lands', timeout_s=0.2, poll_s=0.05)
    assert ok is False and got == ''
