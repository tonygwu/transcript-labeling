"""Large, synthetic recording annotations over both real HTTP server paths."""
from contextlib import contextmanager
import copy
import json

import pytest

from conftest import write_sidecar
from test_serve import CHECKLIST, MODES, get, post, serving
from test_service import login, new_dataset, running, versions
from transcript_labeling.spans import build_sidecar
from transcript_labeling.testing import write_session

SMALL_CAP = 256 * 1024
ANSWER_CAP = 8 * 1024 * 1024
KEY = "item/one"
OTHER = "item/two"


@contextmanager
def backend(tmp_path, mode):
    text = "word " * 5000
    if mode == "service":
        with running(tmp_path) as c:
            status, made = new_dataset(
                c, modes=list(MODES), config={"review_mode": "manual", "checklist": {
                    "fields": CHECKLIST["fields"][:1], "goal": {"count": 1, "help": "Items verified"},
                    "accept_when_true": ["present"]}},
                speakers=[{"id": "subject", "label": "Jane Roe", "key": "1"},
                          {"id": "other", "label": "Alex Doe", "key": "2"}],
                items=[{"name": "one", "text": text}, {"name": "two", "text": "Other words."}])
            assert status == 200, made
            login(c, made["url"])
            ds = c.svc.dataset("demo", "call-1")
            v = versions(c)
            def send(msg=None, raw=None):
                return c.json("POST", "/p/demo/call-1/api/answer",
                              {**msg, "versions": v} if msg else None, raw=raw)
            def read():
                status, data = c.json("GET", "/p/demo/call-1/api/answers")
                assert status == 200
                return data
            yield send, read, ds.answers, ds.item(KEY), ds.item(OTHER), c
    else:
        side = build_sidecar(KEY, text, [])
        other = build_sidecar(OTHER, "Other words.", [])
        data = {"rows": [{"key": KEY}, {"key": OTHER}], "quote_rows": [],
                "transcript_index": {s["key"]: {"text_sha256": s["text_sha256"],
                                               "token_count": s["token_count"]} for s in (side, other)},
                "session": {"checklist": CHECKLIST}}
        session = write_session(tmp_path / "legacy", data, modes=MODES)
        for item in (side, other):
            write_sidecar(session.page, item)
        with serving([session]) as (_, root):
            base = root + "/s/t"
            def send(msg=None, raw=None):
                return post(base, msg, raw=raw)
            def read():
                status, body = get(base, "/api/answers")
                assert status == 200
                return json.loads(body)
            yield send, read, session.answers, side, other, None


def annotation(item, ranges):
    return {"schema_version": 1, "key": item["key"], "text_sha256": item["text_sha256"],
            "token_count": item["token_count"], "checked_by": "operator", "ranges": ranges}


@pytest.mark.parametrize("mode", ["service", "legacy"])
def test_large_answer_roundtrip_and_rejections_preserve_other_answers(tmp_path, mode):
    with backend(tmp_path, mode) as (send, read, path, item, other, client):
        # Establish unrelated labels, another recording's spans, and text corrections
        # using the same public save path that the owner uses.
        other_spans = annotation(other, [{"start": 0, "end": 1, "speaker": "other", "origin": "human"}])
        for msg in [
            {"kind": "labels", "id": OTHER, "value": {"present": True, "format": "solo"}},
            {"kind": "spans", "id": OTHER, "value": other_spans},
            {"kind": "corrections", "id": KEY, "value": {
                **annotation(item, []), "revision": 0,
                "edits": [{"start": 0, "end": 1, "original": "word", "replacement": "changed"}]}}
        ]:
            status, result = send(msg)
            assert status == 200, result
        before = read()
        ranges = [{"start": i, "end": i + 1, "speaker": "subject" if i % 2 else "other",
                   "origin": "human" if i % 2 else "model_confirmed",
                   **({"suggestions_sha256": "a" * 64, "suggestion_bundle_sha256": "b" * 64,
                       "suggestion_evidence_scope": "range"} if i % 2 == 0 else {})}
                  for i in range(item["token_count"])]
        value = annotation(item, ranges)
        msg = {"kind": "spans", "id": KEY, "value": value}
        size = len(json.dumps(msg).encode())
        assert SMALL_CAP < size < ANSWER_CAP
        status, result = send(msg)
        assert status == 200, result
        after = read()  # reload through a fresh HTTP request
        disk = json.loads(path.read_bytes())
        assert after["spans"][KEY] == disk["spans"][KEY]
        assert after["spans"][KEY]["ranges"] == ranges
        for section in ("labels", "corrections", "attribution"):
            assert after[section] == before[section]
        assert after["spans"][OTHER] == before["spans"][OTHER]
        saved = path.read_bytes()
        # A larger transport bound must not bypass semantic or strict JSON checks.
        bad = copy.deepcopy(msg)
        bad["value"]["ranges"][-1]["end"] += 1
        status, result = send(bad)
        assert status == 400 and "word offsets" in result["error"], result
        raw = json.dumps(msg).encode()[:-1] + b',"kind":"spans"}'
        status, result = send(raw=raw)
        assert status == 400 and "written twice" in result["error"], result
        assert path.read_bytes() == saved
        if client:
            # Restarting the store or an outside rewrite still refuses old page tokens.
            client.svc._stores.clear()
            status, result = send(msg)
            assert status == 409 and result["stale"] is True, result
            assert path.read_bytes() == saved


@pytest.mark.parametrize("mode", ["service", "legacy"])
def test_answer_size_boundary_and_oversize_refusal(tmp_path, mode):
    with backend(tmp_path, mode) as (send, read, path, item, other, client):
        msg = {"kind": "spans", "id": KEY, "value": annotation(item, [])}
        if client:
            msg["versions"] = versions(client)
        raw = json.dumps(msg).encode()
        raw += b" " * (ANSWER_CAP - len(raw))
        status, result = send(raw=raw)
        assert status == 200, result
        saved = path.read_bytes()
        for body in (raw + b" ", raw + raw):
            status, result = send(raw=body)
            assert status == 400, result
            assert result["error"] == f"body must be 1..{ANSWER_CAP} bytes, got {len(body)}"
            assert path.read_bytes() == saved


def test_small_routes_keep_their_limit_and_large_answers_need_a_session(tmp_path):
    with backend(tmp_path, "service") as (_, _, _, _, _, c):
        raw = b" " * (SMALL_CAP + 1)
        for route in ("/api/login", "/p/demo/call-1/api/note", "/p/demo/call-1/api/reveal",
                      "/p/demo/call-1/api/done"):
            status, result = c.json("POST", route, raw=raw)
            assert status == 400, result
            assert result["error"] == f"body must be 1..{SMALL_CAP} bytes, got {len(raw)}"
        status, result = c.json("POST", "/p/demo/call-1/api/answer", raw=raw, session="", bearer=True)
        assert status == 401 and "browser session" in result["error"], result
