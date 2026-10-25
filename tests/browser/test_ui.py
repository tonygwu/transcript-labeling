"""The labeling page in a real Chromium: login link, marking words, a split caption, reveal, Finish.

Deselected by default. Run with:

    uv run --group browser pytest -m browser

It needs Playwright's Chromium build for the pinned Playwright version
(`uv run --group browser playwright install chromium` fetches it once).
`uv run pytest -m browser` without the group fails with "Playwright is missing".

  LOGIN      the link logs in once; reopened, it says it was used and its Continue link
             opens the dataset; a fresh browser is refused; the landing page lists the dataset
  MARK       words dragged across with the mouse and painted with a speaker are saved
  SPLIT      one caption painted with two speakers exports as a split segment
  REVEAL     suggestions appear only on request; a reveal clicked while a save is still in
             flight waits for it, so the last mark is part of the blind answer
  FINISH     Finish is saved and the page says so; the marks survive a reload
  PASSAGE    a verbatim-shaped item (person, channel, video link, two focus passages):
             each passage's result follows the ranges marked over it
  THEMES     both flows run in light and in dark; the dark background must differ from the
             light one; every step fails on a page script error

Screenshots of each step go to $TL_SCREENSHOTS when it is set, else to a pytest temp
folder; the folder is printed so a person can look at them. A run first deletes the
screenshots an earlier run left there (only names of the form NN-*-light.png and
NN-*-dark.png), so a run that stops early leaves no stale picture behind.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from transcript_labeling import cli
from transcript_labeling.client import ServiceClient
from transcript_labeling.export3 import export_dataset
from transcript_labeling.home import Home
from transcript_labeling.service import Service, build_service_server

pytestmark = pytest.mark.browser

ZOOM = Path(__file__).resolve().parent.parent / "fixtures" / "zoom" / "notes.txt"
SPEAKERS = [{"id": "ann", "label": "Ann Able", "key": "1"}, {"id": "bo", "label": "Bo Brand", "key": "2"}]
BRACKET = ("[00:00:05] Welcome back to the show. >> Thanks for having me. [00:00:12] We think the bridge "
           "will open next spring, and the budget holds. >> That is good news for the town. [00:00:31] "
           "The second span opens in the autumn if the steel arrives on time.")
WIDTH, HEIGHT = 390, 844
#: A word drawn as a model suggestion. Plain unreviewed words also carry the `draft` class
#: (their speaker is "unknown"), so the title is what tells a suggestion apart.
SUGGESTED = '.span-transcript .word[title^="Model suggestion"]'
#: The light theme's page background (page.css `--bg`); dark must differ from it.
LIGHT_BG = "rgb(244, 244, 242)"
THEMES = pytest.mark.parametrize("scheme", ["light", "dark"])


@pytest.fixture
def svc(tmp_path, monkeypatch):
    home = Home(tmp_path / "home")
    monkeypatch.setenv("TL_HOME", str(home.root))
    assert cli.main(["init"]) == 0
    service = Service(home)
    httpd = build_service_server(service, 0)
    cfg = json.loads(home.config_path.read_text())
    cfg["port"] = httpd.server_address[1]
    home.config_path.write_text(json.dumps(cfg))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield service
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture(scope="session")
def shots(tmp_path_factory):
    out = Path(os.environ.get("TL_SCREENSHOTS") or tmp_path_factory.mktemp("screenshots"))
    out.mkdir(parents=True, exist_ok=True)
    for scheme in ("light", "dark"):
        for old in out.glob(f"[0-9][0-9]-*-{scheme}.png"):
            old.unlink()
    print(f"\nscreenshots: {out}")
    return out


@pytest.fixture
def browser():
    # Imported here, not at the top: the default gate collects this file and must not need Playwright.
    try:
        from playwright import sync_api
    except ImportError:
        pytest.fail("Playwright is missing; run with: uv run --group browser pytest -m browser")
    with sync_api.sync_playwright() as p:
        b = p.chromium.launch()
        try:
            yield b
        finally:
            b.close()


def new_dataset(svc, dataset, items, **extra):
    body = {"project": "demo", "dataset": dataset, "repo": "me/repo", "create_project": True,
            "speakers": SPEAKERS, "items": items, **extra}
    return ServiceClient(svc.home).post("new", body)["url"]


def open_page(browser, scheme):
    """A page, the script errors it raises, and every request that failed.

    `failed` holds (status, path) for each HTTP response of 400 or more, and (0, path) for
    each request that got no response at all (aborted, or the connection reset). The
    browser also logs each of these as a console error ("Failed to load resource"); those
    are kept out of `errors` and checked through `failed` instead, so a refusal the test
    expects (a used login link) is told apart from one it does not.
    """
    ctx = browser.new_context(viewport={"width": WIDTH, "height": HEIGHT}, color_scheme=scheme)
    page = ctx.new_page()
    errors, failed = [], []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda m: m.type == "error" and not m.text.startswith("Failed to load resource")
            and errors.append(m.text))
    page.on("response", lambda r: r.status >= 400 and failed.append((r.status, urlsplit(r.url).path)))
    page.on("requestfailed", lambda r: failed.append((0, urlsplit(r.url).path)))
    return ctx, page, errors, failed


def check_theme(page, scheme):
    bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert (bg == LIGHT_BG) == (scheme == "light"), f"{scheme} page has background {bg}"


def login(page, url):
    page.goto(url)
    page.wait_for_selector(".item[data-k]")


def open_item(page, key):
    page.click(f'.item[data-k="{key}"]')
    page.wait_for_selector(".span-transcript [data-w]")
    expand = page.locator("[data-expand]")
    if expand.count() and "full" in expand.inner_text():
        expand.click()
        page.wait_for_selector(".span-transcript [data-w]")


def drag_words(page, first, last, editor=0):
    """Drag with the mouse from the start of word `first` to the end of word `last`.

    `editor` picks the span editor when the page shows several (one per focus passage).
    """
    area = page.locator(".span-transcript").nth(editor)
    a, b = area.locator(f'[data-w="{first}"]'), area.locator(f'[data-w="{last}"]')
    # A press inside an earlier selection drags that text instead of starting a new
    # selection, so clear it first.
    page.evaluate("getSelection().removeAllRanges()")
    # Centre the words in the transcript box, which scrolls on its own: a drag that ends
    # at the box's edge scrolls the box under the mouse and selects past the last word.
    a.evaluate("el => el.scrollIntoView({block: 'center'})")
    box_a, box_b = a.bounding_box(), b.bounding_box()
    page.mouse.move(box_a["x"] + 1, box_a["y"] + box_a["height"] / 2)
    page.mouse.down()
    page.mouse.move(box_b["x"] + box_b["width"] - 2, box_b["y"] + box_b["height"] / 2, steps=8)
    page.mouse.up()
    page.wait_for_timeout(100)
    got = page.evaluate("n => document.querySelectorAll('.span-selection')[n].textContent", editor)
    assert f"Selected {last - first + 1} words" in got, f"dragging over words {first}-{last} selected {got!r}"


def paint(page, speaker, editor=0):
    page.locator(f'[data-paint="{speaker}"]').nth(editor).click()


def spans_of(svc, dataset, key):
    ds = svc.dataset("demo", dataset)
    return (svc.answers_store("demo", dataset, ds).load()["spans"].get(key) or {}).get("ranges", [])


def wait_for(page, check, what, timeout_s=10.0):
    for _ in range(int(timeout_s / 0.1)):
        if check():
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"timed out waiting for {what}")


@THEMES
def test_the_zoom_flow(svc, browser, shots, scheme):
    others = [{"name": f"extra-{n}", "text": f"A short plain talk number {n}, with a few words.", "group": "call"}
              for n in range(6)]
    url = new_dataset(svc, "call-1", [{"name": "notes", "text": ZOOM.read_text(), "group": "call",
                                       "suggest_map": "Ann Able=ann,Bo Brand=bo"}, *others])
    key = "call/notes"
    item = svc.dataset("demo", "call-1").item(key)
    # "Sure. The budget is fine, but the timeline worries me. What do you think?" (Ann Able's caption)
    seg = next(s for s in item["segments"] if item["tokens"][s["body"][0]] == "Sure.")
    b0, b1 = seg["body"]
    later = next(s for s in item["segments"] if item["tokens"][s["body"][0]] == "Sorry,")
    l0, l1 = later["body"]

    ctx, page, errors, failed = open_page(browser, scheme)
    login(page, url)
    check_theme(page, scheme)
    open_item(page, key)
    assert page.locator("[data-reveal]").count() == 1, "suggestions must start hidden (reveal: after_answer)"
    assert page.locator(SUGGESTED).count() == 0
    page.screenshot(path=shots / f"02-item-{scheme}.png", full_page=True)

    # MARK and SPLIT: the first five words of one caption as Ann, the rest as Bo.
    drag_words(page, b0, b0 + 4)
    paint(page, "ann")
    wait_for(page, lambda: len(spans_of(svc, "call-1", key)) == 1, "the first range to save")
    drag_words(page, b0 + 5, b1 - 1)
    paint(page, "bo")
    wait_for(page, lambda: len(spans_of(svc, "call-1", key)) == 2, "the second range to save")
    ranges = sorted((r["start"], r["end"], r["speaker"]) for r in spans_of(svc, "call-1", key))
    assert ranges == [(b0, b0 + 5, "ann"), (b0 + 5, b1, "bo")]
    page.screenshot(path=shots / f"03-marked-{scheme}.png", full_page=True)
    ds = svc.dataset("demo", "call-1")
    rows = export_dataset(ds, "segments")["data"][key]
    split = next(r for r in rows if r["pieces"] and r["pieces"][0]["text"].startswith("Sure."))
    assert split["split"] is True and [p["speaker"] for p in split["pieces"]] == ["ann", "bo"]

    # REVEAL while a save is in flight: hold the next answer save, click reveal, then let
    # the save through. The reveal must wait for it, so the held mark is blind.
    held, reveals = [], []
    page.route("**/api/answer", lambda route: held.append(route))
    page.on("request", lambda req: req.url.endswith("/api/reveal") and reveals.append(req.url))
    drag_words(page, l0, l1 - 1)
    paint(page, "ann")
    wait_for(page, lambda: held, "the third save to be sent")
    page.click("[data-reveal]")
    page.wait_for_timeout(300)
    assert reveals == [], "the reveal was sent before the save ahead of it finished"
    held[0].continue_()
    page.unroute("**/api/answer")
    page.wait_for_selector(SUGGESTED)
    review = svc.review_store("demo", "call-1", ds).load()
    assert sorted((r["start"], r["end"]) for r in review["reveals"][key]["ranges_before_reveal"]) == \
        [(b0, b0 + 5), (b0 + 5, b1), (l0, l1)]
    page.screenshot(path=shots / f"04-revealed-{scheme}.png", full_page=True)

    # FINISH
    page.click("#finish")
    page.wait_for_selector("#banner:not([hidden])")
    assert "Finished" in page.inner_text("#banner")
    assert svc.review_store("demo", "call-1", ds).load()["finished"]

    # The marks survive a reload.
    page.reload()
    page.wait_for_selector(".item[data-k]")
    open_item(page, key)
    page.wait_for_function(f"document.querySelectorAll('.span-transcript .word.reviewed').length >= {b1 - b0}")
    assert errors == [] and failed == [], (errors, failed)

    # LOGIN: the used link says so in this browser and its Continue link opens the dataset.
    page.goto(url)
    page.wait_for_selector("#message:not([hidden])")
    assert "already used" in page.inner_text("#message")
    page.screenshot(path=shots / f"05-used-link-{scheme}.png", full_page=True)
    page.click("#continue")
    page.wait_for_selector(".item[data-k]")
    assert "/p/demo/call-1/" in page.url
    # The landing page lists the dataset.
    page.goto(url.split("/login")[0] + "/")
    page.wait_for_selector('#projects a[href="/p/demo/call-1/"]')
    check_theme(page, scheme)
    page.screenshot(path=shots / f"06-landing-{scheme}.png", full_page=True)
    assert errors == [] and failed == [(409, "/api/login")], (errors, failed)
    ctx.close()

    # A fresh browser is refused, without a Continue link.
    ctx2, page2, errors2, failed2 = open_page(browser, scheme)
    page2.goto(url)
    page2.wait_for_selector("#message:not([hidden])")
    message = page2.inner_text("#message")
    assert "already used" in message and "still logged in" not in message
    assert page2.locator("#continue").is_hidden()
    page2.screenshot(path=shots / f"07-refused-link-{scheme}.png", full_page=True)
    assert errors2 == [] and failed2 == [(409, "/api/login")], (errors2, failed2)
    ctx2.close()


def test_a_reveal_queued_behind_a_save_refused_as_stale_is_never_sent(svc, browser):
    # The page's save is refused as stale (the answers file changed outside the server)
    # while a reveal waits behind it. The page then stops saving, so the reveal must not
    # be sent either: it would end blindness for an item whose blind marks never saved.
    url = new_dataset(svc, "call-2", [{"name": "notes", "text": ZOOM.read_text(), "group": "call",
                                       "suggest_map": "Ann Able=ann,Bo Brand=bo"}])
    key = "call/notes"
    item = svc.dataset("demo", "call-2").item(key)
    seg = next(s for s in item["segments"] if item["tokens"][s["body"][0]] == "Sure.")
    b0 = seg["body"][0]
    ctx, page, errors, failed = open_page(browser, "light")
    login(page, url)
    open_item(page, key)
    held, reveals = [], []
    page.route("**/api/answer", lambda route: held.append(route))
    page.on("request", lambda req: req.url.endswith("/api/reveal") and reveals.append(req.url))
    drag_words(page, b0, b0 + 4)
    paint(page, "ann")
    wait_for(page, lambda: held, "the save to be sent")
    page.click("[data-reveal]")
    held[0].fulfill(status=409, content_type="application/json",
                    body=json.dumps({"error": "the answers file changed outside the server; reload", "stale": True}))
    page.wait_for_timeout(500)
    assert reveals == [], "a reveal was sent from a page that can no longer save"
    assert key not in svc.review_store("demo", "call-2", svc.dataset("demo", "call-2")).load().get("reveals", {})
    assert errors == [] and failed == [(409, "/p/demo/call-2/api/answer")], (errors, failed)
    ctx.close()


@THEMES
def test_a_verbatim_shaped_item_with_two_passages(svc, browser, shots, scheme):
    words = BRACKET.split()
    p1 = (words.index("We"), words.index("holds.") + 1)
    p2 = (words.index("The"), len(words))
    row = {"title": "Morning show, bridge segment", "person": "Ann Able", "role": "council member",
           "channel": "Town Radio", "video_id": "abc123XYZ", "upload": "20260105", "duration": 31}
    url = new_dataset(svc, "show-1", [{"name": "talk", "text": BRACKET, "format": "bracket-ts", "row": row,
                                       "focus": [{"start": p1[0], "end": p1[1], "title": "The bridge claim",
                                                  "question": "Who says this?"},
                                                 {"start": p2[0], "end": p2[1], "title": "The second span"}]}],
                      config={"media": {"url": "https://media.example/watch?v={video_id}&t={t}"}})
    key = "item/talk"
    ctx, page, errors, failed = open_page(browser, scheme)
    login(page, url)
    check_theme(page, scheme)
    page.click(f'.item[data-k="{key}"]')
    page.wait_for_selector(".span-transcript [data-w]")
    assert "Town Radio" in page.inner_text("#detail") and "uploaded 2026-01-05" in page.inner_text("#detail")
    assert page.locator("#yt").get_attribute("href").startswith("https://media.example/watch?v=abc123XYZ&t=")
    page.screenshot(path=shots / f"08-passage-{scheme}.png", full_page=True)
    ds = svc.dataset("demo", "show-1")

    def result(start):
        reviewed = svc.reviewed(ds, svc.answers_store("demo", "show-1", ds).load())
        return reviewed["attribution"].get(f"item:talk:{start}", {}).get("answer")
    drag_words(page, p1[0], p1[1] - 1)
    paint(page, "bo")
    wait_for(page, lambda: result(p1[0]) == "bo", "the first passage's result to follow the ranges")
    assert result(p2[0]) is None
    page.keyboard.press("]")  # the next passage
    page.wait_for_function("document.querySelector('#detail').innerText.includes('The second span')")
    drag_words(page, p2[0], p2[1] - 1, editor=1)
    paint(page, "ann", editor=1)
    wait_for(page, lambda: result(p2[0]) == "ann", "the second passage's result to follow the ranges")
    page.screenshot(path=shots / f"09-passages-answered-{scheme}.png", full_page=True)
    assert errors == [] and failed == [], (errors, failed)
    ctx.close()
