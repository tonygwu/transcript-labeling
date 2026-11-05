"""Contextual boundaries must prove their location, never guess offsets."""
import copy
import json

import pytest

from transcript_labeling.contextual import build_request, resolve_response
from transcript_labeling.formats import parse
from transcript_labeling.items import make_item
from transcript_labeling.tokens import item_kinds, text_hash


SPEAKERS = [{"id": "me", "description": "Jane Roe, candidate"},
            {"id": "interviewer", "description": "Sam Poe, interviewer"}]


def request(text="Hello there yes how are you today fine thanks goodbye", **kwargs):
    p = parse(text)
    item = make_item("calls/example", text, segments=p["segments"], meta=p["meta"])
    item["suggestions"] = [{"SECRET_HUMAN_REFERENCE": "never include"}]
    item["corrections"] = {"SECRET_HUMAN_REFERENCE": "never include"}
    return item, build_request(item, SPEAKERS, unclear="unclear", **kwargs)


def response(turns, model="example-model"):
    return {"response": json.dumps({"turns": turns}), "requested_model": model,
            "served_model": None, "model_identity": "unverified", "transport": {"adapter": "test"}}


def turn(req, pos, speaker):
    words = req["speech"]
    marker = words[pos // req["marker_every"] * req["marker_every"]]["index"]
    return {"marker": marker, "first_words": " ".join(w["text"] for w in words[pos:pos+req["anchor_words"]]),
            "speaker": speaker}


def resolve(req, turns):
    return resolve_response(req, response(turns), requested_model="example-model", run_id="test-run")


def test_contextual_splits_inside_caption_and_keeps_original_coordinates():
    text = "00:00:01 --> 00:00:02\nUnknown: Hello there yes\n\n00:00:02 --> 00:00:03\nSpeaker 1: how are you today fine thanks goodbye\n"
    item, req = request(text)
    result = resolve(req, [turn(req, 0, "me"), turn(req, 2, "interviewer"), turn(req, 8, "unclear")])
    labels = {i: r["speaker"] for r in result["ranges"] for i in range(r["start"], r["end"])}
    speech = [i for i, kind in enumerate(item_kinds(item)) if kind == "speech"]
    assert set(labels) == set(speech)
    assert [labels[i] for i in speech] == ["me"] * 2 + ["interviewer"] * 6 + ["unclear"] * 2
    assert result["text_sha256"] == text_hash(text)
    assert all(r["source"] == "contextual_model" for r in result["ranges"])
    assert result["provenance"]["model_identity"] == "unverified"
    assert "SECRET_HUMAN_REFERENCE" not in json.dumps(req)


@pytest.mark.parametrize("change,match", [
    (lambda ts: ts[0].update(marker=-1), "marker"),
    (lambda ts: ts[0].update(marker=True), "marker"),
    (lambda ts: ts[0].update(first_words="invented words"), "anchor"),
    (lambda ts: ts[0].update(speaker="missing"), "speaker"),
    (lambda ts: ts[0].update(origin="human"), "fields"),
    (lambda ts: ts.reverse(), "first|order"),
    (lambda ts: ts.append(dict(ts[0])), "order"),
    (lambda ts: ts.pop(0), "first"),
])
def test_bad_or_unordered_boundaries_fail_loud(change, match):
    _, req = request()
    turns = [turn(req, 0, "me"), turn(req, 4, "interviewer")]
    change(turns)
    with pytest.raises(ValueError, match=match):
        resolve(req, turns)


def test_repeated_anchor_refused_instead_of_picking_first():
    _, req = request("yes yes yes yes yes yes yes yes yes yes yes yes", anchor_words=2)
    with pytest.raises(ValueError, match="ambiguous"):
        resolve(req, [turn(req, 0, "me")])


def test_exact_case_punctuation_and_anchor_length_required():
    _, req = request()
    for words in ["hello there yes how are you today fine", "Hello there", "Hello, there yes how are you today fine"]:
        t = turn(req, 0, "me")
        t["first_words"] = words
        with pytest.raises(ValueError, match="anchor"):
            resolve(req, [t])


def test_marker_only_searches_its_own_window():
    _, req = request(marker_every=3)
    t = turn(req, 4, "me")
    t["marker"] = req["speech"][0]["index"]
    with pytest.raises(ValueError, match="anchor"):
        resolve(req, [t])


def test_stale_geometry_and_tampered_request_refused():
    item, req = request()
    item["text"] += " stale"
    with pytest.raises(ValueError, match="source"):
        build_request(item, SPEAKERS, unclear="unclear")
    changed = copy.deepcopy(req)
    changed["speech"][0]["index"] = 999
    with pytest.raises(ValueError, match="request|source"):
        resolve(changed, [turn(req, 0, "me")])


def test_wrong_model_and_identity_claims_refused():
    _, req = request()
    payload = response([turn(req, 0, "me")])
    for changes in [{"requested_model": "other"}, {"served_model": "other", "model_identity": "reported"},
                    {"served_model": None, "model_identity": "reported"}]:
        with pytest.raises(ValueError, match="model"):
            resolve_response(req, {**payload, **changes}, requested_model="example-model", run_id="test")


def test_no_speech_and_invalid_settings_refused():
    with pytest.raises(ValueError, match="speech"):
        request("[00:00:01] ...")
    for kw in [{"marker_every": 0}, {"anchor_words": True}]:
        with pytest.raises(ValueError):
            request(**kw)


def test_response_requires_only_json_and_complete_turns():
    _, req = request()
    for raw in ['{"turns": []}', '{"turns": [], "extra": 1}', '```json\n{"turns": []}\n```',
                '{"turns":[],"turns":[]}']:
        payload = response([])
        payload["response"] = raw
        with pytest.raises(ValueError):
            resolve_response(req, payload, requested_model="example-model", run_id="test")
