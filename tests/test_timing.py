"""A focus passage's play link starts just before the quote, not at the minute mark.

Ported from verbatim-index `scripts/test_play_time.py` (the HELPER and SHARED
checks; its LEADERS and PUNDITS checks test verbatim's own builders). Synthetic
text only: this repository is public.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

import transcript_labeling
from transcript_labeling import timing as V

P = V.play_time
LEAD = V.PLAY_LEAD_SECONDS
# Four marks, at 0:10, 1:10, 2:10 and 3:10, each followed by 120 words, so every
# word n is spoken at 10 + n/2 seconds. A turn mark `>>` sits inside each quote.
QUOTE_AT = (2, 24, 64, 110)
DURATION = 400


def stamp(sec: int) -> str:
    return f"[{sec // 3600:02d}:{sec // 60 % 60:02d}:{sec % 60:02d}]"


def uniform_text() -> str:
    out = []
    for n in range(480):
        if n % 120 == 0:
            out.append(stamp(10 + n // 2))
        if n - 1 in QUOTE_AT:
            out.append(">>")
        out.append(f"w{n}")
    return " ".join(out)


def toks_of(text: str) -> list:
    return list(re.finditer(r"\S+", text))


def tok_of_word(toks: list, n: int) -> int:
    return next(i for i, t in enumerate(toks) if t.group() == f"w{n}")


def test_a_word_between_two_marks_is_interpolated_by_word_position_less_the_lead():
    toks = toks_of(uniform_text())
    assert P(toks, tok_of_word(toks, 180), DURATION) == 100 - LEAD


def test_the_lead_is_a_parameter():
    toks = toks_of(uniform_text())
    assert P(toks, tok_of_word(toks, 180), DURATION, lead=0) == 100


def test_turn_marks_are_not_counted_as_spoken_words():
    turns = toks_of(stamp(0) + " a " + " ".join([">>"] * 10) + " b " + stamp(10))
    assert P(turns, 12, None, lead=0) == 5


def test_after_the_last_mark_the_typical_rate_carries_on():
    toks = toks_of(uniform_text())
    assert P(toks, tok_of_word(toks, 470), DURATION, lead=0) == 245


def test_after_the_last_mark_the_time_is_clamped_to_the_duration():
    short = toks_of(uniform_text().rsplit(" ", 1)[0] + " " + " ".join(f"x{k}" for k in range(1000)))
    assert P(short, len(short) - 1, DURATION, lead=0) == DURATION


def test_before_the_first_mark_the_time_is_extrapolated_back_and_never_negative():
    pre = toks_of("p0 p1 p2 p3 p4 p5 p6 p7 p8 p9 " + uniform_text())
    assert P(pre, 0, DURATION, lead=0) == 5 and P(pre, 4, DURATION, lead=0) == 7
    assert P(pre, 0, DURATION, lead=20) == 0


def test_in_a_sparse_gap_a_word_is_placed_at_the_typical_rate_not_stretched():
    # 0:00, 1:00, 2:00 with 120 words a gap, then 10 words over 280 s, the shape a
    # paragraph-loop collapse leaves, then a normal gap again.
    words = iter(range(10 ** 6))
    parts = []
    for sec, count in ((0, 120), (60, 120), (120, 10), (400, 120), (460, 0)):
        parts.append(stamp(sec))
        parts += [f"w{next(words)}" for _ in range(count)]
    sparse = toks_of(" ".join(parts))
    # Stretched interpolation would give 232.
    assert P(sparse, tok_of_word(sparse, 244), 500, lead=0) == 122


def test_without_marks_the_time_comes_from_words_over_duration_or_is_none():
    nomark = toks_of(" ".join(f"w{k}" for k in range(200)))
    assert P(nomark, 100, 100, lead=0) == 50
    assert P(nomark, 100, None) is None


def test_with_one_mark_and_no_rate():
    one = toks_of("a b c " + stamp(30) + " d e f")
    assert P(one, 5, None, lead=0) == 30 and P(one, 0, None, lead=0) == 0


def test_a_token_index_outside_the_transcript_is_refused():
    toks = toks_of(uniform_text())
    with pytest.raises(IndexError):
        P(toks, len(toks), DURATION)


def test_mark_times_lists_every_mark_with_its_seconds():
    tokens = ["[00:00:05]", "a", "[01:02:03]", "b", "[0:00:05]"]
    assert V.mark_times(tokens) == [[0, 5], [2, 3723]]


def test_excerpts_cut_three_windows_with_the_nearest_earlier_mark():
    ex = V.excerpts("[00:00:00] " + " ".join(f"w{i}" for i in range(400)) + " [00:05:07] "
                    + " ".join(f"x{i}" for i in range(600)))
    assert len(ex) == 3
    assert ex[-1]["t"] == 307
    assert "[00:" not in "".join(e["text"] for e in ex)
    assert V.excerpts("") == []


def test_only_the_timing_module_converts_a_mark_to_seconds():
    pkg = Path(transcript_labeling.__file__).parent
    offenders = [p.name for p in pkg.rglob("*.py") if p.name != "timing.py" and "3600" in p.read_text()]
    assert offenders == []
    assert "3600" in inspect.getsource(V)
