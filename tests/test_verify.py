"""verify proves a session's answers are intact, fails loud on each kind of damage, and never writes.

Ported from verbatim-index `scripts/test_annotations_cli.py` (VERIFY).
"""
from __future__ import annotations

import hashlib
import json

import pytest

from conftest import KEY, QID
from transcript_labeling.dataset import write_manifest
from transcript_labeling.verify import sidecar_digest, verify_session


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def failed(report):
    return [c["check"] for c in report["checks"] if not c["ok"]]


def test_verify_passes_on_a_sound_session_every_check(quote_session):
    rep = verify_session(quote_session, expect_answers_sha256=sha(quote_session.answers))
    assert rep["ok"], failed(rep)
    assert any("server's answers equal" in c["check"] and c["ok"] for c in rep["checks"])
    assert any("byte for byte" in c["check"] and c["ok"] for c in rep["checks"])
    assert rep["counts"]["spans_ranges"] == 1 and rep["counts"]["page_quotes"] == 1


def test_verify_fails_on_a_wrong_sha(quote_session):
    rep = verify_session(quote_session, expect_answers_sha256="0" * 64)
    assert not rep["ok"] and failed(rep) == ["the answers file has the expected sha256"]


def mutate_answers(session, change):
    d = json.loads(session.answers.read_text())
    change(d)
    session.answers.write_text(json.dumps(d, indent=1))


@pytest.mark.parametrize("label,change,expect", [
    ("a speaker range the transcript cannot hold",
     lambda d: d["spans"][KEY]["ranges"][0].update(end=999),
     "every speaker annotation validates against its transcript"),
    ("an answer for a quote the page does not carry",
     lambda d: d["attribution"].update({"elsewhere:x:1": {"answer": "subject"}}),
     "every saved answer names an id the page carries"),
    ("a stored quote result the ranges contradict",
     lambda d: d["attribution"][QID].update(answer="other"),
     "a read returns the answers file unchanged (quote results already follow the ranges)"),
])
def test_verify_fails_on_damaged_answers_and_never_writes(quote_session, label, change, expect):
    mutate_answers(quote_session, change)
    before = sha(quote_session.answers)
    rep = verify_session(quote_session)
    assert not rep["ok"] and expect in failed(rep), (label, failed(rep))
    assert sha(quote_session.answers) == before


def test_verify_fails_on_a_sidecar_whose_hash_moved(quote_session):
    side = quote_session.sidecar(KEY)
    side.write_text(json.dumps({**json.loads(side.read_text()), "text_sha256": "0" * 64}))
    assert "every recording's sidecar matches the page's transcript hash and token count" in \
        failed(verify_session(quote_session))


def with_exports(session, exports):
    write_manifest(session.root, {**session.manifest, "files": {**session.manifest["files"], "exports": exports}})
    return type(session).open(session.root)


def test_verify_reproduces_listed_exports_and_fails_when_one_drifts(quote_session):
    from transcript_labeling.export import export_spans
    export_spans(quote_session, quote_session.root / "spans.json")
    s = with_exports(quote_session, {"spans": "spans.json"})
    assert verify_session(s)["ok"]
    d = json.loads((s.root / "spans.json").read_text())
    d["note"] = "edited"
    (s.root / "spans.json").write_text(json.dumps(d))
    assert failed(verify_session(s)) == ["export spans reproduces spans.json"]


def test_an_export_kind_with_no_exporter_fails_and_a_client_exporter_is_used(quote_session):
    (quote_session.root / "q.json").write_text('{"x": 1}')
    s = with_exports(quote_session, {"quotes": "q.json"})
    rep = verify_session(s)
    assert failed(rep) == ["export quotes reproduces q.json"]
    assert "no exporter" in next(c["detail"] for c in rep["checks"] if not c["ok"])
    assert verify_session(s, exporters={"quotes": lambda session: {"x": 1}})["ok"]


def test_sidecar_digest_matches_verbatims_formula(quote_session):
    # verbatim-index annotations.py (d5a3a8bf) computes the digest exactly like this;
    # the value below was computed with that code on this fixture.
    assert sidecar_digest(quote_session) == "eb804f37327e23e0674ffddea3541bb9c5a1ca7fe529a269828ffc9138a359a8"


def test_a_missing_export_file_or_a_broken_exporter_is_a_failed_check_not_a_crash(quote_session):
    s = with_exports(quote_session, {"spans": "gone.json"})
    rep = verify_session(s)
    assert failed(rep) == ["export spans reproduces gone.json"]
    (quote_session.root / "q.json").write_text("{}")
    s = with_exports(quote_session, {"quotes": "q.json"})

    def broken(session):
        raise KeyError("x")
    rep = verify_session(s, exporters={"quotes": broken})
    assert failed(rep) == ["export quotes reproduces q.json"]
    assert "KeyError" in next(c["detail"] for c in rep["checks"] if not c["ok"])


def test_an_empty_expected_hash_is_checked_not_skipped(quote_session):
    rep = verify_session(quote_session, expect_answers_sha256="")
    assert failed(rep) == ["the answers file has the expected sha256"]
