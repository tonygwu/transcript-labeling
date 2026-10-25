"""The service's static files: empty page shells, the stylesheet and the page script.

`app.js` is assembled from `prelude.js`, `app_main.js` and the two editors, in one
module, so the page's code shares one scope as it did when it was inlined, and the
prelude's session-carrying `fetch` is the only `fetch` that code sees. No file here
holds a dataset's names or text.
"""
from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve().parent
ASSET_TYPES = {"html": "text/html; charset=utf-8", "css": "text/css; charset=utf-8",
               "js": "text/javascript; charset=utf-8"}
SHELLS = ("page.html", "login.html", "landing.html", "page.css", "login.js", "landing.js")


def build_app_js() -> str:
    """The page module: prelude, main script, and the two editors where the main script names them."""
    main = (HERE / "app_main.js").read_text(encoding="utf-8")
    parts = main.split("/*__SPAN_EDITOR__*/")
    if len(parts) != 2 or main.count("/*__TEXT_CORRECTIONS__*/") != 1:
        raise SystemExit("REFUSING: app_main.js must name each editor exactly once")
    before, after = parts
    mid, tail = after.split("/*__TEXT_CORRECTIONS__*/")
    return ((HERE / "prelude.js").read_text(encoding="utf-8") + before
            + (HERE / "span_editor.js").read_text(encoding="utf-8") + mid
            + (HERE / "corrections.js").read_text(encoding="utf-8") + tail)


def asset_bytes(name: str) -> bytes:
    if name == "app.js":
        return build_app_js().encode("utf-8")
    if name not in SHELLS:
        raise KeyError(name)
    return (HERE / name).read_bytes()
