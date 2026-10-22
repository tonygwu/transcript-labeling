"""Items: validated as a batch, written through staging, never moved under saved answers."""
from __future__ import annotations

import json

import pytest

from transcript_labeling.dataset3 import Dataset
from transcript_labeling.items import add_items, catalog_row, focus_passage, item_errors, make_item, suggestion

SPEAKERS = [{"id": "ann", "label": "Ann", "key": "1"}, {"id": "bo", "label": "Bo", "key": "2"}]
TEXT = "[00:00:10] Hello and welcome back everyone. Today we talk about the bridge plan in some detail."


@pytest.fixture
def ds(tmp_path):
    return Dataset.create(tmp_path / "d", "demo", "c", {"speakers": SPEAKERS})


def entry(key="call/one", text=TEXT, **kw):
    focus = kw.pop("focus", None)
    return {"item": make_item(key, text, **kw), "row": {"title": key.split("/")[1], "person": "Ann Able"},
            "focus": focus}


def test_make_item_is_a_sidecar_with_its_text():
    item = make_item("call/one", TEXT, segments=[{"header": [0, 1], "body": [1, 16]}], meta=[[0, 1]],
                     source={"name": "x.txt", "format": "plain"})
    assert item["text"] == TEXT and item["token_count"] == 16 and item["times"] == [[0, 10]]
    assert item["meta"] == [[0, 1]] and item["segments"][0]["body"] == [1, 16]
    assert item_errors(item, ["ann", "bo", "unclear"]) == []


@pytest.mark.parametrize("change,expect", [
    (lambda i: i.update(text=i["text"] + " extra"), "whitespace tokens"),
    (lambda i: i.update(text_sha256="0" * 64), "sha256"),
    (lambda i: i.update(token_count=3), "disagree"),
    (lambda i: i.update(suggestions=[suggestion(0, 99, "ann")]), "outside the text"),
    (lambda i: i.update(suggestions=[suggestion(1, 2, "nobody")]), "is not one of"),
    (lambda i: i.update(meta=[[10, 99]]), "outside"),
    (lambda i: i.update(segments=[{"body": [0, 99]}]), "outside the text"),
])
def test_item_errors_name_each_fault(change, expect):
    item = make_item("call/one", TEXT)
    change(item)
    assert any(expect in e for e in item_errors(item, ["ann", "bo", "unclear"])), item_errors(item, ["ann"])


def test_focus_passage_from_tokens_and_from_characters():
    item = make_item("call/one", TEXT)
    by_tok = focus_passage(item, {"start": 6, "end": 8, "title": "T", "why": "W", "question": "Q?"}, 0)
    assert (by_tok["quote_start"], by_tok["quote_end"], by_tok["text"]) == (6, 8, "Today we")
    assert by_tok["start"] == 0 and by_tok["end"] == 16 and by_tok["qid"] == "call:one:6"
    assert by_tok["title"] == "T" and by_tok["why"] == "W" and by_tok["question"] == "Q?"
    assert by_tok["t"] == 5  # 10 s mark, a few words in, less the 5 s lead
    at = TEXT.index("bridge")
    by_char = focus_passage(item, {"char_start": at, "char_end": at + len("bridge plan"), "id": "p1",
                                   "context": [10, 14]}, 1)
    assert (by_char["quote_start"], by_char["quote_end"], by_char["qid"]) == (11, 13, "p1")
    assert (by_char["start"], by_char["end"]) == (10, 14)
    for bad in ({"start": 5, "end": 99}, {"start": 3, "end": 3}, {"start": 5, "end": 6, "context": [6, 9]}):
        with pytest.raises(SystemExit, match="REFUSING"):
            focus_passage(item, bad, 2)


def test_a_passage_by_characters_needs_the_text():
    item = make_item("call/one", TEXT)
    del item["text"]
    with pytest.raises(SystemExit, match="keeps no text"):
        focus_passage(item, {"char_start": 0, "char_end": 4}, 0)


def test_catalog_row_defaults():
    assert catalog_row("call/one", {}) == {"key": "call/one", "slug": "call", "sid": "one", "title": "one",
                                           "person": ""}
    assert catalog_row("call/one", {"title": "T", "duration": 60})["duration"] == 60
    with pytest.raises(SystemExit, match="unknown row fields"):
        catalog_row("call/one", {"nope": 1})


def test_add_items_writes_items_manifest_and_catalog(ds, tmp_path):
    res = add_items(ds, tmp_path / "state", [entry(focus=[{"start": 6, "end": 8}]), entry("call/two", "a b c")])
    assert res == {"items": 2, "passages": 1, "total_items": 2}
    ds2 = Dataset.open(ds.root)
    assert set(ds2.manifest["items"]) == {"call/one", "call/two"}
    assert ds2.item("call/two")["tokens"] == ["a", "b", "c"]
    cat = ds2.catalog()
    assert [r["key"] for r in cat["rows"]] == ["call/one", "call/two"]
    assert cat["quote_rows"][0]["quotes"][0]["qid"] == "call:one:6"
    assert not any(p.name.startswith("staging-") for p in (tmp_path / "state").iterdir())


def test_a_bad_batch_writes_nothing(ds, tmp_path):
    good, bad = entry("call/one"), entry("call/two", "a b c")
    bad["item"]["token_count"] = 9
    before = (ds.root / "manifest.json").read_bytes()
    with pytest.raises(SystemExit, match="nothing was written"):
        add_items(ds, tmp_path / "state", [good, bad])
    assert (ds.root / "manifest.json").read_bytes() == before
    assert not (ds.items_dir / "call" / "one.json").exists()
    with pytest.raises(SystemExit, match="names a key twice"):
        add_items(ds, tmp_path / "state", [entry(), entry()])
    with pytest.raises(ValueError):
        add_items(ds, tmp_path / "state", [entry("../escape")])


def test_an_existing_item_needs_upsert_and_its_text_cannot_move_under_answers(ds, tmp_path):
    state = tmp_path / "state"
    add_items(ds, state, [entry(focus=[{"start": 6, "end": 8}])])
    with pytest.raises(SystemExit, match="pass --upsert"):
        add_items(ds, state, [entry()])
    # No answers yet: a new text is allowed.
    add_items(ds, state, [entry(text=TEXT + " More.", focus=[{"start": 6, "end": 8}])], upsert=True)
    ds = Dataset.open(ds.root)
    item = ds.item("call/one")
    ds.answers.write_text(json.dumps({"labels": {}, "attribution": {"call:one:6": {"answer": "ann"}},
                                      "spans": {"call/one": {"key": "call/one", "text_sha256": item["text_sha256"],
                                                             "token_count": item["token_count"], "ranges": [],
                                                             "checked_by": "o"}}}))
    with pytest.raises(SystemExit, match="text changed under saved answers"):
        add_items(ds, state, [entry(text=TEXT)], upsert=True)
    with pytest.raises(SystemExit, match="would cover other words"):
        add_items(ds, state, [entry(text=TEXT + " More.", focus=[{"start": 6, "end": 9, "id": "call:one:6"}])],
                  upsert=True)
    add_items(ds, state, [entry(text=TEXT + " More.", focus=[{"start": 6, "end": 8}])], upsert=True)
    with pytest.raises(SystemExit, match="would drop answered passages"):
        add_items(ds, state, [entry(text=TEXT + " More.")], upsert=True)


def test_a_stale_dataset_object_cannot_bypass_the_guard(ds, tmp_path):
    state = tmp_path / "state"
    stale_a, stale_b = Dataset.open(ds.root), Dataset.open(ds.root)
    add_items(ds, state, [entry("call/x", "first version of the words")])
    item = Dataset.open(ds.root).item("call/x")
    ds.answers.write_text(json.dumps({"labels": {}, "attribution": {}, "spans": {"call/x": {
        "key": "call/x", "text_sha256": item["text_sha256"], "token_count": 5, "ranges": [], "checked_by": "o"}}}))
    add_items(stale_a, state, [entry("call/y", "a b c")])
    assert set(Dataset.open(ds.root).manifest["items"]) == {"call/x", "call/y"}, "an item fell out of the manifest"
    with pytest.raises(SystemExit, match="pass --upsert"):
        add_items(stale_b, state, [entry("call/x", "second text")])
    with pytest.raises(SystemExit, match="text changed under saved answers"):
        add_items(stale_b, state, [entry("call/x", "second text")], upsert=True)


def test_passage_ids_are_unique_across_items(ds, tmp_path):
    state = tmp_path / "state"
    add_items(ds, state, [entry("call/x", focus=[{"start": 1, "end": 3, "id": "p1"}])])
    with pytest.raises(SystemExit, match="already belongs to call/x"):
        add_items(ds, state, [entry("call/y", focus=[{"start": 1, "end": 3, "id": "p1"}])])
    with pytest.raises(SystemExit, match="is used twice"):
        add_items(ds, state, [entry("call/z", focus=[{"start": 1, "end": 3, "id": "q"},
                                                     {"start": 4, "end": 6, "id": "q"}])])


def test_header_ranges_cannot_move_under_answers_and_an_upsert_keeps_the_row(ds, tmp_path):
    state = tmp_path / "state"
    add_items(ds, state, [entry("call/x", "h1 h2 a b c", meta=[[0, 2]])])
    item = Dataset.open(ds.root).item("call/x")
    ds.answers.write_text(json.dumps({"labels": {}, "attribution": {}, "spans": {"call/x": {
        "key": "call/x", "text_sha256": item["text_sha256"], "token_count": 5, "ranges": [], "checked_by": "o"}}}))
    with pytest.raises(SystemExit, match="header ranges changed"):
        add_items(ds, state, [entry("call/x", "h1 h2 a b c", meta=[[0, 1]])], upsert=True)
    keep = {"item": make_item("call/x", "h1 h2 a b c", meta=[[0, 2]]), "row": None, "focus": None}
    add_items(ds, state, [keep], upsert=True)
    assert Dataset.open(ds.root).catalog()["rows"][0]["person"] == "Ann Able"


def test_a_folder_of_version_2_sidecars_is_never_rewritten(tmp_path):
    legacy = Dataset.create(tmp_path / "v2", "demo", "c",
                            {"speakers": SPEAKERS, "files": {"items_dir": "speaker_check.transcripts"}})
    with pytest.raises(SystemExit, match="cannot be rewritten here"):
        add_items(legacy, tmp_path / "state", [entry()])


def test_a_killed_writers_staging_folder_is_removed(ds, tmp_path):
    state = tmp_path / "state"
    (state / "staging-dead").mkdir(parents=True)
    add_items(ds, state, [entry()])
    assert not (state / "staging-dead").exists()


@pytest.mark.parametrize("where, bad", [("duration", float("nan")), ("source", float("nan")),
                                        ("focus", float("inf")), ("row", "Caf\ud83d"), ("focus", "Caf\ud83d"),
                                        ("source", object())])
def test_a_value_json_cannot_hold_refuses_the_batch_before_anything_is_written(tmp_path, where, bad):
    # NaN, half of an emoji (a lone surrogate), and a value of no JSON type.
    ds = Dataset.create(tmp_path / "d", "demo", "c", {"speakers": SPEAKERS})
    item = make_item("call/one", "hello there friend", source={"x": bad} if where == "source" else None)
    row = {"title": bad if where == "row" else "One", **({"duration": bad} if where == "duration" else {})}
    focus = [{"start": 0, "end": 2, "title": bad}] if where == "focus" else []
    with pytest.raises(SystemExit, match="nothing was written: call/one: its item, row or focus passages cannot "
                                         "be written as JSON"):
        add_items(ds, tmp_path / "state", [{"item": item, "row": row, "focus": focus}])
    ds = Dataset.open(tmp_path / "d")
    assert ds.manifest["items"] == {} and ds.catalog()["rows"] == [] and not ds.item_path("call/one").exists()
