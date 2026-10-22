"""Formats: the Zoom shape is detected and parsed strictly; headers are never speech."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from transcript_labeling.formats import detect, parse, parse_suggest_map, segment_suggestions
from transcript_labeling.items import item_errors, make_item
from transcript_labeling.tokens import item_kinds

FIX = Path(__file__).resolve().parent / "fixtures" / "zoom"
NOTES = (FIX / "notes.txt").read_text()
PLAIN = (FIX / "plain.txt").read_text()


def words(text, rng):
    return " ".join(re.findall(r"\S+", text)[rng[0]:rng[1]])


@pytest.mark.parametrize("text", [NOTES, PLAIN])
def test_the_zoom_shape_is_detected_with_or_without_notes(text):
    assert detect(text) == "zoom-notes"


def test_segments_labels_times_and_headers():
    p = parse(NOTES)
    assert p["format"] == "zoom-notes" and len(p["segments"]) == 7
    assert p["labels"] == ["Ann Able", "Bo Brand", "Speaker 4", "Unknown"]
    s = p["segments"]
    assert [x["label"] for x in s] == ["Ann Able", "Bo Brand", "Ann Able", "Unknown", "Speaker 4", "Bo Brand",
                                       "Ann Able"]
    assert (s[2]["t_start"], s[2]["t_end"]) == (10, 15)
    assert words(NOTES, s[1]["header"]) == "00:00:05 --> 00:00:09 Bo Brand:"
    assert words(NOTES, s[1]["body"]) == "Yes, loud and clear. Shall we start with the budget?"
    # A colon inside speech is not a label.
    assert words(NOTES, s[4]["body"]) == "At 10:30 we meet again, right?"
    notes = p["meta"][0]
    assert notes[0] == 0 and words(NOTES, notes).startswith("Meeting notes:")


def test_an_item_from_the_parse_marks_headers_and_notes_as_meta_and_keeps_the_text():
    p = parse(NOTES)
    item = make_item("call/one", NOTES, segments=p["segments"], meta=p["meta"])
    assert item_errors(item, ["ann", "bo", "unclear"]) == []
    kinds = item_kinds(item)
    toks = item["tokens"]
    speech = [toks[i] for i, k in enumerate(kinds) if k == "speech"]
    assert "Bo" not in speech and "-->" not in speech and "Attendees:" not in speech
    assert speech[:6] == ["Good", "morning,", "can", "you", "hear", "me?"]
    # Every body token is speech or punctuation; every header token is meta.
    for seg in p["segments"]:
        assert set(kinds[seg["header"][0]:seg["header"][1]]) == {"meta"}
        assert "meta" not in kinds[seg["body"][0]:seg["body"][1]]


def test_a_broken_cue_is_refused_with_its_line():
    with pytest.raises(SystemExit, match="line 4 is neither"):
        parse(PLAIN.replace("Ann Able: Good morning, can you hear me?\n\n",
                            "Ann Able: Good morning, can you hear me?\n\nstray words\n", 1))
    with pytest.raises(SystemExit, match="has no text line"):
        parse("00:00:01 --> 00:00:02\n\n00:00:03 --> 00:00:04\nA: hi\n")


def test_webvtt_and_srt_are_refused_and_other_shapes_detected():
    with pytest.raises(SystemExit, match="WebVTT"):
        detect("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi\n")
    with pytest.raises(SystemExit, match="SRT"):
        detect("1\n00:00:01,000 --> 00:00:02,000\nhi\n")
    with pytest.raises(SystemExit, match="SRT"):  # a byte-order mark before the cue number
        parse("\ufeff1\n00:00:01,000 --> 00:00:02,000\nAnn: hi\n")
    assert detect("[00:00:05] hello >> there") == "bracket-ts"
    assert detect("just words here") == "plain"
    assert parse("just words", "plain") == {"format": "plain", "segments": None, "meta": [], "labels": []}
    with pytest.raises(SystemExit, match="must be one of"):
        parse("x", "vtt")


def test_a_cue_with_two_speakers_on_two_lines_becomes_two_segments_with_its_times():
    p = parse(NOTES)
    bo, ann = p["segments"][5], p["segments"][6]
    assert (bo["t_start"], bo["t_end"]) == (ann["t_start"], ann["t_end"]) == (24, 27)
    assert words(NOTES, bo["body"]) == "Right, same time."
    assert words(NOTES, ann["header"]) == "Ann Able:" and words(NOTES, ann["body"]) == "Great, see you then."
    sug = segment_suggestions(p["segments"], {"Ann Able": "ann", "Bo Brand": "bo"}, ["ann", "bo", "unclear"])
    assert [(words(NOTES, [x["start"], x["end"]]), x["speaker"]) for x in sug][-2:] == \
        [("Right, same time.", "bo"), ("Great, see you then.", "ann")]


def test_a_later_line_without_a_label_stays_with_its_speaker():
    text = ("00:00:01 --> 00:00:02\nAnn Able: first line\nmore words, at 10:30 or so\n\n"
            "00:00:03 --> 00:00:04\nBo Brand: ok\n")
    p = parse(text)
    assert [s["label"] for s in p["segments"]] == ["Ann Able", "Bo Brand"]
    assert words(text, p["segments"][0]["body"]) == "first line more words, at 10:30 or so"


@pytest.mark.parametrize("line", ["Cy Cole: No, not yet.", "Here is the thing: more words"])
def test_a_later_line_that_opens_like_an_unknown_label_is_refused(line):
    # A speaker who opens no cue would otherwise be credited to the speaker above and
    # vanish from the label list.
    text = (f"00:00:01 --> 00:00:02\nAnn Able: Shall we sign today?\n{line}\n\n"
            "00:00:03 --> 00:00:04\nAnn Able: Okay.\n")
    with pytest.raises(SystemExit, match="line 3 starts like a speaker label that opens no cue"):
        parse(text)


@pytest.mark.parametrize("text,expect", [
    ("00:00:01 --> 00:00:02\nno label here at all\n", "line 2 has no `Label:`"),
    ("00:00:01 --> 00:00:02\none two three four five six: words\n", "line 2 has no `Label:`"),
    ("00:00:01 --> 00:00:02\n10:30 we meet\n", "line 2 has no `Label:`"),
    ("notes\n00:00:01 --> 0:00:02\n\n00:00:03 --> 00:00:04\nA: hi\n", "line 2, before the first cue"),
])
def test_a_cue_without_a_label_or_a_broken_time_line_is_refused(text, expect):
    with pytest.raises(SystemExit, match=expect):
        parse(text)


def test_a_byte_order_mark_does_not_hide_the_first_cue():
    text = "\ufeff" + PLAIN
    p = parse(text)
    assert len(p["segments"]) == 7 and p["meta"][0] == p["segments"][0]["header"] == [0, 5]
    assert words(text, p["segments"][0]["body"]) == "Good morning, can you hear me?"


def test_naming_the_format_does_not_skip_the_webvtt_refusal():
    with pytest.raises(SystemExit, match="WebVTT"):
        parse("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nA: hi\n", "zoom-notes")


def test_suggest_map_and_segment_suggestions():
    assert parse_suggest_map("Ann Able=ann, Bo Brand=bo") == {"Ann Able": "ann", "Bo Brand": "bo"}
    assert parse_suggest_map(None) == {}
    for bad in ("Ann", "Ann=ann,Ann=bo", "=x"):
        with pytest.raises(SystemExit, match="suggest-map"):
            parse_suggest_map(bad)
    p = parse(NOTES)
    sug = segment_suggestions(p["segments"], {"Ann Able": "ann", "Bo Brand": "bo"}, ["ann", "bo", "unclear"])
    assert [s["speaker"] for s in sug] == ["ann", "bo", "ann", "bo", "ann"]
    assert all(s["source"] == "segment_label" for s in sug)
    with pytest.raises(SystemExit, match="labels the file does not have"):
        segment_suggestions(p["segments"], {"Cy": "ann"}, ["ann"])
    with pytest.raises(SystemExit, match="speakers the dataset does not have"):
        segment_suggestions(p["segments"], {"Ann Able": "zed"}, ["ann"])
    assert segment_suggestions(p["segments"], {}, ["ann"]) == []
