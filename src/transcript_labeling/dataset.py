"""A labeling dataset on disk: its manifest, its page data and its answers file.

This module reads the version-2 layout that verbatim-index writes, which is a
SESSION directory:

  manifest.json   schema_version 2, tool "annotations", name, projects, modes,
                  blind, transcript_dir, files {page, answers, quote_key,
                  exports?}, recordings {key: {projects, text_sha256, token_count}}
  <page>          the page, with its data block (`page.data`)
  <page stem>.transcripts/<group>/<name>.json   one sidecar per item
  <answers>       `{labels, attribution, spans, corrections}`

The package knows no project vocabulary of its own: a version-2 manifest may name
any projects. The modes are the package's: what a person records.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .page.data import page_data, page_ids
from .spans import quote_review, sidecar_path
from .store import load_answers, write_atomic

SCHEMA_VERSION = 2
MANIFEST = "manifest.json"
MODES = ("checklist", "speakers", "captions")
#: The answers section each mode writes. `attribution` belongs to no mode: the
#: server derives it from `spans` and never accepts it from the page.
MODE_SECTION = {"checklist": "labels", "speakers": "spans", "captions": "corrections"}
REQUIRED = ("schema_version", "tool", "name", "projects", "modes", "blind", "transcript_dir", "files", "recordings")
REQUIRED_FILES = ("page", "answers", "quote_key")
#: A session name, as verbatim's `build` and `migrate` accept it. The server mounts a
#: session at `/s/<name>/`, so a name that needs URL escaping could never be reached.
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class Session:
    """A version-2 session directory and its manifest. Opening one checks the manifest whole."""

    def __init__(self, root: Path, manifest: dict):
        self.root = root
        self.manifest = manifest

    @classmethod
    def open(cls, root) -> "Session":
        root = Path(root)
        path = root / MANIFEST
        if not path.exists():
            raise SystemExit(f"REFUSING: {root} has no {MANIFEST}")
        manifest = json.loads(path.read_text())
        problems = manifest_errors(manifest)
        if problems:
            raise SystemExit(f"REFUSING: {path}: " + "; ".join(problems))
        return cls(root, manifest)

    @property
    def name(self) -> str:
        return self.manifest["name"]

    @property
    def projects(self) -> list[str]:
        return self.manifest["projects"]

    @property
    def modes(self) -> list[str]:
        return self.manifest["modes"]

    def file(self, role: str) -> Path | None:
        name = self.manifest["files"].get(role)
        return self.root / name if name else None

    @property
    def page(self) -> Path:
        return self.file("page")

    @property
    def answers(self) -> Path:
        return self.file("answers")

    def sidecar(self, key: str) -> Path:
        return sidecar_path(self.page, key)

    def data(self) -> dict:
        return page_data(self.page.read_text())

    def sections(self) -> set[str]:
        """The answers sections the page may write, from the session's modes."""
        return {MODE_SECTION[m] for m in self.modes}


def manifest_errors(manifest: dict) -> list[str]:
    """Everything wrong with a version-2 manifest; empty when it is sound."""
    if not isinstance(manifest, dict):
        return ["the manifest is not an object"]
    if manifest.get("schema_version") == 1:
        return ["this is a version-1 speaker-check manifest; adopt the session with verbatim's `annotations.py migrate`"]
    errs = [f"missing `{k}`" for k in REQUIRED if k not in manifest]
    if errs:
        return errs
    if manifest["schema_version"] != SCHEMA_VERSION or manifest["tool"] != "annotations":
        errs.append(f"schema_version {manifest['schema_version']!r} / tool {manifest['tool']!r} is not "
                    f"{SCHEMA_VERSION} / 'annotations'")
    if not isinstance(manifest["name"], str) or not NAME.fullmatch(manifest["name"]):
        errs.append(f"name {manifest['name']!r} must match {NAME.pattern}")
    projects = manifest["projects"]
    projects_ok = isinstance(projects, list) and projects and all(isinstance(p, str) and p for p in projects)
    if not projects_ok:
        errs.append(f"projects {projects!r} must be a non-empty list of names")
    modes = manifest["modes"]
    if not isinstance(modes, list) or not modes or not all(isinstance(m, str) for m in modes) \
            or set(modes) - set(MODES):
        errs.append(f"modes {modes!r} must be a non-empty subset of {list(MODES)}")
    if not isinstance(manifest["files"], dict):
        errs.append("files must be an object")
    else:
        errs += [f"files has no `{k}`" for k in REQUIRED_FILES if not manifest["files"].get(k)]
    recordings = manifest["recordings"]
    if not isinstance(recordings, dict) or not all(isinstance(r, dict) for r in recordings.values()):
        errs.append("recordings must be an object of objects")
    elif projects_ok:
        for key, rec in recordings.items():
            tags = rec.get("projects")
            if not isinstance(tags, list) or not tags or not all(isinstance(t, str) for t in tags) \
                    or set(tags) - set(projects):
                errs.append(f"recording {key} has projects {tags} outside the session's")
                break
    return errs


def write_manifest(root: Path, manifest: dict) -> None:
    problems = manifest_errors(manifest)
    if problems:
        raise SystemExit("REFUSING to write a manifest: " + "; ".join(problems))
    write_atomic(root / MANIFEST, json.dumps(manifest, indent=1, ensure_ascii=False) + "\n")


def guard_answers(answers_path: Path, rows: list, quote_rows: list, transcript_index: dict,
                  allow_off_page: bool, sidecars: dict, old_page: Path) -> None:
    """Refuse a build that would strand answers already saved in this dataset.

    A build rewrites the page, the sidecars and the quote key. If the new page no
    longer carries an answered recording or quote, its answers can no longer be
    changed or exported; if a recording's text changed, its saved ranges and
    corrections point at words that are no longer there. Either way the answers file
    itself is untouched, but only git could bring the dataset back, so the build
    stops before writing anything. `allow_off_page` accepts the first case on purpose
    (a recording dropped from the checklist); nothing accepts the second.
    """
    if not answers_path.exists():
        return
    answers = load_answers(answers_path)
    ids = page_ids({"rows": rows, "quote_rows": quote_rows, "transcript_index": transcript_index})
    off = {s: sorted(set(answers[s]) - ids[s]) for s in answers if set(answers[s]) - ids[s]}
    moved = sorted(k for s in ("spans", "corrections") for k, v in answers[s].items()
                   if k in transcript_index and (v.get("text_sha256"), v.get("token_count"))
                   != (transcript_index[k]["text_sha256"], transcript_index[k]["token_count"]))
    if moved:
        raise SystemExit(f"REFUSING: the transcript text changed under saved annotations for {sorted(set(moved))[:5]}; "
                         "their word offsets would point at other words. Nothing was written.")
    # An answered quote must keep its words: the same id over other words would carry
    # the old answer to a quote nobody answered.
    before = {}
    if old_page.exists():
        before = {c["qid"]: (c.get("quote_start"), c.get("quote_end"))
                  for r in page_data(old_page.read_text()).get("quote_rows", []) for c in r.get("quotes", [])}
    shifted = []
    for r in quote_rows:
        for c in r["quotes"]:
            q = c["qid"]
            if q not in answers["attribution"]:
                continue
            if q in before and before[q] != (c.get("quote_start"), c.get("quote_end")):
                shifted.append(q)
            elif r["key"] in answers["spans"] and "quote_start" in c:
                result = quote_review(c, sidecars[r["key"]], answers["spans"][r["key"]])
                old = answers["attribution"][q]
                if (result["answer"], result["review_status"]) != (old.get("answer"), old.get("review_status")):
                    shifted.append(q)
    if shifted:
        raise SystemExit(f"REFUSING: answered quotes {shifted[:5]} would cover other words or change their result on "
                         "the new page. Nothing was written.")
    if off and not allow_off_page:
        raise SystemExit("REFUSING: the new page would not carry answers already saved in "
                         f"{answers_path}: {{{', '.join(f'{s}: {v[:3]}' for s, v in off.items())}}}. Build with the "
                         "same inputs as before, or allow off-page answers to keep them in the file without a "
                         "page. Nothing was written.")
