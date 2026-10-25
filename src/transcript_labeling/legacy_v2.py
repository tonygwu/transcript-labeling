"""Adopt a version-2 session (verbatim-index's layout) as a version-3 dataset, in place.

Adoption writes two files, `manifest.json` and `catalog.json`, and nothing else:
the answers file, the sidecars, the quote key, the exports and the old page stay
byte-identical, and adoption refuses if any of them changed while it ran. The
version-3 manifest keeps the whole version-2 manifest in `legacy_from`, reads the
sidecars in place (`files.items_dir` is the page's `.transcripts` folder), and
configures verbatim's speakers: `subject` (labelled with the item's person),
`other` and `unclear`, with `mixed_answer: "both"`, so every stored range and
derived result stays identical. The catalog is the page's own `rows` and
`quote_rows`, carried over unchanged.

Model suggestions in a version-2 page were always visible, so an adopted dataset
reveals them from the start (`reveal: always`), as its owner labelled it.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import __version__
from .dataset import Session
from .dataset3 import MANIFEST, SCHEMA_VERSION, TOOL, Dataset, manifest_errors, normalize_config
from .store import read_json_object, write_atomic

VERBATIM_SPEAKERS = [{"id": "subject", "label": "{person}", "key": "s"},
                     {"id": "other", "label": "Someone else", "key": "e"}]
VERBATIM_UNCLEAR = {"id": "unclear", "label": "Unsure", "key": "u"}
YOUTUBE = {"url": "https://www.youtube.com/watch?v={video_id}&t={t}s"}


def _digest(root: Path, skip: set[str]) -> dict:
    """sha256 of every file under `root`, except the top-level files named in `skip`."""
    return {rel: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and (rel := str(p.relative_to(root))) not in skip}


def adopted_config(session: Session, overrides: dict | None = None) -> dict:
    """The version-3 config for a version-2 session: verbatim's speakers, its checklist and help."""
    block = session.data().get("session") or {}
    files = session.manifest["files"]
    config = {"title": block.get("name") or session.name, "modes": session.modes,
              "speakers": VERBATIM_SPEAKERS, "unclear": VERBATIM_UNCLEAR, "mixed_answer": "both",
              "label_template": "{person}", "checklist": block.get("checklist") if "checklist" in session.modes else None,
              "reveal": "always", "media": YOUTUBE, "project_help": block.get("project_help") or {},
              "tags": session.projects,
              "files": {"answers": files["answers"], "items_dir": str(Path(files["page"]).with_suffix(".transcripts")),
                        "catalog": "catalog.json", "review": "review.json"}}
    return {**config, **(overrides or {})}


def adopt(root, project: str, overrides: dict | None = None) -> Dataset:
    """Turn the version-2 session at `root` into a version-3 dataset of `project`, in place."""
    root = Path(root)
    if (root / MANIFEST).exists() and read_json_object(root / MANIFEST).get("schema_version") == 3:
        raise SystemExit(f"REFUSING: {root} is already a version-3 dataset")
    session = Session.open(root)
    if (root / "catalog.json").exists():
        raise SystemExit(f"REFUSING: {root} already has a catalog.json")
    data = session.data()
    before = _digest(root, {MANIFEST})
    config = normalize_config(adopted_config(session, overrides))
    items = {k: {"text_sha256": v["text_sha256"], "token_count": v["token_count"]}
             for k, v in data.get("transcript_index", {}).items()}
    manifest = {"schema_version": SCHEMA_VERSION, "tool": TOOL, "name": session.name, "project": project,
                **config, "items": items, "created_with": __version__,
                "legacy_from": {"manifest": session.manifest}}
    problems = manifest_errors(manifest)
    if problems:  # checked before anything is written, so a refusal leaves the session as it was
        raise SystemExit(f"REFUSING: the adopted manifest would be invalid: {'; '.join(problems)}")
    ds = Dataset(root, manifest)
    for key in items:
        side = ds.item(key)
        if (side["text_sha256"], side["token_count"]) != (items[key]["text_sha256"], items[key]["token_count"]):
            raise SystemExit(f"REFUSING: sidecar {key} does not match the page's transcript index")
    # verbatim's page writer let NaN through, and every later read refuses it, so a
    # catalog holding one would leave a dataset nothing can open or adopt again.
    try:
        catalog = json.dumps({"rows": data.get("rows", []), "quote_rows": data.get("quote_rows", [])},
                             indent=1, ensure_ascii=False, allow_nan=False) + "\n"
        json.dumps(manifest, allow_nan=False)  # written after the catalog, so checked before it
    except ValueError:
        raise SystemExit("REFUSING: the page's rows hold NaN or Infinity, or the adopted settings do, which "
                         "JSON does not allow; nothing was written") from None
    v2_manifest = (root / MANIFEST).read_bytes()
    write_atomic(root / "catalog.json", catalog)
    ds.write_manifest(manifest)
    after = _digest(root, {MANIFEST, "catalog.json"})
    if after != before:
        # Something else wrote the session while it was adopted: put it back as it was.
        write_atomic(root / MANIFEST, v2_manifest.decode())
        (root / "catalog.json").unlink()
        changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        raise SystemExit(f"REFUSING: {changed[:5]} changed while the session was being adopted; the session is "
                         "back as it was. Run adoption again once nothing else writes it.")
    return Dataset.open(root)
