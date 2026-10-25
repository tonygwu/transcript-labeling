"""The labeling service over a real socket: empty shells, sessions, browser-only writes, the agent API.

  SHELLS     /, /login and /p/<project>/<dataset>/ answer without a session and hold no project
             name, catalog entry or item text
  SESSION    every page api route without a session is 401
  HOST       a spoofed Host is 421
  ORIGIN     a foreign Origin and a no-cors text/plain POST are refused
  TOKEN      the agent API without the bearer token is 401
  BROWSER    answer, note, reveal and done writes with only the bearer token are refused
  LOGIN      a link logs in once; a reused or expired one fails, and a stored session still works
  FLOW       new -> url -> login -> page -> transcript (suggestions hidden) -> answer -> reveal ->
             done -> finish, with an event per write that holds no text
  MCP        a client marked mcp cannot create or write a git-private project
  COMMITS    a git-private dataset's labels are committed by the server's committer after Finish;
             a failing tick is logged, never raised; status reports commit state
  HEADERS    every security header is on every response
  STALE      a write carries the versions its page loaded; after an outside change it is a 409
             with "stale" for every page that loaded before it, even after another page read the file
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
import threading

import pytest

import subprocess

from transcript_labeling.gitcommit import MARKER
from transcript_labeling.home import Home
from transcript_labeling.security import SECURITY_HEADERS
from transcript_labeling.service import Service, build_service_server, commit_tick, start_committer

ZOOM = (Path(__file__).resolve().parent / "fixtures" / "zoom" / "notes.txt").read_text()
SECRET_WORD = "loud and clear"  # in the fixture text; must never appear in a shell page
SPEAKERS = [{"id": "ann", "label": "Ann Able", "key": "1"}, {"id": "bo", "label": "Bo Brand", "key": "2"}]


class Client:
    def __init__(self, base: str, token: str):
        self.base, self.token, self.session = base, token, None

    def req(self, method, path, body=None, *, bearer=False, session=None, headers=None, raw=None):
        h = {"content-type": "application/json"} if body is not None or raw is not None else {}
        if bearer:
            h["Authorization"] = f"Bearer {self.token}"
        s = self.session if session is None else session
        if s:
            h["X-TL-Session"] = s
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def json(self, method, path, body=None, **kw):
        code, raw, _ = self.req(method, path, body, **kw)
        return code, json.loads(raw) if raw else None


@contextmanager
def running(tmp_path):
    home = Home(tmp_path / "home")
    home.init()
    saves = []
    svc = Service(home, on_save=lambda p, d, f: saves.append((p, d, f)))
    httpd = build_service_server(svc, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        c = Client(f"http://127.0.0.1:{httpd.server_address[1]}", home.token())
        c.saves = saves
        c.svc = svc
        yield c
    finally:
        httpd.shutdown()
        httpd.server_close()


def new_dataset(c, **extra):
    body = {"project": "demo", "dataset": "call-1", "repo": "me/repo", "create_project": True,
            "speakers": SPEAKERS, "items": [{"name": "one", "text": ZOOM, "group": "call",
                                             "suggest_map": "Ann Able=ann,Bo Brand=bo",
                                             "focus": [{"start": 20, "end": 24, "title": "Budget"}]}], **extra}
    return c.json("POST", "/api/v1/new", body, bearer=True)


def login(c, url):
    code = url.split("#code=")[1]
    status, body = c.json("POST", "/api/login", {"code": code})
    assert status == 200, body
    c.session = body["session"]
    return body["target"]


def versions(c):
    status, answers = c.json("GET", "/p/demo/call-1/api/answers")
    assert status == 200, answers
    return answers["versions"]


@pytest.fixture
def svc_client(tmp_path):
    with running(tmp_path) as c:
        status, body = new_dataset(c)
        assert status == 200, body
        c.url = body["url"]
        yield c


def test_shells_answer_without_a_session_and_hold_no_names_or_text(svc_client):
    c = svc_client
    for path in ("/", "/login", "/p/demo/call-1/"):
        status, raw, headers = c.req("GET", path, session="")
        page = raw.decode()
        assert status == 200, path
        for leak in ("demo", "call-1", "Ann Able", SECRET_WORD, "Budget"):
            assert leak not in page, (path, leak)
        assert headers["Content-Security-Policy"].startswith("default-src 'self'; script-src 'self'")
        assert "Access-Control-Allow-Origin" not in headers
    status, raw, _ = c.req("GET", "/static/app.js", session="")
    assert status == 200 and b"tlLoadPage" in raw and SECRET_WORD.encode() not in raw
    assert c.req("GET", "/static/../service.py", session="")[0] == 404


@pytest.mark.parametrize("path", ["/api/projects", "/api/whoami", "/p/demo/call-1/api/page",
                                  "/p/demo/call-1/api/answers", "/p/demo/call-1/api/transcript?key=call/one"])
def test_every_page_api_route_needs_a_session(svc_client, path):
    assert svc_client.req("GET", path, session="")[0] == 401
    assert svc_client.req("GET", path, session="forged")[0] == 401


def test_a_spoofed_host_is_421(svc_client):
    for path in ("/", "/healthz", "/api/v1/projects"):
        assert svc_client.req("GET", path, headers={"Host": "evil.example"}, bearer=True)[0] == 421


def test_a_foreign_origin_or_a_text_plain_post_is_refused(svc_client):
    c = svc_client
    login(c, c.url)
    answer = {"kind": "spans", "id": "call/one", "value": {}}
    status, _, _ = c.req("POST", "/p/demo/call-1/api/answer", answer, headers={"Origin": "http://evil.example"})
    assert status == 403
    status, _, _ = c.req("POST", "/p/demo/call-1/api/answer", raw=json.dumps(answer).encode(),
                         headers={"content-type": "text/plain"})
    assert status == 415


def test_the_agent_api_needs_the_token(svc_client):
    assert svc_client.req("GET", "/api/v1/projects")[0] == 401
    assert svc_client.req("POST", "/api/v1/url", {"project": "demo", "dataset": "call-1"})[0] == 401
    assert svc_client.json("GET", "/api/v1/projects", bearer=True)[0] == 200


@pytest.mark.parametrize("route,body", [
    ("answer", {"kind": "labels", "id": "call/one", "value": {}}),
    ("note", {"id": "call/one", "text": "x"}),
    ("reveal", {"key": "call/one"}),
    ("done", {"finish": True}),
])
def test_browser_writes_refuse_the_bearer_token(svc_client, route, body):
    status, raw, _ = svc_client.req("POST", f"/p/demo/call-1/api/{route}", body, bearer=True, session="")
    assert status == 401 and b"browser session" in raw


def test_a_link_logs_in_once_and_a_session_keeps_working(svc_client):
    c = svc_client
    target = login(c, c.url)
    assert target == "/p/demo/call-1/"
    session = c.session
    code = c.url.split("#code=")[1]
    status, body = c.json("POST", "/api/login", {"code": code}, session="")
    assert status == 409 and "already used" in body["error"] and body["target"] == "/p/demo/call-1/"
    assert c.json("GET", "/api/whoami", session=session)[0] == 200
    assert c.json("POST", "/api/login", {"code": "nope"}, session="")[0] == 409


def test_the_whole_flow(svc_client):
    c = svc_client
    login(c, c.url)
    status, data = c.json("GET", "/p/demo/call-1/api/page")
    assert status == 200 and data["session"]["speakers"] == SPEAKERS
    assert data["transcript_index"]["call/one"]["token_count"] > 50
    assert data["quote_rows"][0]["quotes"][0]["title"] == "Budget"
    status, item = c.json("GET", "/p/demo/call-1/api/transcript?key=call/one")
    assert status == 200 and item["suggestions"] == [] and item["suggestions_hidden"] == 5
    assert "text" not in item and item["token_kinds"][0] == "meta"
    body0 = item["segments"][0]["body"]
    value = {"key": "call/one", "text_sha256": item["text_sha256"], "token_count": item["token_count"],
             "checked_by": "someone-else", "ranges": [{"start": body0[0], "end": body0[1], "speaker": "ann",
                                                        "origin": "human"}]}
    v = versions(c)
    status, res = c.json("POST", "/p/demo/call-1/api/answer",
                         {"kind": "spans", "id": "call/one", "value": value, "versions": v})
    assert status == 200, res
    _, answers = c.json("GET", "/p/demo/call-1/api/answers")
    assert answers["spans"]["call/one"]["checked_by"] == "operator"  # the server says who checked
    assert answers["versions"] == v  # the page's own save leaves its versions valid
    bad = {**value, "ranges": [{**value["ranges"][0], "speaker": "subject"}]}
    assert c.json("POST", "/p/demo/call-1/api/answer",
                  {"kind": "spans", "id": "call/one", "value": bad, "versions": v})[0] == 400
    status, rev = c.json("POST", "/p/demo/call-1/api/reveal", {"key": "call/one", "versions": v})
    assert status == 200 and len(rev["suggestions"]) == 5
    assert rev["reveal"]["ranges_before_reveal"] == value["ranges"]
    _, item2 = c.json("GET", "/p/demo/call-1/api/transcript?key=call/one")
    assert len(item2["suggestions"]) == 5 and item2["suggestions_hidden"] == 0
    assert c.json("POST", "/p/demo/call-1/api/note", {"id": "call:one:20", "text": "check this", "versions": v})[0] == 200
    assert c.json("POST", "/p/demo/call-1/api/note", {"id": "nowhere", "text": "x", "versions": v})[0] == 400
    assert c.json("POST", "/p/demo/call-1/api/done", {"key": "call/one", "done": True, "versions": v})[0] == 200
    assert c.json("POST", "/p/demo/call-1/api/done", {"finish": True, "versions": v})[0] == 200
    status, st = c.json("GET", "/api/v1/status?project=demo&dataset=call-1", bearer=True)
    assert st["done"] == ["call/one"] and st["finished"] and st["revealed"] == ["call/one"] and st["notes"] == 1
    events = c.svc.events("demo", "call-1").read()
    assert [e["kind"] for e in events] == ["answer", "reveal", "note", "done", "finish"]
    assert all(e["login"] == "browser" and len(e["sha256_after"]) == 64 for e in events)
    assert SECRET_WORD not in json.dumps(events) and "check this" not in json.dumps(events)
    assert c.saves[-1] == ("demo", "call-1", True)
    status, projects = c.json("GET", "/api/projects")
    assert projects["projects"][0]["datasets"][0]["finished"] is True


def test_new_refuses_an_unknown_project_without_create_and_a_bad_dataset_leaves_nothing(tmp_path):
    with running(tmp_path) as c:
        status, body = new_dataset(c, create_project=False)
        assert status == 404 and "--create-project" in body["error"]
        status, body = new_dataset(c, speakers=[{"id": "Bad", "label": "x", "key": "1"}])
        assert status == 409 and "must be a string matching" in body["error"]
        assert "demo" not in c.svc.registry.load()["projects"]  # the project this call made is gone too
        assert not (c.svc.home.data_dir("demo") / "call-1").exists()
        # A typo in the repo plus a bad item: the corrected retry is not refused as "not a writer".
        bad_item = [{"name": "one", "text": ZOOM, "row": {"words": "many"}}]
        assert new_dataset(c, repo="me/rpeo", items=bad_item)[0] == 409
        assert new_dataset(c)[0] == 200
        assert c.svc.registry.project("demo")["writers"] == ["me/repo"]
        # A failure inside an existing project leaves the project alone.
        assert new_dataset(c, dataset="call-2", items=bad_item)[0] == 409
        assert sorted(c.svc.registry.project("demo")["datasets"]) == ["call-1"]


def test_writers_and_mcp_guards(svc_client, tmp_path):
    c = svc_client
    items = {"project": "demo", "dataset": "call-1", "repo": "other/repo", "upsert": True,
             "items": [{"name": "two", "text": "a b c"}]}
    status, body = c.json("POST", "/api/v1/items", items, bearer=True)
    assert status == 409 and "not a writer" in body["error"]
    assert c.json("POST", "/api/v1/items", {**items, "owner_override": True}, bearer=True)[0] == 200
    git = {"checkout": str(tmp_path / "co"), "branch": "main", "commit": False}
    c.svc.registry.create_project("priv", privacy="git-private", writers=["me/repo"], git=git)
    ds = {"project": "priv", "dataset": "d", "config": {"speakers": SPEAKERS}, "repo": "me/repo", "path": "labels/d"}
    status, body = c.json("POST", "/api/v1/datasets", ds, bearer=True, headers={"X-TL-Client": "mcp"})
    assert status == 403 and "local-only" in body["error"]
    status, body = c.json("POST", "/api/v1/projects", {"name": "p2", "writers": [], "repo": "me/repo",
                                                        "privacy": "git-private"}, bearer=True)
    assert status == 403 and "CLI only" in body["error"]
    assert c.json("POST", "/api/v1/datasets", ds, bearer=True)[0] == 200  # the CLI may


def test_a_save_after_an_outside_change_is_refused_for_pages_that_loaded_before_it(svc_client):
    c = svc_client
    login(c, c.url)
    _, item = c.json("GET", "/p/demo/call-1/api/transcript?key=call/one")
    value = {"key": "call/one", "text_sha256": item["text_sha256"], "token_count": item["token_count"],
             "ranges": [{"start": 6, "end": 8, "speaker": "bo", "origin": "human"}]}
    answer = {"kind": "spans", "id": "call/one", "value": value}
    old = versions(c)
    assert c.json("POST", "/p/demo/call-1/api/answer", {**answer, "versions": old})[0] == 200
    path = c.svc.registry.dataset_dir("demo", "call-1") / "human_answers.json"
    path.write_text(json.dumps({"labels": {}, "attribution": {}}))  # a revert, outside the service
    new = versions(c)  # another tab loads: it sees the revert
    assert new["answers"] != old["answers"] and new["review"] == old["review"]
    status, body = c.json("POST", "/p/demo/call-1/api/answer", {**answer, "versions": old})
    assert status == 409 and body["stale"] is True and "reload" in body["error"]
    assert json.loads(path.read_text()) == {"labels": {}, "attribution": {}}  # nothing written over the revert
    assert c.json("POST", "/p/demo/call-1/api/answer", {**answer, "versions": new})[0] == 200
    # review.json was not touched, so the old page may still write notes.
    assert c.json("POST", "/p/demo/call-1/api/note", {"id": "call/one", "text": "x", "versions": old})[0] == 200


@pytest.mark.parametrize("versions_value", [None, {}, {"answers": 3}, "p.0"])
def test_a_write_without_the_page_versions_is_refused(svc_client, versions_value):
    c = svc_client
    login(c, c.url)
    status, body = c.json("POST", "/p/demo/call-1/api/answer",
                          {"kind": "labels", "id": "call/one", "value": {}, "versions": versions_value})
    assert status == 400 and "versions.answers" in body["error"]


def test_healthz_needs_no_session_and_names_the_version(svc_client):
    status, body = svc_client.json("GET", "/healthz", session="")
    assert status == 200 and body["ok"] is True and body["version"]


def test_every_security_header_is_on_every_response(svc_client):
    c = svc_client
    for method, path, kw in (("GET", "/", {"session": ""}), ("GET", "/healthz", {"session": ""}),
                             ("GET", "/api/whoami", {"session": ""}), ("GET", "/api/v1/projects", {"bearer": True}),
                             ("GET", "/static/app.js", {"session": ""}), ("GET", "/nowhere", {"session": ""})):
        _, _, headers = c.req(method, path, **kw)
        for name, value in SECURITY_HEADERS.items():
            assert headers.get(name) == value, (path, name)


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def test_a_git_private_dataset_is_committed_by_the_service_after_finish(tmp_path):
    repo = tmp_path / "data"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.invalid")
    git(repo, "config", "user.name", "t")
    (repo / "README").write_text("data\n")
    git(repo, "add", "README")
    git(repo, "commit", "-qm", "init")
    git(repo, "config", MARKER, "1")
    with running(tmp_path) as c:
        c.svc.registry.create_project("priv", privacy="git-private", writers=["me/repo"],
                                      git={"checkout": str(repo), "branch": "main", "commit": True})
        committer = start_committer(c.svc, interval_s=3600)  # ticked by hand below
        ds = {"project": "priv", "dataset": "d", "config": {"speakers": SPEAKERS}, "repo": "me/repo",
              "path": "labels/d"}
        assert c.json("POST", "/api/v1/datasets", ds, bearer=True)[0] == 200
        items = {"project": "priv", "dataset": "d", "repo": "me/repo", "items": [{"name": "one", "text": ZOOM,
                                                                                 "group": "call"}]}
        assert c.json("POST", "/api/v1/items", items, bearer=True)[0] == 200
        _, link = c.json("POST", "/api/v1/url", {"project": "priv", "dataset": "d"}, bearer=True)
        login(c, link["url"])
        v = c.json("GET", "/p/priv/d/api/answers")[1]["versions"]
        assert c.json("POST", "/p/priv/d/api/done", {"key": "call/one", "done": True, "versions": v})[0] == 200
        assert commit_tick(c.svc, committer) == []  # not due: 300 s idle has not passed
        assert c.json("POST", "/p/priv/d/api/done", {"finish": True, "versions": v})[0] == 200
        res = commit_tick(c.svc, committer)
        assert res and res[0]["committed"], res
        assert git(repo, "log", "-1", "--format=%s").startswith("Save human labels: priv/d (2 saves, tl ")
        assert git(repo, "show", "--name-only", "--format=", "HEAD").split() == ["labels/d/review.json"]
        _, st = c.json("GET", "/api/v1/status?project=priv&dataset=d", bearer=True)
        assert st["commits"]["commits"] == "on" and st["commits"]["last_commit"] == res[0]["sha"]


def test_a_restart_notes_labels_the_last_server_had_not_committed(tmp_path):
    repo = tmp_path / "data"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.invalid")
    git(repo, "config", "user.name", "t")
    (repo / "README").write_text("data\n")
    git(repo, "add", "README")
    git(repo, "commit", "-qm", "init")
    git(repo, "config", MARKER, "1")
    with running(tmp_path) as c:
        c.svc.registry.create_project("priv", privacy="git-private", writers=["me/repo"],
                                      git={"checkout": str(repo), "branch": "main", "commit": True})
        for name in ("saved", "clean"):
            ds = {"project": "priv", "dataset": name, "config": {"speakers": SPEAKERS}, "repo": "me/repo",
                  "path": f"labels/{name}"}
            assert c.json("POST", "/api/v1/datasets", ds, bearer=True)[0] == 200
        (repo / "labels" / "saved" / "review.json").write_text("{}")  # saved, never committed
        (repo / "labels" / "clean" / "review.json").write_text("{}")
        git(repo, "add", "labels/clean/review.json")
        git(repo, "commit", "-qm", "an earlier label commit")
        committer = start_committer(c.svc, interval_s=3600)  # as a freshly started server would
        assert committer.status("priv", "saved")["pending_saves"] == 1
        assert committer.status("priv", "clean")["pending_saves"] == 0


def test_a_failing_commit_tick_is_logged_and_never_raised(svc_client, capsys):
    class Exploding:
        def tick(self, files_of):
            raise SystemExit("REFUSING: the registry is unreadable")
    assert commit_tick(svc_client.svc, Exploding()) == [{"error": "SystemExit: REFUSING: the registry is unreadable"}]
    assert "commit tick failed: SystemExit" in capsys.readouterr().err


def test_status_says_when_no_committer_runs(svc_client):
    _, st = svc_client.json("GET", "/api/v1/status?project=demo&dataset=call-1", bearer=True)
    assert st["commits"] == {"commits": "the committer is not running in this server"}


@pytest.mark.parametrize("bad_item", [
    {"name": "one", "text": "a b c", "focus": ["not an object"]},
    {"name": "one", "text": "a b c", "row": ["a list"]},
    {"name": "one", "text": "a b c", "source": "a string"},
    {"name": "one", "text": "a b c", "focus": [{"start": 0, "end": 1, "id": "free text with spaces"}]},
])
def test_a_badly_typed_new_leaves_nothing_and_its_retry_works(tmp_path, bad_item):
    with running(tmp_path) as c:
        status, body = new_dataset(c, items=[bad_item])
        assert status in (400, 409), body
        assert "demo" not in c.svc.registry.load()["projects"] or \
            c.svc.registry.load()["projects"]["demo"]["datasets"] == {}
        assert new_dataset(c)[0] == 200


@pytest.mark.parametrize("route, body", [
    ("answer", {"kind": "labels", "id": ["x"], "value": {}}),
    ("note", {"id": {"x": 1}, "text": "x"}),
    ("reveal", {"key": ["call/one"]}),
    ("done", {"key": {"k": 1}, "done": True}),
])
def test_a_badly_typed_browser_write_gets_an_answer(svc_client, route, body):
    c = svc_client
    login(c, c.url)
    status, res = c.json("POST", f"/p/demo/call-1/api/{route}", {**body, "versions": versions(c)})
    assert status == 400, res


def test_badly_typed_agent_calls_get_an_answer(svc_client):
    c = svc_client
    assert c.json("POST", "/api/v1/url", {"project": 5, "dataset": "call-1"}, bearer=True)[0] == 404
    assert c.json("POST", "/api/v1/url", {"project": "demo", "dataset": "call-1", "item": 7}, bearer=True)[0] == 400
    assert c.json("POST", "/api/v1/items", {"project": "demo", "dataset": "call-1", "repo": "me/repo", "items": 5},
                  bearer=True)[0] == 400
    status, body = c.json("POST", "/api/v1/projects", {"name": "p3", "writers": "ab/cd", "repo": "me/repo"},
                          bearer=True)
    assert status == 400 and "list" in body["error"]


def test_an_internal_error_is_a_500_with_only_the_type(svc_client, monkeypatch):
    c = svc_client
    login(c, c.url)
    monkeypatch.setattr(c.svc, "dataset", lambda p, d: (_ for _ in ()).throw(RuntimeError("secret detail")))
    status, body = c.json("GET", "/p/demo/call-1/api/page")
    assert status == 500 and body == {"error": "internal error (RuntimeError); see the server log"}


def test_suggestions_stay_hidden_until_the_item_has_an_answer(svc_client):
    c = svc_client
    login(c, c.url)
    status, body = c.json("POST", "/p/demo/call-1/api/reveal", {"key": "call/one", "versions": versions(c)})
    assert status == 409 and "first" in body["error"]


def test_a_write_reports_a_failed_event_log_but_still_saves(svc_client, monkeypatch):
    c = svc_client
    login(c, c.url)

    class Broken:
        def append(self, *a, **k):
            raise OSError("disk full")
    monkeypatch.setattr(c.svc, "events", lambda p, d: Broken())
    status, body = c.json("POST", "/p/demo/call-1/api/note",
                          {"id": "call/one", "text": "kept", "versions": versions(c)})
    assert status == 200 and body["warning"] == "saved, but the event log failed (OSError)"
    assert c.json("GET", "/p/demo/call-1/api/answers")[1]["review"]["notes"]["call/one"]["text"] == "kept"


def test_a_post_with_a_bad_host_or_to_nowhere_and_a_mcp_items_write_are_refused(svc_client, tmp_path):
    c = svc_client
    assert c.req("POST", "/api/v1/items", {}, bearer=True, headers={"Host": "evil.example"})[0] == 421
    git = {"checkout": str(tmp_path / "co"), "branch": "main", "commit": False}
    c.svc.registry.create_project("priv", privacy="git-private", writers=["me/repo"], git=git)
    status, body = c.json("POST", "/api/v1/items", {"project": "priv", "dataset": "d", "repo": "me/repo",
                                                     "items": []}, bearer=True, headers={"X-TL-Client": "mcp"})
    assert status == 403 and "local-only" in body["error"]


@pytest.mark.parametrize("raw, why", [(b'{"title": "a", "title": "b"}', "a key is written twice"),
                                      (b'{"title": NaN}', "NaN or Infinity")])
def test_a_body_the_strict_reader_refuses_gets_400(svc_client, raw, why):
    code, body = svc_client.json("POST", "/api/v1/projects", raw=raw, bearer=True)
    assert code == 400 and body["error"].startswith(f"body is not JSON: {why}"), (code, body)


def test_a_refused_body_is_never_read_as_the_next_request(svc_client):
    import socket
    port = int(svc_client.base.rsplit(":", 1)[1])
    smuggled = b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
    head = (f"POST /api/v1/projects HTTP/1.1\r\nHost: evil.example\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(smuggled)}\r\n\r\n").encode()
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(head + smuggled)
        data = b""
        while chunk := sock.recv(65536):
            data += chunk
    assert data.count(b"HTTP/1.1 ") == 1 and data.startswith(b"HTTP/1.1 421")


def test_a_refused_post_with_an_unreadable_length_still_gets_its_answer(svc_client):
    import socket
    port = int(svc_client.base.rsplit(":", 1)[1])
    head = (f"POST /api/v1/projects HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: text/plain\r\n"
            "Content-Length: abc\r\n\r\n").encode()
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(head)
        data = b""
        while chunk := sock.recv(65536):
            data += chunk
    assert data.startswith(b"HTTP/1.1 415"), data[:80]


def test_http_servers_own_refusals_carry_the_headers(svc_client):
    status, raw, headers = svc_client.req("OPTIONS", "/", session="")
    assert status == 501 and json.loads(raw)["error"]
    for name, value in SECURITY_HEADERS.items():
        assert headers.get(name) == value, name


def test_the_export_route_is_strict_about_no_text_and_answers_data_errors(svc_client):
    c = svc_client
    q = "/api/v1/export?project=demo&dataset=call-1&kind=spans"
    status, body = c.json("GET", q + "&no_text=yes", bearer=True)
    assert status == 400 and "no_text must be" in body["error"]
    status, body = c.json("GET", q + "&no_text=1", bearer=True)
    assert status == 200 and body["no_text"] is True
    path = c.svc.registry.dataset_dir("demo", "call-1") / "human_answers.json"
    path.write_text(json.dumps({"labels": {}, "attribution": {}, "spans": {"call/one": {
        "key": "call/one", "text_sha256": "0" * 64, "token_count": 1, "checked_by": "operator",
        "ranges": []}}, "corrections": {}}))
    status, body = c.json("GET", q, bearer=True)
    assert status == 409 and body["error"].startswith("ValueError")


def test_any_failure_creating_a_dataset_leaves_nothing_registered(svc_client, monkeypatch):
    c = svc_client
    ds = {"project": "demo", "dataset": "d2", "config": {"speakers": SPEAKERS}, "repo": "me/repo"}
    assert c.json("POST", "/api/v1/datasets", {**ds, "config": ["x"]}, bearer=True)[0] == 400
    import transcript_labeling.agent_api as api

    def explode(*a, **k):
        raise RuntimeError("disk trouble")
    monkeypatch.setattr(api.Dataset, "create", explode)
    status, body = c.json("POST", "/api/v1/datasets", ds, bearer=True)
    assert status == 500 and "RuntimeError" in body["error"]
    assert "d2" not in c.svc.registry.load()["projects"]["demo"]["datasets"]


def test_a_broken_dataset_never_stops_restart_recovery(tmp_path):
    repo = tmp_path / "data"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", MARKER, "1")
    with running(tmp_path) as c:
        c.svc.registry.create_project("priv", privacy="git-private", writers=["me/repo"],
                                      git={"checkout": str(repo), "branch": "main", "commit": True})
        for name in ("gone", "saved"):
            ds = {"project": "priv", "dataset": name, "config": {"speakers": SPEAKERS}, "repo": "me/repo",
                  "path": f"labels/{name}"}
            assert c.json("POST", "/api/v1/datasets", ds, bearer=True)[0] == 200
            (repo / "labels" / name / "review.json").write_text("{}")
        (repo / "labels" / "gone" / "manifest.json").unlink()  # say, a branch switch in the data checkout
        committer = start_committer(c.svc, interval_s=3600)  # must not raise
        assert committer.status("priv", "saved")["pending_saves"] == 1


def test_a_get_with_a_body_is_refused_and_closes_the_connection(tmp_path):
    import socket
    with running(tmp_path) as c:
        port = int(c.base.rsplit(":", 1)[1])
        hidden = b"GET /api/v1/projects HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
        req = (f"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Length: {len(hidden)}\r\n\r\n"
               .encode() + hidden)
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(req)
            data = b""
            while chunk := sock.recv(65536):
                data += chunk
        assert data.count(b"HTTP/1.") == 1 and b" 400 " in data.split(b"\r\n")[0]
        assert b"Connection: close" in data


def test_a_failed_new_reports_its_own_error_when_the_project_cannot_be_rolled_back(tmp_path, monkeypatch, capsys):
    with running(tmp_path) as c:
        def taken(project):  # another call added a dataset to the new project meanwhile
            raise SystemExit(f"REFUSING: project {project!r} holds datasets; it is not removed")
        monkeypatch.setattr(c.svc.registry, "remove_project", taken)
        focus = [{"name": "one", "text": ZOOM, "focus": [{"start": 20, "end": 24, "why": 5}]}]
        status, body = new_dataset(c, items=focus)
        assert status == 409 and "must be strings" in body["error"], body
    assert "project 'demo' was not rolled back: REFUSING: project 'demo' holds datasets" in capsys.readouterr().err


def test_text_fields_must_be_strings(tmp_path):
    with running(tmp_path) as c:
        status, body = c.json("POST", "/api/v1/projects", {"name": "p2", "writers": [], "repo": "me/repo",
                                                           "title": ["x"]}, bearer=True)
        assert status == 400 and "title" in body["error"]
        assert new_dataset(c, config=["x"])[0] == 400
        assert new_dataset(c, config=[])[0] == 400  # empty, but still not an object
        status, body = new_dataset(c, title=["x"])  # the dataset title: refused before anything is made
        assert status == 400 and body["error"] == "title must be a string and config an object", body
        assert "demo" not in c.svc.registry.load()["projects"]
        focus = [{"name": "one", "text": ZOOM, "focus": [{"start": 20, "end": 24, "why": 5}]}]
        status, body = new_dataset(c, items=focus)
        assert status == 409 and "must be strings" in body["error"]
        spoof = [{"name": "one", "text": ZOOM, "source": {"format": "srt", "name": "x.txt"}}]
        assert new_dataset(c, items=spoof)[0] == 200
        item = c.svc.dataset("demo", "call-1").item("item/one")
        assert item["source"] == {"name": "x.txt", "format": "zoom-notes"}


def test_the_server_log_rotates_and_keeps_three_old_files(tmp_path):
    from transcript_labeling.service import RotatingLog
    log = RotatingLog(tmp_path / "logs" / "server.log", max_bytes=100, backups=3)
    failures = []

    def writer(n):
        try:
            for i in range(25):
                log.write(f"line {n:02d} {i:03d} " + "x" * 20 + "\n")
        except Exception as exc:  # two threads rotating at once would collide here
            failures.append(repr(exc))
    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert failures == []
    names = sorted(p.name for p in (tmp_path / "logs").iterdir())
    assert names == ["server.log", "server.log.1", "server.log.2", "server.log.3"]
    assert all((tmp_path / "logs" / n).stat().st_size <= 100 for n in names)
    lines = [ln for n in names for ln in (tmp_path / "logs" / n).read_text().splitlines()]
    assert all(ln.startswith("line ") and len(ln) == 32 for ln in lines)  # no line torn by a rotation
    assert oct((tmp_path / "logs" / "server.log").stat().st_mode & 0o777) == "0o600"


def test_install_log_takes_over_stderr_and_moves_a_large_launchd_log(tmp_path, monkeypatch):
    import io
    import sys
    from transcript_labeling import service
    home = Home(tmp_path / "home")
    home.init()
    big = home.logs_dir / "launchd.log"
    big.write_bytes(b"x" * (service.LOG_MAX_BYTES + 1))
    before = io.StringIO()  # stands in for launchd.log; the real stderr is restored after the test
    monkeypatch.setattr(sys, "stderr", before)
    log = service.install_log(home)
    assert sys.stderr is log and not big.exists() and (home.logs_dir / "launchd.log.1").exists()
    assert before.getvalue() == f"transcript-labeling {service.__version__}: logging to {log.path}\n"
    print("hello from the server", file=sys.stderr)
    assert (home.logs_dir / "server.log").read_text() == "hello from the server\n"


def test_a_log_that_cannot_be_written_never_raises(tmp_path, monkeypatch):
    import io
    import shutil
    import sys
    from transcript_labeling.service import RotatingLog
    folder = tmp_path / "logs"
    log = RotatingLog(folder / "server.log", max_bytes=1000)
    log.write("one\n")
    shutil.rmtree(folder)  # deleted while the server runs: made again
    assert log.write("two\n") == 4
    assert (folder / "server.log").read_text() == "two\n"
    fallback = io.StringIO()
    monkeypatch.setattr(sys, "__stderr__", fallback)
    (folder / "server.log").chmod(0o400)  # cannot be opened for writing: reported, not raised
    try:
        assert log.write("three\n") == 6
    finally:
        (folder / "server.log").chmod(0o600)
    assert (folder / "server.log").read_text() == "two\n"
    assert "cannot write" in fallback.getvalue() and "PermissionError" in fallback.getvalue()


def test_a_commit_tick_survives_a_vanished_log_folder(svc_client, tmp_path, monkeypatch):
    import shutil
    import sys
    from transcript_labeling.service import RotatingLog
    folder = tmp_path / "logs"
    monkeypatch.setattr(sys, "stderr", RotatingLog(folder / "server.log"))

    class Exploding:
        def tick(self, files_of):
            raise SystemExit("REFUSING: the registry is unreadable")
    shutil.rmtree(folder, ignore_errors=True)
    assert commit_tick(svc_client.svc, Exploding()) == [{"error": "SystemExit: REFUSING: the registry is unreadable"}]
    assert "commit tick failed" in (folder / "server.log").read_text()


def test_the_log_never_makes_a_deleted_home_again(tmp_path, monkeypatch):
    import io
    import shutil
    import sys
    from transcript_labeling.service import RotatingLog
    home = tmp_path / "home"
    log = RotatingLog(home / "logs" / "server.log")
    log.write("one\n")
    shutil.rmtree(home)  # the whole home deleted while the server runs
    fallback = io.StringIO()
    monkeypatch.setattr(sys, "__stderr__", fallback)
    log.write("two\n")
    assert not home.exists()
    assert "cannot write" in fallback.getvalue() and "was lost" in fallback.getvalue()
