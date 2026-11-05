"""Contextual drafts are validated and versioned without becoming human answers."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from transcript_labeling.dataset3 import Dataset
from transcript_labeling.items import add_items, make_item
from transcript_labeling.suggestions import apply_bundle, suggestion_bundle_hash, suggestions_hash, update_suggestions


@pytest.fixture
def item():
    return make_item("call/one", "[00:00:00] Hello hello. How are you? Fine.")


@pytest.fixture
def bundle(item):
    return {"schema_version": 1, "text_sha256": item["text_sha256"], "token_count": item["token_count"],
            "ranges": [{"start": 1, "end": 3, "speaker": "a", "source": "contextual_model"},
                       {"start": 3, "end": 6, "speaker": "unclear", "source": "contextual_model"},
                       {"start": 6, "end": 7, "speaker": "b", "source": "contextual_model"}],
            "provenance": {"method": "contextual_speaker_turns", "requested_model": "example-model",
                           "served_model": "example-model", "model_identity": "reported",
                           "prompt_sha256": "a" * 64, "request_sha256": "b" * 64,
                           "response_sha256": "c" * 64, "run_id": "synthetic-run"}}


@pytest.mark.parametrize("change,match", [
    (lambda b: b.update(text_sha256="0" * 64), "different text"),
    (lambda b: b.update(token_count=99), "different text"),
    (lambda b: b.update(token_count=True), "positive integer"),
    (lambda b: b.update(schema_version=True), "schema_version"),
    (lambda b: b.update(extra=1), "exactly"),
    (lambda b: b.update(ranges={}), "must be a list"),
    (lambda b: b["ranges"].insert(0, None), "needs"),
    (lambda b: b["ranges"][1].update(start=2), "non-overlapping"),
    (lambda b: b["ranges"][0].update(start=False), "non-overlapping"),
    (lambda b: b["ranges"][0].update(start=0), "non-speech"),
    (lambda b: b["ranges"][0].update(end=99), "non-overlapping"),
    (lambda b: b["ranges"][0].update(speaker="missing"), "unknown speaker"),
    (lambda b: b["ranges"][0].update(source="human"), "never a human"),
    (lambda b: b["ranges"][0].update(origin="human"), "needs"),
    (lambda b: b["ranges"][0].update(reason=[]), "reason"),
    (lambda b: b["provenance"].pop("request_sha256"), "provenance needs"),
    (lambda b: b["provenance"].update(method="source-map"), "method"),
    (lambda b: b["provenance"].update(served_model=None), "requires served_model"),
    (lambda b: b["provenance"].update(served_model="substituted-model"), "differs from requested_model"),
    (lambda b: b["provenance"].update(model_identity="unverified"), "requires served_model=null"),
    (lambda b: b["provenance"].update(prompt_sha256="bad"), "SHA256"),
    (lambda b: b["provenance"].update(transport={"usage": float("nan")}), "strict UTF-8 JSON"),
])
def test_bad_bundle_is_refused_without_changing_item(item, bundle, change, match):
    original = deepcopy(item)
    change(bundle)
    with pytest.raises(ValueError, match=match):
        apply_bundle(item, bundle, ["a", "b", "unclear"])
    assert item == original


def test_uncertainty_is_an_explicit_draft_and_missing_speech_is_not_filled(item, bundle):
    bundle["ranges"] = bundle["ranges"][:2]
    updated = apply_bundle(item, bundle, ["a", "b", "unclear"])
    assert updated["suggestions"][-1]["speaker"] == "unclear"
    assert updated["suggestions"][-1]["end"] == 6
    assert updated["tokens"] == item["tokens"]
    assert all("origin" not in r for r in updated["suggestions"])
    assert updated["suggestions_sha256"] == suggestions_hash(bundle["ranges"])


def test_competing_updates_cannot_replace_each_other(tmp_path, item, bundle):
    ds = Dataset.create(tmp_path / "dataset", "demo", "one", {
        "speakers": [{"id": "a", "label": "Me", "key": "1"}, {"id": "b", "label": "Interviewer", "key": "2"}]})
    state = tmp_path / "state"
    add_items(ds, state, [{"item": item}])
    expected = suggestion_bundle_hash(item)
    other = deepcopy(bundle)
    other["ranges"][0]["speaker"] = "b"

    def update(value):
        try:
            return update_suggestions(ds, state, item["key"], value, expected)
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(update, [bundle, other]))
    assert sum(isinstance(r, dict) and r["changed"] for r in results) == 1
    assert sum(isinstance(r, str) and "suggestions changed" in r for r in results) == 1
    saved = Dataset.open(ds.root).item(item["key"])
    assert len(saved["suggestion_history"]) == 1
    assert saved["suggestion_history"][0]["suggestions"] == []


def test_same_ranges_with_competing_provenance_refuse_stale_update(tmp_path, item, bundle):
    ds = Dataset.create(tmp_path / "dataset", "demo", "one", {
        "speakers": [{"id": "a", "label": "Me", "key": "1"}, {"id": "b", "label": "Interviewer", "key": "2"}]})
    state = tmp_path / "state"
    first = apply_bundle(item, bundle, ["a", "b", "unclear"])
    add_items(ds, state, [{"item": first}])
    expected = suggestion_bundle_hash(first)
    second = deepcopy(bundle)
    second["provenance"]["run_id"] = "second-run"
    update_suggestions(ds, state, item["key"], second, expected)
    third = deepcopy(bundle)
    third["provenance"]["run_id"] = "third-run"
    with pytest.raises(ValueError, match="suggestions changed"):
        update_suggestions(ds, state, item["key"], third, expected)
    current = ds.item(item["key"])
    assert current["suggestions_sha256"] == first["suggestions_sha256"]
    assert current["suggestion_provenance"]["run_id"] == "second-run"
    assert current["suggestion_history"][0]["suggestion_provenance"]["run_id"] == "synthetic-run"


def test_source_tag_confirmation_is_human_review_without_model_claim(item):
    from transcript_labeling.spans import quote_review, validate_annotation
    annotation = {"key": item["key"], "text_sha256": item["text_sha256"], "token_count": item["token_count"],
                  "checked_by": "owner", "ranges": [{"start": 1, "end": 3, "speaker": "a",
                                                        "origin": "suggestion_confirmed"}]}
    validate_annotation(annotation, item, ["a", "b", "unclear"])
    assert quote_review({"quote_start": 1, "quote_end": 3}, item, annotation,
                        speakers=["a", "b", "unclear"])["answer"] == "a"
    for scope in ("range", "legacy_annotation"):
        annotation["ranges"][0]["suggestion_evidence_scope"] = scope
        validate_annotation(annotation, item, ["a", "b", "unclear"])
    annotation["ranges"][0]["suggestion_evidence_scope"] = "guessed"
    with pytest.raises(ValueError, match="suggestion_evidence_scope"):
        validate_annotation(annotation, item, ["a", "b", "unclear"])


def test_blind_review_does_not_receive_suggestion_history_or_provenance(tmp_path, item, bundle):
    from transcript_labeling.service import transcript_view
    ds = Dataset.create(tmp_path / "dataset", "demo", "one", {
        "speakers": [{"id": "a", "label": "Me", "key": "1"}, {"id": "b", "label": "Interviewer", "key": "2"}]})
    updated = apply_bundle(item, bundle, ["a", "b", "unclear"], remember=True)
    hidden = transcript_view(ds, updated, {"reveals": {}})
    assert hidden["suggestions"] == []
    assert "suggestion_history" not in hidden
    assert "suggestion_provenance" not in hidden
    assert "suggestion_bundle_sha256" not in hidden
    shown = transcript_view(ds, updated, {"reveals": {item["key"]: {}}})
    assert shown["suggestions"] == bundle["ranges"]
    assert shown["suggestion_provenance"] == bundle["provenance"]
    assert shown["suggestion_bundle_sha256"] == suggestion_bundle_hash(updated)
    assert "suggestion_history" not in shown


@pytest.mark.parametrize("field", ["suggestions_sha256", "suggestion_bundle_sha256"])
@pytest.mark.parametrize("level", ["range", "annotation"])
def test_annotation_provenance_hashes_are_optional_but_strict(item, field, level):
    from transcript_labeling.spans import validate_annotation
    value = {"key": item["key"], "text_sha256": item["text_sha256"], "token_count": item["token_count"],
             "checked_by": "owner", "ranges": [{"start": 1, "end": 3, "speaker": "a",
                                               "origin": "suggestion_confirmed"}]}
    target = value["ranges"][0] if level == "range" else value
    target[field] = "a" * 64
    validate_annotation(value, item, ["a", "b", "unclear"])
    for invalid in ("not-a-hash", "A" * 64, None, 123):
        target[field] = invalid
        with pytest.raises(ValueError, match="lowercase SHA256"):
            validate_annotation(value, item, ["a", "b", "unclear"])


def test_history_and_current_digests_are_verified(item, bundle):
    from transcript_labeling.suggestions import validate_stored_suggestions
    first = apply_bundle(item, bundle, ["a", "b", "unclear"])
    second = deepcopy(bundle)
    second["provenance"]["run_id"] = "second-run"
    current = apply_bundle(first, second, ["a", "b", "unclear"], remember=True)
    validate_stored_suggestions(current, ["a", "b", "unclear"])
    broken = deepcopy(current)
    broken["suggestion_history"][0]["suggestion_provenance"]["run_id"] = "tampered"
    with pytest.raises(ValueError, match="historical bundle differs"):
        validate_stored_suggestions(broken, ["a", "b", "unclear"])
    broken = deepcopy(current)
    broken["suggestions"][0]["speaker"] = "b"
    with pytest.raises(ValueError, match="stored suggestions differ"):
        validate_stored_suggestions(broken, ["a", "b", "unclear"])
    broken = deepcopy(current)
    broken["suggestion_provenance"]["served_model"] = "substituted-model"
    with pytest.raises(ValueError, match="differs from requested_model"):
        validate_stored_suggestions(broken, ["a", "b", "unclear"])
