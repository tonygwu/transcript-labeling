"""The labeling page module, run by node against a stub DOM and a stub server.

Ported from verbatim-index `scripts/test_annotations_page.py` (SHADOW, RENDER and
MODES), now against the service's page: an empty shell whose module script
(`page/assets.build_app_js`) fetches its data with the session header.

  SHELL      the module loads its data from api/page with the session header, and a
             401 stops it with a login message
  SHADOW     no function parameter hides a module-level binding its own body uses
  RENDER     flagged recordings come first, each under its own section; the draft
             stays hidden until every question has an answer, so those answers carry
             no draft_shown; then the card and its tags show, and only a changed
             answer (not notes) carries draft_shown, with the blind answers kept
             beside it once (answers_before_draft); a page without drafts has no
             sections
  MODES      the page follows its dataset: no checklist mode, no checklist and no
             checklist key; two project tags show and filter; a captions-only dataset
             never claims an item is done
  GENERIC    a dataset item with no checklist and no passages opens its transcript,
             counts as done only when marked done, and links no media unless the
             dataset names a media URL
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from transcript_labeling.page.assets import asset_bytes, build_app_js

#: A stub page for node: enough DOM for the script to render, a local server that
#: answers with SAVED and records each save, the page opened at START when given,
#: NOTES typed into the notes box when given, and then one KEY press.
PAGE_HARNESS = r"""
const nodes = {};
const mk = id => nodes[id] ||= {id, innerHTML: "", textContent: "", className: "", hidden: false, style: {},
  setAttribute(k, v) { this[k] = v; }, querySelectorAll() { return []; }, querySelector() { return null; },
  addEventListener() {}, scrollIntoView() {}, closest() { return null; }, dataset: {}};
let keydown = null;
const posts = [], fetched = [];
global.document = {getElementById: mk, querySelector() { return null; }, querySelectorAll() { return []; },
  addEventListener(type, fn) { if (type === "keydown") keydown = fn; }, createElement: () => mk("x" + Math.random())};
global.localStorage = {getItem: k => k === "tl-session" ? "sess-1" : null};
global.window = {open() {}, addEventListener() {}, removeEventListener() {},
  fetch: async (url, opt) => {
    fetched.push([url, ((opt || {}).headers || {})["X-TL-Session"]]);
    if (url === "api/page") return PAGE_STATUS === 200 ? {ok: true, status: 200, json: async () => DATA}
                                                       : {ok: false, status: PAGE_STATUS, json: async () => ({})};
    if (url === "api/answers") return {ok: true, json: async () => SAVED};
    if (url === "api/answer" || url === "api/note" || url === "api/done" || url === "api/reveal") {
      posts.push({url, ...JSON.parse(opt.body)});
      if (STALE) return {ok: false, status: 409, json: async () => ({error: "changed outside", stale: true})};
      return {ok: true, json: async () => ({ok: true})}; }
    throw new Error("unexpected fetch " + url);
  }};
global.location = {hash: START ? "#" + encodeURIComponent(START) : ""};
global.history = {replaceState() {}};
global.CSS = {escape: s => s};
let threw = "";
(async () => {
  try { await (new Function("return (async () => {\n" + SCRIPT + "\n})()"))(); }
  catch (e) { threw = String(e && e.stack || e).split("\n")[0]; }
})();
setTimeout(() => {
  const list = nodes.list && nodes.list.innerHTML, detail = nodes.detail && nodes.detail.innerHTML;
  const projects = nodes.projects && !nodes.projects.hidden ? nodes.projects.innerHTML : "";
  const keyhelp = nodes.keyhelp && nodes.keyhelp.innerHTML, count = nodes.count && nodes.count.textContent;
  const status = nodes.status && nodes.status.textContent, banner = nodes.banner && !nodes.banner.hidden ? nodes.banner.textContent : "";
  const title = nodes.title && nodes.title.textContent;
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
  // Keys are pressed 20 ms apart, so each save settles before the next press.
  const keys = Array.isArray(KEY) ? KEY : KEY ? [KEY] : [];
  keys.forEach((k, i) => setTimeout(() => keydown && keydown({key: k, target: {tagName: "BODY", closest: () => null},
                                                                preventDefault() {}}), 20 * i));
  setTimeout(() => console.log(JSON.stringify({list, detail, posts, threw, projects, keyhelp, count, picked, status,
                                               allPressed, banner, title, fetched,
                                               bannerAfter: nodes.banner && !nodes.banner.hidden ? nodes.banner.textContent : ""})),
             (NOTES !== null ? 800 : 30) + 20 * keys.length);
}, 30);
"""

CHECKLIST = {
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
HELP = {"alpha": "the alpha judges credited them to this person", "beta": "the beta site credits them to this person"}
#: verbatim's configuration: the subject, someone else, unsure; both for a mixed quote.
LEGACY_SPEAKERS = [{"id": "subject", "label": "{person}", "key": "s"}, {"id": "other", "label": "Someone else",
                                                                         "key": "e"}]
YOUTUBE = {"url": "https://www.youtube.com/watch?v={video_id}&t={t}s"}


def session(projects=("alpha",), modes=("checklist", "speakers", "captions"), *, speakers=LEGACY_SPEAKERS,
            media=YOUTUBE, mixed="both") -> dict:
    return {"name": "t", "project": "demo", "title": "Demo dataset", "projects": list(projects), "modes": list(modes),
            "checklist": CHECKLIST if "checklist" in modes else None,
            "project_help": {p: HELP.get(p, p + " help") for p in projects}, "speakers": speakers,
            "unclear": {"id": "unclear", "label": "Unsure", "key": "u"}, "mixed_answer": mixed,
            "label_template": "{person}", "reveal": "after_answer", "media": media}


def node() -> str:
    path = shutil.which("node")
    # Fail, never skip: a suite that skips the page without node hides a broken page.
    assert path, "node is required for the page tests; install it (brew install node)"
    return path


VERSIONS = {"answers": "p1.0", "review": "p1.0"}


def run_page(rows, saved=None, sess=None, quote_rows=None, pick=None, filt=None, start=None, key="y", notes=None,
             page_status=200, index=None, stale=False):
    """The page module run by node on `rows`, starting from the `saved` answers; what it rendered and saved."""
    data = {"rows": rows, "quote_rows": quote_rows or [], "transcript_index": index or {},
            "session": sess or session()}
    saved = {"versions": VERSIONS, **(saved or {"labels": {}, "attribution": {}})}
    head = (f"const SCRIPT = {json.dumps(build_app_js())};\nconst SAVED = {json.dumps(saved)};\n"
            f"const DATA = {json.dumps(data)};\nconst PAGE_STATUS = {page_status};\n"
            f"const PICK = {json.dumps(pick)};\nconst FILTER = {json.dumps(filt)};\n"
            f"const START = {json.dumps(start)};\nconst KEY = {json.dumps(key)};\nconst NOTES = {json.dumps(notes)};\n"
            f"const STALE = {json.dumps(stale)};\n")
    n = subprocess.run([node(), "-"], input=head + PAGE_HARNESS, capture_output=True, text=True, encoding="utf-8",
                       timeout=60)
    assert n.returncode == 0 and n.stdout.strip(), n.stderr[-600:]
    out = json.loads(n.stdout.strip().splitlines()[-1])
    return {k: out.get(k) or ("" if k not in ("posts", "fetched") else []) for k in
            ("list", "detail", "posts", "threw", "projects", "keyhelp", "count", "picked", "status", "allPressed",
             "banner", "title", "fetched", "bannerAfter")}


def flat(s: str) -> str:
    return re.sub(r"\s+", " ", s)


# ------------------------------------------------------------------ SHELL

def test_the_module_loads_its_data_with_the_session_and_titles_the_page():
    r = run_page(ROWS)
    assert r["threw"] == "" and r["title"] == "Demo dataset"
    assert ["api/page", "sess-1"] in r["fetched"] and ["api/answers", "sess-1"] in r["fetched"]
    assert all(sess == "sess-1" for _, sess in r["fetched"])


def test_every_save_sends_back_the_versions_the_page_loaded():
    r = run_page(ROWS)
    assert r["posts"] and all(p["versions"] == VERSIONS for p in r["posts"])


def test_a_stale_refusal_stops_saving_and_asks_for_a_reload():
    r = run_page(ROWS, stale=True)
    assert len(r["posts"]) == 1
    r = run_page(ROWS, stale=True, key=["y", "n"])  # the second press comes after the refusal settles
    assert len(r["posts"]) == 1, "the page kept saving after a stale refusal"
    assert r["banner"] == ""  # before the save
    assert "changed outside this page" in r["bannerAfter"] and "Reload the page" in r["bannerAfter"]


def test_no_save_sends_checked_by():
    # The server says who checked; the page never claims it.
    r = run_page(ROWS)
    assert r["posts"] and all("checked_by" not in json.dumps(p) for p in r["posts"])


def test_a_session_without_its_speakers_config_stops_the_page():
    sess = session()
    del sess["mixed_answer"]
    r = run_page(ROWS, sess=sess)
    assert "without its speakers, unclear entry or mixed answer" in r["banner"] and r["posts"] == []


def test_agent_supplied_row_and_option_values_are_escaped():
    row = {**ROWS[0], "words": "<img src=x id=INJ>", "upload": "2026-01-01"}
    r = run_page([row])
    assert "<img src=x id=INJ>" not in r["detail"] and "&lt;img src=x id=INJ&gt; words" in r["detail"]
    r = run_page([{**ROWS[0], "upload": "<!--1234"}])
    assert "<!--" not in r["detail"] and "uploaded &lt;!---12-34" in r["detail"]
    odd = {**CHECKLIST, "fields": [{**CHECKLIST["fields"][0],
                                    "options": [{"value": "don't know", "label": "?", "key": "q"}]}]}
    sess = session()
    sess["checklist"] = odd
    r = run_page(ROWS, sess=sess)
    assert 'data-v="&quot;don\'t know&quot;"' in r["detail"]


def test_answers_without_versions_leave_the_page_read_only():
    r = run_page(ROWS, saved={"labels": {}, "attribution": {}, "versions": None})
    assert r["posts"] == [] and "can't be saved" in r["banner"]


def test_without_a_session_the_page_says_to_log_in():
    r = run_page(ROWS, page_status=401)
    assert "Open a new login link" in r["banner"] and r["list"] == ""


def test_the_shells_hold_no_inline_script_or_style():
    for name in ("page.html", "login.html", "landing.html"):
        html = asset_bytes(name).decode()
        assert "<script>" not in html and "style=" not in html and "<style" not in html, name
        assert 'src="/static/' in html
    assert "style=" not in build_app_js()


# ------------------------------------------------------------------ SHADOW

def test_no_function_parameter_hides_a_module_level_binding_its_body_dereferences():
    # FOUND 2026-09-20 in verbatim, in a real browser: the page grew a module-level
    # `store` holding the save adapter, and `setField(store, id, f, v)` already took a
    # parameter of that name. Inside setField the parameter won, so every save died
    # with "store.put is not a function". `node --check` passes it.
    js = build_app_js()
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


def test_a_checklist_without_draft_fields_still_shows_the_draft():
    # draft_fields is optional in a dataset's checklist; the page lists none rather than failing.
    sess = session()
    sess["checklist"] = {k: v for k, v in CHECKLIST.items() if k != "draft_fields"}
    rv = run_page(ROWS, {"labels": {"p/absent": {**FULL, "checked_by": "operator", "key": "p/absent"}},
                         "attribution": {}}, sess=sess, start="p/absent")
    assert rv["threw"] == "" and "<b>Model draft</b>" in rv["detail"], rv["threw"]


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


def test_a_field_without_a_draft_label_is_named_by_its_label():
    sess = session()
    sess["checklist"] = {**CHECKLIST, "fields": [{k: v for k, v in f.items() if k != "draft_label"}
                                                 for f in CHECKLIST["fields"]]}
    rd = run_page(ROWS, {"labels": {"p/absent": {"present": True, "format": "debate"}}, "attribution": {}},
                  sess=sess, start="p/absent")
    assert "differs from the model's on: Subject present, Format</div>" in rd["detail"] and rd["threw"] == ""


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


# ------------------------------------------------------------------ GENERIC

ANN_BO = [{"id": "ann", "label": "Ann Able", "key": "1"}, {"id": "bo", "label": "Bo Brand", "key": "2"}]
ITEM = {"key": "call/one", "slug": "call", "sid": "one", "title": "Weekly call", "person": ""}


def test_a_plain_item_opens_its_transcript_and_is_done_only_when_marked():
    sess = session(("demo",), ("speakers", "captions"), speakers=ANN_BO, media=None, mixed="mixed")
    r = run_page([ITEM], sess=sess, key=None, index={"call/one": {"text_sha256": "x", "token_count": 3}})
    assert r["threw"] == ""
    assert '<details class="range-extra" open>' in r["detail"] and "Mark who speaks in this transcript" in r["detail"]
    assert 'id="done"' in r["detail"] and "Mark this item done" in r["detail"]
    assert "Items</b>" in r["list"] and "0 of 1 done" in r["list"]
    assert 'class="yt"' not in r["detail"] and "youtube" not in r["detail"].lower()
    assert "Note on this item" in r["detail"]
    assert "<b>1</b> Ann Able · <b>2</b> Bo Brand · <b>u</b> Unsure" in r["keyhelp"] and "open video" not in r["keyhelp"]
    done = run_page([ITEM], sess=sess, key=None, saved={"labels": {}, "attribution": {},
                                                        "review": {"notes": {}, "done": {"call/one": {}},
                                                                   "reveals": {}, "finished": None}})
    assert done["count"].startswith("1 of 1 items done") and "Done ✓" in done["detail"]
    assert "Nothing here" in done["list"]  # the To do filter hides a done item


def test_media_links_follow_the_datasets_template_only():
    row = {**ROWS[0], "video_id": "abc"}
    with_media = run_page([row], key=None)
    assert 'href="https://www.youtube.com/watch?v=abc&amp;t=120s"' in with_media["detail"]
    without = run_page([row], sess=session(media=None), key=None)
    assert 'class="yt"' not in without["detail"] and "youtube" not in without["detail"].lower()


def test_a_passage_shows_its_title_why_question_and_note():
    q = {**QROW, "quotes": [{**QROW["quotes"][0], "title": "Budget", "why": "Who agreed?", "question": "Who said it?"}]}
    r = run_page([], sess=session(("alpha",), ("speakers", "captions")), quote_rows=[q],
                 saved={"labels": {}, "attribution": {}, "review": {"notes": {"p:q:3": {"text": "check me"}},
                                                                    "done": {}, "reveals": {}, "finished": None}})
    assert r["threw"] == ""
    assert '<b class="qtitle">Budget</b>' in r["detail"] and "Who agreed?" in r["detail"]
    assert "<b>Who said it?</b>" in r["detail"] and 'data-note="p:q:3"' in r["detail"] and "check me" in r["detail"]


# Marking selected words with a speaker key, header clicks, reveal and Finish need a
# real DOM selection and the span editor's markup, which this stub DOM does not build;
# the browser check (Playwright) covers them.
