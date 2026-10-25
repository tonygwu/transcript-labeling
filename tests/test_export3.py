"""Version-3 exports and verify, and adopting a version-2 session in place."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from conftest import KEY, QID
from transcript_labeling.agent_api import item_entry
from transcript_labeling.dataset3 import Dataset
from transcript_labeling.export3 import ExportError, export_dataset, verify_dataset
from transcript_labeling.items import add_items
from transcript_labeling.legacy_v2 import adopt
from transcript_labeling.spans import reconcile_quote_reviews
from transcript_labeling.store import DatasetStore, ReviewStore, load_answers

ZOOM = (Path(__file__).resolve().parent / "fixtures" / "zoom" / "notes.txt").read_text()
SPEAKERS = [{"id": "ann", "label": "Ann Able", "key": "1"}, {"id": "bo", "label": "Bo Brand", "key": "2"}]


@pytest.fixture
def zoom_ds(tmp_path):
    ds = Dataset.create(tmp_path / "d", "demo", "call-1", {"speakers": SPEAKERS})
    spec = {"name": "one", "group": "call", "text": ZOOM, "suggest_map": "Ann Able=ann,Bo Brand=bo",
            "focus": [{"start": 20, "end": 24, "title": "Budget", "id": "p1"}]}
    add_items(ds, tmp_path / "state", [item_entry(ds, spec)])
    ds = Dataset.open(ds.root)
    item = ds.item("call/one")
    segs = item["segments"]
    a0, a1 = segs[0]["body"]          # Ann's first caption, marked whole
    b0, b1 = segs[1]["body"]          # Bo's caption, split between Bo and unclear
    ranges = [{"start": a0, "end": a1, "speaker": "ann", "origin": "human"},
              {"start": b0, "end": b0 + 4, "speaker": "bo", "origin": "human"},
              {"start": b0 + 4, "end": b1, "speaker": "unclear", "origin": "human"}]
    value = {"key": "call/one", "text_sha256": item["text_sha256"], "token_count": item["token_count"],
             "checked_by": "operator", "ranges": ranges}
    lock = tmp_path / "state" / ".tl.lock"
    # Saved as the service saves: passage results derived from the ranges in the same write.
    answers = DatasetStore(ds.answers, lock)
    answers.update(lambda a: reconcile_quote_reviews(
        {**a, "spans": {"call/one": value}}, ds.root / "x.html", ds.catalog()["quote_rows"],
        item_path=ds.item_path, review=ds.review), answers.load_versioned()[1])
    review = ReviewStore(ds.review_path, lock)
    review.update(lambda r: {**r, "notes": {"p1": {"text": "who agreed?"}}}, review.load_versioned()[1])
    return ds


def test_segments_show_the_split_the_unclear_and_the_unmarked(zoom_ds):
    seg = export_dataset(zoom_ds, "segments")["data"]["call/one"]
    assert seg[0]["reviewed"] and seg[0]["speakers"] == ["ann"] and not seg[0]["split"]
    assert seg[1]["reviewed"] and seg[1]["split"] and [p["speaker"] for p in seg[1]["pieces"]] == ["bo", "unclear"]
    assert seg[1]["pieces"][0]["text"] == "Yes, loud and clear."
    assert not seg[2]["reviewed"] and seg[2]["pieces"][0]["speaker"] is None  # unmarked means unknown
    assert seg[2]["label"] == "Ann Able" and (seg[2]["t_start"], seg[2]["t_end"]) == (10, 15)


def test_fill_suggestions_fills_only_unmarked_words_and_says_so(zoom_ds):
    seg = export_dataset(zoom_ds, "segments", fill="suggestions")["data"]["call/one"]
    assert [(p["speaker"], p["origin"]) for p in seg[2]["pieces"]] == [("ann", "suggestion")]
    assert not seg[2]["reviewed"]
    assert seg[1]["pieces"][1]["origin"] == "human"  # a human range is never replaced
    assert seg[3]["pieces"][0]["speaker"] is None   # Unknown has no mapping, so it stays unknown


def test_focus_spans_notes_and_all(zoom_ds):
    focus = export_dataset(zoom_ds, "focus")["data"]
    assert focus[0]["qid"] == "p1" and focus[0]["title"] == "Budget" and focus[0]["note"] == "who agreed?"
    spans = export_dataset(zoom_ds, "spans")["data"]["call/one"]["ranges"]
    assert spans[1]["speech_text"] == "Yes, loud and clear."
    notes = export_dataset(zoom_ds, "notes")["data"]
    assert notes == [{"id": "p1", "where": "review", "text": "who agreed?", "updated_at": None}]
    everything = export_dataset(zoom_ds, "all")
    assert set(everything) >= {"spans", "segments", "focus", "corrections", "notes"} and "labels" not in everything
    with pytest.raises(ExportError, match="no labels"):
        export_dataset(zoom_ds, "labels")
    with pytest.raises(ExportError, match="must be one of"):
        export_dataset(zoom_ds, "pages")


def test_no_text_lets_no_transcript_word_out(zoom_ds):
    out = json.dumps(export_dataset(zoom_ds, "all", no_text=True, fill="suggestions")).lower()
    words = [w.lower() for w in re.findall(r"\S+", ZOOM)]
    # No run of three transcript words survives anywhere in the export.
    leaked = [" ".join(words[i:i + 3]) for i in range(len(words) - 2) if " ".join(words[i:i + 3]) in out]
    assert leaked == [] and '"no_text": true' in out


def test_verify_passes_and_probes_a_throwaway_server(zoom_ds):
    sha = hashlib.sha256(zoom_ds.answers.read_bytes()).hexdigest()
    rep = verify_dataset(zoom_ds, sha)
    assert rep["ok"], [c for c in rep["checks"] if not c["ok"]]
    assert any("throwaway server" in c["check"] and c["ok"] for c in rep["checks"])


def test_verify_fails_on_an_item_that_moved(zoom_ds):
    p = zoom_ds.item_path("call/one")
    item = json.loads(p.read_text())
    item["token_count"] += 1
    p.write_text(json.dumps(item))
    rep = verify_dataset(zoom_ds, probe=False)
    assert not rep["ok"] and "every item file matches the manifest's hash and token count" in \
        [c["check"] for c in rep["checks"] if not c["ok"]]


def test_verify_reports_an_unreadable_item_and_goes_on(zoom_ds):
    zoom_ds.item_path("call/one").write_text("{not json")
    rep = verify_dataset(zoom_ds, probe=False)
    names = [c["check"] for c in rep["checks"]]
    at = names.index("every item file matches the manifest's hash and token count")
    assert not rep["ok"] and not rep["checks"][at]["ok"] and "call/one" in json.dumps(rep["checks"][at])
    assert len(names) > at + 1  # the checks after it still ran (the saved ranges on that item fail too)


# ------------------------------------------------------------------ adopting version 2

def tree(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_adoption_writes_only_manifest_and_catalog_and_keeps_every_result(quote_session):
    before = tree(quote_session.root)
    v2_answers = load_answers(quote_session.answers)
    ds = adopt(quote_session.root, "verbatim")
    after = tree(quote_session.root)
    assert sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k)) == \
        ["catalog.json", "manifest.json"]
    assert ds.manifest["legacy_from"]["manifest"]["schema_version"] == 2
    assert ds.speaker_ids() == ["subject", "other", "unclear"] and ds.manifest["mixed_answer"] == "both"
    data = ds.page_data()
    v2 = quote_session.data()
    assert data["quote_rows"] == v2["quote_rows"] and data["rows"] == v2["rows"]
    assert data["transcript_index"] == v2["transcript_index"]
    v3 = reconcile_quote_reviews(v2_answers, ds.root / "x.html", data["quote_rows"], item_path=ds.item_path,
                                 review=ds.review)
    assert v3 == v2_answers and v3["attribution"][QID]["answer"] == "subject"
    assert verify_dataset(ds)["ok"]
    with pytest.raises(SystemExit, match="cannot be rewritten here"):
        add_items(ds, quote_session.root.parent / "state", [])
    with pytest.raises(SystemExit, match="already a version-3 dataset"):
        adopt(quote_session.root, "verbatim")


def test_adoption_refuses_a_sidecar_that_does_not_match(quote_session):
    side = quote_session.sidecar(KEY)
    side.write_text(json.dumps({**json.loads(side.read_text()), "token_count": 1}))
    manifest_before = (quote_session.root / "manifest.json").read_bytes()
    with pytest.raises(SystemExit, match="does not match"):
        adopt(quote_session.root, "verbatim")
    assert (quote_session.root / "manifest.json").read_bytes() == manifest_before
    assert not (quote_session.root / "catalog.json").exists()


def test_adoption_refuses_a_page_row_json_cannot_hold(quote_session):
    # verbatim's page writer let NaN through; main refuses it on every later read.
    page = quote_session.page
    page.write_text(page.read_text().replace('"duration": 60', '"duration": NaN', 1))
    assert quote_session.data()["quote_rows"][0]["duration"] != 60
    manifest_before = (quote_session.root / "manifest.json").read_bytes()
    with pytest.raises(SystemExit, match="rows hold NaN or Infinity"):
        adopt(quote_session.root, "verbatim")
    assert (quote_session.root / "manifest.json").read_bytes() == manifest_before
    assert not (quote_session.root / "catalog.json").exists()


def test_adoption_refuses_a_version_2_manifest_json_cannot_hold(quote_session):
    path = quote_session.root / "manifest.json"
    path.write_text(path.read_text().rstrip().removesuffix("}") + ', "x": NaN}\n')
    manifest_before = path.read_bytes()
    with pytest.raises(SystemExit, match="manifest.json holds NaN or Infinity"):
        adopt(quote_session.root, "verbatim")
    assert path.read_bytes() == manifest_before and not (quote_session.root / "catalog.json").exists()


# ------------------------------------------------------------------ no_text: nothing but structure

MARKERS = ["zebraword", "quokkaword", "wombatword", "narwhalword", "ibexword", "okapiword", "tapirword"]
CHECK = {"fields": [{"name": "present", "label": "Present", "type": "bool", "required": True,
                     "options": [{"value": True, "label": "Yes", "key": "y"}, {"value": False, "label": "No", "key": "n"}]},
                    {"name": "format", "label": "Format", "type": "enum", "required": True,
                     "options": [{"value": "solo", "label": "solo", "key": "s"},
                                 {"value": "pair", "label": "pair", "key": "p"}]}],
         "goal": {"count": 1, "help": "items checked"}, "accept_when_true": ["present"]}
# The labels below also carry "claim", free text no checklist field asks for (a dataset
# checklist takes no free-text field). Its marker guards the full export's field list:
# to_labels drops a key no field names, so it never reaches the no-text filter. Free
# text in labels reaches that filter through "notes".


@pytest.fixture
def full_ds(tmp_path):
    from transcript_labeling.corrections import update_corrections
    config = {"speakers": SPEAKERS, "modes": ["checklist", "speakers", "captions"], "checklist": CHECK}
    ds = Dataset.create(tmp_path / "d", "demo", "call-1", config)
    spec = {"name": "one", "group": "call", "text": ZOOM, "suggest_map": "Ann Able=ann,Bo Brand=bo",
            "focus": [{"start": 20, "end": 24, "id": "p1", "title": "ibexword", "why": "okapiword",
                       "question": "tapirword"}]}
    add_items(ds, tmp_path / "state", [item_entry(ds, spec)])
    ds = Dataset.open(ds.root)
    item = ds.item("call/one")
    b0, b1 = item["segments"][0]["body"]
    correction = update_corrections({"key": "call/one", "text_sha256": item["text_sha256"],
                                     "token_count": item["token_count"],
                                     "edits": [{"start": b0, "end": b0 + 1, "original": item["tokens"][b0],
                                                "replacement": "wombatword"}],
                                     "revision": 0, "checked_by": "operator"}, None, item)
    lock = tmp_path / "state" / ".tl.lock"
    answers = DatasetStore(ds.answers, lock)
    answers.update(lambda a: reconcile_quote_reviews({
        **a, "labels": {"call/one": {"key": "call/one", "present": True, "format": "pair", "claim": "zebraword",
                                     "notes": "quokkaword", "checked_by": "operator"}},
        "spans": {"call/one": {"key": "call/one", "text_sha256": item["text_sha256"],
                               "token_count": item["token_count"], "checked_by": "operator",
                               "ranges": [{"start": b0, "end": b1, "speaker": "ann", "origin": "human"}]}},
        "corrections": {"call/one": correction}}, ds.root / "x.html", ds.catalog()["quote_rows"],
        item_path=ds.item_path, review=ds.review), answers.load_versioned()[1])
    review = ReviewStore(ds.review_path, lock)
    review.update(lambda r: {**r, "notes": {"p1": {"text": "narwhalword"}}}, review.load_versioned()[1])
    return ds


def strings(obj):
    """Every string value in `obj`, keys left out: field names are not transcript text."""
    if isinstance(obj, dict):
        return [x for v in obj.values() for x in strings(v)]
    if isinstance(obj, (list, tuple)) or hasattr(obj, "__next__"):
        return [x for v in obj for x in strings(v)]
    return [obj] if isinstance(obj, str) else []


def test_no_text_keeps_only_structure_in_every_kind(full_ds):
    full = json.dumps(export_dataset(full_ds, "all", fill="suggestions"))
    # The fixture really has them; "zebraword" rides on a label key no checklist field
    # asks for, which even the full labels export leaves out.
    assert [m for m in MARKERS if m not in full] == ["zebraword"]
    out = export_dataset(full_ds, "all", no_text=True, fill="suggestions")
    dumped = json.dumps(out)
    assert [m for m in MARKERS if m in dumped] == []
    assert "Ann Able" not in dumped and "Bo Brand" not in dumped  # caption labels name people
    values = " ".join(strings(out[k] for k in ("spans", "segments", "focus", "corrections", "labels", "notes")))
    words = set(re.findall(r"[a-z]{6,}", ZOOM.lower()))  # every long transcript word, one at a time
    assert sorted(w for w in words if w in values.lower()) == []
    label = out["labels"]["labels"]["call/one"]
    assert label["present"] is True and label["format"] == "pair" and "claim" not in label and label["has_notes"]
    assert out["focus"][0]["has_note"] is True and "title" not in out["focus"][0]
    assert out["corrections"]["call/one"]["edits"] == [{"start": out["corrections"]["call/one"]["edits"][0]["start"],
                                                        "end": out["corrections"]["call/one"]["edits"][0]["end"]}]
    assert all("label" not in row for row in out["segments"]["call/one"])


def test_segment_pieces_are_exact_text_and_summaries_count_only_the_owners_ranges(full_ds):
    seg = export_dataset(full_ds, "segments", fill="suggestions")["data"]["call/one"]
    item = full_ds.item("call/one")
    for row in seg:
        for p in row["pieces"]:
            assert p["text"] in item["text"]  # an exact substring, punctuation included
    filled = [row for row in seg if any(p["origin"] == "suggestion" for p in row["pieces"])
              and not any(p["origin"] == "human" for p in row["pieces"])]
    assert filled and all(row["speakers"] == [] and row["suggested_speakers"] for row in filled)


def test_a_piece_keeps_the_punctuation_inside_it(tmp_path):
    text = "00:00:01 --> 00:00:04\nAnn Able: Sure - the  budget\tis fine .\n"  # exact spacing comes back
    ds = Dataset.create(tmp_path / "d", "demo", "c", {"speakers": SPEAKERS})
    add_items(ds, tmp_path / "state", [item_entry(ds, {"name": "one", "text": text})])
    ds = Dataset.open(ds.root)
    item = ds.item("item/one")
    b0, b1 = item["segments"][0]["body"]
    answers = DatasetStore(ds.answers, tmp_path / "state" / ".tl.lock")
    answers.update(lambda a: {**a, "spans": {"item/one": {
        "key": "item/one", "text_sha256": item["text_sha256"], "token_count": item["token_count"],
        "checked_by": "operator", "ranges": [{"start": b0, "end": b1, "speaker": "ann", "origin": "human"}]}}},
        answers.load_versioned()[1])
    pieces = export_dataset(ds, "segments")["data"]["item/one"][0]["pieces"]
    assert [p["text"] for p in pieces] == ["Sure - the  budget\tis fine ."]


def test_verify_reports_the_schema_and_tool_versions(zoom_ds):
    from transcript_labeling import __version__
    rep = verify_dataset(zoom_ds, probe=False)
    assert rep["schema_version"] == 3 and rep["tool_version"] == __version__ and rep["created_with"]


def test_adoption_refuses_before_writing_and_restores_a_session_changed_mid_run(quote_session, monkeypatch):
    before = tree(quote_session.root)
    with pytest.raises(SystemExit, match="would be invalid"):
        adopt(quote_session.root, "Bad Name")
    assert tree(quote_session.root) == before  # nothing written, so a retry can succeed
    import transcript_labeling.legacy_v2 as legacy
    real = legacy.Dataset.write_manifest

    def write_then_someone_else_writes(self, manifest):
        real(self, manifest)
        quote_session.answers.write_text(quote_session.answers.read_text() + " ")
    monkeypatch.setattr(legacy.Dataset, "write_manifest", write_then_someone_else_writes)
    with pytest.raises(SystemExit, match="changed while the session was being adopted"):
        adopt(quote_session.root, "verbatim")
    after = tree(quote_session.root)
    assert "catalog.json" not in after and after["manifest.json"] == before["manifest.json"]


def test_an_adopted_dataset_records_the_tool_version_that_adopted_it(quote_session):
    from transcript_labeling import __version__
    from transcript_labeling.legacy_v2 import adopt
    ds = adopt(quote_session.root, "verbatim")
    rep = verify_dataset(ds, probe=False)
    assert rep["created_with"] == __version__
    assert {"check": "the manifest names the tool version that wrote it", "ok": True} in rep["checks"]
    del ds.manifest["created_with"]  # as a dataset adopted before this field existed
    rep = verify_dataset(ds, probe=False)
    assert rep["ok"] is False
    assert {"check": "the manifest names the tool version that wrote it", "ok": False} in rep["checks"]
