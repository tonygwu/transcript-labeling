"""The page's data block: what a built page carries, and the ids it may answer.

A version-2 page (the verbatim-index format) embeds its data as one line,
`const D = {...};`, with `</` written `<\\/` so it is safe inside a <script>.
"""
from __future__ import annotations

import json
import re

#: The page's data block, one line: `const D = {...};`. `</` is written `<\/`.
DATA_LINE = re.compile(r"^const D = (\{.*\});$", re.M)


def page_payload(data: dict) -> str:
    """The data block's JSON, safe inside a <script> element."""
    return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")


def page_data(page: str) -> dict:
    """The data block of a built page. A page without one stops the caller."""
    m = DATA_LINE.search(page)
    if not m:
        raise SystemExit("REFUSING: no `const D = {...};` data block in the page; rebuild it")
    return json.loads(m.group(1).replace("<\\/", "</"))


def page_ids(data: dict) -> dict[str, set[str]]:
    """The recording keys and quote ids the page can send, per answers section.

    Raises rather than returning empty sets: a page that carries nothing to answer
    is a build this tool does not understand, and accepting every id from it would
    turn a loud mismatch into a file of answers nothing can export.
    """
    keys = {r["key"] for r in data.get("rows", []) if r.get("key")}
    qids = {c["qid"] for r in data.get("quote_rows", []) for c in r.get("quotes", []) if c.get("qid")}
    transcripts = set(data.get("transcript_index", {}))
    if not keys and not qids and not transcripts:
        raise SystemExit("REFUSING: the page carries no recordings and no quotes")
    return {"labels": keys, "attribution": qids, "spans": transcripts, "corrections": transcripts}
