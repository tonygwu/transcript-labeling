"""Version-3 datasets: configured speakers, a checked manifest, and the page's data block."""
from __future__ import annotations

import copy
import json
import os

import pytest

from transcript_labeling.dataset3 import CONFIG_KEYS, Dataset, manifest_errors, normalize_config
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
    ({"speakers": [{"id": "Ann", "label": "T", "key": "1"}]}, "must be a string matching"),
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
        notes_placeholder="anything odd",
        # verbatim's draft also lists a field no question asks (main_speaker), so names are not matched
        draft_fields=[["present", "subject present"], ["main_speaker", "main speaker"], ["venue", "format"]])
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


@pytest.mark.parametrize("extra, expect", [
    ({"draft_fields": "present"}, "`draft_fields` must be a list of"),
    ({"draft_fields": [["ok"]]}, "`draft_fields` must be a list of"),
    ({"draft_fields": [["ok", 1]]}, "`draft_fields` must be a list of"),
    ({"draft_fields": [["", "OK"]]}, "`draft_fields` must be a list of"),
    ({"draft_fields": [{"ok": "OK", "no": "No"}]}, "`draft_fields` must be a list of"),  # two keys, as a pair
    ({"draft_fields": None}, "`draft_fields` must be a list of"),
])
def test_the_draft_fields_the_page_shows_are_checked(extra, expect):
    with pytest.raises(SystemExit, match=expect):
        normalize_config(checklist_config(BOOL, **extra))
    with pytest.raises(SystemExit, match="'ok': `draft_label` must be a string"):
        normalize_config(checklist_config({**BOOL, "draft_label": ["ok"]}))
    assert "draft_fields" not in normalize_config(checklist_config(BOOL))["checklist"]  # optional
    assert normalize_config(checklist_config(BOOL, draft_fields=[]))["checklist"]["draft_fields"] == []


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


WRONG = [5, "x", None, True, 1.5, [], {}, [5], [None], ["x"], [[]], [{}], {"a": 5}]


@pytest.mark.parametrize("key", sorted(CONFIG_KEYS))
def test_a_config_value_of_the_wrong_type_is_not_a_crash(tmp_path, key):
    full = {"speakers": SPEAKERS, "modes": ["speakers", "captions", "checklist"], "title": "t",
            "checklist": {"fields": [{"name": "ok", "label": "OK", "type": "bool", "options": YES_NO}], **GOAL},
            "unclear": {"id": "unclear", "label": "U", "key": "u"}, "mixed_answer": "both", "reveal": "always",
            "label_template": "{person}", "project_help": {}, "files": {}, "tags": []}
    normalize_config(full)
    manifest = Dataset.create(tmp_path / "d", "demo", "c", {"speakers": SPEAKERS}).manifest
    for value in WRONG:
        try:
            normalize_config({**full, key: value})
        except SystemExit:
            pass  # a named refusal; any other exception fails the test
        assert isinstance(manifest_errors({**manifest, **full, key: value}), list)


@pytest.mark.parametrize("speaker", [{"id": 5, "label": "Five", "key": "5"}, {"id": None, "label": "N", "key": "5"}])
def test_a_speaker_id_must_be_a_string_not_converted_to_one(speaker):
    with pytest.raises(SystemExit, match="must be a string matching"):
        normalize_config({"speakers": [SPEAKERS[0], speaker]})
    with pytest.raises(SystemExit, match="must be a string matching"):
        normalize_config({"speakers": SPEAKERS, "unclear": {**speaker, "key": "u"}})


@pytest.mark.parametrize("text, expect", [
    ("[]", "is not a JSON object"), ("5", "is not a JSON object"), ("null", "is not a JSON object"),
    ('"x"', "is not a JSON object"), ('{"name": "c",\n<<<<<<< HEAD\n', "is not valid JSON"),
])
def test_opening_a_manifest_that_is_not_an_object_is_a_named_refusal(tmp_path, text, expect):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "manifest.json").write_text(text)
    with pytest.raises(SystemExit, match=f"manifest.json {expect}"):
        Dataset.open(tmp_path / "d")


@pytest.mark.parametrize("key", ["title", "modes", "speakers", "unclear", "mixed_answer", "label_template",
                                 "checklist", "reveal", "media", "project_help", "files"])
def test_a_manifest_missing_a_setting_is_refused_when_opened(tmp_path, key):
    ds = Dataset.create(tmp_path / "d", "demo", "c", {"speakers": SPEAKERS})
    assert manifest_errors(ds.manifest) == []
    manifest = {k: v for k, v in ds.manifest.items() if k != key}
    (tmp_path / "d" / "manifest.json").write_text(json.dumps(manifest))
    # `files` was refused before this check, with its own message.
    expect = "missing `files`" if key == "files" else rf"settings \['{key}'\] are missing; `tl` writes"
    with pytest.raises(SystemExit, match=expect):
        Dataset.open(tmp_path / "d")


def test_a_manifest_with_files_written_short_is_refused(tmp_path):
    ds = Dataset.create(tmp_path / "d", "demo", "c", {"speakers": SPEAKERS})
    assert any("['files'] are not written in full" in e for e in manifest_errors({**ds.manifest, "files": {}}))
    assert any("['files'] are not written in full" in e
               for e in manifest_errors({**ds.manifest, "files": {"answers": "human_answers.json"}}))


@pytest.mark.parametrize("data, expect", [
    (b"[]", "is not a JSON object"), (b"\xe9t\xe9", "is not UTF-8 text"),
    (b"[" * 100000 + b"]" * 100000, "is nested too deeply to read"),
    (b"{\n<<<<<<< HEAD\n", "is not valid JSON"),
    (b'{"rows": ' + b"9" * 5000 + b"}", "cannot be read as JSON"),
], ids=["list", "latin-1", "deep", "conflict", "huge-number"])
def test_dataset_files_that_cannot_be_read_as_an_object_are_named_refusals(tmp_path, data, expect):
    import sys
    # Python's default limit, so PYTHONINTMAXSTRDIGITS=0 cannot let the huge number through.
    old = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(4300)
    try:
        _check_unreadable(tmp_path, data, expect)
    finally:
        sys.set_int_max_str_digits(old)


def _check_unreadable(tmp_path, data, expect):
    ds = Dataset.create(tmp_path / "d", "demo", "c", {"speakers": SPEAKERS})
    add_items(ds, tmp_path / "state", [{"item": make_item("item/a", "Hello there, friend."), "row": {"title": "A"}}])
    for path, read in ((tmp_path / "d" / "manifest.json", lambda: Dataset.open(tmp_path / "d")),
                       (ds.file("catalog"), ds.catalog), (ds.item_path("item/a"), lambda: ds.item("item/a"))):
        good = path.read_bytes()
        path.write_bytes(data)
        with pytest.raises(SystemExit, match=rf"{path.name} ({expect})"):
            read()
        path.write_bytes(good)


def test_an_item_written_under_a_latin_1_locale_reads_back(tmp_path):
    # Item files are read as UTF-8, so they must be written as UTF-8 whatever the locale.
    import subprocess
    import sys
    code = ("import sys, pathlib\n"
            "from transcript_labeling.dataset3 import Dataset\n"
            "from transcript_labeling.items import add_items, make_item\n"
            "t = pathlib.Path(sys.argv[1])\n"
            "ds = Dataset.create(t / 'd', 'demo', 'c', {'speakers': [{'id': 'ann', 'label': 'Ann', 'key': '1'}]})\n"
            "add_items(ds, t / 'state', [{'item': make_item('item/a', 'Caf\\u00e9 au lait'), 'row': {'title': 'A'}}])\n"
            "print(sys.getfilesystemencoding(), ds.item('item/a')['text'] == 'Caf\\u00e9 au lait')\n")
    env = {**os.environ, "LC_ALL": "en_US.ISO8859-1", "PYTHONUTF8": "0", "PYTHONIOENCODING": "utf-8"}
    out = subprocess.run([sys.executable, "-c", code, str(tmp_path)], env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr[-500:]
    assert out.stdout.split()[-1] == "True"


@pytest.mark.parametrize("modes, key", [(["captions"], "modes"), (["checklist", "speakers"], "modes"),
                                        (["speakers"], "unclear")])
def test_a_missing_setting_is_named_even_when_its_default_would_clash(tmp_path, modes, key):
    # Each default clashes: speakers mode with no speakers, speakers mode with no
    # checklist mode beside a checklist, and the unclear key u that a speaker holds.
    speakers = [] if modes == ["captions"] else [{"id": "ann", "label": "Ann", "key": "u"}]
    config = {"speakers": speakers, "modes": modes, "unclear": {"id": "unsure", "label": "Unsure", "key": "x"}}
    if "checklist" in modes:
        config["checklist"] = {"fields": [{"name": "ok", "label": "OK", "type": "bool",
                                           "options": [{"value": True, "label": "Y", "key": "y"},
                                                       {"value": False, "label": "N", "key": "n"}]}],
                               "goal": {"count": 1, "help": "h"}, "accept_when_true": ["ok"]}
    ds = Dataset.create(tmp_path / "d", "demo", "c", config)
    assert manifest_errors({k: v for k, v in ds.manifest.items() if k != key}) == \
        [f"settings ['{key}'] are missing; `tl` writes every setting"]


def test_a_missing_setting_does_not_hide_a_broken_items_map(tmp_path):
    ds = Dataset.create(tmp_path / "d", "demo", "c", {"speakers": SPEAKERS})
    manifest = {**{k: v for k, v in ds.manifest.items() if k != "title"}, "items": {"a/b": 5}}
    assert manifest_errors(manifest) == ["settings ['title'] are missing; `tl` writes every setting",
                                         "items must map each key to {text_sha256, token_count}"]


def _checklist(key):
    return {"fields": [{"name": "present", "label": "P", "type": "bool", "required": True,
                        "options": [{"value": True, "label": "Yes", "key": key},
                                    {"value": False, "label": "No", "key": "n"}]}],
            "goal": {"count": 1, "help": "h"}, "accept_when_true": ["present"]}


@pytest.mark.parametrize("key", ["J", "k", "1", "u"])
def test_a_checklist_key_the_page_already_binds_is_refused(key):
    # J/K move between items, 1 and 2 mark speakers, u marks unclear: a checklist option
    # on any of them would make one key press do two things.
    with pytest.raises(SystemExit, match="already uses"):
        normalize_config({"speakers": SPEAKERS, "modes": ["checklist", "speakers"], "checklist": _checklist(key)})
    assert normalize_config({"speakers": SPEAKERS, "modes": ["checklist", "speakers"], "checklist": _checklist("y")})
