"""Correction handoff requires contextual proposals for every original speech word.

This is a preparation check, never evidence of accuracy or human confirmation.
Explicit manual and blind reviews, and datasets created before this policy,
keep their existing behavior. No dataset or human state is changed here.
"""
from __future__ import annotations

from .dataset3 import Dataset
from .items import item_errors
from .suggestions import validate_stored_suggestions
from .tokens import item_kinds


def review_readiness(ds: Dataset) -> dict:
    """Return text-free preparation evidence from a freshly opened, locked dataset."""
    if "review_mode" not in ds.manifest:
        return {"mode": "legacy", "requires_contextual": False, "ready": True, "items": [],
                "reason": "legacy dataset: existing review policy retained; contextual preparation is not required"}
    mode = ds.manifest["review_mode"]
    if mode != "contextual":
        return {"mode": mode, "requires_contextual": False, "ready": True, "items": [],
                "reason": f"explicit {mode} review: contextual preparation is not required"}
    rows = []
    for key, identity in sorted(ds.manifest["items"].items()):
        item = ds.item(key)
        validate_stored_suggestions(item, ds.speaker_ids())
        if item_errors(item, ds.speaker_ids()):
            raise ValueError(f"{key}: invalid source item; run verify before preparing a review")
        if (item["text_sha256"], item["token_count"]) != (identity["text_sha256"], identity["token_count"]):
            raise ValueError(f"{key}: item source identity differs from the manifest")
        speech_words = sum(kind == "speech" for kind in item_kinds(item))
        # Validated contextual bundles have ordered, non-overlapping speech-only
        # ranges, so summing their lengths proves exact coverage without guessing.
        contextual_words = (sum(span["end"] - span["start"] for span in item["suggestions"])
                            if "suggestion_provenance" in item else 0)
        rows.append({"key": key, "speech_words": speech_words, "contextual_words": contextual_words,
                     "ready": speech_words > 0 and contextual_words == speech_words})
    ready = bool(rows) and all(row["ready"] for row in rows)
    return {"mode": mode, "requires_contextual": True, "ready": ready, "items": rows,
            "reason": ("contextual suggestions cover every speech word; human confirmation is still required"
                       if ready else "contextual suggestions are incomplete; prepare, run an explicitly selected model, "
                       "and attach validated suggestions for every item before requesting a login link")}
