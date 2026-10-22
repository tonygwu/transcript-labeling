"""Checklist answers: complete labels pass, partial ones are counted, never filled.

Ported from verbatim-index `scripts/test_annotations_page.py` (VALID, EXCLUDED,
PARTIAL, FOREIGN and the to_labels part of DRAFTS) with a synthetic spec in place
of the pundits one.
"""
from __future__ import annotations

import pytest

from transcript_labeling.checklist import RESERVED_KEYS, answered, keep_assisted, to_labels

SPEC = {"fields": [
    {"name": "present", "label": "Subject present", "type": "bool", "required": True,
     "options": [{"value": True, "label": "Yes", "key": "y"}, {"value": False, "label": "No", "key": "n"}]},
    {"name": "format", "label": "Format", "type": "enum", "required": True,
     "options": [{"value": v, "label": v, "key": str(i + 1)} for i, v in enumerate(("solo", "conversation"))]},
    {"name": "topic", "label": "Topic", "type": "text", "required": False},
]}
KEYS = {"p/a", "p/b", "p/c", "p/d"}
DOCS = [
    {"key": "p/a", "present": True, "format": "conversation", "checked_by": "operator"},
    {"key": "p/b", "present": False, "format": "solo", "checked_by": "operator", "notes": "guest only"},
    {"key": "p/c", "present": True, "format": "solo", "topic": "  "},
    {"key": "p/d", "present": True},
]


def test_complete_labels_carry_every_field_checker_and_notes():
    res = to_labels(DOCS, KEYS, SPEC)
    assert res["labels"]["p/a"] == {"present": True, "format": "conversation", "topic": None,
                                    "checked_by": "operator", "notes": ""}
    assert res["labels"]["p/b"]["present"] is False and res["labels"]["p/b"]["notes"] == "guest only"
    assert res["labels"]["p/c"]["checked_by"] == "operator"


def test_a_partial_label_is_left_out_and_counted():
    res = to_labels(DOCS, KEYS, SPEC)
    assert "p/d" not in res["labels"]
    assert res["partial"] == ["p/d"]
    assert to_labels(DOCS[:1], KEYS, SPEC)["unlabelled"] == ["p/b", "p/c", "p/d"]


def test_a_label_for_an_item_not_on_the_checklist_stops_the_import():
    with pytest.raises(SystemExit, match="not on the checklist"):
        to_labels(DOCS + [{"key": "q/zz", "present": True, "format": "solo"}], KEYS, SPEC)


def test_draft_shown_and_blind_answers_survive_the_import():
    before = {"present": True, "format": "conversation"}
    lab = to_labels([{"key": "p/a", "present": False, "format": "solo", "draft_shown": "model-x",
                      "answers_before_draft": before}], KEYS, SPEC)["labels"]["p/a"]
    assert lab["draft_shown"] == "model-x" and lab["answers_before_draft"] == before
    lab0 = to_labels([{"key": "p/a", "present": True, "format": "solo"}], KEYS, SPEC)["labels"]["p/a"]
    assert "draft_shown" not in lab0 and "answers_before_draft" not in lab0


@pytest.mark.parametrize("doc", [
    {"draft_shown": "m", "answers_before_draft": {"present": True}},
    {"draft_shown": "m", "answers_before_draft": {"present": True, "format": "x"}},
    {"answers_before_draft": {"present": True, "format": "solo"}},
])
def test_bad_blind_answers_stop_the_import(doc):
    with pytest.raises(SystemExit, match="answers_before_draft"):
        to_labels([{"key": "p/a", "present": True, "format": "solo", **doc}], KEYS, SPEC)


def test_answered_by_type():
    b, e, t = SPEC["fields"]
    assert answered(b, False) and not answered(b, 0) and not answered(b, None)
    assert answered(e, "solo") and not answered(e, "debate")
    assert answered(t, "x") and not answered(t, " ") and not answered(t, 3)


def test_keep_assisted_never_drops_the_sticky_fields():
    stored = {"draft_shown": "m", "answers_before_draft": {"present": True}}
    assert keep_assisted({"present": False}, stored) == {"present": False, **stored}
    assert keep_assisted({"present": False, "answers_before_draft": {"x": 1}}, {"draft_shown": "m"}) == \
        {"present": False, "draft_shown": "m"}
    assert keep_assisted({"present": False}, None) == {"present": False}
    # A later copy of the blind answers, from a second tab, never replaces the first.
    later = keep_assisted({"present": False, "answers_before_draft": {"present": False}}, stored)
    assert later["answers_before_draft"] == {"present": True}


def test_reserved_keys_are_the_page_bindings():
    assert RESERVED_KEYS == {"j", "k", "v", "[", "]", "arrowup", "arrowdown"}
