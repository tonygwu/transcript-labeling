"""Source-pinned model text proposals and human decisions.

All examples are synthetic. No recording or private interview enters this suite.
"""
import copy
import json

import pytest

from transcript_labeling.contextual import build_request, resolve_response, response_schema
from transcript_labeling.corrections import corrected_text, update_corrections
from transcript_labeling.items import make_item
from transcript_labeling.suggestions import apply_bundle, suggestion_bundle_hash


def example():
    item = make_item("calls/example", "Um, my name is Jan.")
    request = build_request(item, [{"id": "participant", "description": "Jane Roe, interview participant"}],
                            unclear="unclear")
    return item, request


def envelope(corrections):
    return {"response": json.dumps({"turns": [{"marker": 0, "first_words": "Um, my name is Jan.",
                                               "speaker": "participant"}], "text_corrections": corrections}),
            "requested_model": "test-model", "served_model": "test-model", "model_identity": "reported"}


def proposal():
    return {"marker": 0, "first_words": "Jan.", "original": "Jan.", "replacement": "Jane.",
            "reason": "The self-introduction conflicts with the supplied participant name."}


def resolved():
    item, request = example()
    bundle = resolve_response(request, envelope([proposal()]), requested_model="test-model", run_id="test")
    return apply_bundle(item, bundle, ["participant", "unclear"])


def decision(item, *, decision="accepted", revision=0):
    return {**{k: item[k] for k in ("key", "text_sha256", "token_count")}, "checked_by": "operator",
            "revision": revision, "proposal_action": {"bundle_sha256": suggestion_bundle_hash(item),
                                                       "index": 0, "decision": decision}}


def test_minimal_name_correction_is_reviewable_and_preserves_source_and_speakers():
    item, request = example()
    before = copy.deepcopy(item)
    assert "text_corrections" in response_schema(request)["required"]
    suggested = resolved()
    assert suggested["text_suggestions"][0]["replacement"] == "Jane."
    assert suggested["text"] == before["text"]
    assert suggested["tokens"] == before["tokens"]
    saved = update_corrections(decision(suggested), None, suggested)
    assert corrected_text(suggested, saved) == "Um, my name is Jane."
    assert saved["edits"][0] == {"start": 4, "end": 5, "original": "Jan.", "replacement": "Jane.",
                                 "origin": "model_confirmed", "suggestion_bundle_sha256": suggestion_bundle_hash(suggested),
                                 "suggestion_index": 0}
    assert suggested["suggestions"] == [{"start": 0, "end": 5, "speaker": "participant", "source": "contextual_model"}]
    assert (suggested["text_sha256"], suggested["token_count"]) == (before["text_sha256"], before["token_count"])


def test_rejection_keeps_original_reading_and_records_decision():
    item = resolved()
    saved = update_corrections(decision(item, decision="rejected"), None, item)
    assert saved["edits"] == []
    assert saved["proposal_decisions"][0]["decision"] == "rejected"
    assert corrected_text(item, saved) == item["text"]


def test_eval_detects_old_behavior_and_checks_both_controls(monkeypatch):
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    from evals.text_corrections.runner import evaluate, requests
    inputs = requests()
    assert set(inputs) == {"known_name", "correct_name", "ambiguous_name"}
    assert "Jane" not in inputs["correct_name"]["prompt"]
    assert "Jane" not in inputs["ambiguous_name"]["prompt"]
    assert "expected" not in json.dumps(inputs).lower()
    baseline = {key: envelope([]) for key in inputs}
    old = evaluate(baseline, "test-model")
    assert (old["attempted"], old["succeeded"], old["failed"], old["errors"]) == (3, 2, 1, {"wrong_text_corrections": 1})
    baseline["known_name"] = envelope([proposal()])
    passed = evaluate(baseline, "test-model")
    assert (passed["attempted"], passed["succeeded"], passed["failed"], passed["errors"]) == (3, 3, 0, {})
    baseline["correct_name"] = envelope([proposal()])
    baseline["ambiguous_name"] = envelope([proposal()])
    assert evaluate(baseline, "test-model")["failed"] == 2


@pytest.mark.parametrize("change", [
    lambda p: p.update(marker=True), lambda p: p.update(first_words="Jan"),
    lambda p: p.update(original="Jan"), lambda p: p.update(replacement="Jan."),
    lambda p: p.update(replacement=""), lambda p: p.update(reason=""), lambda p: p.update(origin="human"),
])
def test_invalid_text_proposals_fail_loud(change):
    _, req = example()
    p = proposal()
    change(p)
    with pytest.raises(ValueError):
        resolve_response(req, envelope([p]), requested_model="test-model", run_id="test")


def test_overlapping_proposals_and_opt_out_are_refused():
    item, req = example()
    with pytest.raises(ValueError, match="non-overlapping"):
        resolve_response(req, envelope([proposal(), proposal()]), requested_model="test-model", run_id="test")
    disabled = build_request(item, req["speakers"], unclear="unclear", text_corrections=False)
    assert "text_corrections" not in response_schema(disabled)["properties"]
    with pytest.raises(ValueError, match="exactly"):
        resolve_response(disabled, envelope([proposal()]), requested_model="test-model", run_id="test")
    payload = envelope([])
    payload["response"] = json.dumps({"turns": json.loads(payload["response"])["turns"]})
    assert resolve_response(disabled, payload, requested_model="test-model", run_id="test")["text_suggestions"] == []


def test_stale_proposal_and_existing_human_edit_are_protected():
    item = resolved()
    value = decision(item)
    value["proposal_action"]["bundle_sha256"] = "a" * 64
    with pytest.raises(ValueError, match="changed"):
        update_corrections(value, None, item)
    manual = {**decision(item), "edits": [{"start": 4, "end": 5, "original": "Jan.", "replacement": "Jay."}]}
    manual.pop("proposal_action")
    saved = update_corrections(manual, None, item)
    with pytest.raises(ValueError, match="overlaps"):
        update_corrections(decision(item, revision=1), saved, item)
    rejected = update_corrections(decision(item, revision=1, decision="rejected"), saved, item)
    assert rejected["edits"] == saved["edits"]


def test_accepted_proposal_survives_new_bundle_manual_edit_and_export(tmp_path):
    from transcript_labeling.dataset3 import Dataset
    from transcript_labeling.items import add_items
    from transcript_labeling.export3 import export_dataset
    from transcript_labeling.store import load_answers
    from transcript_labeling.suggestions import update_suggestions, validate_stored_suggestions
    item = resolved()
    ds = Dataset.create(tmp_path / "dataset", "demo", "one", {
        "speakers": [{"id": "participant", "label": "Participant", "key": "1"}]})
    state = tmp_path / "state"
    add_items(ds, state, [{"item": item}])
    ds = Dataset.open(ds.root)
    saved = update_corrections(decision(item), None, item)
    answers = load_answers(ds.answers)
    speakers = {"key": item["key"], "text_sha256": item["text_sha256"], "token_count": 5,
                "ranges": [{"start": 0, "end": 5, "speaker": "participant", "origin": "human"}],
                "checked_by": "operator", "schema_version": 1}
    answers["spans"][item["key"]] = speakers
    answers["corrections"][item["key"]] = saved
    ds.answers.write_text(json.dumps(answers))
    source = ds.item_path(item["key"]).read_bytes()
    exported = export_dataset(ds, "all")
    assert exported["corrections"][item["key"]]["corrected_text"] == "Um, my name is Jane."
    assert exported["spans"][item["key"]]["ranges"][0]["speaker"] == "participant"
    assert ds.item_path(item["key"]).read_bytes() == source
    _, request = example()
    bundle = resolve_response(request, envelope([]), requested_model="test-model", run_id="new-run")
    before = ds.answers.read_bytes()
    update_suggestions(ds, state, item["key"], bundle, suggestion_bundle_hash(item))
    assert ds.answers.read_bytes() == before
    current = ds.item(item["key"])
    validate_stored_suggestions(current, ds.speaker_ids())
    assert export_dataset(ds, "corrections")["data"][item["key"]]["edits"][0]["origin"] == "model_confirmed"
    value = {**saved, "edits": [{"start": 0, "end": 1, "original": "Um,", "replacement": "Hmm,"}, *saved["edits"]]}
    later = update_corrections(value, saved, current)
    assert later["edits"][1] == saved["edits"][0]
    assert corrected_text(current, later) == "Hmm, my name is Jane."
    structural = json.dumps(export_dataset(ds, "all", no_text=True))
    assert "Jan" not in structural and "Jane" not in structural and proposal()["reason"] not in structural
    assert "model_confirmed" in structural
