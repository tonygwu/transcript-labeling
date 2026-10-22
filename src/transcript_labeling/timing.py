"""Clock times inside a transcript: play links, mark times and reading excerpts.

A transcript may carry `[hh:mm:ss]` marks, which a caption fetcher writes about
once a minute, each right before the caption whose start it records. This module
places a word in time for its play link and cuts reading excerpts.

It is the ONLY module in this package that turns a mark's hours and minutes into
seconds (`tests/test_timing.py` checks the others do not), so a focus passage and
an excerpt cannot be timed two different ways.
"""
from __future__ import annotations

import re

EXCERPT_AT = (0.12, 0.50, 0.85)
EXCERPT_WORDS = 130
MARK = re.compile(r"\[(\d{2}):(\d{2}):(\d{2})\]")
# Seconds of lead before a quote's estimated start. MEASURED 2026-09-28 in
# verbatim-index on one video (3,187 words, true caption times fetched once):
# interpolation lands a median 1.4 s from the true time, and with a 5 s lead
# playback starts after the word for 19 of 3,187 words, against 192 with 3 s.
PLAY_LEAD_SECONDS = 5
# A gap between two marks whose words arrive at less than this share of the
# recording's typical rate has lost words, as a paragraph-loop collapse does.
SPARSE_GAP_SHARE = 0.5


def play_time(toks: list, a: int, duration=None, lead: int = PLAY_LEAD_SECONDS):
    """Whole seconds at which to start playing the word at token index `a`, or None.

    `toks` are the `re.finditer(r"\\S+", text)` matches of a transcript. A mark is
    exact to the second and the words between two marks fill the time between
    them, so the word's time is interpolated between the marks around it by
    spoken-word position. Marks and `>>` turn marks are not words. The result is
    `lead` seconds earlier, so playback starts just before the quote.

    Three cases do not interpolate. Each leans toward starting early rather than
    late, because a late start makes the listener scrub back past the opening
    words they must attribute. None of them is guaranteed to land before the quote:

      - A SPARSE gap, whose words arrive at under SPARSE_GAP_SHARE of the typical
        rate, has lost words. Where the survivors sat inside it is unknown, so the
        word is placed at the typical rate from the earlier mark. That lands in the
        first half of the gap, since a sparse gap's words take under half its time
        at the typical rate.
      - Before the first mark, or after the last, the word is extrapolated from the
        nearest mark at the typical rate, and clamped to [0, duration].
      - With no rate at all (one mark and no duration) the nearest earlier mark, or
        0, is used; with no mark and no duration the answer is None.

    The typical rate is the median words per second over the recording's gaps, not
    word count over `duration`, which counts intros and outros with no speech.
    With fewer than two marks it falls back to words over `duration`.
    """
    if not 0 <= a < len(toks):
        raise IndexError(f"token {a} is outside a transcript of {len(toks)} tokens")
    marks, words, pos = [], 0, None
    for i, t in enumerate(toks):
        if i == a:
            pos = words
        m = MARK.fullmatch(t.group())
        if m:
            h, mi, s = (int(x) for x in m.groups())
            marks.append((words, h * 3600 + mi * 60 + s))
        elif t.group() != ">>":
            words += 1
    rates = sorted((c1 - c0) / (s1 - s0) for (c0, s0), (c1, s1) in zip(marks, marks[1:])
                   if c1 > c0 and s1 > s0)
    if rates:
        typical = rates[len(rates) // 2]
    elif duration and words:
        typical = words / duration
    else:
        typical = None
    before = [k for k, (c, _) in enumerate(marks) if c <= pos]
    if before:
        k = before[-1]
        c0, s0 = marks[k]
        if k + 1 < len(marks):
            c1, s1 = marks[k + 1]
            if typical and (c1 - c0) / max(s1 - s0, 1e-9) < SPARSE_GAP_SHARE * typical:
                t = s0 + (pos - c0) / typical
            else:
                t = s0 + (pos - c0) / (c1 - c0) * (s1 - s0)
        else:
            t = s0 + (pos - c0) / typical if typical else s0
    elif marks:
        c1, s1 = marks[0]
        t = s1 - (c1 - pos) / typical if typical else 0
    elif typical:
        t = pos / typical
    else:
        return None
    if duration:
        t = min(t, duration)
    return max(0, int(t) - lead)


def clock_seconds(h, m, s) -> int:
    """Seconds since 00:00:00 for an `hh:mm:ss` clock reading given as its three parts."""
    return int(h) * 3600 + int(m) * 60 + int(s)


def mark_seconds(token: str) -> int | None:
    """The seconds a `[hh:mm:ss]` mark token records, or None for any other token."""
    mark = MARK.fullmatch(token)
    if not mark:
        return None
    return clock_seconds(*mark.groups())


def mark_times(tokens: list[str]) -> list[list[int]]:
    """`[token index, seconds]` for every `[hh:mm:ss]` mark token, in order."""
    return [[i, secs] for i, token in enumerate(tokens) if (secs := mark_seconds(token)) is not None]


def excerpts(text: str) -> list[dict]:
    """Three windows of the transcript, each with the time of the nearest earlier mark."""
    words = list(re.finditer(r"\S+", text))
    out = []
    for frac in EXCERPT_AT:
        if not words:
            break
        i = min(int(len(words) * frac), max(0, len(words) - EXCERPT_WORDS))
        span = words[i:i + EXCERPT_WORDS]
        start = span[0].start()
        marks = [m for m in MARK.finditer(text, 0, start)]
        secs = mark_seconds(marks[-1].group()) if marks else None
        chunk = text[start:span[-1].end()]
        out.append({"at": frac, "t": secs, "text": MARK.sub("", chunk).strip(),
                    "start": i, "end": i + len(span)})
    return out
