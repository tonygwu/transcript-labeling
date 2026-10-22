"""Exports read the answers and the sidecars and never write either."""
from __future__ import annotations

import hashlib
import json

import pytest

from conftest import KEY
from transcript_labeling.corrections import update_corrections
from transcript_labeling.export import export_caption_corrections, export_labels, export_spans, label_docs
from transcript_labeling.testing import write_session

CHECKLIST = {"fields": [{"name": "present", "label": "Present", "type": "bool", "required": True,
                         "options": [{"value": True, "label": "Yes", "key": "y"}]}]}


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_spans_and_corrections_exports_leave_answers_and_sidecars_alone(tmp_path, quote_session):
    side = json.loads(quote_session.sidecar(KEY).read_text())
    answers = json.loads(quote_session.answers.read_text())
    value = {"key": KEY, "text_sha256": side["text_sha256"], "token_count": side["token_count"], "revision": 0,
             "checked_by": "operator", "edits": [{"start": 1, "end": 2, "original": "Welcome", "replacement": "Hello"}]}
    answers["corrections"][KEY] = update_corrections(value, None, side)
    quote_session.answers.write_text(json.dumps(answers))
    before = (sha(quote_session.answers), sha(quote_session.sidecar(KEY)))
    spans = export_spans(quote_session, tmp_path / "spans.json")
    assert spans["recordings"][KEY]["ranges"][0]["speech_text"] == "I think the grid will run out"
    fixes = export_caption_corrections(quote_session, tmp_path / "c.json")
    assert fixes["recordings"][KEY]["corrected_text"].startswith("[00:00:05] Hello to the show")
    assert (sha(quote_session.answers), sha(quote_session.sidecar(KEY))) == before


def test_labels_export_uses_the_pages_checklist(tmp_path):
    data = {"rows": [{"key": "a/x"}, {"key": "a/y"}], "transcript_index": {},
            "session": {"checklist": CHECKLIST}}
    s = write_session(tmp_path / "s", data, modes=("checklist",))
    s.answers.write_text(json.dumps({"labels": {"a/x": {"present": True}, "a/y": {}}, "attribution": {}}))
    res = export_labels(s)
    assert res["labels"] == {"a/x": {"present": True, "checked_by": "operator", "notes": ""}}
    assert res["partial"] == ["a/y"]


def test_labels_export_refuses_a_session_without_a_checklist(quote_session):
    with pytest.raises(SystemExit, match="no checklist mode"):
        export_labels(quote_session)


def test_label_docs_carry_their_key_and_a_stored_key_wins():
    assert label_docs({"a/x": {"present": True}}) == [{"present": True, "key": "a/x"}]
    assert label_docs({"a/x": {"key": "a/y"}}) == [{"key": "a/y"}]
