"""The `tl` command end to end, against an in-process service on a free port.

  FLOW       init -> new (with a login link) -> url -> status -> export -> verify -> wait
  WRITERS    a write names the calling repository; none is refused with a hint
  EXPORT     local-only exports default to $TL_HOME/exports; text into a git work tree
             is refused unless --no-text; --out - is stdout
  WAIT       exits 3 on timeout (2 is argparse's usage error), 0 once the owner pressed Finish
  PROJECT    tl project create makes a git-private project; it refuses a public remote
  DOCTOR     reports the service's version; fails when nothing answers
  DOWN       a command against a stopped service says so and stops
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest

from transcript_labeling import cli, gitproject
from transcript_labeling.home import Home
from transcript_labeling.service import Service, build_service_server

ZOOM = Path(__file__).resolve().parent / "fixtures" / "zoom" / "notes.txt"
SECRET_WORD = "loud and clear"  # in the fixture text
SPEAKERS = ("--speakers", "ann=Ann Able:1,bo=Bo Brand:2")


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def tl(tmp_path, monkeypatch):
    """`tl ARGS...` in-process against a running service; returns (exit code, stdout, stderr)."""
    home = Home(tmp_path / "home")
    monkeypatch.setenv("TL_HOME", str(home.root))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init"]) == 0
    svc = Service(home)
    httpd = build_service_server(svc, 0)
    cfg = json.loads(home.config_path.read_text())
    cfg["port"] = httpd.server_address[1]
    home.config_path.write_text(json.dumps(cfg))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def run(*argv, capsys=None):
        try:
            code = cli.main(list(argv))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else str(exc.code)
        return code
    run.home, run.svc, run.httpd = home, svc, httpd
    try:
        yield run
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_whole_flow(tl, capsys, tmp_path):
    capsys.readouterr()
    assert tl("new", "demo/call-1", "--from-file", str(ZOOM), "--create-project", "--repo", "me/repo",
              "--speakers", "ann=Ann Able:1,bo=Bo Brand:2", "--suggest-map", "Ann Able=ann,Bo Brand=bo",
              "--group", "call") == 0
    out = capsys.readouterr().out
    assert "http://127.0.0.1:" in out and "/login#code=" in out and SECRET_WORD not in out
    assert tl("url", "demo/call-1", "--item", "call/notes") == 0
    assert "/login#code=" in capsys.readouterr().out
    assert tl("status", "demo/call-1") == 0
    assert "demo/call-1: 1 items, 0 with ranges, 0 done" in capsys.readouterr().out
    tl.svc.committer = StubCommitter({"commits": "on", "pending_saves": 2, "last_error": "index.lock is held",
                                      "last_commit": None})
    assert tl("status", "demo/call-1") == 0
    assert "commits: on, 2 saves not yet committed; last git error: index.lock is held" in capsys.readouterr().out
    tl.svc.committer = None
    assert tl("status", "demo/call-1", "--json") == 0
    st = json.loads(capsys.readouterr().out)
    assert st["items"] == 1 and st["finished"] is None
    assert tl("export", "demo/call-1", "segments") == 0
    default = tl.home.exports_dir("demo", "call-1") / "segments.json"
    assert default.exists() and f"wrote {default}" in capsys.readouterr().err
    assert tl("verify", "demo/call-1") == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert tl("schema", "item") == 0
    assert json.loads(capsys.readouterr().out)["name"] == "item"
    assert tl("dataset", "list") == 0
    assert json.loads(capsys.readouterr().out) == ["demo/call-1"]


def test_a_write_names_the_calling_repository(tl, capsys, tmp_path):
    code = tl("new", "demo/call-1", "--from-file", str(ZOOM), "--create-project", *SPEAKERS)
    assert "must name the calling repository" in str(code)
    repo = tmp_path / "code"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "remote", "add", "origin", "git@github.com:me/code.git")
    os.chdir(repo)
    assert tl("new", "demo/call-1", "--from-file", str(ZOOM), "--create-project", *SPEAKERS) == 0
    assert tl.svc.registry.project("demo")["writers"] == ["me/code"]


def test_new_refuses_an_unknown_project_without_create_project(tl):
    code = tl("new", "demo/call-1", "--from-file", str(ZOOM), "--repo", "me/repo", *SPEAKERS)
    assert "REFUSING (404)" in str(code) and "--create-project" in str(code)


def test_new_without_speakers_says_which_flag(tl):
    code = tl("new", "demo/call-1", "--from-file", str(ZOOM), "--repo", "me/repo", "--create-project")
    assert "needs --speakers" in str(code)
    assert tl("new", "demo/call-2", "--from-file", str(ZOOM), "--repo", "me/repo", "--create-project",
              "--modes", "captions") == 0


def test_exports_refuse_text_inside_a_work_tree(tl, capsys, tmp_path):
    assert tl("new", "demo/call-1", "--from-file", str(ZOOM), "--create-project", "--repo", "me/repo", *SPEAKERS) == 0
    repo = tmp_path / "checkout"
    repo.mkdir()
    git(repo, "init", "-q")
    code = tl("export", "demo/call-1", "spans", "--out", str(repo / "spans.json"))
    assert "inside the git work tree" in str(code) and not (repo / "spans.json").exists()
    assert tl("export", "demo/call-1", "spans", "--out", str(repo / "spans.json"), "--no-text") == 0
    assert SECRET_WORD not in (repo / "spans.json").read_text()
    capsys.readouterr()
    assert tl("export", "demo/call-1", "segments", "--out", "-") == 0
    assert json.loads(capsys.readouterr().out)["kind"] == "segments"
    assert not (tl.home.exports_dir("demo", "call-1") / "segments.json").exists()


def test_wait_times_out_then_returns_once_finished(tl, capsys, monkeypatch):
    monkeypatch.setattr(cli, "WAIT_POLL_S", 0.05)
    assert tl("new", "demo/call-1", "--from-file", str(ZOOM), "--create-project", "--repo", "me/repo", *SPEAKERS) == 0
    start = time.monotonic()
    assert tl("wait", "demo/call-1", "--until", "done", "--timeout", "0.2") == cli.WAIT_TIMEOUT_EXIT == 3
    assert time.monotonic() - start >= 0.2
    assert "not finished after 0.2 s" in capsys.readouterr().err
    store = tl.svc.review_store("demo", "call-1", tl.svc.dataset("demo", "call-1"))

    def finish_soon():
        time.sleep(0.3)
        store.update(lambda r: {**r, "finished": {"at": "2026-10-22T06:00:00+00:00", "checked_by": "operator"}},
                     store.load_versioned()[1])
    threading.Thread(target=finish_soon).start()
    start = time.monotonic()
    assert tl("wait", "demo/call-1", "--until", "done", "--timeout", "5") == 0  # Finish lands mid-wait
    assert 0.3 <= time.monotonic() - start < 5
    assert "finished at 2026-10-22T06:00:00+00:00" in capsys.readouterr().out
    assert "must be a number of seconds" in str(tl("wait", "demo/call-1", "--until", "done", "--timeout", "nan"))
    assert tl("wait", "demo/call-1", "--until", "finished", "--timeout", "1") == 2  # argparse's usage error


def test_project_create_makes_a_git_private_project_and_refuses_a_public_remote(tl, capsys, tmp_path,
                                                                                monkeypatch):
    repo = tmp_path / "data"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "remote", "add", "origin", "git@github.com:me/labels-data.git")
    monkeypatch.setattr(gitproject, "gh_visibility", lambda url: "PRIVATE" if "labels-data" in url else "PUBLIC")
    assert tl("project", "create", "priv", "--git-checkout", str(repo), "--repo", "me/code") == 0
    entry = json.loads(capsys.readouterr().out)
    assert entry["privacy"] == "git-private" and entry["writers"] == ["me/code", "me/labels-data"]
    git(repo, "remote", "add", "pub", "https://github.com/me/code")
    code = tl("project", "create", "priv2", "--git-checkout", str(repo), "--repo", "me/code")
    assert "must be a private repository" in str(code)


class StubCommitter:
    def __init__(self, state):
        self.state = state

    def status(self, project, dataset):
        return self.state

    def note_save(self, *a, **k):
        pass


def test_doctor_reports_a_git_error_in_a_committing_project(tl, capsys, tmp_path, monkeypatch):
    repo = tmp_path / "data"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "remote", "add", "origin", "git@github.com:me/labels-data.git")
    git(repo, "config", "tl.syncLock", "1")
    monkeypatch.setattr(gitproject, "gh_visibility", lambda url: "PRIVATE")
    assert tl("project", "create", "priv", "--git-checkout", str(repo), "--repo", "me/code") == 0
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"speakers": [{"id": "ann", "label": "Ann Able", "key": "1"}]}))
    assert tl("dataset", "create", "priv/d1", "--config", str(cfg), "--path", "labels/d1", "--repo", "me/code") == 0
    tl.svc.committer = StubCommitter({"commits": "on", "pending_saves": 3, "last_error": None, "last_commit": None})
    capsys.readouterr()
    assert tl("doctor") == 0
    assert "priv/d1: 3 saves waiting for the next commit" in capsys.readouterr().out
    tl.svc.committer = StubCommitter({"commits": "on", "pending_saves": 3, "last_error": "a merge is in progress",
                                      "last_commit": None})
    assert tl("doctor") == 1
    assert "FAIL priv/d1: 3 saves not committed; last git error: a merge is in progress" in capsys.readouterr().out


def test_doctor_reports_the_service_and_fails_when_nothing_answers(tl, capsys):
    assert tl("doctor") == 0
    assert "ok   service" in capsys.readouterr().out
    tl.httpd.shutdown()
    tl.httpd.server_close()
    assert tl("doctor") == 1
    assert "nothing answers" in capsys.readouterr().out


def test_a_stopped_service_is_reported(tl):
    tl.httpd.shutdown()
    tl.httpd.server_close()
    code = tl("status", "demo/call-1")
    assert "is not answering" in str(code) and "tl doctor" in str(code)


@pytest.mark.parametrize("spec, expect", [
    ("ann=Ann Able:1,bo=Bo: Brand:2", [{"id": "ann", "label": "Ann Able", "key": "1"},
                                      {"id": "bo", "label": "Bo: Brand", "key": "2"}]),
    ("", []),
])
def test_parse_speakers(spec, expect):
    assert cli.parse_speakers(spec) == expect


@pytest.mark.parametrize("bad", ["ann", "ann=Ann Able", "=x"])
def test_parse_speakers_refuses(bad):
    with pytest.raises(SystemExit, match="id=Label:key"):
        cli.parse_speakers(bad)


def test_item_names_come_from_the_file_name(tmp_path):
    assert cli.item_name(Path("My Call (2).txt"), None) == "my-call-2"
    assert cli.item_name(Path("x.txt"), "given") == "given"
    with pytest.raises(SystemExit, match="pass --name"):
        cli.item_name(Path("___.txt"), None)


def test_doctor_fails_when_the_service_refuses_the_token_or_runs_another_version(tl, capsys, monkeypatch):
    tl.home.token_path.write_text("x" * 43 + "\n")  # well formed, but not the token the service holds
    assert tl("doctor") == 1
    assert "answers /healthz but refuses an agent call" in capsys.readouterr().out
    monkeypatch.setattr(cli, "__version__", "9.9.9")
    assert tl("doctor") == 1
    assert "the service runs 0.1.0, but 9.9.9 is installed" in capsys.readouterr().out


def test_doctor_reports_a_corrupt_config_and_keeps_going_past_a_broken_dataset(tl, capsys, tmp_path, monkeypatch):
    repo = tmp_path / "data"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "remote", "add", "origin", "git@github.com:me/labels-data.git")
    git(repo, "config", "tl.syncLock", "1")
    monkeypatch.setattr(gitproject, "gh_visibility", lambda url: "PRIVATE")
    assert tl("project", "create", "priv", "--git-checkout", str(repo), "--repo", "me/code") == 0
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"speakers": [{"id": "ann", "label": "Ann Able", "key": "1"}]}))
    for d in ("a1", "b2"):
        assert tl("dataset", "create", f"priv/{d}", "--config", str(cfg), "--path", f"labels/{d}",
                  "--repo", "me/code") == 0
    (repo / "labels" / "a1" / "manifest.json").unlink()
    tl.svc.committer = StubCommitter({"commits": "on", "pending_saves": 1, "last_error": None, "last_commit": None})
    capsys.readouterr()
    assert tl("doctor") == 1
    out = capsys.readouterr().out
    assert "FAIL priv/a1: REFUSING (409)" in out and "priv/b2: 1 saves waiting" in out
    tl.home.config_path.write_text("{not json")
    assert tl("doctor") == 1
    assert "config.json is not valid JSON" in capsys.readouterr().out


def test_items_add_needs_upsert_to_replace(tl, capsys, tmp_path):
    assert tl("new", "demo/call-1", "--from-file", str(ZOOM), "--create-project", "--repo", "me/repo", *SPEAKERS) == 0
    other = tmp_path / "second.txt"
    other.write_text("A second talk, in plain text.\n")
    assert tl("items", "add", "demo/call-1", "--from-file", str(other), "--repo", "me/repo") == 0
    assert set(tl.svc.dataset("demo", "call-1").manifest["items"]) == {"item/notes", "item/second"}
    code = tl("items", "add", "demo/call-1", "--from-file", str(other), "--repo", "me/repo")
    assert "pass --upsert" in str(code)
    other.write_text("A second talk, now longer, in plain text.\n")
    assert tl("items", "add", "demo/call-1", "--from-file", str(other), "--repo", "me/repo", "--upsert") == 0
    assert tl.svc.dataset("demo", "call-1").item("item/second")["token_count"] == 8


def test_exports_are_private_files_and_a_folder_is_refused(tl, capsys, tmp_path):
    assert tl("new", "demo/call-1", "--from-file", str(ZOOM), "--create-project", "--repo", "me/repo", *SPEAKERS) == 0
    out = tmp_path / "out.json"
    out.write_text("old")
    out.chmod(0o644)
    assert tl("export", "demo/call-1", "segments", "--out", str(out)) == 0
    assert stat.S_IMODE(out.stat().st_mode) == 0o600 and SECRET_WORD in out.read_text()
    assert tl("export", "demo/call-1", "segments") == 0
    default = tl.home.exports_dir("demo", "call-1") / "segments.json"
    assert stat.S_IMODE(default.stat().st_mode) == 0o600
    assert "is a folder" in str(tl("export", "demo/call-1", "segments", "--out", str(tmp_path)))
    assert "--out is empty" in str(tl("export", "demo/call-1", "segments", "--out", ""))


def test_project_create_outside_a_checkout_needs_repo(tl, tmp_path, monkeypatch):
    repo = tmp_path / "data"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "remote", "add", "origin", "git@github.com:me/labels-data.git")
    monkeypatch.setattr(gitproject, "gh_visibility", lambda url: "PRIVATE")
    assert "must name the calling repository" in str(tl("project", "create", "priv", "--git-checkout", str(repo)))
    assert "priv" not in tl.svc.registry.load()["projects"]


def test_a_broken_reply_stops_with_a_message(tl, monkeypatch):
    import http.server

    class Bad(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/api/v1/status"):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"<html>not json</html>")
            else:
                self.close_connection = True  # drop the call without any reply

        def log_message(self, *a):
            pass
    bad = http.server.HTTPServer(("127.0.0.1", 0), Bad)
    threading.Thread(target=bad.serve_forever, daemon=True).start()
    try:
        cfg = json.loads(tl.home.config_path.read_text())
        cfg["port"] = bad.server_address[1]
        tl.home.config_path.write_text(json.dumps(cfg))
        assert "sent a reply that is not JSON" in str(tl("status", "demo/call-1"))
        assert "broke off the call" in str(tl("verify", "demo/call-1"))
    finally:
        bad.shutdown()
        bad.server_close()


def test_a_service_refusal_is_not_prefixed_twice(tl):
    assert tl("new", "demo/call-1", "--from-file", str(ZOOM), "--create-project", "--repo", "me/repo", *SPEAKERS) == 0
    code = tl("items", "add", "demo/call-1", "--from-file", str(ZOOM), "--repo", "me/repo")
    assert str(code).startswith("REFUSING (409): nothing was written") and "REFUSING: " not in str(code)


def test_the_client_refuses_a_request_json_cannot_hold(tmp_path):
    from transcript_labeling.client import ServiceClient
    home = Home(tmp_path / "h")
    home.init(port=9)  # nothing listens: the refusal must come before any connection
    with pytest.raises(SystemExit, match="the request holds NaN or Infinity"):
        ServiceClient(home).post("new", {"title": float("nan")})


def test_doctor_reports_a_home_inside_a_git_work_tree(tl, capsys, tmp_path):
    (tmp_path / ".git").mkdir()  # a `git init` in the folder above the home, after `tl init`
    assert tl("doctor") == 1
    assert f"{tl.home.root} is inside the git work tree {tmp_path}" in capsys.readouterr().out


def test_a_config_file_with_a_key_written_twice_is_refused(tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"title": "first", "title": "second"}')
    with pytest.raises(SystemExit, match="a key is written twice"):
        cli.read_json_file(str(cfg))
