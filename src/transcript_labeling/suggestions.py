"""Source-aligned contextual suggestions, separate from every human answer.

An envelope pins the original text and records model-call provenance. Updating an
existing item replaces only its suggestions and retains the previous bundle so
human confirmations that cite an earlier suggestions hash remain interpretable.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .dataset3 import Dataset
from .store import StaleWrite, fingerprint, locked, write_atomic
from .tokens import item_kinds, text_hash

SHA256 = re.compile(r"[0-9a-f]{64}")
ENVELOPE_KEYS = {"schema_version", "text_sha256", "token_count", "ranges", "provenance"}
PROVENANCE_KEYS = {"method", "requested_model", "served_model", "model_identity", "prompt_sha256",
                   "request_sha256", "response_sha256", "run_id"}
RANGE_KEYS = {"start", "end", "speaker", "source"}


def suggestions_hash(ranges: list[dict]) -> str:
    """The existing sidecar hash convention, also saved by human annotations."""
    return text_hash(json.dumps(ranges, sort_keys=True))


def suggestion_bundle_hash(item: dict) -> str:
    """Compare-and-swap identity includes provenance as well as the proposed ranges."""
    return text_hash(json.dumps({"suggestions": item["suggestions"],
                                "provenance": item.get("suggestion_provenance")}, sort_keys=True))


def _sha(value, field: str) -> None:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase SHA256 digest")


def validate_bundle(bundle: dict, item: dict, speakers: list[str]) -> None:
    """Refuse stale offsets, guessed provenance, conflicting ranges and metadata labels."""
    if not isinstance(bundle, dict) or set(bundle) != ENVELOPE_KEYS:
        raise ValueError(f"suggestions must have exactly {sorted(ENVELOPE_KEYS)}")
    if type(bundle["schema_version"]) is not int or bundle["schema_version"] != 1:
        raise ValueError("suggestions schema_version must be 1")
    _sha(bundle["text_sha256"], "text_sha256")
    if type(bundle["token_count"]) is not int or bundle["token_count"] < 1:
        raise ValueError("suggestions token_count must be a positive integer")
    if (bundle["text_sha256"], bundle["token_count"]) != (item["text_sha256"], item["token_count"]):
        raise ValueError("suggestions refer to a different text hash or token count")
    provenance = bundle["provenance"]
    if (not isinstance(provenance, dict) or not PROVENANCE_KEYS <= set(provenance)
            or set(provenance) - PROVENANCE_KEYS - {"transport"}):
        raise ValueError(f"suggestion provenance needs {sorted(PROVENANCE_KEYS)}; only transport is optional")
    if provenance["method"] != "contextual_speaker_turns":
        raise ValueError("provenance method must be contextual_speaker_turns")
    for field in ("requested_model", "run_id"):
        if not isinstance(provenance[field], str) or not provenance[field].strip():
            raise ValueError(f"provenance {field} must be a non-empty string")
    identity, served = provenance["model_identity"], provenance["served_model"]
    if identity not in ("reported", "unverified"):
        raise ValueError("provenance model_identity must be reported or unverified")
    if served is not None and (not isinstance(served, str) or not served.strip()):
        raise ValueError("provenance served_model must be a non-empty string or null")
    if identity == "reported" and served is None:
        raise ValueError("reported model identity requires served_model")
    if identity == "reported" and served != provenance["requested_model"]:
        raise ValueError("reported served_model differs from requested_model")
    if identity == "unverified" and served is not None:
        raise ValueError("unverified model identity requires served_model=null")
    for field in ("prompt_sha256", "request_sha256", "response_sha256"):
        _sha(provenance[field], f"provenance {field}")
    if "transport" in provenance and not isinstance(provenance["transport"], dict):
        raise ValueError("provenance transport must be an object")
    ranges = bundle["ranges"]
    if not isinstance(ranges, list):
        raise ValueError("suggestion ranges must be a list")
    kinds, end = item_kinds(item), 0
    for index, span in enumerate(ranges):
        if (not isinstance(span, dict) or not RANGE_KEYS <= set(span)
                or set(span) - RANGE_KEYS - {"turn_id", "reason"}):
            raise ValueError(f"suggestion range {index} needs {sorted(RANGE_KEYS)}; turn_id and reason are optional")
        a, b = span["start"], span["end"]
        if (type(a) is not int or type(b) is not int or not 0 <= a < b <= item["token_count"] or a < end):
            raise ValueError("suggestion ranges must be ordered, non-overlapping offsets within the text")
        if span["speaker"] not in speakers:
            raise ValueError(f"suggestion range {index} has an unknown speaker")
        if span["source"] != "contextual_model":
            raise ValueError("contextual suggestions must have source contextual_model, never a human origin")
        if any(kinds[i] != "speech" for i in range(a, b)):
            raise ValueError(f"suggestion range {index} contains non-speech metadata or punctuation")
        for field in ("turn_id", "reason"):
            if field in span and (not isinstance(span[field], str) or not span[field].strip()):
                raise ValueError(f"suggestion range {index} {field} must be a non-empty string")
        end = b
    try:
        json.dumps(bundle, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError) as exc:
        raise ValueError("suggestion bundle must be writable as strict UTF-8 JSON") from exc


def apply_bundle(item: dict, bundle: dict, speakers: list[str], *, remember: bool = False) -> dict:
    """Return a new item with the bundle; never modify source text or human files."""
    validate_bundle(bundle, item, speakers)
    result = dict(item)
    if remember:
        history = list(item.get("suggestion_history", []))
        prior = {"suggestions_sha256": item["suggestions_sha256"], "suggestions": item["suggestions"],
                 "suggestion_bundle_sha256": suggestion_bundle_hash(item)}
        if "suggestion_provenance" in item:
            prior["suggestion_provenance"] = item["suggestion_provenance"]
        history.append(prior)
        result["suggestion_history"] = history
    result.update(suggestions=bundle["ranges"], suggestion_provenance=bundle["provenance"],
                  suggestions_sha256=suggestions_hash(bundle["ranges"]),
                  suggestion_scope="Contextual suggestions over source-aligned speech; unmarked speech stays unknown.")
    return result


def validate_stored_suggestions(item: dict, speakers: list[str]) -> None:
    """Check current and historical evidence without repairing or filling anything."""
    history = item.get("suggestion_history", [])
    if not isinstance(history, list):
        raise ValueError("suggestion_history must be a list")
    for index, snapshot in enumerate([item, *history]):
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("suggestions"), list):
            raise ValueError("stored suggestions must be a list in each bundle")
        if not all(isinstance(r, dict) for r in snapshot["suggestions"]):
            raise ValueError("each stored suggestion must be an object")
        _sha(snapshot.get("suggestions_sha256"), "stored suggestions_sha256")
        if snapshot["suggestions_sha256"] != suggestions_hash(snapshot["suggestions"]):
            raise ValueError("stored suggestions differ from their hash")
        if index:
            _sha(snapshot.get("suggestion_bundle_sha256"), "historical suggestion_bundle_sha256")
            if snapshot["suggestion_bundle_sha256"] != suggestion_bundle_hash(snapshot):
                raise ValueError("historical bundle differs from its hash")
        if "suggestion_provenance" in snapshot:
            validate_bundle({"schema_version": 1, "text_sha256": item["text_sha256"],
                             "token_count": item["token_count"], "ranges": snapshot["suggestions"],
                             "provenance": snapshot["suggestion_provenance"]}, item, speakers)


def update_suggestions(ds: Dataset, state_dir: Path, key: str, bundle: dict, expected: str) -> dict:
    """Compare and replace one item atomically, with exact-bundle idempotent retries."""
    from .items import item_errors
    _sha(expected, "expected_bundle_sha256")
    with locked(state_dir / ".tl.lock"):
        ds = Dataset.open(ds.root)
        if "legacy_from" in ds.manifest or ds.manifest["files"]["items_dir"].endswith(".transcripts"):
            raise ValueError("legacy sidecars are read in place; migrate them before replacing suggestions")
        if not isinstance(key, str) or key not in ds.manifest["items"]:
            raise ValueError("key must name an existing item in this dataset")
        path = ds.item_path(key)
        before = fingerprint(path)
        item = ds.item(key)
        errors = item_errors(item, ds.speaker_ids())
        manifest = ds.manifest["items"][key]
        if (item["text_sha256"], item["token_count"]) != (manifest["text_sha256"], manifest["token_count"]):
            errors.append("item text identity differs from the manifest")
        validate_stored_suggestions(item, ds.speaker_ids())
        if errors:
            raise ValueError("existing item is invalid: " + "; ".join(errors))
        replacement = apply_bundle(item, bundle, ds.speaker_ids())
        same = (item["suggestions"] == replacement["suggestions"]
                and item.get("suggestion_provenance") == replacement["suggestion_provenance"])
        if same:
            return {"key": key, "changed": False, "suggestions": len(item["suggestions"]),
                    "suggestions_sha256": item["suggestions_sha256"],
                    "suggestion_bundle_sha256": suggestion_bundle_hash(item)}
        if suggestion_bundle_hash(item) != expected:
            raise ValueError("suggestions changed since they were read; read the item and retry")
        replacement = apply_bundle(item, bundle, ds.speaker_ids(), remember=True)
        try:
            write_atomic(path, json.dumps(replacement, ensure_ascii=False, allow_nan=False) + "\n", expect=before)
        except StaleWrite as exc:
            raise ValueError(str(exc)) from exc
        return {"key": key, "changed": True, "suggestions": len(replacement["suggestions"]),
                "suggestions_sha256": replacement["suggestions_sha256"],
                "suggestion_bundle_sha256": suggestion_bundle_hash(replacement)}
