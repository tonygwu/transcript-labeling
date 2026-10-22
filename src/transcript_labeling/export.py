"""What a dataset's answers become: the files agents read back.

  spans        every human word range, with its text
  corrections  every caption correction, with the corrected reading text
  labels       complete checklist answers; partial ones are counted, never filled

An export reads the answers file and the sidecars. It never writes either.
"""
from __future__ import annotations

from pathlib import Path

from .checklist import to_labels
from .corrections import export_corrections
from .dataset import Session
from .spans import import_spans
from .store import load_answers

KINDS = ("spans", "corrections", "labels")


def label_docs(labels: dict) -> list[dict]:
    """The answers file's labels as documents, each carrying its key."""
    return [dict(v, key=v.get("key", k)) for k, v in labels.items() if isinstance(v, dict)]


def export_labels(session: Session) -> dict:
    """Complete checklist answers, computed in memory. A session without a checklist stops the caller."""
    data = session.data()
    checklist = (data.get("session") or {}).get("checklist")
    if "checklist" not in session.modes or not checklist:
        raise SystemExit(f"REFUSING: session {session.name} has no checklist mode")
    answers = load_answers(session.answers)
    return to_labels(label_docs(answers["labels"]), {r["key"] for r in data.get("rows", [])}, checklist)


def export_spans(session: Session, out: Path) -> dict:
    return import_spans(session.answers, session.page, out)


def export_caption_corrections(session: Session, out: Path) -> dict:
    return export_corrections(session.answers, session.page, out)
