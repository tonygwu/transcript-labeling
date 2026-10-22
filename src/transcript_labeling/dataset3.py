"""A version-3 dataset: the layout the service creates and serves.

    <dataset folder>/
      manifest.json  schema_version 3. Written by `tl` at create or update, never by the
                     server during labeling.
      catalog.json   per-item display metadata (`rows`) and focus passages (`quote_rows`),
                     the shape a version-2 page carried in its data block
      items/<group>/<name>.json   tokens, text, text_sha256, token_count, times, segments,
                     meta header ranges, suggestions; a superset of a version-2 sidecar,
                     so `files.items_dir` can point at an existing `<page>.transcripts/`
      <answers file> exactly the version-2 file: labels, attribution, spans, corrections
      review.json    server-owned notes, done marks, suggestion reveals and Finish state;
                     created on first use

An item key is `<group>/<name>`, each part letters, digits, `_` or `-`.

The speakers are configuration. A dataset lists `speakers: [{id, label, key}]`, an
`unclear` entry, and `mixed_answer`, the result of a focus passage that more than
one speaker covers. A label may be a template such as `{person}`, filled from the
item's catalog row. verbatim-index configures `subject` / `other` / `unclear` with
`mixed_answer: "both"`, so its stored ranges and derived results stay identical.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import __version__
from .checklist import RESERVED_KEYS, STICKY_LABEL_FIELDS
from .dataset import MODE_SECTION, MODES
from .registry import NAME
from .spans import quote_review, sidecar_path
from .store import read_json_object, write_atomic

SCHEMA_VERSION = 3
TOOL = "transcript-labeling"
MANIFEST = "manifest.json"
SPEAKER_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
DEFAULT_FILES = {"answers": "human_answers.json", "items_dir": "items", "catalog": "catalog.json",
                 "review": "review.json"}
DEFAULT_UNCLEAR = {"id": "unclear", "label": "Unsure", "key": "u"}
#: When model suggestions may show: only after the item's own answers ("after_answer",
#: blind-first), or from the start ("always").
REVEAL = ("after_answer", "always")
CONFIG_KEYS = {"title", "modes", "speakers", "unclear", "mixed_answer", "label_template", "checklist", "reveal",
               "media", "project_help", "files", "tags"}


#: The settings `normalize_config` always returns, so every manifest `tl` writes holds them.
WRITTEN_KEYS = CONFIG_KEYS - {"tags"}


def normalize_config(config: dict) -> dict:
    """A dataset config with every default filled in. A config that cannot work stops the caller."""
    if not isinstance(config, dict):
        raise SystemExit("REFUSING: a dataset config must be an object")
    unknown = sorted(set(config) - CONFIG_KEYS)
    if unknown:
        raise SystemExit(f"REFUSING: unknown dataset config keys {unknown}; known: {sorted(CONFIG_KEYS)}")
    modes = config.get("modes", ["speakers", "captions"])
    if (not isinstance(modes, list) or not modes or not all(isinstance(m, str) for m in modes)
            or set(modes) - set(MODES) or len(set(modes)) != len(modes)):
        raise SystemExit(f"REFUSING: modes {modes!r} must be a non-empty subset of {list(MODES)}")
    speakers = config.get("speakers", [])
    if not isinstance(speakers, list):
        raise SystemExit(f"REFUSING: speakers must be a list of {{id, label, key}}, got {speakers!r}")
    unclear = config.get("unclear", DEFAULT_UNCLEAR)
    if "speakers" in modes and not speakers:
        raise SystemExit("REFUSING: the speakers mode needs `speakers`: [{id, label, key}, ...]")
    entries = [*speakers, unclear] if speakers else [unclear]
    ids, keys = [], []
    for s in entries:
        if not isinstance(s, dict) or set(s) != {"id", "label", "key"}:
            raise SystemExit(f"REFUSING: speaker {s!r} must be exactly {{id, label, key}}")
        if not isinstance(s["id"], str) or not SPEAKER_ID.fullmatch(s["id"]):
            raise SystemExit(f"REFUSING: speaker id {s['id']!r} must be a string matching {SPEAKER_ID.pattern}")
        if not isinstance(s["label"], str) or not s["label"].strip():
            raise SystemExit(f"REFUSING: speaker {s['id']!r} needs a label")
        if not isinstance(s["key"], str) or len(s["key"]) != 1 or s["key"].lower() in RESERVED_KEYS:
            raise SystemExit(f"REFUSING: speaker {s['id']!r} key {s['key']!r} must be one character the page does "
                             f"not already use ({sorted(RESERVED_KEYS)})")
        ids.append(s["id"])
        keys.append(s["key"].lower())
    if len(set(ids)) != len(ids) or len(set(keys)) != len(keys):
        raise SystemExit(f"REFUSING: speaker ids {ids} and keys {keys} must each be unique")
    mixed = config.get("mixed_answer", "mixed")
    if not isinstance(mixed, str) or not SPEAKER_ID.fullmatch(mixed) or mixed in ids:
        raise SystemExit(f"REFUSING: mixed_answer {mixed!r} must match {SPEAKER_ID.pattern} and not be a speaker id")
    checklist = config.get("checklist")
    if checklist is not None and not (isinstance(checklist, dict) and isinstance(checklist.get("fields"), list)
                                      and all(isinstance(f, dict) for f in checklist["fields"])):
        raise SystemExit("REFUSING: checklist must be an object with a `fields` list of objects")
    if ("checklist" in modes) != bool(checklist):
        raise SystemExit("REFUSING: the checklist mode and a `checklist` spec go together")
    for key, kind in (("title", str), ("label_template", (str, type(None))), ("project_help", dict),
                      ("files", dict), ("tags", (list, type(None)))):
        if key in config and not isinstance(config[key], kind):
            raise SystemExit(f"REFUSING: {key} has the wrong type ({type(config[key]).__name__})")
    if checklist:
        checklist_errors(checklist, RESERVED_KEYS | set(keys))
    reveal = config.get("reveal", "after_answer")
    if reveal not in REVEAL:
        raise SystemExit(f"REFUSING: reveal {reveal!r} must be one of {REVEAL}")
    media = config.get("media")
    if media is not None and (not isinstance(media, dict) or set(media) != {"url"}
                              or not str(media["url"]).startswith("https://")):
        raise SystemExit("REFUSING: media must be {\"url\": \"https://...{video_id}...{t}...\"} or absent")
    files = {**DEFAULT_FILES, **config.get("files", {})}
    if set(files) != set(DEFAULT_FILES):
        raise SystemExit(f"REFUSING: files may name only {sorted(DEFAULT_FILES)}")
    for k, v in files.items():
        # The items folder may be nested (a version-2 `<page>.transcripts/`); the
        # files sit directly in the dataset folder.
        parts = v.split("/") if isinstance(v, str) else [""]
        if not v or any(p in ("", ".", "..") for p in parts) or (k != "items_dir" and len(parts) > 1):
            raise SystemExit(f"REFUSING: files.{k} {v!r} must stay inside the dataset folder")
    if len(set(files.values())) != len(files) or MANIFEST in files.values():
        raise SystemExit(f"REFUSING: files {files} must name four different files, none of them {MANIFEST}")
    tags = config.get("tags")
    if tags is not None and not all(isinstance(t, str) and SPEAKER_ID.fullmatch(t) for t in tags):
        raise SystemExit(f"REFUSING: tags {tags!r} must be names matching {SPEAKER_ID.pattern}")
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in config.get("project_help", {}).items()):
        raise SystemExit("REFUSING: project_help must map names to strings")
    return {"title": config.get("title", ""), "modes": modes, "speakers": speakers, "unclear": unclear,
            "mixed_answer": mixed, "label_template": config.get("label_template"), "checklist": checklist,
            "reveal": reveal, "media": media, "project_help": config.get("project_help", {}),
            "files": files, **({"tags": tags} if tags else {})}


#: The checklist field types the page can answer. It draws option buttons only, so a
#: free-text field would never be answered and every item would stay incomplete.
FIELD_TYPES = ("bool", "enum")
#: Names a saved checklist label already uses beside the answers; a field may not take one.
LABEL_RECORD_KEYS = ("key", "checked_by", "notes", "updated_at", *STICKY_LABEL_FIELDS)


def checklist_errors(checklist: dict, taken: set[str]) -> None:
    """Refuse a checklist the page or the labels export could not use.

    Checks what they read: `goal` ({count, help}) and `accept_when_true` (bool field
    names), and each field's `name`, `label`, `type`, `required` and options, each
    option with a value, a label and a one-character key. Other keys (help text,
    draft labels) pass through. Not yet checked: `draft_fields`, which the page reads
    when an item carries a model draft. `taken` holds the keys the page already binds.
    """
    fields = checklist["fields"]
    if not fields:
        raise SystemExit("REFUSING: a checklist needs at least one field")
    names, keys, bools = set(), set(), set()
    for i, field in enumerate(fields):
        name = field.get("name")
        if not isinstance(name, str) or not name or name in names:
            raise SystemExit(f"REFUSING: checklist field {i} needs a name of its own, got {name!r}")
        if name in LABEL_RECORD_KEYS:
            raise SystemExit(f"REFUSING: checklist field name {name!r} is one a saved label already uses "
                             f"({list(LABEL_RECORD_KEYS)})")
        names.add(name)
        if not isinstance(field.get("label"), str) or not field["label"].strip():
            raise SystemExit(f"REFUSING: checklist field {name!r} needs a label")
        if field.get("type") not in FIELD_TYPES:
            raise SystemExit(f"REFUSING: checklist field {name!r} has type {field.get('type')!r}; "
                             f"the page answers only {list(FIELD_TYPES)}")
        if not isinstance(field.get("required", False), bool):
            raise SystemExit(f"REFUSING: checklist field {name!r}: `required` must be true or false")
        options = field.get("options")
        if not isinstance(options, list) or not options or not all(isinstance(o, dict) for o in options):
            raise SystemExit(f"REFUSING: checklist field {name!r}: `options` must be a non-empty list of objects")
        for option in options:
            key, label = option.get("key"), option.get("label")
            if (not isinstance(key, str) or len(key) != 1 or not key.strip() or not isinstance(label, str)
                    or "value" not in option):
                raise SystemExit(f"REFUSING: checklist field {name!r}: each option needs a value, a label "
                                 f"and a one-character key, got {option!r}")
            if field["type"] == "bool" and not isinstance(option["value"], bool):
                raise SystemExit(f"REFUSING: checklist field {name!r} is a bool question, so each option's "
                                 f"value must be true or false, got {option['value']!r}")
            if key.lower() in taken | keys:
                raise SystemExit(f"REFUSING: checklist key {key!r} is one the page already uses")
            keys.add(key.lower())
        if field["type"] == "bool":
            bools.add(name)
    goal = checklist.get("goal")
    if not (isinstance(goal, dict) and type(goal.get("count")) is int and goal["count"] >= 0
            and isinstance(goal.get("help"), str)):
        raise SystemExit(f"REFUSING: a checklist needs `goal`: {{count, help}}, the items to verify and why; "
                         f"got {goal!r}")
    accept = checklist.get("accept_when_true")
    if not (isinstance(accept, list) and all(isinstance(f, str) and f in bools for f in accept)):
        raise SystemExit(f"REFUSING: a checklist needs `accept_when_true`, a list of its bool field names; "
                         f"got {accept!r}")


def manifest_errors(manifest: dict) -> list[str]:
    """Everything wrong with a version-3 manifest; empty when it is sound."""
    if not isinstance(manifest, dict):
        return ["the manifest is not an object"]
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("tool") != TOOL:
        return [f"not a version-{SCHEMA_VERSION} {TOOL} manifest (schema_version "
                f"{manifest.get('schema_version')!r}, tool {manifest.get('tool')!r})"]
    errs = [f"missing `{k}`" for k in ("name", "project", "items", "files") if k not in manifest]
    if errs:
        return errs
    for k in ("name", "project"):
        if not isinstance(manifest[k], str) or not NAME.fullmatch(manifest[k]):
            errs.append(f"{k} {manifest[k]!r} must match {NAME.pattern}")
    # Named before any default is filled in: a default can clash with the rest of the
    # manifest (a missing `modes` defaults to speakers mode) and hide which one is gone.
    absent = sorted(WRITTEN_KEYS - set(manifest))
    if absent:
        errs.append(f"settings {absent} are missing; `tl` writes every setting")
    else:
        try:
            normal = normalize_config({k: manifest[k] for k in CONFIG_KEYS if k in manifest})
        except SystemExit as exc:
            errs.append(str(exc))
        else:
            # `tl` writes every setting in full, and the dataset code reads them from the
            # manifest itself, so a shortened one would fail later with a KeyError.
            short = sorted(k for k, v in normal.items() if manifest[k] != v)
            if short:
                errs.append(f"settings {short} are not written in full; `tl` writes every setting in full")
    if not isinstance(manifest["items"], dict) or not all(
            isinstance(v, dict) and isinstance(v.get("text_sha256"), str) and type(v.get("token_count")) is int
            for v in manifest["items"].values()):
        errs.append("items must map each key to {text_sha256, token_count}")
    return errs


class Dataset:
    """A version-3 dataset folder. Opening one checks the manifest whole."""

    def __init__(self, root: Path, manifest: dict):
        self.root = Path(root)
        self.manifest = manifest

    @classmethod
    def open(cls, root) -> "Dataset":
        root = Path(root)
        path = root / MANIFEST
        if not path.exists():
            raise SystemExit(f"REFUSING: {root} has no {MANIFEST}")
        manifest = read_json_object(path)
        if manifest.get("schema_version") == 2:
            raise SystemExit(f"REFUSING: {path} is a version-2 session; adopt it with `tl dataset adopt`")
        problems = manifest_errors(manifest)
        if problems:
            raise SystemExit(f"REFUSING: {path}: " + "; ".join(problems))
        return cls(root, manifest)

    @classmethod
    def create(cls, root: Path, project: str, name: str, config: dict) -> "Dataset":
        """Write a new dataset's manifest and empty catalog. Refuses a folder that already has one."""
        root = Path(root)
        if (root / MANIFEST).exists():
            raise SystemExit(f"REFUSING: {root} already has a {MANIFEST}")
        manifest = {"schema_version": SCHEMA_VERSION, "tool": TOOL, "name": name, "project": project,
                    **normalize_config(config), "items": {}, "created_with": __version__}
        root.mkdir(parents=True, exist_ok=True)
        ds = cls(root, manifest)
        ds.write_catalog({"rows": [], "quote_rows": []})
        ds.write_manifest(manifest)
        return ds

    # ------------------------------------------------------------ files
    @property
    def name(self) -> str:
        return self.manifest["name"]

    @property
    def project(self) -> str:
        return self.manifest["project"]

    @property
    def modes(self) -> list[str]:
        return self.manifest["modes"]

    def file(self, role: str) -> Path:
        return self.root / self.manifest["files"][role]

    @property
    def answers(self) -> Path:
        return self.file("answers")

    @property
    def review_path(self) -> Path:
        return self.file("review")

    @property
    def items_dir(self) -> Path:
        return self.root / self.manifest["files"]["items_dir"]

    def item_path(self, key: str) -> Path:
        """Where item `key` lives. A key that could escape the items folder stops the caller."""
        # sidecar_path validates the key and puts it under `<x>.transcripts`; items_dir
        # names the folder directly.
        rel = sidecar_path(Path("x.html"), key).relative_to("x.transcripts")
        return self.items_dir / rel

    def item(self, key: str) -> dict:
        return read_json_object(self.item_path(key))

    def catalog(self) -> dict:
        path = self.file("catalog")
        if not path.exists():
            raise SystemExit(f"REFUSING: {path} is missing; the dataset's catalog was deleted or never written")
        cat = read_json_object(path)
        if not isinstance(cat.get("rows"), list) or not isinstance(cat.get("quote_rows"), list):
            raise SystemExit(f"REFUSING: {path} needs `rows` and `quote_rows` lists")
        return cat

    def write_catalog(self, catalog: dict) -> None:
        write_atomic(self.file("catalog"), json.dumps(catalog, indent=1, ensure_ascii=False) + "\n")

    def write_manifest(self, manifest: dict) -> None:
        problems = manifest_errors(manifest)
        if problems:
            raise SystemExit("REFUSING to write a manifest: " + "; ".join(problems))
        write_atomic(self.root / MANIFEST, json.dumps(manifest, indent=1, ensure_ascii=False) + "\n")
        self.manifest = manifest

    # ------------------------------------------------------------ labeling
    def sections(self) -> set[str]:
        return {MODE_SECTION[m] for m in self.modes}

    def speaker_ids(self) -> list[str]:
        return [s["id"] for s in self.manifest["speakers"]] + [self.manifest["unclear"]["id"]]

    def review(self, quote: dict, transcript: dict, annotation: dict) -> dict:
        """A focus passage's result, under this dataset's speakers."""
        return quote_review(quote, transcript, annotation, speakers=self.speaker_ids(),
                            unclear=self.manifest["unclear"]["id"], mixed=self.manifest["mixed_answer"])

    def session_block(self) -> dict:
        """What the page needs to know about its dataset."""
        m = self.manifest
        tags = m.get("tags") or [m["project"]]
        return {"name": m["name"], "project": m["project"], "title": m["title"] or m["name"], "projects": tags,
                "modes": m["modes"], "checklist": m["checklist"] if "checklist" in m["modes"] else None,
                "project_help": {t: m["project_help"].get(t, "") for t in tags}, "speakers": m["speakers"],
                "unclear": m["unclear"], "mixed_answer": m["mixed_answer"], "label_template": m["label_template"],
                "reveal": m["reveal"], "media": m["media"]}

    def page_data(self) -> dict:
        """The data block the page renders: rows, quote_rows, transcript_index and session."""
        cat = self.catalog()
        return {"rows": cat["rows"], "quote_rows": cat["quote_rows"],
                "transcript_index": {k: {"text_sha256": v["text_sha256"], "token_count": v["token_count"]}
                                     for k, v in self.manifest["items"].items()},
                "session": self.session_block()}
