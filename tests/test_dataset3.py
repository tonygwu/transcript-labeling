"""Version-3 datasets: configured speakers, a checked manifest, and the page's data block."""
from __future__ import annotations

import copy
import json

import pytest

from transcript_labeling.dataset3 import Dataset, manifest_errors, normalize_config
from transcript_labeling.items import add_items, make_item
from transcript_labeling.spans import quote_review, validate_annotation
from transcript_labeling.tokens import token_kinds

SPEAKERS = [{"id": "ann", "label": "Ann", "key": "1"}, {"id": "bo", "label": "Bo", "key": "2"}]
VERBATIM = {"speakers": [{"id": "subject", "label": "{person}", "key": "1"},
                         {"id": "other", "label": "Someone else", "key": "2"}],
            "unclear": {"id": "unclear", "label": "Unsure", "key": "3"}, "mixed_answer": "both",
            "label_template": "{person}"}


def test_defaults_are_filled_in():
    cfg = normalize_config({"speakers": SPEAKERS})
    assert cfg["modes"] == ["speakers", "captions"] and cfg["mixed_answer"] == "mixed"
    assert cfg["unclear"] == {"id": "unclear", "label": "Unsure", "key": "u"} and cfg["reveal"] == "after_answer"
    assert cfg["files"] == {"answers": "human_answers.json", "items_dir": "items", "catalog": "catalog.json",
                            "review": "review.json"}


@pytest.mark.parametrize("config,expect", [
    ({}, "needs `speakers`"),
    ({"speakers": SPEAKERS, "bogus": 1}, "unknown dataset config keys"),
    ({"speakers": [{"id": "Ann", "label": "T", "key": "1"}]}, "must match"),
    ({"speakers": [{"id": "a", "label": "A", "key": "j"}]}, "must be one character the page does not"),
    ({"speakers": [{"id": "a", "label": "A", "key": "1"}, {"id": "a", "label": "B", "key": "2"}]}, "unique"),
    ({"speakers": [{"id": "a", "label": "A", "key": "1"}, {"id": "b", "label": "B", "key": "1"}]}, "unique"),
    ({"speakers": [{"id": "a", "label": "A", "key": "1"}, {"id": "unclear", "label": "B", "key": "2"}]}, "unique"),
    ({"speakers": [{"id": "a", "label": "", "key": "1"}]}, "needs a label"),
    ({"speakers": SPEAKERS, "mixed_answer": "ann"}, "not be a speaker id"),
    ({"speakers": SPEAKERS, "mixed_answer": ""}, "must match"),
    ({"speakers": SPEAKERS, "files": {"answers": "catalog.json"}}, "four different files"),
    ({"speakers": SPEAKERS, "files": {"catalog": "manifest.json"}}, "four different files"),
    ({"speakers": SPEAKERS, "checklist": "yes"}, "checklist must be an object"),
    ({"speakers": SPEAKERS, "files": "x"}, "wrong type"),
    ({"speakers": SPEAKERS, "tags": "alpha"}, "wrong type"),
    ({"speakers": SPEAKERS, "tags": ["Alpha"]}, "tags"),
    ({"speakers": SPEAKERS, "project_help": {"a": 1}}, "project_help"),
    ({"speakers": SPEAKERS, "modes": ["checklist"]}, "go together"),
    ({"speakers": SPEAKERS, "modes": ["painting"]}, "subset of"),
    ({"speakers": SPEAKERS, "reveal": "never"}, "reveal"),
    ({"speakers": SPEAKERS, "media": {"url": "http://x"}}, "media"),
    ({"speakers": SPEAKERS, "files": {"answers": "../a.json"}}, "inside the dataset folder"),
    ({"speakers": SPEAKERS, "files": {"answers": "sub/a.json"}}, "inside the dataset folder"),
    ({"speakers": SPEAKERS, "files": {"nope": "x"}}, "may name only"),
])
def test_a_config_that_cannot_work_is_refused(config, expect):
    with pytest.raises(SystemExit, match=expect):
        normalize_config(config)


def test_a_nested_items_dir_is_allowed_for_version_2_sidecars():
    cfg = normalize_config({"speakers": SPEAKERS, "files": {"items_dir": "speaker_check.transcripts"}})
    assert cfg["files"]["items_dir"] == "speaker_check.transcripts"


def test_create_open_and_refuse_twice(tmp_path):
    ds = Dataset.create(tmp_path / "d", "demo", "call-1", {"speakers": SPEAKERS, "title": "Call 1"})
    again = Dataset.open(tmp_path / "d")
    assert again.manifest == ds.manifest and again.manifest["created_with"]
    assert ds.catalog() == {"rows": [], "quote_rows": []}
    assert ds.speaker_ids() == ["ann", "bo", "unclear"]
    assert ds.sections() == {"spans", "corrections"}
    with pytest.raises(SystemExit, match="already has"):
        Dataset.create(tmp_path / "d", "demo", "call-1", {"speakers": SPEAKERS})
    with pytest.raises(SystemExit, match="has no manifest"):
        Dataset.open(tmp_path / "none")


def test_a_version_2_manifest_points_at_adoption(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"schema_version": 2}))
    with pytest.raises(SystemExit, match="tl dataset adopt"):
        Dataset.open(tmp_path)


def test_manifest_errors_catch_a_broken_items_map_and_bad_names(tmp_path):
    ds = Dataset.create(tmp_path / "d", "demo", "c", {"speakers": SPEAKERS})
    m = copy.deepcopy(ds.manifest)
    m["items"] = {"a/b": {"text_sha256": "x"}}
    assert any("items must map" in e for e in manifest_errors(m))
    for k, bad in (("name", "My Set"), ("project", "a/b")):
        assert any(f"{k} {bad!r} must match" in e for e in manifest_errors({**ds.manifest, k: bad}))


def test_a_missing_catalog_is_refused_not_read_as_empty(tmp_path):
    ds = Dataset.create(tmp_path / "d", "demo", "c", {"speakers": SPEAKERS})
    (ds.root / "catalog.json").unlink()
    with pytest.raises(SystemExit, match="catalog.json is missing"):
        ds.catalog()
    assert manifest_errors({"schema_version": 3, "tool": "other"})[0].startswith("not a version-3")


def test_item_path_refuses_escaping_keys(tmp_path):
    ds = Dataset.create(tmp_path / "d", "demo", "c", {"speakers": SPEAKERS})
    assert ds.item_path("call/one") == tmp_path / "d" / "items" / "call" / "one.json"
    for bad in ("../x", "a/b/c", "a/../b", "/abs"):
        with pytest.raises(ValueError):
            ds.item_path(bad)


def test_page_data_carries_the_speakers_and_the_items(tmp_path):
    ds = Dataset.create(tmp_path / "d", "demo", "c", {"speakers": SPEAKERS, "title": "T"})
    add_items(ds, tmp_path / "state", [{"item": make_item("call/one", "hello there friend"), "row": {"title": "One"}}])
    data = Dataset.open(ds.root).page_data()
    assert data["session"]["speakers"] == SPEAKERS and data["session"]["projects"] == ["demo"]
    assert data["session"]["title"] == "T" and data["session"]["checklist"] is None
    assert data["transcript_index"]["call/one"]["token_count"] == 3
    assert data["rows"][0]["title"] == "One" and data["rows"][0]["person"] == ""


def test_configured_speakers_drive_validation_and_results():
    item = make_item("call/one", "one two three four")
    value = {"key": "call/one", "text_sha256": item["text_sha256"], "token_count": 4, "checked_by": "o",
             "ranges": [{"start": 0, "end": 2, "speaker": "ann", "origin": "human"},
                        {"start": 2, "end": 4, "speaker": "bo", "origin": "human"}]}
    with pytest.raises(ValueError):
        validate_annotation(value, item)  # version-2 speakers by default
    ids = ["ann", "bo", "unclear"]
    validate_annotation(value, item, ids)
    q = {"quote_start": 0, "quote_end": 4}
    assert quote_review(q, item, value, speakers=ids, mixed="mixed")["answer"] == "mixed"
    value["ranges"][1]["speaker"] = "unclear"
    assert quote_review(q, item, value, speakers=ids, mixed="mixed")["answer"] == "unclear"


def test_meta_ranges_are_never_speech():
    toks = "00:00:01 --> 00:00:05 Bo: hello there".split()
    assert token_kinds(toks, [[0, 4]]) == ["meta"] * 4 + ["speech", "speech"]
    with pytest.raises(ValueError, match="outside"):
        token_kinds(toks, [[4, 9]])


YES_NO = [{"value": True, "label": "Yes", "key": "y", "cls": "y"}, {"value": False, "label": "No", "key": "n"}]
GOAL = {"goal": {"count": 1, "help": "items verified"}, "accept_when_true": []}


def checklist_config(*fields, **extra):
    checklist = {"fields": list(fields), **GOAL, **extra}
    return {"speakers": SPEAKERS, "modes": ["speakers", "checklist"], "checklist": checklist}


def test_a_checklist_shaped_like_verbatims_is_accepted_unchanged():
    cfg = checklist_config(
        {"name": "present", "label": "Present", "help": "takes part live", "type": "bool", "required": True,
         "draft_label": "present", "options": YES_NO},
        {"name": "venue", "label": "Format", "help": "of this recording", "type": "enum", "required": True,
         "differs_only_if": "present", "prompt": "Pick one.",
         "options": [{"value": v, "label": v, "key": str(i + 3), "help": "h"} for i, v in enumerate(["talk", "panel"])]},
        goal={"count": 5, "help": "recordings verified so far"}, accept_when_true=["present"],
        notes_placeholder="anything odd")
    assert normalize_config(cfg)["checklist"] == cfg["checklist"]


def opts(*values, keys="abcd"):
    return [{"value": v, "label": str(v), "key": k} for v, k in zip(values, keys)]


@pytest.mark.parametrize("field, expect", [
    ({"label": "x", "type": "bool", "options": YES_NO}, "needs a name"),
    ({"name": "a", "type": "bool", "options": YES_NO}, "'a' needs a label"),
    ({"name": "a", "label": "A", "type": "yesno", "options": YES_NO}, "has type 'yesno'"),
    ({"name": "a", "label": "A", "type": "text", "required": True}, "has type 'text'; the page answers only"),
    ({"name": "a", "label": "A", "type": "bool", "required": "yes", "options": YES_NO},
     "`required` must be true or false"),
    ({"name": "a", "label": "A", "type": "enum", "options": "ab"}, "must be a non-empty list of objects"),
    ({"name": "a", "label": "A", "type": "enum", "options": ["a", "b"]}, "must be a non-empty list of objects"),
    ({"name": "a", "label": "A", "type": "bool"}, "must be a non-empty list of objects"),
    ({"name": "a", "label": "A", "type": "enum", "options": [{"value": 1, "label": "One"}]},
     "a value, a label and a one-character key"),
    ({"name": "a", "label": "A", "type": "enum", "options": [{"label": "One", "key": "o"}]},
     "a value, a label and a one-character key"),
    ({"name": "a", "label": "A", "type": "enum", "options": opts(1, keys=["yes"])}, "one-character key"),
    ({"name": "a", "label": "A", "type": "enum", "options": opts(1, keys=[" "])}, "one-character key"),
    ({"name": "a", "label": "A", "type": "bool", "options": opts("yes", "no")}, "value must be true or false"),
    ({"name": "a", "label": "A", "type": "enum", "options": opts(1, keys="j")}, "already uses"),
    ({"name": "a", "label": "A", "type": "enum", "options": opts(1, keys="1")}, "already uses"),
    *[({"name": n, "label": "A", "type": "bool", "options": YES_NO}, "a saved label already uses")
      for n in ("notes", "checked_by", "key", "updated_at", "draft_shown", "answers_before_draft")],
])
def test_a_checklist_field_the_page_cannot_use_is_refused(field, expect):
    with pytest.raises(SystemExit, match=expect):
        normalize_config(checklist_config(field))


BOOL = {"name": "ok", "label": "OK", "type": "bool", "options": YES_NO}


@pytest.mark.parametrize("extra, expect", [
    ({"goal": None}, "needs `goal`"),
    ({"goal": {"count": "5", "help": "h"}}, "needs `goal`"),
    ({"goal": {"count": True, "help": "h"}}, "needs `goal`"),
    ({"goal": {"count": -1, "help": "h"}}, "needs `goal`"),
    ({"goal": {"count": 5}}, "needs `goal`"),
    ({"accept_when_true": None}, "needs `accept_when_true`"),
    ({"accept_when_true": ["other"]}, "needs `accept_when_true`"),
    ({"accept_when_true": "ok"}, "needs `accept_when_true`"),
])
def test_the_checklist_goal_and_acceptance_the_page_reads_are_checked(extra, expect):
    with pytest.raises(SystemExit, match=expect):
        normalize_config(checklist_config(BOOL, **extra))
    enum = {"name": "pick", "label": "Pick", "type": "enum", "options": opts(1, 2)}
    with pytest.raises(SystemExit, match="needs `accept_when_true`"):  # an enum field is never true
        normalize_config(checklist_config(BOOL, enum, accept_when_true=["pick"]))


def test_checklist_names_and_keys_are_unique_across_fields_and_there_is_one_field():
    with pytest.raises(SystemExit, match="needs a name of its own"):
        normalize_config(checklist_config(BOOL, BOOL))
    with pytest.raises(SystemExit, match="'Y' is one the page already uses"):
        normalize_config(checklist_config({**BOOL, "name": "a"},
                                          {**BOOL, "name": "b", "options": [{"value": True, "label": "Y", "key": "Y"}]}))
    with pytest.raises(SystemExit, match="at least one field"):
        normalize_config(checklist_config())


def test_a_malformed_checklist_in_a_manifest_is_a_named_error_not_a_crash(tmp_path):
    ds = Dataset.create(tmp_path / "d", "demo", "c", {"speakers": SPEAKERS})
    manifest = {**ds.manifest, "modes": ["speakers", "checklist"],
                "checklist": {"fields": [{"name": "a", "label": "A", "type": "enum", "options": "ab"}], **GOAL}}
    assert any("must be a non-empty list of objects" in e for e in manifest_errors(manifest))
