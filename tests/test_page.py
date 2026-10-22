"""The labeling page, run by node against a stub DOM.

Ported from verbatim-index `scripts/test_annotations_page.py` (SHADOW, RENDER and
MODES) with a synthetic checklist in place of the pundits one, plus a golden check
that this package renders exactly the page verbatim's renderer does.

  GOLDEN     the page rendered here equals verbatim's, byte for byte
  SHADOW     no function parameter hides a module-level binding its own body uses
  RENDER     flagged recordings come first, each under its own section; the draft
             stays hidden until every question has an answer, so those answers carry
             no draft_shown; then the card and its tags show, and only a changed
             answer (not notes) carries draft_shown, with the blind answers kept
             beside it once (answers_before_draft); a page without drafts has no
             sections
  MODES      the page follows its session: a session without the checklist mode shows
             no checklist and binds no checklist key; a recording tagged with two
             projects shows both tags, and the page offers a project filter; a
             captions-only session never claims a recording is done
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from transcript_labeling.page import page as V

FIX = Path(__file__).resolve().parent / "fixtures" / "golden_v2"

#: A stub page for node: enough DOM for the script to render, a local server that
#: answers with SAVED and records each save, the page opened at START when given,
#: NOTES typed into the notes box when given, and then one KEY press.
PAGE_HARNESS = r"""
const nodes = {};
const mk = id => nodes[id] ||= {id, innerHTML: "", textContent: "", className: "", hidden: false, style: {},
  setAttribute(k, v) { this[k] = v; }, querySelectorAll() { return []; }, querySelector() { return null; },
  addEventListener() {}, scrollIntoView() {}, closest() { return null; }, dataset: {}};
let keydown = null;
const posts = [];
global.document = {getElementById: mk, querySelector() { return null; }, querySelectorAll() { return []; },
  addEventListener(type, fn) { if (type === "keydown") keydown = fn; }, createElement: () => mk("x" + Math.random())};
global.window = {open() {}, addEventListener() {}, removeEventListener() {}};
global.location = {hash: START ? "#" + encodeURIComponent(START) : ""};
global.history = {replaceState() {}};
global.CSS = {escape: s => s};
global.fetch = async (url, opt) => {
  if (url === "api/answers") return {ok: true, json: async () => SAVED};
  if (url === "api/answer") { posts.push(JSON.parse(opt.body)); return {ok: true, json: async () => ({ok: true})}; }
  throw new Error("unexpected fetch " + url);
};
let threw = "";
try { new Function(SCRIPT)(); } catch (e) { threw = String(e && e.stack || e).split("\n")[0]; }
setTimeout(() => {
  const list = nodes.list && nodes.list.innerHTML, detail = nodes.detail && nodes.detail.innerHTML;
  const projects = nodes.projects && !nodes.projects.hidden ? nodes.projects.innerHTML : "";
  const keyhelp = nodes.keyhelp && nodes.keyhelp.innerHTML, count = nodes.count && nodes.count.textContent;
  const status = nodes.status && nodes.status.textContent;
  const allPressed = String(nodes["f-all"] && nodes["f-all"]["aria-pressed"]);
  let picked = "";
  if (PICK && nodes.projects && nodes.projects.onclick) {
    nodes.projects.onclick({target: {closest: () => ({dataset: {project: PICK}})}});
    picked = nodes.list.innerHTML;
  }
  if (FILTER && nodes["f-" + FILTER] && nodes["f-" + FILTER].onclick) {
    nodes["f-" + FILTER].onclick();
    picked = nodes.list.innerHTML;
  }
  // The page saves notes 700 ms after the last keystroke.
  if (NOTES !== null && nodes.notes && nodes.notes.oninput) { nodes.notes.value = NOTES; nodes.notes.oninput(); }
  if (keydown && KEY) keydown({key: KEY, target: {tagName: "BODY", closest: () => null}, preventDefault() {}});
  setTimeout(() => console.log(JSON.stringify({list, detail, posts, threw, projects, keyhelp, count, picked, status, allPressed})),
             NOTES !== null ? 800 : 20);
}, 20);
"""

CHECKLIST = json.loads((FIX / "data.json").read_text())["session"]["checklist"]
HELP = {"alpha": "the alpha judges credited them to this person", "beta": "the beta site credits them to this person"}


def session(projects=("alpha",), modes=("checklist", "speakers", "captions")) -> dict:
    return {"name": "t", "projects": list(projects), "modes": list(modes),
            "checklist": CHECKLIST if "checklist" in modes else None,
            "project_help": {p: HELP.get(p, p + " help") for p in projects}}


def node() -> str:
    path = shutil.which("node")
    # Fail, never skip: a suite that skips the page without node hides a broken page.
    assert path, "node is required for the page tests; install it (brew install node)"
    return path


def run_page(rows, saved=None, sess=None, quote_rows=None, pick=None, filt=None, start=None, key="y", notes=None):
    """The page's own script run by node on `rows`, starting from the `saved` answers; what it rendered and saved."""
    page = V.render_page({"rows": rows, "quote_rows": quote_rows or [], "transcript_index": {},
                          "session": sess or session()}, "t", "s")
    script = page[page.index("<script>") + len("<script>"):page.rindex("</script>")]
    # On stdin, not -e: a real page can pass the argument-length limit.
    head = (f"const SCRIPT = {json.dumps(script)};\nconst SAVED = {json.dumps(saved or {'labels': {}, 'attribution': {}})};\n"
            f"const PICK = {json.dumps(pick)};\nconst FILTER = {json.dumps(filt)};\n"
            f"const START = {json.dumps(start)};\nconst KEY = {json.dumps(key)};\nconst NOTES = {json.dumps(notes)};\n")
    n = subprocess.run([node(), "-"], input=head + PAGE_HARNESS, capture_output=True, text=True, encoding="utf-8",
                       timeout=60)
    assert n.returncode == 0 and n.stdout.strip(), n.stderr[-600:]
    out = json.loads(n.stdout.strip().splitlines()[-1])
    return {k: out.get(k) or ("" if k != "posts" else []) for k in
            ("list", "detail", "posts", "threw", "projects", "keyhelp", "count", "picked", "status", "allPressed")}


def flat(s: str) -> str:
    return re.sub(r"\s+", " ", s)


# ------------------------------------------------------------------ GOLDEN

def test_the_page_rendered_here_equals_verbatims_byte_for_byte():
    data = json.loads((FIX / "data.json").read_text(encoding="utf-8"))
    got = V.render_page(data, "Annotations · alpha + beta", "Modes: checklist, speakers, captions. Saves as you go.")
    assert got.encode("utf-8") == (FIX / "page.html").read_bytes()


def test_a_checklist_key_the_page_already_binds_is_refused():
    bad = {**CHECKLIST, "fields": [{**CHECKLIST["fields"][0], "options": [{"value": True, "label": "Y", "key": "J"}]}]}
    with pytest.raises(SystemExit, match="already uses"):
        V.render_page({"rows": [], "session": {**session(), "checklist": bad}}, "t", "s")


def test_title_and_subtitle_are_escaped():
    page = V.render_page({"rows": [], "session": session()}, "<b>T</b>", "a & b")
    assert "<title>&lt;b&gt;T&lt;/b&gt;</title>" in page and "a &amp; b" in page


# ------------------------------------------------------------------ SHADOW

def test_no_function_parameter_hides_a_module_level_binding_its_body_dereferences():
    # FOUND 2026-09-20 in verbatim, in a real browser: the page grew a module-level
    # `store` holding the save adapter, and `setField(store, id, f, v)` already took a
    # parameter of that name. Inside setField the parameter won, so every save died
    # with "store.put is not a function". `node --check` passes it.
    src = V.asset("template.html")
    js = src[src.index("const D = "):]
    globals_ = set()
    for decl in re.findall(r"^(?:let|const|var)\s+(.*?);?$", js, re.M):
        # One `let` line declares several names. Split on commas at bracket depth 0,
        # never on the first `=`, or the check sees only the first name and is vacuous.
        depth, part, parts = 0, "", []
        for ch in decl:
            if ch in "{[(":
                depth += 1
            elif ch in "}])":
                depth -= 1
            if ch == "," and depth == 0:
                parts.append(part)
                part = ""
            else:
                part += ch
        parts.append(part)
        for p in parts:
            name = p.split("=")[0].strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", name):
                globals_.add(name)
    assert {"labels", "attrib", "saver", "writable"} <= globals_, f"extraction missed names: {sorted(globals_)}"
    offenders = []
    for m in re.finditer(r"function\s+(\w+)\s*\(([^)]*)\)\s*\{", js):
        fname, params = m.group(1), m.group(2)
        names = {re.split(r"[=:]", p)[0].strip() for p in params.split(",") if p.strip()}
        names = {n for n in names if re.fullmatch(r"[A-Za-z_$][\w$]*", n)}
        body = js[m.end():m.end() + 1400]
        for n in names & globals_:
            if re.search(r"\b" + re.escape(n) + r"\s*\.\w", body):
                offenders.append(f"{fname}({n}) hides the module-level {n}")
    assert not offenders, "; ".join(offenders)


# ------------------------------------------------------------------ RENDER

GOOD = {"present": True, "format": "solo", "confidence": "high", "reason": "R", "evidence": ["E1"]}
SHOWN = {**GOOD, "model": "model-x"}
BASE = {"slug": "p", "role": "", "title": "T", "channel": "c", "url": "", "video_id": "v", "duration": 600,
        "words": 100, "hints": [], "description": "", "forms": ["Jane Roe"], "mentions": 1, "excerpts": []}
ROWS = [{**BASE, "key": "p/conf", "sid": "conf", "person": "Ann Able", "upload": "20240101", "draft": SHOWN, "doubt": []},
        {**BASE, "key": "p/unsure", "sid": "unsure", "person": "Zed Zulu", "upload": "20240101",
         "draft": {**SHOWN, "confidence": "medium"}, "doubt": ["model confidence medium"]},
        {**BASE, "key": "p/absent", "sid": "absent", "person": "Ann Able", "upload": "20240202",
         "draft": {**SHOWN, "present": False}, "doubt": ["model says the subject is absent"]}]
FULL = {"present": False, "format": "solo"}
ORDER = r'class="sec"[^>]*>([^<]+?) <small|data-k="([^"]+)"'


def order_of(list_html):
    return [a or b for a, b in re.findall(ORDER, list_html)]


@pytest.fixture(scope="module")
def first():
    return run_page(ROWS)


def test_flagged_recordings_come_first_under_their_own_section(first):
    assert first["threw"] == ""
    assert order_of(first["list"]) == ["Check first", "p/absent", "p/unsure", "Model was confident", "p/conf"], \
        first["threw"]


def test_until_its_questions_are_answered_the_draft_is_hidden(first):
    d = first["detail"]
    assert 'data-id="p/absent"' in d and "Model draft hidden" in d
    for leak in ("<b>Model draft</b>", "model says the subject is absent", "<blockquote>", "model-x",
                 "confidence high", 'class="mtag"'):
        assert leak not in d, leak


def test_an_answer_given_while_the_draft_is_hidden_is_blind(first):
    post = first["posts"][0]
    assert post["kind"] == "labels" and post["id"] == "p/absent"
    assert post["value"]["present"] is True and "draft_shown" not in post["value"]


def test_once_every_question_has_an_answer_the_draft_shows_and_a_change_is_marked():
    rv = run_page(ROWS, {"labels": {"p/absent": {**FULL, "notes": "n0", "checked_by": "operator", "key": "p/absent"}},
                         "attribution": {}}, start="p/absent")
    d = rv["detail"]
    assert "<b>Model draft</b>" in d and "Model draft hidden" not in d
    assert "model says the subject is absent" in d and "<blockquote>E1</blockquote>" in d
    assert d.count('class="mtag"') == 2 and rv["threw"] == ""
    pv = rv["posts"][0]
    assert pv["id"] == "p/absent" and pv["value"]["present"] is True
    assert pv["value"]["draft_shown"] == "model-x" and pv["value"]["answers_before_draft"] == FULL


def test_a_second_change_keeps_the_blind_answers_of_the_first():
    blind = {"present": True, "format": "debate"}
    r2 = run_page(ROWS, {"labels": {"p/absent": {**FULL, "draft_shown": "model-x", "answers_before_draft": blind}},
                         "attribution": {}}, start="p/absent")
    assert r2["posts"][0]["value"]["present"] is True and r2["posts"][0]["value"]["answers_before_draft"] == blind


def test_a_label_already_marked_draft_shown_with_no_blind_answers_gains_none():
    rold = run_page(ROWS, {"labels": {"p/absent": {**FULL, "draft_shown": "model-x"}}, "attribution": {}},
                    start="p/absent")
    assert rold["posts"][0]["value"]["present"] is True and "answers_before_draft" not in rold["posts"][0]["value"]


def test_the_same_answer_given_again_adds_no_draft_shown():
    rs = run_page(ROWS, {"labels": {"p/absent": {**FULL, "present": True}}, "attribution": {}}, start="p/absent")
    v = rs["posts"][0]["value"]
    assert v["present"] is True and "draft_shown" not in v and "answers_before_draft" not in v


def test_the_answer_that_completes_the_checklist_is_given_blind():
    rl = run_page(ROWS, {"labels": {"p/absent": {"format": "solo"}}, "attribution": {}}, start="p/absent")
    v = rl["posts"][0]["value"]
    assert "Model draft hidden" in rl["detail"] and v["present"] is True and "draft_shown" not in v


def test_notes_typed_after_the_draft_is_shown_add_no_draft_shown():
    rn = run_page(ROWS, {"labels": {"p/absent": FULL}, "attribution": {}}, start="p/absent", key=None, notes="n1")
    v = rn["posts"][0]["value"]
    assert v["notes"] == "n1" and "draft_shown" not in v and "answers_before_draft" not in v


def test_a_label_that_says_the_draft_was_shown_keeps_it_on_screen():
    rk = run_page(ROWS, {"labels": {"p/absent": {"present": True, "draft_shown": "model-x"}}, "attribution": {}},
                  start="p/absent", key=None)
    assert "<b>Model draft</b>" in rk["detail"] and rk["threw"] == ""


def test_a_page_without_drafts_has_no_sections_and_keeps_surname_order():
    r0 = run_page([{k: v for k, v in x.items() if k not in ("draft", "doubt")} for x in ROWS])
    assert order_of(r0["list"]) == ["p/conf", "p/absent", "p/unsure"]
    assert r0["posts"] and "draft_shown" not in r0["posts"][0]["value"] and r0["threw"] == ""


def test_each_section_header_counts_only_its_own_recordings(first):
    assert "Check first <small>· 0 of 2 checked</small>" in first["list"]
    assert "Model was confident <small>· 0 of 1 checked</small>" in first["list"]
    rc = run_page(ROWS, {"labels": {"p/unsure": {"present": True, "format": "solo"}}, "attribution": {}})
    assert "Check first <small>· 1 of 2 checked</small>" in rc["list"] and rc["threw"] == ""
    # The other section's count must not move: a header that counted every checked
    # recording would read "1 of 1" here.
    assert "Model was confident <small>· 0 of 1 checked</small>" in rc["list"]


def test_the_drafts_evidence_is_escaped():
    ev = run_page([{**ROWS[2], "draft": {**ROWS[2]["draft"], "evidence": ["E1 <i>&"]}}],
                  {"labels": {"p/absent": FULL}, "attribution": {}}, start="p/absent")
    assert "<blockquote>E1 &lt;i&gt;&amp;</blockquote>" in ev["detail"]


def test_the_card_names_each_field_that_differs_and_only_those():
    rd = run_page(ROWS, {"labels": {"p/absent": {"present": True, "format": "debate"}}, "attribution": {}},
                  start="p/absent")
    assert "differs from the model's on: subject present, format</div>" in rd["detail"] and rd["threw"] == ""
    ra = run_page(ROWS, {"labels": {"p/absent": {"present": False, "format": "debate"}}, "attribution": {}},
                  start="p/absent")
    assert "Model draft" in ra["detail"] and "differs" not in ra["detail"] and ra["threw"] == ""


def test_a_recording_with_no_usable_draft_goes_first_and_says_so():
    nd = run_page([{**BASE, "key": "p/nodraft", "sid": "nodraft", "person": "Ann Able", "upload": "20240101",
                    "draft": None, "doubt": ["no model draft"]}, ROWS[0]])
    assert order_of(nd["list"]) == ["Check first", "p/nodraft", "Model was confident", "p/conf"]
    assert "No usable model draft" in nd["detail"] and '<span class="chip">no model draft</span>' in nd["detail"]
    assert 'class="mtag"' not in nd["detail"] and nd["threw"] == ""


# ------------------------------------------------------------------ MODES

QROW = {**{k: v for k, v in BASE.items() if k not in ("hints", "excerpts", "mentions", "description", "url", "words")},
        "key": "p/q", "sid": "q", "person": "Ann Able", "upload": "20240101", "projects": ["alpha", "beta"],
        "quotes": [{"qid": "p:q:3", "project": "alpha", "t": 5, "start": 0, "end": 9, "quote_start": 3,
                    "quote_end": 6, "before": "", "text": "QUOTE-A", "after": ""},
                   {"qid": "abcdef0123456789", "project": "beta", "t": 9, "start": 4, "end": 12,
                    "quote_start": 7, "quote_end": 9, "before": "", "text": "QUOTE-B", "after": ""}]}
TWO = session(("alpha", "beta"), ("speakers", "captions"))


def test_the_checklist_questions_and_keys_come_from_the_spec(first):
    det = flat(first["detail"])
    assert "1 · Subject present" in det and "2 · Format" in det and 'data-mode="checklist"' in det
    assert "Keys: <b>y</b>/<b>n</b> subject present" in first["keyhelp"]


def test_without_the_checklist_mode_there_is_no_checklist_and_no_checklist_key():
    m = run_page([], sess=TWO, quote_rows=[QROW])
    assert m["threw"] == "" and "data-f=" not in m["detail"] and 'data-mode="checklist"' not in m["detail"]
    assert m["posts"] == [] and "subject present" not in m["keyhelp"]
    assert m["list"].count('class="pchip"') == 2
    assert 'class="pchip">alpha<' in m["list"] and 'class="pchip">beta<' in m["list"]
    assert "QUOTE-A" in m["detail"] and "QUOTE-B" in m["detail"]
    assert HELP["alpha"] in m["detail"] and HELP["beta"] in m["detail"]
    for p in ("all", "alpha", "beta"):
        assert f'data-project="{p}"' in m["projects"]


def test_the_project_filter_keeps_that_projects_recordings():
    only_a = {**QROW, "key": "p/i", "sid": "i", "person": "Bo Brand", "projects": ["alpha"],
              "quotes": [{**QROW["quotes"][0], "qid": "p:i:3"}]}
    f = run_page([], sess=TWO, quote_rows=[QROW, only_a], pick="beta")
    assert 'data-k="p/q"' in f["picked"] and 'data-k="p/i"' not in f["picked"] and 'data-k="p/i"' in f["list"]


def test_a_captions_only_session_shows_no_quotes_and_never_claims_done():
    cap = run_page([], sess=session(("alpha",), ("captions",)), quote_rows=[{**QROW, "projects": ["alpha"]}])
    assert cap["threw"] == "" and "QUOTE-A" not in cap["detail"]
    assert "Correct the captions of this recording" in cap["detail"]
    assert cap["count"] == "0 of 1 recordings have caption corrections" and 'data-k="p/q"' in cap["list"]
    assert "nothing else to answer" in cap["status"] and "0 of 1 corrected" in cap["list"]
    assert "quotes answered" not in cap["list"] and cap["allPressed"] == "true"
    capd = run_page([], sess=session(("alpha",), ("captions",)), quote_rows=[{**QROW, "projects": ["alpha"]}],
                    filt="done")
    assert capd["threw"] == "" and "data-k=" not in capd["picked"] and "Nothing here" in capd["picked"]


def test_with_one_project_there_is_no_filter_and_no_tag():
    one = run_page([], sess=session(("alpha",), ("speakers", "captions")), quote_rows=[{**QROW, "projects": ["alpha"]}])
    assert one["projects"] == "" and 'class="pchip"' not in one["list"]
