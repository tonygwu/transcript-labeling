"""Exports of a version-3 dataset, the JSON agents read back, and its verify report.

  spans        every human word range, with its words (`text`, `speech_text`)
  segments     each caption segment of an item, split where the owner's ranges split it:
               pieces with a speaker or None (unmarked means unknown), and `reviewed`
               when every spoken word has a human range. `fill=suggestions` fills
               unmarked words from model suggestions, marked `origin: suggestion`.
  focus        each focus passage with its derived result and note
  corrections  caption corrections with the corrected reading text
  labels       complete checklist answers; partial ones are counted, never filled
  notes        every note: legacy ones inside the answers file and review.json's
  all          every kind above that the dataset's modes produce

With `no_text`, no transcript word and no free text leaves the export, so the file
may be committed to a repository. Each kind keeps a fixed list of structural fields
(offsets, ids, speaker ids, origins, hashes, times, and bool or enum checklist
answers) and drops everything else: caption labels, passage titles and questions,
notes, free-text checklist answers and every correction's words. A dropped note
leaves `has_note` or `has_notes` behind.
"""
from __future__ import annotations

import json
import re
import tempfile
import threading
import urllib.request
from pathlib import Path

from . import __version__
from .checklist import to_labels
from .corrections import corrected_text, validate_corrections
from .dataset3 import Dataset
from .export import label_docs
from .spans import validate_annotation
from .store import load_answers, load_review
from .tokens import item_kinds, text_hash

KINDS = ("spans", "segments", "focus", "corrections", "labels", "notes")
# The only fields a no_text export keeps, per kind and nesting level.
SPAN_KEEP = ("schema_version", "key", "text_sha256", "token_count", "checked_by", "updated_at", "assisted",
             "suggestions_sha256")
RANGE_KEEP = ("start", "end", "speaker", "origin")
SEGMENT_KEEP = ("index", "t_start", "t_end", "start", "end", "reviewed", "speakers", "split", "suggested_speakers")
PIECE_KEEP = ("start", "end", "speaker", "origin")
FOCUS_KEEP = ("qid", "key", "start", "end", "answer", "review_status", "unreviewed_words")
CORRECTION_KEEP = ("key", "text_sha256", "token_count", "revision", "checked_by", "updated_at",
                   "corrected_text_sha256")
HISTORY_KEEP = ("revision", "updated_at", "checked_by")
EDIT_KEEP = ("start", "end")
NOTE_KEEP = ("id", "where", "updated_at")


class ExportError(Exception):
    pass


def _pick(d: dict, keep) -> dict:
    return {k: d[k] for k in keep if k in d}


def _no_text(kind: str, data, ds: Dataset):
    """`data` of `kind` with only its structural fields (see the module docstring)."""
    if kind == "spans":
        return {k: {**_pick(v, SPAN_KEEP), "ranges": [_pick(r, RANGE_KEEP) for r in v["ranges"]]}
                for k, v in data.items()}
    if kind == "segments":
        return {k: [{**_pick(row, SEGMENT_KEEP), "pieces": [_pick(p, PIECE_KEEP) for p in row["pieces"]]}
                    for row in rows] for k, rows in data.items()}
    if kind == "focus":
        return [{**_pick(q, FOCUS_KEEP), "has_note": bool(q.get("note"))} for q in data]
    if kind == "corrections":
        def edits(es):
            return [_pick(e, EDIT_KEEP) for e in es]
        return {k: {**_pick(v, CORRECTION_KEEP), "edits": edits(v.get("edits", [])),
                    "history": [{**_pick(h, HISTORY_KEEP), "before": edits(h.get("before", [])),
                                 "after": edits(h.get("after", []))} for h in v.get("history", [])]}
                for k, v in data.items()}
    if kind == "labels":
        safe = {f["name"] for f in ds.manifest["checklist"]["fields"] if f["type"] in ("bool", "enum")}

        def doc(d):
            out = {k: v for k, v in d.items() if k in safe or k in ("checked_by", "draft_shown")}
            if "answers_before_draft" in d:
                out["answers_before_draft"] = {k: v for k, v in d["answers_before_draft"].items() if k in safe}
            return {**out, "has_notes": bool(d.get("notes"))}
        return {**data, "labels": {k: doc(d) for k, d in data["labels"].items()}}
    if kind == "notes":
        return [_pick(n, NOTE_KEEP) for n in data]
    raise ExportError(f"no no_text rule for {kind!r}")


def export_dataset(ds: Dataset, kind: str, *, reviewed: dict | None = None, review: dict | None = None,
                   no_text: bool = False, fill: str | None = None) -> dict:
    """One export of `ds`. `reviewed` is the answers with derived results (default: read and derive)."""
    if kind not in (*KINDS, "all"):
        raise ExportError(f"export kind {kind!r} must be one of {(*KINDS, 'all')}")
    if fill not in (None, "suggestions"):
        raise ExportError(f"fill {fill!r} must be 'suggestions' or absent")
    answers = reviewed if reviewed is not None else _reviewed(ds)
    review = review if review is not None else load_review(ds.review_path)
    kinds = [k for k in KINDS if _applies(ds, k)] if kind == "all" else [kind]
    body = {}
    for k in kinds:
        if not _applies(ds, k):
            raise ExportError(f"dataset {ds.project}/{ds.name} has no {k} to export (modes: {ds.modes})")
        body[k] = EXPORTERS[k](ds, answers, review, fill)
        if no_text:
            body[k] = _no_text(k, body[k], ds)
    out = {"schema_version": 1, "tool": f"transcript-labeling {__version__}", "project": ds.project,
           "dataset": ds.name, "kind": kind, "no_text": no_text,
           **({"fill": fill} if fill else {}),
           "note": "Unmarked words are unknown. Suggestions are never labels unless fill=suggestions says so, "
                   "and then each filled piece says origin: suggestion.",
           **(body if kind == "all" else {"data": body[kind]})}
    return out


def _applies(ds: Dataset, kind: str) -> bool:
    return {"spans": "speakers", "segments": "speakers", "focus": "speakers", "corrections": "captions",
            "labels": "checklist"}.get(kind, ds.modes[0]) in ds.modes


def _reviewed(ds: Dataset) -> dict:
    from .spans import reconcile_quote_reviews
    return reconcile_quote_reviews(load_answers(ds.answers), ds.root / "unused.html", ds.catalog()["quote_rows"],
                                   item_path=ds.item_path, review=ds.review)


def _spans(ds, answers, review, fill):
    out = {}
    for key, value in answers["spans"].items():
        item = ds.item(key)
        validate_annotation(value, item, ds.speaker_ids())
        kinds = item_kinds(item)
        out[key] = {**value, "ranges": [{**r, "text": " ".join(item["tokens"][r["start"]:r["end"]]),
                                         "speech_text": " ".join(item["tokens"][i] for i in range(r["start"], r["end"])
                                                                 if kinds[i] == "speech")}
                                        for r in value["ranges"]]}
    return out


def _labels_at(ranges: list[dict], n: int) -> list:
    at = [None] * n
    for r in ranges:
        for i in range(r["start"], r["end"]):
            at[i] = (r["speaker"], r.get("origin", "human"))
    return at


def _token_spans(item: dict) -> list | None:
    """Each token's (start, end) character offsets in the item's text, or None for a legacy item."""
    if "text" not in item:
        return None
    spans = [m.span() for m in re.finditer(r"\S+", item["text"])]
    if len(spans) != item["token_count"]:
        raise ExportError(f"item {item['key']}: its text splits into {len(spans)} tokens, not {item['token_count']}")
    return spans


def _segments(ds, answers, review, fill):
    """Each caption segment, split into pieces where the owner's ranges split it.

    A piece is a contiguous run of the caption body's tokens with one speaker and one
    origin. Punctuation and other non-speech tokens join the piece they follow, so a
    piece's `text` is an exact substring of the item's text (legacy items, which keep
    only tokens, join them with spaces). `speakers`, `split` and `reviewed` describe
    the owner's own ranges only; with `fill=suggestions`, `suggested_speakers` lists
    what the filled pieces add.
    """
    out = {}
    for key in ds.manifest["items"]:
        item = ds.item(key)
        segments = item.get("segments")
        if not segments:
            continue
        kinds = item_kinds(item)
        offsets = _token_spans(item)
        human = _labels_at((answers["spans"].get(key) or {}).get("ranges", []), item["token_count"])
        suggested = _labels_at([{**s, "origin": "suggestion"} for s in item.get("suggestions", [])],
                               item["token_count"]) if fill == "suggestions" else [None] * item["token_count"]
        rows = []
        for n, seg in enumerate(segments):
            a, b = seg["body"]
            speech = [i for i in range(a, b) if kinds[i] == "speech"]
            pieces, cur = [], None
            for i in range(a, b):
                if kinds[i] != "speech":
                    if cur and cur["end"] == i and kinds[i] != "meta":
                        cur["end"] = i + 1
                    continue
                who = human[i] or suggested[i] or (None, None)
                if cur and cur["speaker"] == who[0] and cur["origin"] == who[1] and cur["end"] == i:
                    cur["end"] = i + 1
                else:
                    cur = {"start": i, "end": i + 1, "speaker": who[0], "origin": who[1]}
                    pieces.append(cur)
            for piece in pieces:
                s0, e0 = piece["start"], piece["end"]
                piece["text"] = (item["text"][offsets[s0][0]:offsets[e0 - 1][1]] if offsets
                                 else " ".join(item["tokens"][s0:e0]))
            mine = [p for p in pieces if p["origin"] not in (None, "suggestion")]
            row = {"index": n, "label": seg.get("label"), "t_start": seg.get("t_start"),
                   "t_end": seg.get("t_end"), "start": a, "end": b,
                   "reviewed": bool(speech) and all(human[i] for i in speech),
                   "speakers": sorted({p["speaker"] for p in mine}),
                   "split": len({p["speaker"] for p in mine}) > 1, "pieces": pieces}
            if fill == "suggestions":
                row["suggested_speakers"] = sorted({p["speaker"] for p in pieces if p["origin"] == "suggestion"})
            rows.append(row)
        out[key] = rows
    return out


def _focus(ds, answers, review, fill):
    out = []
    for row in ds.catalog()["quote_rows"]:
        for q in row.get("quotes", []):
            result = answers["attribution"].get(q["qid"], {})
            out.append({"qid": q["qid"], "key": row["key"], "start": q["quote_start"], "end": q["quote_end"],
                        **{f: q[f] for f in ("title", "why", "question") if f in q},
                        "quote_text": q.get("text", ""), "answer": result.get("answer"),
                        "review_status": result.get("review_status", "incomplete"),
                        "unreviewed_words": result.get("unreviewed_words"),
                        "note": (review["notes"].get(q["qid"]) or {}).get("text")})
    return out


def _corrections(ds, answers, review, fill):
    out = {}
    for key, value in answers["corrections"].items():
        item = ds.item(key)
        validate_corrections(value, item)
        text = corrected_text(item, value)
        out[key] = {**value, "corrected_text": text, "corrected_text_sha256": text_hash(text)}
    return out


def _labels(ds, answers, review, fill):
    checklist = ds.manifest["checklist"]
    keys = {r["key"] for r in ds.catalog()["rows"]}
    return to_labels(label_docs(answers["labels"]), keys, checklist)


def _notes(ds, answers, review, fill):
    notes = [{"id": k, "where": "review", "text": v["text"], "updated_at": v.get("updated_at")}
             for k, v in sorted(review["notes"].items())]
    notes += [{"id": k, "where": "labels", "text": v["notes"]} for k, v in sorted(answers["labels"].items())
              if isinstance(v, dict) and v.get("notes")]
    notes += [{"id": k, "where": "attribution", "text": v["notes"]} for k, v in sorted(answers["attribution"].items())
              if isinstance(v, dict) and v.get("notes")]
    return notes


EXPORTERS = {"spans": _spans, "segments": _segments, "focus": _focus, "corrections": _corrections,
             "labels": _labels, "notes": _notes}


# ------------------------------------------------------------------ verify

def verify_dataset(ds: Dataset, expect_answers_sha256: str | None = None, probe: bool = True) -> dict:
    """The verify report for a version-3 dataset. `report["ok"]` is true only when every check passed."""
    from .verify import sha256_bytes
    raw = ds.answers.read_bytes() if ds.answers.exists() else b""
    before = sha256_bytes(raw)
    checks: list[dict] = []

    def check(name, ok, detail=None):
        checks.append({"check": name, "ok": bool(ok), **({"detail": detail} if detail is not None and not ok else {})})

    if expect_answers_sha256 is not None:
        check("the answers file has the expected sha256", before == expect_answers_sha256,
              f"{before} != {expect_answers_sha256}")
    answers = load_answers(ds.answers)
    data = ds.page_data()
    from .service import ids_of
    ids = ids_of(data)
    off = {s: sorted(set(answers[s]) - ids[s]) for s in answers}
    check("every saved answer names an id the dataset carries", not any(off.values()),
          {s: v[:5] for s, v in off.items() if v})
    bad_items, items = [], {}
    for key, entry in ds.manifest["items"].items():
        try:
            item = ds.item(key)
        except (OSError, ValueError, SystemExit) as exc:  # Dataset.item refuses a corrupt file by name
            bad_items.append(f"{key}: {exc}")
            continue
        items[key] = item
        if (item["text_sha256"], item["token_count"]) != (entry["text_sha256"], entry["token_count"]) \
                or item["token_count"] != len(item["tokens"]):
            bad_items.append(f"{key}: item hash or token count differs from the manifest")
    check("every item file matches the manifest's hash and token count", not bad_items, bad_items[:5])
    bad = []
    for key, value in answers["spans"].items():
        try:
            validate_annotation(value, items[key], ds.speaker_ids())
        except (KeyError, ValueError) as exc:
            bad.append(f"{key}: {exc}")
    check("every speaker annotation validates against its item", not bad, bad[:5])
    bad = []
    for key, value in answers["corrections"].items():
        try:
            validate_corrections(value, items[key])
        except (KeyError, ValueError) as exc:
            bad.append(f"{key}: {exc}")
    check("every caption correction validates against its item", not bad, bad[:5])
    reviewed = _reviewed(ds)
    check("a read returns the answers file unchanged (passage results already follow the ranges)",
          reviewed == answers, sorted(q for q in reviewed["attribution"]
                                      if reviewed["attribution"][q] != answers["attribution"].get(q))[:5])
    check("the manifest names the tool version that wrote it", isinstance(ds.manifest.get("created_with"), str),
          ds.manifest.get("created_with"))
    report = {"project": ds.project, "dataset": ds.name, "answers_sha256": before,
              "schema_version": ds.manifest["schema_version"], "created_with": ds.manifest.get("created_with"),
              "tool_version": __version__, "checks": checks}
    if probe:
        report["live_serve"] = _probe(ds, reviewed, check)
    after = sha256_bytes(ds.answers.read_bytes()) if ds.answers.exists() else sha256_bytes(b"")
    check("the answers file is byte-identical after verify", after == before, f"{before} -> {after}")
    report["ok"] = all(c["ok"] for c in checks)
    return report


def _probe(ds: Dataset, reviewed: dict, check) -> dict:
    """Serve the dataset from a throwaway in-process service with its own home, token and session."""
    from .home import Home
    from .registry import Registry
    from .service import Service, build_service_server
    with tempfile.TemporaryDirectory() as td:
        home = Home(Path(td) / "home")
        home.init()
        reg = Registry(home)
        reg.create_project(ds.project, privacy="git-private", writers=["verify"],
                           git={"checkout": str(ds.root.resolve().parent), "branch": "verify", "commit": False})
        reg.add_dataset(ds.project, ds.name, ds.root.resolve().name)
        svc = Service(home)
        httpd = build_service_server(svc, 0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            session, target = svc.auth.redeem(svc.auth.mint_code(f"/p/{ds.project}/{ds.name}/"))
            base = f"http://127.0.0.1:{httpd.server_address[1]}{target}"

            def get(path):
                req = urllib.request.Request(base + path, headers={"X-TL-Session": session})
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.loads(r.read())
            page, served = get("api/page"), get("api/answers")
        finally:
            httpd.shutdown()
            httpd.server_close()
    served.pop("review", None)
    served.pop("versions", None)
    check("a throwaway server serves the dataset's page data", page == ds.page_data())
    check("its answers equal the file's, with passage results derived", served == reviewed)
    return {"answers_sections": {s: len(v) for s, v in served.items()}}


# ------------------------------------------------------------------ agent API

def agent_export(svc, query: dict) -> dict:
    from .agent_api import ApiError, _q
    project, dataset, kind = _q(query, "project"), _q(query, "dataset"), _q(query, "kind")
    ds = svc.dataset(project, dataset)
    flag = query.get("no_text", [""])[0]
    if flag not in ("", "0", "1", "false", "true"):
        raise ApiError(400, f"no_text must be 1, true, 0 or false, got {flag!r}")
    reviewed = svc.reviewed(ds, svc.answers_store(project, dataset, ds).load())
    try:
        return export_dataset(ds, kind, reviewed=reviewed, review=svc.review_store(project, dataset, ds).load(),
                              no_text=flag in ("1", "true"), fill=query.get("fill", [None])[0])
    except ExportError as exc:
        raise ApiError(400, str(exc))


def agent_verify(svc, query: dict) -> dict:
    from .agent_api import _q
    project, dataset = _q(query, "project"), _q(query, "dataset")
    expect = query.get("expect_answers_sha256", [None])[0]
    return verify_dataset(svc.dataset(project, dataset), expect)


SCHEMAS = {
    "item": {"name": "letters, digits, _ or -", "text": "the transcript, kept exactly",
             "group": "optional; the item key is <group>/<name> (default group 'item')",
             "format": "optional: zoom-notes | bracket-ts | plain (default: detected)",
             "row": "optional catalog fields: title, person, role, channel, upload, video_id, duration, "
                    "description, hints, projects, group, words",
             "focus": "optional passages: [{start, end} or {char_start, char_end}, title?, why?, question?, id?, "
                      "context?: [start, end]]",
             "suggest_map": "optional 'Label=speaker_id,...': segment labels become model suggestions",
             "source": "optional object kept with the item, such as the file name; its format is always the "
                       "parser's"},
    "dataset": {"title": "", "modes": "subset of checklist, speakers, captions",
                "speakers": "[{id, label, key}]; label may be a template such as {person}",
                "unclear": "{id, label, key} (default unclear / Unsure / u)",
                "mixed_answer": "result of a passage several speakers cover (default mixed)",
                "checklist": "checklist spec, with the checklist mode", "reveal": "after_answer | always",
                "media": "{url: 'https://...{video_id}...{t}...'} or absent"},
    "answers": {"labels": "{key: checklist answers}", "attribution": "{qid: result derived from spans}",
                "spans": "{key: {key, text_sha256, token_count, ranges: [{start, end, speaker, origin}], checked_by}}",
                "corrections": "{key: {edits, revision, history, ...}}"},
    "export": {"kinds": list(KINDS) + ["all"], "no_text": "drop every transcript word",
               "fill": "suggestions: fill unmarked segment words from suggestions, marked origin: suggestion"},
}


def agent_schema(svc, query: dict) -> dict:
    from .agent_api import ApiError
    name = query.get("name", [""])[0]
    if name not in SCHEMAS:
        raise ApiError(400, f"schema name must be one of {sorted(SCHEMAS)}")
    return {"name": name, "schema": SCHEMAS[name]}
