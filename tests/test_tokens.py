"""Tokens and their kinds: caption metadata keeps its index and is never speech."""
from __future__ import annotations

import re

import pytest

from transcript_labeling import tokens as T


def test_caption_markers_are_not_speech():
    tokens = '[00:06:06] Hello [ __ ] there ... >> goodbye [ ___'.split()
    assert T.token_kinds(tokens) == ['timestamp', 'speech', 'gap', 'gap', 'gap', 'speech', 'gap', 'turn', 'speech',
                                     'gap', 'gap']
    assert T.token_kinds('[Music] really 2024 was good'.split()) == ['gap', 'speech', 'speech', 'speech', 'speech']
    assert T.token_kinds(['—', '!?', 'ok.']) == ['punctuation', 'punctuation', 'speech']


def test_every_kind_is_declared():
    sample = '[00:00:01] >> [ … ok — [laughter]'.split()
    assert set(T.token_kinds(sample)) <= set(T.KINDS)


def test_tokenize_matches_finditer_and_text_hash_is_sha256():
    text = "  a\tb\n\nc  d "
    assert T.tokenize(text) == [m.group() for m in re.finditer(r"\S+", text)] == ["a", "b", "c", "d"]
    assert T.text_hash("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_char_span_to_tokens():
    text = "alpha beta gamma delta"
    toks = list(re.finditer(r"\S+", text))
    assert T.char_span_to_tokens(toks, 6, 10) == (1, 1)
    assert T.char_span_to_tokens(toks, 3, 13) == (0, 2)
    with pytest.raises(ValueError):
        T.char_span_to_tokens(toks, 5, 6)
