"""The labeling page: one template, two editors, and the data it is filled with.

One page serves every dataset. What it asks is set by the dataset, never by the
project that owns it: the session block's MODES say which panels the page shows,
and its PROJECTS tag each item and quote.

  checklist  questions about the item, from the session's checklist spec. Saved in
             the answers file's `labels`.
  speakers   who said which words: word ranges over the transcript, saved in
             `spans`. A quote's result (`attribution`) is DERIVED from the ranges
             on the server; no button sets it.
  captions   corrections to the caption text, saved in `corrections` as an overlay
             on the original tokens.

`template.html`, `span_editor.js` and `corrections.js` are byte-identical to the
verbatim-index originals they were ported from, so a page rendered here equals
one rendered there (`tests/test_page.py` checks it against a golden file).
"""
from __future__ import annotations

import html
from pathlib import Path

from ..checklist import RESERVED_KEYS
from .data import page_payload

HERE = Path(__file__).resolve().parent


def asset(name: str) -> str:
    """A page file shipped with the package."""
    return (HERE / name).read_text(encoding="utf-8")


def render_page(data: dict, title: str, subtitle: str) -> str:
    """The page with its data, its words and its two embedded scripts filled in."""
    checklist = (data.get("session") or {}).get("checklist")
    for field in (checklist or {}).get("fields", []):
        for option in field.get("options", []):
            if str(option.get("key", "")).lower() in RESERVED_KEYS:
                raise SystemExit(f"REFUSING: checklist key {option['key']!r} is one the page already uses")
    page = asset("template.html")
    for mark, value in (("/*__DATA__*/null", page_payload(data)), ("/*__TITLE__*/", html.escape(title)),
                        ("/*__SUBTITLE__*/", html.escape(subtitle)),
                        ("/*__SPAN_EDITOR__*/", asset("span_editor.js")),
                        ("/*__TEXT_CORRECTIONS__*/", asset("corrections.js"))):
        if mark not in page:
            raise SystemExit(f"REFUSING: template placeholder {mark} is missing")
        page = page.replace(mark, value)
    return page
