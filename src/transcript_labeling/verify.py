"""Prove a dataset's answers are intact, as a JSON report that names every check.

`verify_session` checks: the answers sha256 against an expected one; every saved
answer names an id the page carries; every sidecar matches the page's hash and
token count and the manifest; every range and correction validates; a read
returns the file unchanged; each export the manifest lists is reproduced; a live
server on a free port serves the page byte for byte and the same answers; and the
answers file is byte-identical afterwards. It also reports a digest of every
sidecar, so a before and after can be compared.

The package reproduces the exports it knows (`labels`, `corrections`, `spans`).
A client with its own export kinds passes an `exporters` entry for each; an
export the manifest lists with no exporter is a failed check, never a skip.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import urllib.request
from pathlib import Path
from typing import Callable

from .corrections import export_corrections, validate_corrections
from .dataset import Session
from .export import export_labels
from .page.data import page_ids
from .spans import import_spans, reconcile_quote_reviews, validate_annotation
from .store import load_answers

Exporter = Callable[[Session], object]


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sidecar_digest(session: Session) -> str:
    """One hash over every sidecar file's name and bytes, to compare across a rebuild.

    The same digest verbatim-index's `annotations.py verify` reports, so a report
    from before a migration compares with one from after.
    """
    d = session.page.with_suffix(".transcripts")
    parts = sorted(f"{p.relative_to(d)} {sha256_bytes(p.read_bytes())}" for p in d.rglob("*.json"))
    return sha256_bytes("\n".join(parts).encode())


def _in_scratch(write: Callable[[Path], dict]) -> dict:
    """An export function that writes a file, computed into a scratch file and read back."""
    with tempfile.TemporaryDirectory() as td:
        return write(Path(td) / "export.json")


BUILTIN_EXPORTERS: dict[str, Exporter] = {
    "labels": lambda s: export_labels(s)["labels"],
    "corrections": lambda s: _in_scratch(lambda out: export_corrections(s.answers, s.page, out)),
    "spans": lambda s: _in_scratch(lambda out: import_spans(s.answers, s.page, out)),
}


def _count(values) -> dict:
    out: dict = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: str(kv[0])))


def verify_session(session: Session, expect_answers_sha256: str | None = None,
                   exporters: dict[str, Exporter] | None = None) -> dict:
    """The verify report for `session`. `report["ok"]` is true only when every check passed."""
    exporters = {**BUILTIN_EXPORTERS, **(exporters or {})}
    raw_bytes = session.answers.read_bytes() if session.answers.exists() else b""
    before = sha256_bytes(raw_bytes)
    checks: list[dict] = []
    report: dict = {"session": session.name, "projects": session.projects, "modes": session.modes,
                    "answers_sha256": before}

    def check(name, ok, detail=None):
        checks.append({"check": name, "ok": bool(ok), **({"detail": detail} if detail is not None and not ok else {})})

    if expect_answers_sha256:
        check("the answers file has the expected sha256", before == expect_answers_sha256,
              f"{before} != {expect_answers_sha256}")
    answers = load_answers(session.answers)
    data = session.data()
    ids = page_ids(data)
    spans = answers["spans"]
    report["counts"] = {
        "labels": len(answers["labels"]), "attribution": len(answers["attribution"]),
        "attribution_by_method": _count(a.get("review_method", "legacy_quote_check")
                                        for a in answers["attribution"].values()),
        "spans_recordings": len(spans), "spans_ranges": sum(len(v["ranges"]) for v in spans.values()),
        "ranges_by_speaker": _count(r["speaker"] for v in spans.values() for r in v["ranges"]),
        "ranges_by_origin": _count(r["origin"] for v in spans.values() for r in v["ranges"]),
        "corrections_recordings": len(answers["corrections"]),
        "correction_edits": sum(len(v["edits"]) for v in answers["corrections"].values()),
        "correction_revisions": {k: v["revision"] for k, v in answers["corrections"].items()},
        "page_recordings": len(ids["spans"]), "page_checklist_rows": len(ids["labels"]),
        "page_quotes": len(ids["attribution"]),
    }
    off_page = {s: sorted(set(answers[s]) - ids[s]) for s in answers}
    check("every saved answer names an id the page carries", not any(off_page.values()),
          {s: v[:5] for s, v in off_page.items() if v})
    bad_side, sidecars = [], {}
    for key, idx in data.get("transcript_index", {}).items():
        try:
            side = json.loads(session.sidecar(key).read_text())
        except (OSError, ValueError) as exc:
            bad_side.append(f"{key}: {exc}")
            continue
        sidecars[key] = side
        if (side["text_sha256"], side["token_count"]) != (idx["text_sha256"], idx["token_count"]) \
                or side["token_count"] != len(side["tokens"]):
            bad_side.append(f"{key}: sidecar hash or token count differs from the page")
    check("every recording's sidecar matches the page's transcript hash and token count", not bad_side, bad_side[:5])
    mrec = session.manifest["recordings"]
    check("the manifest lists exactly the page's recordings, with the same hashes",
          set(mrec) == set(data.get("transcript_index", {})) and all(
              mrec[k]["text_sha256"] == v["text_sha256"] for k, v in data.get("transcript_index", {}).items()),
          sorted(set(mrec) ^ set(data.get("transcript_index", {})))[:5])
    bad = []
    for key, value in spans.items():
        try:
            validate_annotation(value, sidecars[key])
        except (KeyError, ValueError) as exc:
            bad.append(f"{key}: {exc}")
    check("every speaker annotation validates against its transcript", not bad, bad[:5])
    bad = []
    for key, value in answers["corrections"].items():
        try:
            validate_corrections(value, sidecars[key])
        except (KeyError, ValueError) as exc:
            bad.append(f"{key}: {exc}")
    check("every caption correction validates against its transcript", not bad, bad[:5])
    reviewed = reconcile_quote_reviews(answers, session.page, data.get("quote_rows", []))
    check("a read returns the answers file unchanged (quote results already follow the ranges)",
          reviewed == answers, sorted(q for q in reviewed["attribution"]
                                      if reviewed["attribution"][q] != answers["attribution"].get(q))[:5])
    for kind, name in (session.manifest["files"].get("exports") or {}).items():
        if kind not in exporters:
            check(f"export {kind} reproduces {name}", False, f"no exporter for export kind {kind!r}")
            continue
        disk = json.loads((session.root / name).read_text())
        try:
            got = exporters[kind](session)
        except (SystemExit, ValueError) as exc:
            # An export that refuses is a failed check, not a missing report.
            check(f"export {kind} reproduces {name}", False, f"the export refused: {exc}")
            continue
        check(f"export {kind} reproduces {name}", got == disk, "differs" if got != disk else None)
    report["live_serve"] = _serve_probe(session, reviewed, check)
    after = sha256_bytes(session.answers.read_bytes()) if session.answers.exists() else sha256_bytes(b"")
    check("the answers file is byte-identical after verify", after == before, f"{before} -> {after}")
    report["sidecars_sha256"] = sidecar_digest(session)
    report["page_sha256"] = sha256_bytes(session.page.read_bytes())
    report["checks"] = checks
    report["ok"] = all(c["ok"] for c in checks)
    return report


def _serve_probe(session: Session, reviewed: dict, check) -> dict:
    """Start the real server on a free port, read the page and the answers, stop it."""
    from .server import build_server
    httpd = build_server([session], 0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}/s/{session.name}/"
    try:
        with urllib.request.urlopen(base, timeout=30) as r:
            page = r.read()
        with urllib.request.urlopen(base + "api/answers", timeout=30) as r:
            got = json.loads(r.read())
    finally:
        httpd.shutdown()
        httpd.server_close()
    check("the server serves the session's page byte for byte", page == session.page.read_bytes())
    check("the server's answers equal the file's, with quote results derived", got == reviewed)
    return {"page_bytes": len(page), "answers_sections": {s: len(v) for s, v in got.items()}}
