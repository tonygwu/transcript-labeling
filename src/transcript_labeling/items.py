"""Items: one transcript each, with its catalog row and its focus passages.

`make_item` builds an item file from a text: whitespace tokens pinned by the
text's sha256 and token count, mark times, optional caption segments and the
header ranges that are not speech, and model suggestions. `add_items` validates a
whole batch, then writes it through a staging folder so a crash midway leaves the
previous dataset readable, with `catalog.json` written last.

Known gap: items are renamed into place one at a time and the manifest is written
after them, so a crash in the middle of an upsert can leave an item file whose
hash differs from the manifest. The page refuses such an item (it checks the hash
against the transcript index). `verify` here checks version-2 sessions only; the
version-3 verify, a later change, must report the mismatch too.

A focus passage is a part of an item the owner should look at first: today's
quote card, or one of a review's passages. An agent gives it by token offsets, or
by character offsets into the item's text, plus an optional title, why and
question.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
from pathlib import Path

from .dataset3 import Dataset
from .spans import ORIGINS, build_sidecar
from .store import load_answers, locked
from .timing import play_time
from .tokens import char_span_to_tokens, item_kinds, text_hash, tokenize

CONTEXT_WORDS = 35
ROW_FIELDS = ("title", "person", "role", "channel", "upload", "video_id", "duration", "description", "hints",
              "projects", "group", "words")
PASSAGE_TEXT_FIELDS = ("title", "why", "question", "project")


def make_item(key: str, text: str, *, suggestions=(), segments=None, meta=None, source=None) -> dict:
    """An item file: a version-2 sidecar plus the text, segments, header ranges and source."""
    item = {**build_sidecar(key, text, list(suggestions)), "text": text}
    if segments is not None:
        item["segments"] = segments
    if meta:
        item["meta"] = meta
    if source:
        item["source"] = source
    return item


def item_errors(item: dict, speakers: list[str]) -> list[str]:
    """Everything wrong with an item; empty when it is sound."""
    errs = []
    toks = item.get("tokens")
    if not isinstance(toks, list) or item.get("token_count") != len(toks):
        return ["tokens and token_count disagree"]
    if "text" in item:
        if tokenize(item["text"]) != toks:
            errs.append("tokens are not the whitespace tokens of text")
        if text_hash(item["text"]) != item.get("text_sha256"):
            errs.append("text_sha256 is not the sha256 of text")
    try:
        item_kinds(item)
    except ValueError as exc:
        errs.append(str(exc))
    for s in item.get("suggestions", []):
        a, b = s.get("start"), s.get("end")
        if type(a) is not int or type(b) is not int or not 0 <= a < b <= len(toks):
            errs.append(f"suggestion {s} is outside the text")
        elif s.get("speaker") not in speakers:
            errs.append(f"suggestion speaker {s.get('speaker')!r} is not one of {speakers}")
    for seg in item.get("segments") or []:
        for f in ("header", "body"):
            r = seg.get(f)
            if r is not None and not (isinstance(r, list) and len(r) == 2 and all(type(x) is int for x in r)
                                      and 0 <= r[0] <= r[1] <= len(toks)):
                errs.append(f"segment {f} {r!r} is outside the text")
    return errs


def focus_passage(item: dict, spec: dict, index: int) -> dict:
    """A quote row entry for one focus passage of `item`, from token or character offsets."""
    toks = list(re.finditer(r"\S+", item["text"])) if "text" in item else None
    if "char_start" in spec or "char_end" in spec:
        if toks is None:
            raise SystemExit(f"REFUSING: {item['key']} keeps no text, so a passage needs token offsets")
        first, last = char_span_to_tokens(toks, spec["char_start"], spec["char_end"])
        start, end = first, last + 1
    else:
        start, end = spec.get("start"), spec.get("end")
    n = item["token_count"]
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= n:
        raise SystemExit(f"REFUSING: passage {index} of {item['key']} is outside its {n} tokens")
    ctx = spec.get("context")
    lo, hi = (ctx if ctx else (max(0, start - CONTEXT_WORDS), min(n, end + CONTEXT_WORDS)))
    if not (type(lo) is int and type(hi) is int and 0 <= lo <= start and end <= hi <= n):
        raise SystemExit(f"REFUSING: passage {index} of {item['key']} has a context that does not hold it")
    words = item["tokens"]
    t = play_time(toks, start) if toks is not None else None
    group, name = item["key"].split("/")
    return {"qid": spec.get("id") or f"{group}:{name}:{start}", "t": t, "start": lo, "end": hi,
            "quote_start": start, "quote_end": end, "before": " ".join(words[lo:start]),
            "text": " ".join(words[start:end]), "after": " ".join(words[end:hi]),
            **{f: spec[f] for f in PASSAGE_TEXT_FIELDS if spec.get(f)}}


def catalog_row(key: str, spec: dict) -> dict:
    """The catalog row of an item: what the page lists and shows about it."""
    group, name = key.split("/")
    unknown = sorted(set(spec) - set(ROW_FIELDS))
    if unknown:
        raise SystemExit(f"REFUSING: unknown row fields {unknown}; known: {list(ROW_FIELDS)}")
    return {"key": key, "slug": group, "sid": name, "title": spec.get("title") or name,
            "person": spec.get("person") or "", **{f: spec[f] for f in ROW_FIELDS
                                                    if f in spec and f not in ("title", "person")}}


def add_items(ds: Dataset, state_dir: Path, entries: list[dict], *, upsert: bool = False) -> dict:
    """Validate and write a batch of items into `ds`. Returns counts.

    Each entry is `{"item": <make_item result>, "row": {...}, "focus": [passage specs]}`.
    The whole batch is checked before anything is written. An existing key is refused
    unless `upsert`, and even then a changed text under saved answers is refused,
    because those answers' offsets would point at other words.
    """
    keys = [e["item"]["key"] for e in entries]
    if len(set(keys)) != len(keys):
        raise SystemExit(f"REFUSING: the batch names a key twice: {sorted(k for k in keys if keys.count(k) > 1)[:3]}")
    with locked(state_dir / ".tl.lock"):
        # Everything is read again under the lock: a Dataset opened earlier may be stale.
        ds = Dataset.open(ds.root)
        if "legacy_from" in ds.manifest or ds.manifest["files"]["items_dir"].endswith(".transcripts"):
            raise SystemExit("REFUSING: this dataset reads a version-2 session's sidecars in place, so its items "
                             "cannot be rewritten here")
        speakers = ds.speaker_ids()
        for stale in state_dir.glob("staging-*"):
            # Left by a killed writer; nothing else stages while the lock is held.
            shutil.rmtree(stale, ignore_errors=True)
        manifest = json.loads(json.dumps(ds.manifest))
        catalog = ds.catalog()
        answers = load_answers(ds.answers)
        answered = {k for s in ("spans", "corrections", "labels") for k in answers[s]}
        answered_qids = set(answers["attribution"])
        old_q = {c["qid"]: (c.get("quote_start"), c.get("quote_end"))
                 for r in catalog["quote_rows"] for c in r.get("quotes", [])}
        rows = {r["key"]: r for r in catalog["rows"]}
        qrows = {r["key"]: r for r in catalog["quote_rows"]}
        owner = {c["qid"]: r["key"] for r in catalog["quote_rows"] for c in r.get("quotes", [])}
        problems, new_q = [], {}
        for e in entries:
            item = e["item"]
            key = item["key"]
            ds.item_path(key)  # validates the key shape
            problems += [f"{key}: {p}" for p in item_errors(item, speakers)]
            if key in manifest["items"]:
                if not upsert:
                    problems.append(f"{key}: already in the dataset; pass --upsert to replace it")
                elif key in answered and manifest["items"][key]["text_sha256"] != item["text_sha256"]:
                    problems.append(f"{key}: its text changed under saved answers")
                elif key in answered and (ds.item(key).get("meta") or []) != (item.get("meta") or []):
                    # Header ranges decide which words are speech, so they move results too.
                    problems.append(f"{key}: its header ranges changed under saved answers")
            unknown_row = sorted(set(e.get("row") or {}) - set(ROW_FIELDS))
            if unknown_row:
                problems.append(f"{key}: unknown row fields {unknown_row}; known: {list(ROW_FIELDS)}")
            quotes = [focus_passage(item, spec, i) for i, spec in enumerate(e.get("focus") or [])]
            dropped = sorted({c["qid"] for c in qrows.get(key, {}).get("quotes", [])} & answered_qids
                             - {q["qid"] for q in quotes})
            if dropped:
                problems.append(f"{key}: the new item would drop answered passages {dropped[:3]}")
            for q in quotes:
                if q["qid"] in new_q:
                    problems.append(f"{key}: passage id {q['qid']} is used twice")
                if owner.get(q["qid"], key) != key:
                    problems.append(f"{key}: passage id {q['qid']} already belongs to {owner[q['qid']]}")
                new_q[q["qid"]] = (q["quote_start"], q["quote_end"])
                if q["qid"] in answered_qids and q["qid"] in old_q and old_q[q["qid"]] != new_q[q["qid"]]:
                    problems.append(f"{key}: answered passage {q['qid']} would cover other words")
            e["_quotes"] = quotes
        if problems:
            raise SystemExit("REFUSING: nothing was written: " + "; ".join(problems[:8]))
        staging = state_dir / f"staging-{secrets.token_hex(6)}"
        try:
            for e in entries:
                rel = ds.item_path(e["item"]["key"]).relative_to(ds.items_dir)
                path = staging / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(e["item"], ensure_ascii=False))
            for e in entries:
                key = e["item"]["key"]
                rel = ds.item_path(key).relative_to(ds.items_dir)
                target = ds.items_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.replace(staging / rel, target)
                except OSError as exc:
                    raise SystemExit(f"REFUSING: could not move {key} into place ({exc}); the staging folder and "
                                     "the dataset must be on one filesystem") from exc
                manifest["items"][key] = {"text_sha256": e["item"]["text_sha256"],
                                          "token_count": e["item"]["token_count"]}
                # An upsert without a row keeps the row the item has.
                rows[key] = catalog_row(key, e["row"]) if e.get("row") or key not in rows else rows[key]
                if e["_quotes"]:
                    qrows[key] = {"key": key, "quotes": e["_quotes"]}
                elif key in qrows and upsert:
                    del qrows[key]
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        ds.write_manifest(manifest)
        ds.write_catalog({**catalog, "rows": list(rows.values()), "quote_rows": list(qrows.values())})
    return {"items": len(entries), "passages": sum(len(e["_quotes"]) for e in entries),
            "total_items": len(manifest["items"])}


def suggestion(start: int, end: int, speaker: str, **why) -> dict:
    """A model suggestion: a range the editor draws dashed until the owner confirms it."""
    if speaker in ORIGINS:
        raise SystemExit(f"REFUSING: {speaker!r} is an origin, not a speaker")
    return {"start": start, "end": end, "speaker": speaker, **why}
