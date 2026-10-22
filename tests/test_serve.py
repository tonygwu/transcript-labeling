"""The local answer server accepts what a session's page can send and refuses everything else.

Ported from verbatim-index `scripts/test_annotations_serve.py`, plus the HTTP
round trips of its spans and corrections tests. It runs a REAL server on a real
socket and talks to it over HTTP, and reads the bind address off the listening
socket rather than off the constant.

  LOOPBACK   the socket listens on 127.0.0.1 and nothing else
  PAGE       / redirects to the only session, and /s/<name>/ serves its page
  ROUNDTRIP  a saved answer comes back from GET api/answers and is on disk
  MERGE      a second answer replaces its own entry and leaves the others alone
  ASSISTED   once a label has draft_shown and its blind answers, no later save drops
             or replaces them
  DERIVED    a quote result cannot be posted: it follows the word ranges
  FOREIGN    an id the page does not carry is refused, and nothing is written
  KIND/MODE  a kind that is no section, or a section no mode writes, is refused
  SHAPE      a non-object value, a bad body and an oversized body are refused
  EXPORT     the file the server writes is the one `export labels` reads
  BROKEN     a page with no data block stops the server rather than accepting any id
  KEPT       an answer for an id missing from a rebuilt page is kept, not dropped
  SESSIONS   two sessions are served side by side and listed at /
  SPANS      ranges round-trip, GET never rewrites the file, a stale hash is refused
  CORRECT    corrections round-trip with server-side revisions and history
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import socket
import threading
import urllib.error
import urllib.request

import pytest

from conftest import KEY, QID, RAW, write_sidecar
from transcript_labeling import server as S
from transcript_labeling.export import export_labels
from transcript_labeling.spans import build_sidecar, sidecar_path
from transcript_labeling.testing import write_session
from transcript_labeling.tokens import token_kinds

# A request returns as soon as the server answers, so this bound costs nothing on a
# healthy run; it only decides how slow a loaded machine may be.
HTTP_TIMEOUT_S = 30
MODES = ("checklist", "speakers", "captions")
CHECKLIST = {"fields": [
    {"name": "present", "label": "Present", "type": "bool", "required": True,
     "options": [{"value": True, "label": "Yes", "key": "y"}, {"value": False, "label": "No", "key": "n"}]},
    {"name": "format", "label": "Format", "type": "enum", "required": True,
     "options": [{"value": v, "label": v, "key": str(i + 1)}
                 for i, v in enumerate(("solo", "reaction", "debate"))]}]}


def post(base, obj, raw=None, kind_path="/api/answer"):
    body = raw if raw is not None else json.dumps(obj).encode()
    req = urllib.request.Request(base + kind_path, data=body, method="POST",
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=HTTP_TIMEOUT_S) as r:
        return r.status, r.read()


def session_data(keys, qids):
    return {"rows": [{"key": k} for k in keys],
            "quote_rows": [{"key": keys[0] if keys else "p/a", "quotes": [{"qid": q} for q in qids]}] if qids else [],
            "transcript_index": {}, "session": {"checklist": CHECKLIST}}


@contextlib.contextmanager
def serving(sessions):
    httpd = S.build_server(sessions, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield httpd, f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def one(tmp_path):
    session = write_session(tmp_path / "one", session_data(["p/a", "p/b"], ["p:a:0", "p:a:1"]), modes=MODES)
    with serving([session]) as (httpd, root):
        yield session, httpd, root, root + "/s/t"


def label(key, **answers):
    return {"key": key, "checked_by": "operator", **answers}


def test_loopback_only(one):
    session, httpd, root, base = one
    assert httpd.server_address[0] == "127.0.0.1"
    assert httpd.socket.getsockname()[0] == "127.0.0.1"
    # gethostbyname(gethostname()) can answer 127.0.0.1, which would test loopback
    # against itself. Ask the routing table instead; the UDP connect sends no packet.
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1, never routed
        outward = probe.getsockname()[0]
    except OSError:
        outward = None
    finally:
        probe.close()
    # Fail, never skip: without a LAN address the bind is unproven.
    assert outward and not outward.startswith("127."), f"no non-loopback address to test against: {outward!r}"
    s = socket.socket()
    s.settimeout(1.5)
    assert s.connect_ex((outward, httpd.server_address[1])) != 0, "the server answers on a LAN address"
    s.close()


def test_page_routes(one):
    session, httpd, root, base = one
    code, body = get(root, "/")
    assert code == 200 and b"const D = " in body
    code, body = get(base, "/")
    assert code == 200 and body == session.page.read_bytes()
    code, body = get(base, "")
    assert code == 200 and b"const D = " in body


def test_roundtrip_merge_and_four_sections(one):
    session, httpd, root, base = one
    code, res = post(base, {"kind": "labels", "id": "p/a", "value": label("p/a", present=True, format="solo")})
    assert code == 200 and res["ok"]
    code, body = get(base, "/api/answers")
    assert json.loads(body)["labels"]["p/a"]["format"] == "solo"
    assert json.loads(session.answers.read_text())["labels"]["p/a"]["format"] == "solo"
    post(base, {"kind": "labels", "id": "p/b", "value": label("p/b", present=False, format="debate")})
    post(base, {"kind": "labels", "id": "p/a", "value": label("p/a", present=True, format="reaction")})
    disk = json.loads(session.answers.read_text())
    assert disk["labels"]["p/a"]["format"] == "reaction" and disk["labels"]["p/b"]["format"] == "debate"
    assert set(disk) == {"labels", "attribution", "spans", "corrections"}


def test_assisted_fields_are_sticky(one):
    session, httpd, root, base = one
    blind = {"present": False, "format": "debate"}
    post(base, {"kind": "labels", "id": "p/b", "value": label("p/b", **{**blind, "present": True},
                                                             draft_shown="model-x", answers_before_draft=blind)})
    # A second tab still showing the older copy of the label saves without either field.
    post(base, {"kind": "labels", "id": "p/b", "value": label("p/b", **{**blind, "format": "solo"})})
    kept = json.loads(session.answers.read_text())["labels"]["p/b"]
    assert kept["draft_shown"] == "model-x" and kept["answers_before_draft"] == blind and kept["format"] == "solo"
    post(base, {"kind": "labels", "id": "p/b", "value": label("p/b", **blind, draft_shown="model-x",
                                                             answers_before_draft={**blind, "format": "solo"})})
    assert json.loads(session.answers.read_text())["labels"]["p/b"]["answers_before_draft"] == blind
    assisted = label("p/a", present=True, format="reaction")
    post(base, {"kind": "labels", "id": "p/a", "value": {**assisted, "draft_shown": "model-x"}})
    post(base, {"kind": "labels", "id": "p/a", "value": {**assisted, "draft_shown": "model-x",
                                                         "answers_before_draft": blind}})
    kept = json.loads(session.answers.read_text())["labels"]["p/a"]
    assert kept["draft_shown"] == "model-x" and "answers_before_draft" not in kept


def test_refusals_write_nothing(one):
    session, httpd, root, base = one
    post(base, {"kind": "labels", "id": "p/a", "value": label("p/a", present=True, format="reaction")})
    before = session.answers.read_bytes()
    code, res = post(base, {"kind": "attribution", "id": "p:a:0", "value": {"qid": "p:a:0", "answer": "other"}})
    assert code == 409 and "word markings" in res["error"]
    code, res = post(base, {"kind": "labels", "id": "q/zz", "value": {"key": "q/zz"}})
    assert code == 400 and "not a labels id" in res["error"]
    code, res = post(base, {"kind": "spans", "id": "p/zz", "value": {}})
    assert code == 400
    code, res = post(base, {"kind": "leans", "id": "p/a", "value": {}})
    assert code == 400 and "kind must be" in res["error"]
    code, res = post(base, {"kind": "labels", "id": "p/a", "value": "solo"})
    assert code == 400
    code, res = post(base, None, raw=b"{not json")
    assert code == 400 and "not JSON" in res["error"]
    code, res = post(base, None, raw=b"[1]")
    assert code == 400 and "not an object" in res["error"]
    for raw, why in ((b'{"kind": "labels", "id": "p/a", "value": {"t": NaN}}', "NaN or Infinity"),
                     (b'{"kind": "labels", "id": "p/a", "value": {"t": 1e400}}', "NaN or Infinity"),
                     (b'{"kind": "labels", "kind": "spans", "id": "p/a", "value": {}}', "written twice")):
        code, res = post(base, None, raw=raw)
        assert code == 400 and why in res["error"], res
    code, res = post(base, None, raw=b"x" * (S.MAX_BODY + 1))
    assert code == 400 and "bytes" in res["error"]
    code, res = post(base, {"kind": "labels"}, kind_path="/api/nothing")
    assert code == 404
    assert session.answers.read_bytes() == before


def test_a_body_four_times_the_cap_gets_the_400_not_a_reset(one):
    # MEASURED 2026-10-11 in verbatim: before the server drained refused bodies, 3 of
    # 60 runs failed at MAX_BODY + 1, and a 1 MiB body was reset 30 times in 30.
    session, httpd, root, base = one
    outcomes = []
    for _ in range(5):
        try:
            outcomes.append(post(base, None, raw=b"x" * (4 * S.MAX_BODY))[0])
        except urllib.error.URLError as exc:
            outcomes.append(f"reset: {exc.reason}")
    assert outcomes == [400] * 5


def test_export_labels_reads_the_servers_file(one):
    session, httpd, root, base = one
    post(base, {"kind": "labels", "id": "p/a", "value": label("p/a", present=True, format="reaction")})
    post(base, {"kind": "labels", "id": "p/b", "value": label("p/b", present=False)})
    got = export_labels(session)
    assert set(got["labels"]) == {"p/a"} and got["labels"]["p/a"]["format"] == "reaction"
    assert got["partial"] == ["p/b"]


def test_a_session_without_the_checklist_mode_refuses_a_checklist_answer(tmp_path):
    quiet = write_session(tmp_path / "sp", session_data(["p/a"], ["p:a:0"]), modes=("speakers",), name="sp")
    with serving([quiet]) as (_, root):
        code, res = post(root + "/s/sp", {"kind": "labels", "id": "p/a", "value": {"key": "p/a"}})
    assert code == 400 and "do not write labels" in res["error"]
    assert not quiet.answers.exists()


def test_a_page_with_no_data_block_stops_the_server(tmp_path):
    bad = write_session(tmp_path / "bad", session_data(["p/a"], []), modes=MODES, name="bad")
    bad.page.write_text("<title>t</title><script>const D = null;</script>")
    with pytest.raises(SystemExit, match="no `const D"):
        S.build_server([bad], 0)


def test_an_answer_for_an_id_a_rebuilt_page_dropped_is_kept(tmp_path):
    first = write_session(tmp_path / "one", session_data(["p/a", "p/b"], []), modes=MODES)
    first.answers.write_text(json.dumps({"labels": {"p/b": {"present": True}}, "attribution": {}}))
    shrunk = write_session(tmp_path / "one", session_data(["p/a"], []), modes=MODES)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        h = S.build_server([shrunk], 0)
    try:
        assert "p/b" in h.mounts["t"].store.load()["labels"]
        assert "p/b" not in h.mounts["t"].ids["labels"]
        assert "cannot be changed from this page" in err.getvalue()
    finally:
        h.server_close()


def test_two_sessions_side_by_side_and_the_refusals(tmp_path):
    a = write_session(tmp_path / "a", session_data(["p/a"], []), modes=MODES, name="t")
    b = write_session(tmp_path / "b", session_data(["q/x"], []), modes=("speakers", "captions"),
                      projects=("alpha", "beta"), name="two")
    with serving([a, b]) as (_, root):
        code, body = get(root, "/")
        page = body.decode()
        assert code == 200 and 'href="/s/t/"' in page and 'href="/s/two/"' in page
        assert "alpha" in page and "beta" in page
        code, body = get(root, "/s/two/")
        assert code == 200 and body == b.page.read_bytes()
    three = write_session(tmp_path / "c", {**session_data(["p/a"], []), "transcript_index": {"p/a": {}}},
                          modes=("speakers",), name="three")
    four = write_session(tmp_path / "d", {**session_data(["p/a"], []), "transcript_index": {"p/a": {}}},
                         modes=("speakers",), name="four")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        S.build_server([four, three], 0).server_close()
    assert "recording p/a is in both four and three" in err.getvalue()
    dup = write_session(tmp_path / "dup", session_data(["p/a"], []))
    with pytest.raises(SystemExit, match="share a name"):
        S.build_server([a, dup], 0)
    with pytest.raises(SystemExit, match="one answers file"):
        S.build_server([a, type(a)(a.root, {**a.manifest, "name": "x"})], 0)
    with pytest.raises(SystemExit, match="no session"):
        S.build_server([], 0)


# ------------------------------------------------------------------ SPANS and CORRECT

@pytest.fixture
def speakers(tmp_path):
    """A session with one transcript and one quote, served."""
    side = build_sidecar(KEY, RAW, [{"start": 7, "end": 14, "speaker": "subject"}])
    quote = {"qid": QID, "quote_start": 7, "quote_end": 14, "text": "I think the grid will run out"}
    data = {"rows": [{"key": KEY}], "quote_rows": [{"key": KEY, "quotes": [quote]}],
            "transcript_index": {KEY: {"text_sha256": side["text_sha256"], "token_count": side["token_count"]}}}
    session = write_session(tmp_path / "s", data, modes=MODES)
    write_sidecar(session.page, side)
    with serving([session]) as (_, root):
        yield session, side, root + "/s/t"


def ranges_value(side, ranges):
    return {"schema_version": 1, "key": KEY, "text_sha256": side["text_sha256"], "token_count": side["token_count"],
            "checked_by": "operator", "ranges": ranges}


def test_spans_roundtrip_derives_the_quote_result(speakers):
    session, side, _ = speakers
    # The store reads the file when the server starts, so this server is a new one.
    pending = ranges_value(side, [{"start": 1, "end": 6, "speaker": "other", "origin": "human"}])
    session.answers.write_text(json.dumps({"labels": {"p/x": {"present": True}}, "spans": {KEY: pending},
                                           "attribution": {QID: {"qid": QID, "answer": "unclear"}}}))
    with serving([session]) as (_, root):
        _spans_roundtrip(session, side, root + "/s/t")


def _spans_roundtrip(session, side, base):
    before = session.answers.read_bytes()
    code, body = get(base, "/api/answers")
    loaded = json.loads(body)
    # Unmarked quote words leave a legacy whole-quote answer pending, never complete.
    assert loaded["attribution"][QID]["answer"] is None
    assert loaded["attribution"][QID]["legacy_answer"] == "unclear"
    assert session.answers.read_bytes() == before, "GET rewrote original human data"
    value = ranges_value(side, [{"start": 7, "end": 14, "speaker": "subject", "origin": "human"}])
    code, res = post(base, {"kind": "spans", "id": KEY, "value": value})
    assert code == 200 and res["attribution"][QID]["answer"] == "subject"
    disk = json.loads(session.answers.read_text())
    assert disk["spans"][KEY] == value and disk["attribution"][QID]["answer"] == "subject"
    assert disk["labels"] == {"p/x": {"present": True}}
    value2 = ranges_value(side, [{"start": 7, "end": 9, "speaker": "subject", "origin": "human"}])
    code, res = post(base, {"kind": "spans", "id": KEY, "value": value2})
    assert res["attribution"][QID]["unreviewed_words"] == 5
    assert json.loads(session.answers.read_text())["attribution"][QID]["answer"] is None


def test_a_stale_or_invalid_range_is_refused_and_writes_nothing(speakers):
    session, side, base = speakers
    good = ranges_value(side, [{"start": 7, "end": 14, "speaker": "subject", "origin": "human"}])
    post(base, {"kind": "spans", "id": KEY, "value": good})
    before = session.answers.read_bytes()
    code, res = post(base, {"kind": "spans", "id": KEY, "value": {**good, "text_sha256": "stale"}})
    assert code == 400 and "transcript changed" in res["error"]
    bad = copy.deepcopy(good)
    bad["ranges"][0]["origin"] = "model_draft"
    assert post(base, {"kind": "spans", "id": KEY, "value": bad})[0] == 400
    assert session.answers.read_bytes() == before


def test_the_transcript_route_serves_tokens_with_kinds_and_refuses_other_keys(speakers):
    session, side, base = speakers
    code, body = get(base, "/api/transcript?key=" + KEY.replace("/", "%2F"))
    assert code == 200 and json.loads(body) == {**side, "token_kinds": token_kinds(side["tokens"])}
    for bad in ("..%2Fsecret", "p%2Fnone"):
        with pytest.raises(urllib.error.HTTPError) as caught:
            get(base, "/api/transcript?key=" + bad)
        assert caught.value.code == 404


def test_corrections_roundtrip_with_server_revisions(speakers):
    session, side, base = speakers
    ranges = ranges_value(side, [{"start": 7, "end": 14, "speaker": "other", "origin": "human"}])
    post(base, {"kind": "spans", "id": KEY, "value": ranges})
    source = sidecar_path(session.page, KEY).read_bytes()
    edit = {"start": 8, "end": 10, "original": "think the", "replacement": "thought a"}
    value = {"key": KEY, "text_sha256": side["text_sha256"], "token_count": side["token_count"], "revision": 0,
             "checked_by": "operator", "edits": [edit]}
    code, res = post(base, {"kind": "corrections", "id": KEY, "value": value})
    assert code == 200 and res["correction"]["revision"] == 1
    code, body = get(base, "/api/answers")
    loaded = json.loads(body)
    assert loaded["corrections"][KEY]["edits"] == [edit] and loaded["spans"][KEY] == ranges
    assert loaded["attribution"][QID]["answer"] == "other"
    before = session.answers.read_bytes()
    code, res = post(base, {"kind": "corrections", "id": KEY, "value": value})
    assert code == 409 and "another window" in res["error"]
    assert session.answers.read_bytes() == before
    post(base, {"kind": "corrections", "id": KEY, "value": {**value, "revision": 1, "edits": []}})
    disk = json.loads(session.answers.read_text())
    assert disk["corrections"][KEY]["edits"] == [] and len(disk["corrections"][KEY]["history"]) == 2
    assert disk["spans"][KEY] == ranges
    assert sidecar_path(session.page, KEY).read_bytes() == source


def test_a_session_name_that_needs_url_escaping_stops_the_server(tmp_path):
    a = write_session(tmp_path / "a", session_data(["p/a"], []), modes=MODES, name="t")
    spaced = type(a)(a.root, {**a.manifest, "name": "my set"})
    with pytest.raises(SystemExit, match="URL escaping"):
        S.build_server([spaced], 0)
