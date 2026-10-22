"""Checklist answers: questions about a whole item, from a dataset's checklist spec.

A spec is `{"fields": [...], "goal": ..., "accept_when_true": [...], ...}`. Each
field has a `name`, a `type` (`bool`, `enum` or free text), a `label`, whether it
is `required`, and `options` with the keys the page binds to them.

Model drafts stay hidden until every required question has an answer, so those
answers are blind. The first answer changed with the draft on screen marks the
label `draft_shown` and keeps the blind answers in `answers_before_draft`. Once a
label has either field, no later save drops or replaces it.
"""
from __future__ import annotations

#: Keys the page itself binds; a checklist option may not take one.
RESERVED_KEYS = {"j", "k", "v", "[", "]", "arrowup", "arrowdown"}
#: Once a label records that its draft was on screen, and the blind answers it had
#: before, no later save drops or replaces either. The page sends the whole label, so
#: a second tab still showing an older copy would otherwise turn an assisted label
#: back into a blind one.
STICKY_LABEL_FIELDS = ("draft_shown", "answers_before_draft")


def answered(field: dict, value) -> bool:
    """Whether a checklist answer is a complete answer to `field`."""
    if field["type"] == "bool":
        return isinstance(value, bool)
    if field["type"] == "enum":
        return any(value == o["value"] for o in field["options"])
    return isinstance(value, str) and bool(value.strip())


def keep_assisted(value: dict, stored: dict | None) -> dict:
    """`value`, with any sticky field the stored label already has.

    A stored label marked draft_shown with no blind answers has none to keep: an
    older page marked every save. A copy sent later would hold answers given with
    the draft on screen, so it is dropped.
    """
    stored = stored or {}
    new = {**value, **{f: stored[f] for f in STICKY_LABEL_FIELDS if f in stored}}
    if stored.get("draft_shown") and "answers_before_draft" not in stored:
        new.pop("answers_before_draft", None)
    return new


def to_labels(docs: list[dict], keys, checklist: dict) -> dict:
    """Complete checklist answers, one per item key.

    Refuses a document whose key is not on the checklist, and one whose
    answers_before_draft is not a complete, valid set beside draft_shown. Reports
    rather than guesses a missing field: a partly answered item is left out,
    not filled.
    """
    fields = checklist["fields"]
    required = [f for f in fields if f.get("required")]
    out, partial, unknown, bad_blind = {}, [], [], []
    for d in docs:
        key = d.get("key")
        if key not in keys:
            unknown.append(key)
            continue
        if not all(answered(f, d.get(f["name"])) for f in required):
            partial.append(key)
            continue
        # The blind answers kept when a label was first changed with the draft on
        # screen: all of them, each valid, and only beside draft_shown.
        before = d.get("answers_before_draft")
        if before is not None and not (isinstance(before, dict) and d.get("draft_shown")
                                       and set(before) == {f["name"] for f in required}
                                       and all(answered(f, before[f["name"]]) for f in required)):
            bad_blind.append(key)
            continue
        out[key] = {**{f["name"]: d.get(f["name"]) for f in fields},
                    "checked_by": d.get("checked_by") or "operator", "notes": d.get("notes") or "",
                    # An assisted label says so, and keeps the answers it had before,
                    # so calibration compares those instead.
                    **({"draft_shown": d["draft_shown"]} if d.get("draft_shown") else {}),
                    **({"answers_before_draft": before} if before is not None else {})}
    if unknown:
        raise SystemExit(f"REFUSING: {len(unknown)} labels name recordings not on the checklist, e.g. {unknown[:3]}")
    if bad_blind:
        raise SystemExit(f"REFUSING: {len(bad_blind)} labels carry answers_before_draft that is not a complete, valid "
                         f"set of answers beside draft_shown, e.g. {bad_blind[:3]}")
    return {"labels": out, "partial": sorted(partial), "unlabelled": sorted(set(keys) - set(out) - set(partial))}
