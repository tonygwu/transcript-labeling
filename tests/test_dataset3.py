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
