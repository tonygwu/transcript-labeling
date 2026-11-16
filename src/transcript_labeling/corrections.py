"""Human caption corrections over immutable raw token offsets (the `captions` mode).

The original transcript stays intact. A replacement is selected as one phrase,
so a change in word count cannot shift existing speaker annotations.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .spans import sidecar_path
from .store import read_json_object, write_atomic
from .tokens import item_kinds, text_hash


def validate_corrections(value: dict, transcript: dict) -> None:
    for key in ("key", "text_sha256", "token_count"):
        if value.get(key) != transcript[key]:
            raise ValueError("transcript changed; reload before correcting text")
    if not isinstance(value.get("edits"), list) or not value.get("checked_by"):
        raise ValueError("corrections require edits and checked_by")
    validate_edits(value["edits"], transcript)
    for edit in value["edits"]:
        if "origin" in edit:
            if edit["origin"] != "model_confirmed":
                raise ValueError("correction origin must be model_confirmed or absent for a manual edit")
            proposal = proposal_evidence(transcript, edit.get("suggestion_bundle_sha256"), edit.get("suggestion_index"))
            if any(edit[k] != proposal[k] for k in ("start", "end", "original", "replacement")):
                raise ValueError("confirmed correction differs from its model proposal")
    decisions = value.get("proposal_decisions", [])
    if not isinstance(decisions, list):
        raise ValueError("proposal_decisions must be a list")
    seen = set()
    for decision in decisions:
        if not isinstance(decision, dict) or set(decision) != {"bundle_sha256", "index", "decision"}:
            raise ValueError("invalid text proposal decision fields")
        proposal_evidence(transcript, decision["bundle_sha256"], decision["index"])
        if decision["decision"] not in ("accepted", "rejected"):
            raise ValueError("text proposal decision must be accepted or rejected")
        identity = (decision["bundle_sha256"], decision["index"])
        if identity in seen:
            raise ValueError("duplicate text proposal decision")
        seen.add(identity)


def validate_edits(edits: list, transcript: dict) -> None:
    """Shared geometry rules for proposals and human corrections."""
    if not isinstance(edits, list):
        raise ValueError("text corrections must be a list")
    kinds = item_kinds(transcript)
    end = 0
    for edit in edits:
        if not isinstance(edit, dict):
            raise ValueError("each correction must be an object")
        a, b = edit.get("start"), edit.get("end")
        if type(a) is not int or type(b) is not int or not 0 <= a < b <= len(kinds) or a < end:
            raise ValueError("corrections must be ordered, non-overlapping ranges")
        if any(k != "speech" for k in kinds[a:b]):
            raise ValueError("select spoken words within one caption segment, without gaps or timestamps")
        if edit.get("original") != " ".join(transcript["tokens"][a:b]):
            raise ValueError("original words do not match the transcript")
        replacement = edit.get("replacement")
        if not isinstance(replacement, str) or not replacement.strip() or len(replacement) > 4000:
            raise ValueError("replacement must contain 1–4000 characters; use Restore original to undo")
        if replacement == edit["original"]:
            raise ValueError("replacement is unchanged; use Restore original to undo")
        end = b


def proposal_evidence(transcript: dict, digest, index) -> dict:
    """Resolve an exact current or retained proposal; never guess by its wording."""
    from .suggestions import suggestion_bundle_hash
    if not isinstance(digest, str) or type(index) is not int or index < 0:
        raise ValueError("text proposal requires a bundle digest and nonnegative integer index")
    for snapshot in [transcript, *transcript.get("suggestion_history", [])]:
        if suggestion_bundle_hash(snapshot) == digest:
            proposals = snapshot.get("text_suggestions", [])
            if index >= len(proposals):
                raise ValueError("text proposal index is outside its bundle")
            return proposals[index]
    raise ValueError("text proposal bundle is unavailable")


def update_corrections(value: dict, previous: dict | None, transcript: dict) -> dict:
    previous = previous or {}
    if previous:
        validate_corrections(previous, transcript)
    revision = previous.get("revision", 0)
    if type(value.get("revision")) is not int or value["revision"] != revision:
        raise ValueError("Corrections changed in another window. Reload before saving.")
    decisions = list(previous.get("proposal_decisions", []))
    keys = ("start", "end", "original", "replacement")
    if "proposal_action" in value:
        from .suggestions import suggestion_bundle_hash
        action = value["proposal_action"]
        if "edits" in value or "proposal_decisions" in value:
            raise ValueError("a text proposal action cannot also replace edits or decisions")
        if not isinstance(action, dict) or set(action) != {"bundle_sha256", "index", "decision"}:
            raise ValueError("invalid text proposal action fields")
        if action["bundle_sha256"] != suggestion_bundle_hash(transcript):
            raise ValueError("Text proposals changed. Reload before reviewing them.")
        proposal = proposal_evidence(transcript, action["bundle_sha256"], action["index"])
        if action["decision"] not in ("accepted", "rejected"):
            raise ValueError("text proposal decision must be accepted or rejected")
        if any(d["bundle_sha256"] == action["bundle_sha256"] and d["index"] == action["index"] for d in decisions):
            raise ValueError("Text proposal already reviewed. Reload before continuing.")
        edits = list(previous.get("edits", []))
        if action["decision"] == "accepted":
            if any(e["start"] < proposal["end"] and e["end"] > proposal["start"] for e in edits):
                raise ValueError("Text proposal overlaps a saved human correction; review that correction manually.")
            edits.append({**{k: proposal[k] for k in keys}, "origin": "model_confirmed",
                          "suggestion_bundle_sha256": action["bundle_sha256"], "suggestion_index": action["index"]})
            edits.sort(key=lambda e: e["start"])
        decisions.append(dict(action))
    else:
        # A browser may resend unchanged confirmed edits, but cannot create their
        # model evidence or overwrite the server's decision history.
        validate_edits(value.get("edits"), transcript)
        edits = []
        for edit in value["edits"]:
            clean = {k: edit[k] for k in keys}
            prior = next((e for e in previous.get("edits", []) if all(e[k] == edit[k] for k in keys)), None)
            if "origin" in edit and (prior is None or any(edit.get(k) != prior.get(k) for k in
                    ("origin", "suggestion_bundle_sha256", "suggestion_index"))):
                raise ValueError("model confirmation evidence must come from a text proposal action")
            edits.append(dict(prior) if prior is not None else clean)
    validate_corrections({**value, "edits": edits, "proposal_decisions": decisions}, transcript)
    # The moment of the save, not a time read from any data: it stamps the edit.
    now = datetime.now(timezone.utc).isoformat()
    # History is appended by the server, never trusted from the browser.
    event = {"revision": revision + 1, "updated_at": now, "checked_by": value["checked_by"],
             "before": previous.get("edits", []), "after": edits}
    if "proposal_action" in value:
        event["proposal_action"] = dict(value["proposal_action"])
    return {"schema_version": 1, "key": transcript["key"], "text_sha256": transcript["text_sha256"],
            "token_count": transcript["token_count"], "revision": revision + 1, "edits": edits,
            "checked_by": value["checked_by"], "updated_at": now,
            **({"proposal_decisions": decisions} if decisions else {}),
            "history": [*previous.get("history", []), event]}


def corrected_text(transcript: dict, value: dict) -> str:
    validate_corrections(value, transcript)
    tokens, pieces, at = transcript["tokens"], [], 0
    for edit in value["edits"]:
        pieces.extend(tokens[at:edit["start"]])
        pieces.append(edit["replacement"])
        at = edit["end"]
    pieces.extend(tokens[at:])
    return " ".join(pieces)


def export_corrections(answers: Path, page: Path, out: Path, *, item_path=None) -> dict:
    """Export corrected reading text; does not overwrite source or grading inputs."""
    item_path = item_path or (lambda key: sidecar_path(page, key))
    raw = read_json_object(answers)
    recordings = {}
    for key, value in raw.get("corrections", {}).items():
        transcript = json.loads(item_path(key).read_text(encoding="utf-8"))
        text = corrected_text(transcript, value)
        recordings[key] = {**value, "corrected_text": text, "corrected_text_sha256": text_hash(text)}
    result = {"schema_version": 1, "recordings": recordings,
              "note": "Human caption corrections; whitespace normalized. Offsets still refer to original tokens. "
                      "Existing scores use the original captions; regrading is a separate step."}
    write_atomic(out, json.dumps(result, ensure_ascii=False, indent=1, allow_nan=False) + "\n")
    return result
