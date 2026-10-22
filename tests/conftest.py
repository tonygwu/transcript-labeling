"""Shared synthetic fixtures. Nothing here reads real data."""
from __future__ import annotations

import json

import pytest

from transcript_labeling.spans import build_sidecar, reconcile_quote_reviews, sidecar_path
from transcript_labeling.testing import write_session

RAW = ("[00:00:05] Welcome to the show today >> I think the grid will run out of power first "
       ">> do you really believe that? >> I do and we will ship it by 2030")
KEY = "jane-roe/talk-1"
QID = "jane-roe:talk-1:7"


def write_sidecar(page, side):
    path = sidecar_path(page, side["key"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(side, ensure_ascii=False))
    return path


@pytest.fixture
def quote_session(tmp_path):
    """A speakers+captions session with one item, one quote, and one saved range over the quote."""
    side = build_sidecar(KEY, RAW, [])
    quote = {"qid": QID, "t": 0, "start": 0, "end": 14, "quote_start": 7, "quote_end": 14,
             "before": "", "text": "I think the grid will run out", "after": ""}
    data = {"rows": [], "quote_rows": [{"key": KEY, "person": "Jane Roe", "role": "", "title": "T", "channel": "",
                                        "upload": "", "video_id": "v", "duration": 60, "forms": ["Jane Roe"],
                                        "quotes": [quote]}],
            "transcript_index": {KEY: {"text_sha256": side["text_sha256"], "token_count": side["token_count"]}}}
    session = write_session(tmp_path / "s", data, name="s")
    write_sidecar(session.page, side)
    answers = {"labels": {}, "attribution": {}, "spans": {KEY: {
        "schema_version": 1, "key": KEY, "text_sha256": side["text_sha256"], "token_count": side["token_count"],
        "ranges": [{"start": 7, "end": 14, "speaker": "subject", "origin": "human"}], "checked_by": "operator",
        "updated_at": "2026-09-28T00:00:00Z", "assisted": False, "suggestions_sha256": side["suggestions_sha256"]}},
        "corrections": {}}
    answers = reconcile_quote_reviews(answers, session.page, data["quote_rows"])
    session.answers.write_text(json.dumps(answers, indent=1, ensure_ascii=False) + "\n")
    return session
