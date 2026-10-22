"""Transcript formats: how an uploaded file becomes an item's text, segments and header ranges.

The item keeps the file's text exactly, so character offsets into the file are
offsets into the item. What changes per format is which tokens are speech:

  zoom-notes  Optional notes before the first cue, then cues of a clock-time line
              `HH:MM:SS --> HH:MM:SS`, a `Label: text` line, and a blank line.
              No WEBVTT header and no cue numbers. Any number of labels, including
              `Unknown`. Every cue must open with a label. A later line of a cue
              that opens with a label some cue opens with is another speaker in
              the same cue, and becomes its own segment with the cue's times. A
              later line that opens like a label no cue opens with is refused,
              because crediting it to the speaker above would hide a speaker. The
              notes, every time line and every label are headers (the `meta`
              token kind): shown, never labelled.
  bracket-ts  verbatim-index captions: `[hh:mm:ss]` marks and `>>` turn marks inside
              running text, which the token kinds already treat as non-speech.
  plain       anything else: every word is speech.

Detection is automatic and strict. A file with full-line clock times is parsed as
zoom-notes or refused with the line that breaks the shape; it never falls back
to plain. WebVTT and SRT are refused until something needs them.
"""
from __future__ import annotations

import re

from .timing import clock_seconds

FORMATS = ("zoom-notes", "bracket-ts", "plain")
TIMING = re.compile(r"\s*(\d{2}):(\d{2}):(\d{2})(?:[.,]\d+)?\s*-->\s*(\d{2}):(\d{2}):(\d{2})(?:[.,]\d+)?\s*")
LABEL = re.compile(r"([^:\n]{1,60}?):(?=\s|$)")
MARK = re.compile(r"\[\d{2}:\d{2}:\d{2}\]")
#: The longest label read as a speaker label rather than speech, in words.
LABEL_MAX_WORDS = 5


def _refuse_unsupported(text: str, lines: list[str]) -> None:
    if text.lstrip("\ufeff").lstrip().startswith("WEBVTT"):
        raise SystemExit("REFUSING: WebVTT is not supported yet; convert it, or add a parser to formats.py")
    timing = [i for i, line in enumerate(lines) if TIMING.fullmatch(line.lstrip("\ufeff"))]
    if any(lines[i - 1].lstrip("\ufeff").strip().isdigit() for i in timing if i > 0):
        raise SystemExit("REFUSING: this looks like SRT (numbered cues), which is not supported yet")


def detect(text: str) -> str:
    """The format of `text`. WebVTT and SRT stop the caller."""
    lines = text.splitlines()
    _refuse_unsupported(text, lines)
    if any(TIMING.fullmatch(line.lstrip("\ufeff")) for line in lines):
        return "zoom-notes"
    if MARK.search(text):
        return "bracket-ts"
    return "plain"


def parse(text: str, fmt: str | None = None) -> dict:
    """`{format, segments, meta, labels}` for `text`. Segments and meta are token ranges."""
    if fmt is not None and fmt not in FORMATS:
        raise SystemExit(f"REFUSING: format {fmt!r} must be one of {FORMATS}")
    _refuse_unsupported(text, text.splitlines())
    fmt = fmt or detect(text)
    if fmt != "zoom-notes":
        return {"format": fmt, "segments": None, "meta": [], "labels": []}
    return _zoom_notes(text)


def _label(line: str) -> tuple[str, int] | None:
    """`(label, end of the label's colon)` when `line` starts `Label: `, else None."""
    m = LABEL.match(line)
    if m and m.group(1).strip() and len(m.group(1).split()) <= LABEL_MAX_WORDS:
        return m.group(1).strip(), m.end()
    return None


def _zoom_notes(text: str) -> dict:
    toks = list(re.finditer(r"\S+", text))
    starts = [t.start() for t in toks]

    def tok_at(char: int) -> int:
        """The index of the first token starting at or after `char`."""
        lo, hi = 0, len(starts)
        while lo < hi:
            mid = (lo + hi) // 2
            if starts[mid] < char:
                lo = mid + 1
            else:
                hi = mid
        return lo

    lines, pos = [], 0
    for raw in text.splitlines(keepends=True):
        # A byte-order mark is kept in the text, but must not hide the first line's shape.
        lines.append((pos, raw.rstrip("\r\n").lstrip("\ufeff") if pos == 0 else raw.rstrip("\r\n"),
                      len(raw) - len(raw.lstrip("\ufeff")) if pos == 0 else 0))
        pos += len(raw)
    first = next((n for n, (_, line, _) in enumerate(lines) if TIMING.fullmatch(line)), None)
    if first is None:
        raise SystemExit("REFUSING: no `HH:MM:SS --> HH:MM:SS` line, so this is not zoom-notes")
    for n in range(first):
        if "-->" in lines[n][1]:
            raise SystemExit(f"REFUSING: line {n + 1}, before the first cue, looks like a broken clock-time line")
    # Pass 1: the cues, each a time line and its text lines.
    cues = []
    n = first
    while n < len(lines):
        if not lines[n][1].strip():
            n += 1
            continue
        m = TIMING.fullmatch(lines[n][1])
        if not m:
            raise SystemExit(f"REFUSING: line {n + 1} is neither a clock-time line nor inside a cue; zoom-notes "
                             "cues are a `HH:MM:SS --> HH:MM:SS` line, a `Label: text` line and a blank line")
        timing_line, body = n, []
        n += 1
        while n < len(lines) and lines[n][1].strip() and not TIMING.fullmatch(lines[n][1]):
            body.append(n)
            n += 1
        if not body:
            raise SystemExit(f"REFUSING: the cue at line {timing_line + 1} has no text line")
        if _label(lines[body[0]][1]) is None:
            raise SystemExit(f"REFUSING: line {body[0] + 1} has no `Label:` speaker label; every zoom-notes cue "
                             "starts with one")
        cues.append((timing_line, m.groups(), body))
    known = {_label(lines[body[0]][1])[0] for _, _, body in cues}
    # Pass 2: segments. A later text line that starts with a label some cue opens with
    # is another speaker inside the same cue: its own segment, with the cue's times.
    meta, segments, labels = [], [], {}
    notes_end = tok_at(lines[first][0])
    if notes_end > 0:
        meta.append([0, notes_end])
    for timing_line, g, body in cues:
        header0 = tok_at(lines[timing_line][0])
        groups, current = [], None
        for ln in body:
            lab = _label(lines[ln][1])
            if current is not None and lab is not None and lab[0] not in known:
                raise SystemExit(f"REFUSING: line {ln + 1} starts like a speaker label that opens no cue, so its "
                                 "words would be credited to the speaker above it; give that speaker a cue of "
                                 "its own, or reword the line so it does not start with `Words:`")
            if current is None or (lab is not None and lab[0] in known):
                current = {"line": ln, "label": lab[0], "colon": lab[1], "lines": [ln]}
                groups.append(current)
            else:
                current["lines"].append(ln)
        for i, grp in enumerate(groups):
            start, line, shift = lines[grp["line"]]
            h0 = header0 if i == 0 else tok_at(start)
            b0 = tok_at(start + shift + grp["colon"])
            last = lines[grp["lines"][-1]]
            b1 = max(tok_at(last[0] + last[2] + len(last[1])), b0)
            meta.append([h0, b0])
            segments.append({"label": grp["label"], "t_start": clock_seconds(*g[:3]),
                             "t_end": clock_seconds(*g[3:]), "header": [h0, b0], "body": [b0, b1]})
            labels[grp["label"]] = labels.get(grp["label"], 0) + 1
    return {"format": "zoom-notes", "segments": segments, "meta": meta,
            "labels": sorted(labels, key=lambda k: (-labels[k], k))}


def parse_suggest_map(spec: str | None) -> dict:
    """`"Speaker 1=jane,Unknown=other"` as a dict. A malformed entry stops the caller."""
    out = {}
    for part in (spec or "").split(","):
        if not part.strip():
            continue
        if part.count("=") != 1:
            raise SystemExit(f"REFUSING: suggest-map entry {part!r} must be `Label=speaker_id`")
        label, sid = (x.strip() for x in part.split("="))
        if not label or not sid or label in out:
            raise SystemExit(f"REFUSING: suggest-map entry {part!r} is empty or repeats a label")
        out[label] = sid
    return out


def segment_suggestions(segments: list[dict] | None, mapping: dict, speakers: list[str]) -> list[dict]:
    """A suggestion over each segment's body whose label the map names.

    The map must name only labels the file has and speakers the dataset has; a label
    the map leaves out gets no suggestion, so its words stay unknown.
    """
    if not mapping:
        return []
    seen = {s["label"] for s in segments or []}
    unknown = sorted(set(mapping) - seen)
    if unknown:
        raise SystemExit(f"REFUSING: suggest-map names labels the file does not have: {unknown}")
    bad = sorted({v for v in mapping.values() if v not in speakers})
    if bad:
        raise SystemExit(f"REFUSING: suggest-map names speakers the dataset does not have: {bad}")
    return [{"start": s["body"][0], "end": s["body"][1], "speaker": mapping[s["label"]],
             "source": "segment_label", "label": s["label"]}
            for s in segments or [] if s["label"] in mapping and s["body"][1] > s["body"][0]]
