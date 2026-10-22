"""Render the Phase 1 golden page with verbatim-index's own render_page (frozen copy)."""
import json, sys
from pathlib import Path
VI, OUT = sys.argv[1], Path(sys.argv[2])
sys.path.insert(0, VI)
import annotations_page as V
checklist = {
    "fields": [
        {"name": "present", "label": "Subject present", "help": "Takes part live", "type": "bool", "required": True,
         "draft_label": "subject present",
         "options": [{"value": True, "label": "Yes", "key": "y", "cls": "y"},
                     {"value": False, "label": "No", "key": "n", "cls": "n"}]},
        {"name": "format", "label": "Format", "help": "of this recording", "type": "enum", "required": True,
         "draft_label": "format", "differs_only_if": "present", "prompt": "Pick the format that fills most of it.",
         "options": [{"value": v, "label": v, "key": str(i + 1), "help": f"{v} help"}
                     for i, v in enumerate(("solo", "conversation", "debate"))]},
    ],
    "notes_placeholder": "e.g. guest-only episode",
    "accept_when_true": ["present"],
    "goal": {"count": 2, "help": "recordings verified so far"},
    "draft_fields": [["present", "subject present"], ["format", "format"]],
}
session = {"name": "demo", "projects": ["alpha", "beta"], "modes": ["checklist", "speakers", "captions"],
           "checklist": checklist,
           "project_help": {"alpha": "the alpha judges credited them to this person",
                            "beta": "the beta site credits them to this person"}}
rows = [{"key": "jane-roe/talk-1", "slug": "jane-roe", "sid": "talk-1", "person": "Jane Roe", "role": "Host",
         "title": "A talk </script> & <b>more</b> at the café — naïve “quotes”", "channel": "Chan", "upload": "20240102", "video_id": "vid1",
         "duration": 600, "words": 120, "hints": ["hint one"], "description": "desc", "forms": ["Jane Roe"],
         "mentions": 2, "excerpts": [{"at": 0.12, "t": 5, "text": "Hello there", "start": 0, "end": 2}],
         "projects": ["alpha", "beta"],
         "draft": {"present": True, "format": "solo", "confidence": "high", "reason": "R", "evidence": ["E1"],
                   "model": "model-x"}, "doubt": []}]
quote_rows = [{"key": "jane-roe/talk-1", "quotes": [
    {"qid": "jane-roe:talk-1:1", "project": "alpha", "t": 3, "start": 0, "end": 4, "quote_start": 1, "quote_end": 3,
     "before": "", "text": "Hello there", "after": ""}]}]
index = {"jane-roe/talk-1": {"text_sha256": "0" * 64, "token_count": 4}}
data = {"rows": rows, "quote_rows": quote_rows, "transcript_index": index, "session": session}
(OUT / "data.json").write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
page = V.render_page(data, "Annotations · alpha + beta", "Modes: checklist, speakers, captions. Saves as you go.")
(OUT / "page.html").write_bytes(page.encode("utf-8"))
print(len(page))
