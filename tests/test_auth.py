"""Login links, browser sessions, the bearer token and the request checks."""
from __future__ import annotations

import json
import stat
import threading
from datetime import datetime, timedelta, timezone

import pytest

from transcript_labeling.auth import Auth, AuthError, digest, token_ok
from transcript_labeling.home import Home
from transcript_labeling.security import CSP, host_ok, json_type_ok, origin_ok

T0 = datetime(2026, 10, 22, 5, 0, tzinfo=timezone.utc)


@pytest.fixture
def auth(tmp_path):
    home = Home(tmp_path / "h")
    home.init()
    return Auth(home)


def test_a_code_logs_in_once_and_the_session_works(auth):
    code = auth.mint_code("/p/demo/call-1/", now=T0)
    session, target = auth.redeem(code, now=T0 + timedelta(hours=1))
    assert target == "/p/demo/call-1/"
    assert auth.session_ok(session, now=T0 + timedelta(days=29))
    assert not auth.session_ok(session, now=T0 + timedelta(days=31))
    with pytest.raises(AuthError, match="already used"):
        auth.redeem(code, now=T0 + timedelta(hours=2))
    assert auth.code_target(code) == "/p/demo/call-1/"


def test_an_expired_or_unknown_code_is_refused(auth):
    code = auth.mint_code("/", now=T0)
    with pytest.raises(AuthError, match="expired"):
        auth.redeem(code, now=T0 + timedelta(days=7, seconds=1))
    with pytest.raises(AuthError, match="not known"):
        auth.redeem("nope", now=T0)
    assert not auth.session_ok("nope") and not auth.session_ok(None) and not auth.session_ok("")


def test_the_sessions_file_is_private_and_holds_only_digests(auth):
    code = auth.mint_code("/p/x/y/", now=T0)
    session, _ = auth.redeem(code, now=T0)
    path = auth.home.sessions_path
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    raw = path.read_text()
    assert code not in raw and session not in raw
    assert digest(code) in raw and digest(session) in raw


def test_old_entries_are_pruned(auth):
    auth.mint_code("/", now=T0)
    s, _ = auth.redeem(auth.mint_code("/", now=T0), now=T0)
    auth.mint_code("/", now=T0 + timedelta(days=40))
    data = json.loads(auth.home.sessions_path.read_text())
    assert len(data["codes"]) == 1 and data["sessions"] == {}


@pytest.mark.parametrize("target", ["https://evil.example/", "//evil.example/", "p/x", "/\\evil.example/",
                                    "/\t/evil.example/", "/\n/evil.example/", "/p/a b/", None])
def test_a_login_target_must_be_a_local_path(auth, target):
    with pytest.raises(SystemExit, match="must be a path"):
        auth.mint_code(target)


def test_a_target_may_name_an_item_in_its_fragment(auth):
    code = auth.mint_code("/p/demo/call-1/#call%2Fone", now=T0)
    assert auth.redeem(code, now=T0)[1] == "/p/demo/call-1/#call%2Fone"


def test_one_code_redeemed_at_once_from_many_threads_gives_one_session(auth):
    code = auth.mint_code("/", now=T0)
    barrier, won, lost = threading.Barrier(12), [], []

    def go():
        barrier.wait()
        try:
            won.append(auth.redeem(code, now=T0))
        except AuthError:
            lost.append(1)
    threads = [threading.Thread(target=go) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(won) == 1 and len(lost) == 11
    assert len(json.loads(auth.home.sessions_path.read_text())["sessions"]) == 1


def test_minting_while_a_code_is_redeemed_never_undoes_the_redemption(auth):
    # Without the lock in mint_code, a mint that read the file before the redemption
    # wrote it puts the code back unused and drops the new session.
    for _ in range(10):
        code = auth.mint_code("/", now=T0)
        barrier, out = threading.Barrier(9), []

        def redeem():
            barrier.wait()
            out.append(auth.redeem(code, now=T0)[0])

        def mint():
            barrier.wait()
            auth.mint_code("/", now=T0)
        threads = [threading.Thread(target=redeem)] + [threading.Thread(target=mint) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(out) == 1 and auth.session_ok(out[0], now=T0)
        with pytest.raises(AuthError, match="already used"):
            auth.redeem(code, now=T0)


def test_a_code_works_until_the_last_second_of_its_seven_days(auth):
    code = auth.mint_code("/", now=T0)
    auth.redeem(code, now=T0 + timedelta(days=7) - timedelta(seconds=1))


def test_bad_inputs_are_refused_not_crashes(auth, tmp_path):
    with pytest.raises(ValueError, match="time zone"):
        auth.mint_code("/", now=datetime(2026, 10, 22, 5, 0))
    with pytest.raises(AuthError, match="not known"):
        auth.redeem(12345, now=T0)
    assert auth.code_target(12345) is None and not auth.session_ok(42)
    with pytest.raises(SystemExit, match="run `tl init`"):
        Auth(Home(tmp_path / "missing"))
    with pytest.raises(ValueError, match="too short"):
        token_ok("Bearer ", "")


def test_token_ok():
    tok = "t" * 40
    assert token_ok(f"Bearer {tok}", tok) and token_ok(f"Bearer  {tok} ", tok)
    assert not token_ok(f"Bearer {tok}x", tok) and not token_ok(tok, tok) and not token_ok(None, tok)


def test_host_origin_and_type_checks():
    assert host_ok("127.0.0.1:8790", 8790, []) and host_ok("localhost:8790", 8790, [])
    assert host_ok("[::1]:8790", 8790, []) and host_ok("labels.example:443", 8790, ["Labels.Example:443"])
    for bad in ("evil.example", "127.0.0.1:9999", "127.0.0.1", None, ""):
        assert not host_ok(bad, 8790, [])
    assert origin_ok(None, "127.0.0.1:8790") and origin_ok("http://127.0.0.1:8790", "127.0.0.1:8790")
    assert not origin_ok("http://evil.example", "127.0.0.1:8790") and not origin_ok("null", "127.0.0.1:8790")
    assert json_type_ok("application/json; charset=utf-8") and not json_type_ok("text/plain")
    assert not json_type_ok(None)
    assert CSP == "default-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'"
    assert origin_ok("https://labels.example", "labels.example", ["labels.example"])
    assert not origin_ok("http://labels.example", "labels.example", ["labels.example"])
