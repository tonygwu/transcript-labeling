"""Regenerate the README's picture of the labeling page, from a synthetic call.

    uv run --group browser python ops/readme_screenshot.py

It starts a throwaway service in a temporary home, never the live one, makes one
dataset, marks most captions with the mouse as a person would, splits one caption
between two speakers, leaves the late joiner's caption unmarked, and writes
docs/images/labeling-page.png. Run it after a change that alters how the page looks.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from pathlib import Path

from transcript_labeling import cli, formats
from transcript_labeling.client import ServiceClient
from transcript_labeling.home import Home
from transcript_labeling.service import Service, build_service_server

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images" / "labeling-page.png"
WIDTH, HEIGHT, SCALE = 1280, 1400, 2
# Invented people and an invented meeting; the repository is public.
CALL = """\
00:00:02 --> 00:00:06
Speaker 1: Morning. Did everyone get the bridge budget I sent last night?

00:00:07 --> 00:00:12
Speaker 2: I did. The numbers look fine, but the timeline worries me. Can the steel arrive by March?

00:00:13 --> 00:00:19
Speaker 1: The supplier says late February. I asked them to confirm in writing before Friday.

00:00:20 --> 00:00:23
Unknown: Sorry, I joined late. Are we talking about the north span?

00:00:24 --> 00:00:31
Speaker 2: Yes, the north span. If the steel slips, we lose the April window and the crane rental doubles.

00:00:32 --> 00:00:37
Speaker 1: Then let us book the crane for both months and cancel one later. Sam, can you price that?
"""
SPEAKERS = [{"id": "jane", "label": "Jane Roe", "key": "1"}, {"id": "sam", "label": "Sam Poe", "key": "2"}]
# Who said each caption, in order: None leaves it unmarked, and a pair splits the
# caption after that many words. The fifth caption holds two speakers, as a
# transcriber's caption sometimes does.
MARKS = ["jane", "sam", "jane", None, (("jane", 4), "sam"), "jane"]


def start_service(home: Home):
    os.environ["TL_HOME"] = str(home.root)
    if cli.main(["init"]) != 0:
        raise SystemExit("tl init failed")
    service = Service(home)
    httpd = build_service_server(service, 0)
    cfg = json.loads(home.config_path.read_text())
    cfg["port"] = httpd.server_address[1]
    home.config_path.write_text(json.dumps(cfg))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return service, httpd


def ranges_to_mark() -> list[tuple[int, int, str]]:
    segments = formats.parse(CALL)["segments"]
    if len(segments) != len(MARKS):
        raise SystemExit(f"CALL has {len(segments)} captions but MARKS names {len(MARKS)}")
    out = []
    for seg, mark in zip(segments, MARKS):
        b0, b1 = seg["body"]
        if mark is None:
            continue
        if isinstance(mark, str):
            out.append((b0, b1, mark))
        else:
            (first, n), second = mark
            out += [(b0, b0 + n, first), (b0 + n, b1, second)]
    return out


def drag(page, first: int, last: int) -> None:
    """Drag with the mouse from the start of word `first` to the end of word `last`."""
    page.evaluate("getSelection().removeAllRanges()")
    a = page.locator(f'.span-transcript [data-w="{first}"]')
    b = page.locator(f'.span-transcript [data-w="{last}"]')
    a.evaluate("el => el.scrollIntoView({block: 'center'})")
    box_a, box_b = a.bounding_box(), b.bounding_box()
    page.mouse.move(box_a["x"] + 1, box_a["y"] + box_a["height"] / 2)
    page.mouse.down()
    page.mouse.move(box_b["x"] + box_b["width"] - 2, box_b["y"] + box_b["height"] / 2, steps=8)
    page.mouse.up()
    page.wait_for_timeout(100)


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit("Playwright is missing; run with: uv run --group browser python ops/readme_screenshot.py")
    marks = ranges_to_mark()
    with tempfile.TemporaryDirectory() as tmp:
        home = Home(Path(tmp) / "home")
        service, httpd = start_service(home)
        try:
            url = ServiceClient(home).post("new", {
                "project": "demo", "dataset": "planning-call", "repo": "me/repo", "create_project": True,
                "speakers": SPEAKERS, "title": "Weekly planning call",
                "items": [{"name": "call-1", "text": CALL, "group": "call", "row": {"title": "North span budget"},
                           "suggest_map": "Speaker 1=jane,Speaker 2=sam"}]})["url"]
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT}, device_scale_factor=SCALE,
                                        color_scheme="light")
                errors = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                page.goto(url)
                page.wait_for_selector(".item[data-k]")
                page.click('.item[data-k="call/call-1"]')
                page.wait_for_selector(".span-transcript [data-w]")
                for start, end, speaker in marks:
                    drag(page, start, end - 1)
                    page.locator(f'[data-paint="{speaker}"]').first.click()
                ds = service.dataset("demo", "planning-call")
                store = service.answers_store("demo", "planning-call", ds)
                for _ in range(100):
                    saved = (store.load()["spans"].get("call/call-1") or {}).get("ranges", [])
                    if len(saved) == len(marks):
                        break
                    page.wait_for_timeout(100)
                else:
                    raise SystemExit(f"only {len(saved)} of {len(marks)} ranges saved")
                # The page keeps the last selection on screen after it is labeled; a reload
                # shows the saved marks without it.
                page.reload()
                page.wait_for_selector(".item[data-k]")
                page.click('.item[data-k="call/call-1"]')
                page.wait_for_function(f"document.querySelectorAll('.span-transcript .word.reviewed').length > 0")
                page.mouse.move(0, 0)
                box = page.locator(".span-transcript").bounding_box()
                below = page.locator(".span-transcript + .opts").bounding_box()
                if box["y"] + box["height"] > HEIGHT:
                    raise SystemExit(f"the transcript ends below the {HEIGHT}px window; raise HEIGHT")
                if errors:
                    raise SystemExit(f"the page raised: {errors}")
                OUT.parent.mkdir(parents=True, exist_ok=True)
                # End the picture in the gap between the transcript and the buttons under it.
                page.screenshot(path=OUT, clip={"x": 0, "y": 0, "width": WIDTH,
                                                "height": (box["y"] + box["height"] + below["y"]) / 2})
                browser.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
