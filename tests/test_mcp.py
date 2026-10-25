"""`tl mcp`, the stdio MCP server, against an in-process service on a free port.

  STDIO      a real `tl mcp` process: initialize, tools/list, new, status, export, wait
  NO TEXT    no tool result carries transcript text; export returns a path under $TL_HOME/exports
  ABSOLUTE   relative file and repo paths are refused, and so are unknown or badly typed arguments
  PRIVATE    an MCP caller cannot write a git-private project
  WAIT       returns still_waiting within its cap, and finished once the owner pressed Finish
  PROTOCOL   notifications get no reply; bad JSON, unknown methods and unknown tools get errors
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from transcript_labeling import cli, gitproject, mcp
from transcript_labeling.home import Home
from transcript_labeling.service import Service, build_service_server

ZOOM = Path(__file__).resolve().parent / "fixtures" / "zoom" / "notes.txt"
SECRET_WORD = "loud and clear"  # in the fixture text
ONE_WORD = "worries"  # once in the fixture text and nowhere else, so even a list of words gives it away
SPEAKERS = [{"id": "ann", "label": "Ann Able", "key": "1"}, {"id": "bo", "label": "Bo Brand", "key": "2"}]


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = Home(tmp_path / "home")
    monkeypatch.setenv("TL_HOME", str(home.root))
    assert cli.main(["init"]) == 0
    svc = Service(home)
    httpd = build_service_server(svc, 0)
    cfg = json.loads(home.config_path.read_text())
    cfg["port"] = httpd.server_address[1]
    home.config_path.write_text(json.dumps(cfg))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    checkout = tmp_path / "agent-checkout"
    checkout.mkdir()
    git(checkout, "init", "-q")
    git(checkout, "remote", "add", "origin", "git@github.com:me/agent.git")
    try:
        yield {"home": home, "svc": svc, "checkout": checkout, "tmp": tmp_path}
    finally:
        httpd.shutdown()
        httpd.server_close()


def call(env, tool, **args):
    """tools/call in process; returns (result object, isError)."""
    reply = mcp.handle(env["home"], {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                     "params": {"name": tool, "arguments": args}})
    assert reply["id"] == 7 and "error" not in reply, reply
    return json.loads(reply["result"]["content"][0]["text"]), reply["result"]["isError"]


def new(env, dataset="call-1"):
    return call(env, "new", project="demo", dataset=dataset, file=str(ZOOM), repo=str(env["checkout"]),
                create_project=True, speakers=SPEAKERS)


def test_a_stdio_round_trip(env):
    proc = subprocess.Popen([sys.executable, "-m", "transcript_labeling.cli", "mcp"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=env["tmp"],
                            env={**os.environ, "TL_HOME": str(env["home"].root)})

    def send(msg):
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def rpc(mid, method, params=None):
        send({"jsonrpc": "2.0", "id": mid, "method": method, **({"params": params} if params else {})})
        reply = json.loads(proc.stdout.readline())
        assert reply["id"] == mid, reply
        return reply

    def tool(mid, name, **args):
        res = rpc(mid, "tools/call", {"name": name, "arguments": args})["result"]
        return json.loads(res["content"][0]["text"]), res["isError"], res["content"][0]["text"]

    try:
        init = rpc(1, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                     "clientInfo": {"name": "test", "version": "0"}})["result"]
        assert init["protocolVersion"] == "2025-03-26" and init["serverInfo"]["name"] == "transcript-labeling"
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        names = [t["name"] for t in rpc(2, "tools/list")["result"]["tools"]]
        assert names == ["new", "items_add", "url", "status", "wait", "export", "verify", "schema", "doctor"]
        made, bad, raw = tool(3, "new", project="demo", dataset="call-1", file=str(ZOOM),
                              repo=str(env["checkout"]), create_project=True, speakers=SPEAKERS)
        assert not bad and "/login#code=" in made["send_the_owner_this_link"] and SECRET_WORD not in raw
        assert env["svc"].registry.project("demo")["writers"] == ["me/agent"]
        st, bad, _ = tool(4, "status", project="demo", dataset="call-1")
        assert not bad and st["items"] == 1
        ex, bad, raw = tool(5, "export", project="demo", dataset="call-1", kind="segments")
        path = Path(ex["path"])
        assert path == env["home"].exports_dir("demo", "call-1") / "segments.json" and SECRET_WORD in path.read_text()
        captions = sum(len(rows) for rows in json.loads(path.read_text())["data"].values())
        assert not bad and SECRET_WORD not in raw and ex["counts"] == {"segments": {"items": 1, "captions": captions}}
        assert captions > 1
        waited, bad, _ = tool(6, "wait", project="demo", dataset="call-1", timeout_s=0)
        assert not bad and waited["still_waiting"] is True
    finally:
        proc.stdin.close()
        assert proc.wait(timeout=10) == 0, proc.stderr.read()
    assert proc.stdout.read() == ""


def test_no_tool_result_carries_transcript_text(env):
    assert ONE_WORD in ZOOM.read_text() and ZOOM.read_text().count(ONE_WORD) == 1
    results = [new(env)]
    ref = {"project": "demo", "dataset": "call-1"}
    item = {**ref, "file": str(ZOOM), "repo": str(env["checkout"])}
    for tool, args in [("items_add", item), ("items_add", {**item, "upsert": True}), ("status", ref),
                       ("verify", ref), ("url", ref), ("wait", {**ref, "timeout_s": 0}), ("doctor", {}),
                       ("schema", {"name": "export"}), ("export", {**ref, "kind": "all"}),
                       ("export", {**ref, "kind": "spans", "no_text": True}),
                       ("export", {**ref, "kind": "labels"})]:  # no checklist here: an error result
        results.append(call(env, tool, **args))
    # The plain items_add refuses (the item exists) and so does the labels export.
    assert [bad for _, bad in results] == [False, True] + [False] * 9 + [True]
    for res, _ in results:
        assert ONE_WORD not in json.dumps(res)
    path = Path(results[-2][0]["path"])
    assert path.name == "spans-notext.json" and ONE_WORD not in path.read_text()
    assert ONE_WORD in (path.parent / "all.json").read_text()


@pytest.mark.parametrize("args, expect", [
    ({"file": "notes.txt"}, "file must be an absolute path"),
    ({"repo": "agent-checkout"}, "repo must be an absolute path"),
    ({"repo": "/nonexistent/checkout"}, "must be your git checkout"),
    ({"extra": 1}, "unknown arguments ['extra']"),
    ({"create_project": "yes"}, "create_project must be a JSON boolean"),
    ({"speakers": "ann=Ann:1"}, "speakers must be a JSON array"),
])
def test_bad_arguments_are_refused(env, args, expect):
    base = {"project": "demo", "dataset": "call-1", "file": str(ZOOM), "repo": str(env["checkout"]),
            "create_project": True, "speakers": SPEAKERS}
    res, bad = call(env, "new", **{**base, **args})
    assert bad and expect in res["error"]
    assert "demo" not in env["svc"].registry.load()["projects"]


def test_missing_arguments_and_a_wait_past_its_cap_are_refused(env):
    res, bad = call(env, "status", project="demo")
    assert bad and "missing ['dataset']" in res["error"]
    new(env)
    res, bad = call(env, "wait", project="demo", dataset="call-1", timeout_s=mcp.WAIT_CAP_S + 1)
    assert bad and "between 0 and 50" in res["error"]


def test_an_mcp_caller_cannot_write_a_git_private_project(env, monkeypatch):
    data = env["tmp"] / "data"
    data.mkdir()
    git(data, "init", "-q", "-b", "main")
    git(data, "remote", "add", "origin", "git@github.com:me/labels-data.git")
    monkeypatch.setattr(gitproject, "gh_visibility", lambda url: "PRIVATE")
    assert cli.main(["project", "create", "priv", "--git-checkout", str(data), "--repo", "me/agent"]) == 0
    cfg = env["tmp"] / "cfg.json"
    cfg.write_text(json.dumps({"speakers": SPEAKERS}))
    assert cli.main(["dataset", "create", "priv/d1", "--config", str(cfg), "--path", "labels/d1",
                     "--repo", "me/agent"]) == 0
    before = sorted(str(f.relative_to(data)) for f in data.rglob("*") if ".git" not in f.parts)
    res, bad = call(env, "items_add", project="priv", dataset="d1", file=str(ZOOM), repo=str(env["checkout"]))
    assert bad and "git-private" in res["error"]
    assert sorted(str(f.relative_to(data)) for f in data.rglob("*") if ".git" not in f.parts) == before
    assert env["svc"].dataset("priv", "d1").manifest["items"] == {}
    res, bad = call(env, "new", project="priv", dataset="d2", file=str(ZOOM), repo=str(env["checkout"]),
                    speakers=SPEAKERS)
    assert bad and "git-private" in res["error"]
    assert not (data / "labels").exists() or list((data / "labels").iterdir()) == [data / "labels" / "d1"]
    assert sorted(env["svc"].registry.project("priv")["datasets"]) == ["d1"]


def test_wait_returns_once_the_owner_pressed_finish(env, monkeypatch):
    monkeypatch.setattr(cli, "WAIT_POLL_S", 0.05)
    new(env)
    res, bad = call(env, "wait", project="demo", dataset="call-1", timeout_s=0.2)
    assert not bad and res["still_waiting"] is True and res["waited_s"] >= 0.2
    store = env["svc"].review_store("demo", "call-1", env["svc"].dataset("demo", "call-1"))
    store.update(lambda r: {**r, "finished": {"at": "2026-10-22T06:00:00+00:00", "checked_by": "operator"}},
                 store.load_versioned()[1])
    res, bad = call(env, "wait", project="demo", dataset="call-1", timeout_s=5)
    assert not bad and res == {"finished": {"at": "2026-10-22T06:00:00+00:00", "checked_by": "operator"}}


def test_doctor_and_schema(env):
    res, bad = call(env, "doctor")
    assert not bad and res["ok"] is True and res["problems"] == []
    res, bad = call(env, "schema", name="item")
    assert not bad and res["name"] == "item"


def test_protocol_errors(env):
    home = env["home"]
    assert mcp.handle(home, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert mcp.handle(home, {"jsonrpc": "2.0", "id": 1, "method": "nope"})["error"]["code"] == -32601
    assert mcp.handle(home, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                             "params": {"name": "rm", "arguments": {}}})["error"]["code"] == -32602
    assert mcp.handle(home, {"id": 3, "method": "ping"})["error"]["code"] == -32600
    newest = mcp.handle(home, {"jsonrpc": "2.0", "id": 4, "method": "initialize",
                               "params": {"protocolVersion": "1999-01-01"}})
    assert newest["result"]["protocolVersion"] == mcp.PROTOCOL_VERSIONS[0]
    out = []

    class Sink:
        def write(self, s):
            out.append(s)

        def flush(self):
            pass
    mcp.serve_stdio(home, stdin=[b"not json\n", b"\n", b'{"jsonrpc": "2.0", "id": 5, "method": "ping"}\n'],
                    stdout=Sink())
    assert [json.loads(s) for s in out] == [
        {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "not JSON"}},
        {"jsonrpc": "2.0", "id": 5, "result": {}}]


def test_malformed_messages_never_stop_the_server(env):
    out = []

    class Sink:
        def write(self, s):
            out.append(json.loads(s))

        def flush(self):
            pass
    bad = [b'{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": [1]}\n',
           b'{"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": "x"}\n',
           b'{"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": ["status"]}}\n',
           b'\xff\xfe{"jsonrpc": "2.0"}\n',
           b"[" * 200000 + b"\n",
           b'{"jsonrpc": "2.0", "id": 8, "id": 7, "method": "ping"}\n',  # a key written twice
           b'{"jsonrpc": "2.0", "id": 9, "method": "ping"}\n']
    mcp.serve_stdio(env["home"], stdin=bad, stdout=Sink())
    assert [(r["id"], r.get("error", {}).get("code")) for r in out] == [
        (1, -32602), (2, -32602), (3, -32602), (None, -32700), (None, -32700), (None, -32700), (9, None)]


def test_replies_are_utf8_whatever_the_locale(env):
    proc = subprocess.run([sys.executable, "-m", "transcript_labeling.cli", "mcp"], capture_output=True,
                          # escaped, so a locale-encoded reader decodes it to a character it must then encode
                          input=b'{"jsonrpc": "2.0", "id": 1, "method": "\\u2603"}\n', cwd=env["tmp"],
                          env={**{k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")},
                               "LC_ALL": "en_US.ISO8859-1", "TL_HOME": str(env["home"].root)}, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.decode("utf-8"))["error"]["message"] == "no such method: \u2603"


def test_an_unexpected_error_is_reported_and_the_server_keeps_serving(env, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("disk trouble")
    monkeypatch.setattr(mcp, "check_arguments", boom)
    res, bad = call(env, "status", project="demo", dataset="call-1")
    assert bad and res["error"] == "internal error in tl mcp: RuntimeError; see its stderr"
    assert "disk trouble" in capsys.readouterr().err


def test_a_text_export_into_a_git_work_tree_is_refused(env):
    new(env)
    git(env["home"].root, "init", "-q")  # a home that someone put inside a checkout
    res, bad = call(env, "export", project="demo", dataset="call-1", kind="segments")
    assert bad and "inside the git work tree" in res["error"]
    assert not (env["home"].exports_dir("demo", "call-1") / "segments.json").exists()
    res, bad = call(env, "export", project="demo", dataset="call-1", kind="segments", no_text=True)
    assert not bad and SECRET_WORD not in Path(res["path"]).read_text()


def test_counts_follow_each_kind(env):
    new(env)
    res, bad = call(env, "export", project="demo", dataset="call-1", kind="all")
    assert not bad and res["counts"] == {"spans": {"items": 0, "ranges": 0},
                                         "segments": {"items": 1, "captions": res["counts"]["segments"]["captions"]},
                                         "focus": {"passages": 0}, "corrections": {"items": 0, "edits": 0},
                                         "notes": {"notes": 0}}
    labels = {"labels": {"a/b": {}}, "partial": ["a/c"], "unlabelled": ["a/d", "a/e"]}
    assert mcp.counts({"kind": "labels", "data": labels}) == {"labels": {"labels": 1, "partial": 1, "unlabelled": 2}}


def test_an_unforeseen_failure_in_one_message_is_answered_and_the_next_is_served(env, monkeypatch, capsys):
    real = mcp.handle

    def flaky(home, msg):
        if msg.get("id") == 1:
            raise RuntimeError("unforeseen")
        return real(home, msg)
    monkeypatch.setattr(mcp, "handle", flaky)
    out = []

    class Sink:
        def write(self, s):
            out.append(json.loads(s))

        def flush(self):
            pass
    mcp.serve_stdio(env["home"], stdin=[b'{"jsonrpc": "2.0", "id": 1, "method": "ping"}\n',
                                        b'{"jsonrpc": "2.0", "id": 2, "method": "ping"}\n'], stdout=Sink())
    assert out == [{"jsonrpc": "2.0", "id": 1, "error": {"code": -32603,
                                                         "message": "internal error in tl mcp: RuntimeError"}},
                   {"jsonrpc": "2.0", "id": 2, "result": {}}]
    assert "unforeseen" in capsys.readouterr().err
