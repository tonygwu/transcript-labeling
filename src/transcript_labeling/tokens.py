"""Whitespace tokens: the one coordinate system every label is written in.

Every offset in this package is an end-exclusive index into the whitespace
tokens (`re.finditer(r"\\S+", text)`) of an item's whole text, pinned by the
text's sha256 and token count. A token that is caption metadata, such as a
`[hh:mm:ss]` mark or a `>>` turn mark, keeps its index but is never speech, so
it is left out of counts and coverage without moving any offset.
"""
from __future__ import annotations

import hashlib
import re

#: What `token_kinds` can return. Only `speech` tokens are labelled.
KINDS = ("timestamp", "turn", "gap", "punctuation", "speech")


def token_kinds(tokens: list[str]) -> list[str]:
    """Caption metadata is never speech, without changing existing raw offsets."""
    kinds = []
    for word in tokens:
        if re.fullmatch(r"\[\d{2}:\d{2}:\d{2}\]", word):
            kind = "timestamp"
        elif re.fullmatch(r">{2,}", word):
            kind = "turn"
        elif re.fullmatch(r"[\[\]_…]+|\.{2,}|\[(?:inaudible|music|applause|laughter)\]", word, re.I):
            kind = "gap"
        elif not re.search(r"\w", word):
            kind = "punctuation"
        else:
            kind = "speech"
        kinds.append(kind)
    return kinds


def text_hash(text: str) -> str:
    """The sha256 that pins a text, and so every offset into it."""
    return hashlib.sha256(text.encode()).hexdigest()


def tokenize(text: str) -> list[str]:
    """The whitespace tokens of `text`, the units every offset counts."""
    return re.findall(r"\S+", text)


def char_span_to_tokens(toks: list, start: int, end: int) -> tuple[int, int]:
    """First and last (inclusive) whitespace tokens overlapping text[start:end].

    `toks` are the `re.finditer(r"\\S+", text)` matches of the text.
    """
    inside = [i for i, t in enumerate(toks) if t.end() > start and t.start() < end]
    if not inside:
        raise ValueError(f"no word overlaps characters {start}:{end}")
    return inside[0], inside[-1]
