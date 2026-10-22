"""Small datasets for tests, here and in client repositories. Synthetic only."""
from __future__ import annotations

from pathlib import Path

from .dataset import SCHEMA_VERSION, Session, write_manifest
from .page.data import page_payload


def write_session(root: Path, data: dict, *, modes=("speakers", "captions"), projects=("demo",),
                  name: str = "t", page: str = "page.html") -> Session:
    """A version-2 session whose page is just the data block, with a valid manifest."""
    root.mkdir(parents=True, exist_ok=True)
    (root / page).write_text("<title>t</title><script>\nconst D = " + page_payload(data) + ";\n</script>\n",
                             encoding="utf-8")
    recordings = {k: {"projects": list(projects), "text_sha256": v.get("text_sha256", ""),
                      "token_count": v.get("token_count", 0)}
                  for k, v in data.get("transcript_index", {}).items()}
    write_manifest(root, {"schema_version": SCHEMA_VERSION, "tool": "annotations", "name": name,
                          "projects": list(projects), "modes": list(modes), "blind": True,
                          "transcript_dir": "transcripts",
                          "files": {"page": page, "answers": "human_answers.json", "quote_key": "quote_key.json"},
                          "recordings": recordings})
    return Session.open(root)
