"""The labeling service: one always-on server for every registered dataset.

Run by launchd as `tl serve`. It reads the registry, manifests and catalogs on each
request, so a new dataset or item appears without a restart.

Every page is an empty shell. `GET /`, `GET /login` and `GET /p/<project>/<dataset>/`
return only markup and scripts, with no project name, catalog entry or transcript
text, because a plain page load cannot carry the session header. The page then
fetches its data with that header.

  browser   POST /api/login {code} -> a session id the page keeps in localStorage
            GET  /api/whoami, /api/projects
            /p/<project>/<dataset>/api/{page, answers, transcript}  (GET)
            /p/<project>/<dataset>/api/{answer, note, reveal, done}  (POST, browser only)
  agents    /api/v1/...  with `Authorization: Bearer <token>` (see `agent_api`)
  anyone    GET /healthz -> {"ok": true, "version": ...}

Every request must carry an allowed Host (421 otherwise). Every POST must be
`application/json` and, from a browser, come from this server's own origin.
Answer, note, reveal and done writes are accepted only with a browser session,
never with the bearer token, so an agent cannot reveal suggestions and break a
blind first pass. Each such write is logged to the dataset's event log.

`api/answers` returns the versions of the answers file and review.json as this page
loaded them, and every write sends them back as `versions`. A write after an
outside change the page has not seen (a git pull, a revert, a server restart) is a
409 with `"stale": true`, and the page stops saving and asks the owner to reload
(see `store._GuardedJSON`).

`tl serve` also starts the committer (`gitcommit.Committer`): every browser write
is noted, and a background thread commits due git-private datasets every
COMMIT_TICK_S. That thread records every error and keeps going, because an
exception in a thread is otherwise lost without a trace.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .auth import Auth, AuthError, token_ok
from .checklist import keep_assisted
from .corrections import update_corrections
from .dataset3 import Dataset
from .gitcommit import Committer, head_holds
from .home import Home
from .page.assets import ASSET_TYPES, asset_bytes
from .registry import NAME, Registry
from .security import SECURITY_HEADERS, host_ok, json_type_ok, origin_ok
from .spans import reconcile_quote_reviews, validate_annotation
from .store import SECTIONS, DatasetStore, EventLog, ReviewStore, StaleWrite, strict_loads
from .tokens import item_kinds

MAX_BODY = 256 * 1024
MAX_AGENT_BODY = 32 * 1024 * 1024
# How much of a refused body is read and thrown away before the refusal is sent.
# Answering with unread bytes in the socket makes the kernel send a reset, and the
# client then sees ECONNRESET instead of the refusal.
DRAIN_LIMIT = 16 * MAX_BODY
SESSION_HEADER = "X-TL-Session"
NOTE_MAX = 4000
COMMIT_TICK_S = 30.0


class Refused(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def now_stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Service:
    """Everything a request handler needs, built once per server."""

    def __init__(self, home: Home, on_save=None):
        self.home = home
        self.registry = Registry(home)
        self.auth = Auth(home)
        self.config = home.config()
        self.token = home.token()
        self.port = self.config["port"]
        self.allowed_hosts = self.config["allowed_hosts"]
        self.operator = self.config["operator_id"]
        # Called after every browser write as on_save(project, dataset, finished).
        self.on_save = on_save or (lambda project, dataset, finished: None)
        # One store per file for the life of the server: a store remembers the file as
        # it last read or wrote it, which is how an outside change is noticed.
        self._stores: dict = {}
        self._stores_lock = threading.Lock()
        self.committer: Committer | None = None  # set by start_committer

    def _store(self, cls, path: Path, lock: Path):
        with self._stores_lock:
            key = (cls, os.path.realpath(path))  # one object per file, however its path is spelled
            if key not in self._stores:
                self._stores[key] = cls(path, lock)
            return self._stores[key]

    # ------------------------------------------------------------ datasets
    def dataset(self, project: str, dataset: str) -> Dataset:
        if not isinstance(project, str) or not isinstance(dataset, str) or not NAME.fullmatch(project) \
                or not NAME.fullmatch(dataset):
            raise Refused(404, "no such dataset")
        try:
            root = self.registry.dataset_dir(project, dataset)
        except SystemExit:
            raise Refused(404, f"no dataset {project}/{dataset}")
        return Dataset.open(root)

    def answers_store(self, project: str, dataset: str, ds: Dataset) -> DatasetStore:
        return self._store(DatasetStore, ds.answers, self.home.state_dir(project, dataset) / ".tl.lock")

    def review_store(self, project: str, dataset: str, ds: Dataset) -> ReviewStore:
        return self._store(ReviewStore, ds.review_path, self.home.state_dir(project, dataset) / ".tl.lock")

    def label_files(self, project: str, dataset: str) -> list[Path]:
        """The files the committer commits for a dataset: the answers file and review.json."""
        ds = self.dataset(project, dataset)
        return [ds.answers, ds.review_path]

    def events(self, project: str, dataset: str) -> EventLog:
        return EventLog(self.home.state_dir(project, dataset) / "events.jsonl")

    def reviewed(self, ds: Dataset, answers: dict) -> dict:
        return reconcile_quote_reviews(answers, ds.root / "unused.html", ds.catalog()["quote_rows"],
                                       item_path=ds.item_path, review=ds.review)


def ids_of(data: dict) -> dict[str, set[str]]:
    """The ids the page may answer, per section. A dataset with no items has none."""
    keys = {r["key"] for r in data["rows"]}
    qids = {c["qid"] for r in data["quote_rows"] for c in r.get("quotes", [])}
    items = set(data["transcript_index"])
    return {"labels": keys & items if items else keys, "attribution": qids, "spans": items, "corrections": items}


def make_handler(svc: Service):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = f"transcript-labeling/{__version__}"

        def log_message(self, fmt, *args):
            # Paths only: never a query string (it can hold an item key), a body or a header.
            # A malformed request line leaves no command or path, and must still be logged.
            command, path = getattr(self, "command", None) or "-", getattr(self, "path", None) or "-"
            status = str(args[1]) if len(args) > 1 else ""
            if command == "POST" or status.startswith(("4", "5")):
                sys.stderr.write("%s %s %s %s\n" % (self.log_date_time_string(), command,
                                                    urlsplit(path).path, status))

        def send_error(self, code, message=None, explain=None):
            # http.server's own refusals (a malformed request line, an unsupported method)
            # get the same JSON body and security headers as every other response.
            self.close_connection = True
            self._json(code, {"error": message or self.responses.get(code, ("error",))[0]})

        # ---------------------------------------------------- responses
        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            if code >= 400:
                # A refused request's body may be unread; never parse it as the next request.
                self.close_connection = True
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if code >= 400:
                self.send_header("Connection", "close")
            for k, v in {**SECURITY_HEADERS, **(extra or {})}.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _drain_body(self) -> None:
            """Drop a refused request's body, up to DRAIN_LIMIT; the connection then closes."""
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0  # unreadable: read nothing; the connection closes all the same
            self._drain(length)

        def _drain(self, length: int) -> None:
            left = min(max(length, 0), DRAIN_LIMIT)
            while left > 0:
                chunk = self.rfile.read(min(left, 64 * 1024))
                if not chunk:
                    break
                left -= len(chunk)
            self.close_connection = True

        # ---------------------------------------------------- checks
        def _host_checked(self) -> bool:
            if host_ok(self.headers.get("Host"), svc.port, svc.allowed_hosts):
                return True
            self._json(421, {"error": "this server does not answer to that Host"})
            return False

        def _session(self) -> bool:
            return svc.auth.session_ok(self.headers.get(SESSION_HEADER))

        def _bearer(self) -> bool:
            return token_ok(self.headers.get("Authorization"), svc.token)

        def _body(self, cap: int) -> dict:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise Refused(400, "unreadable Content-Length")
            if length <= 0 or length > cap:
                self._drain(length)
                raise Refused(400, f"body must be 1..{cap} bytes, got {length}")
            raw = self.rfile.read(length)
            try:  # strict, as every stored file is read: no repeated key, NaN or Infinity
                msg = strict_loads(raw)
            except (ValueError, UnicodeDecodeError) as exc:
                raise Refused(400, f"body is not JSON: {exc}")
            if not isinstance(msg, dict):
                raise Refused(400, "body is not an object")
            return msg

        def _post_checks(self) -> None:
            if not json_type_ok(self.headers.get("Content-Type")):
                self._drain_body()
                raise Refused(415, "POST bodies must be application/json")
            if not origin_ok(self.headers.get("Origin"), self.headers.get("Host"), svc.allowed_hosts):
                self._drain_body()
                raise Refused(403, "cross-origin requests are refused")

        # ---------------------------------------------------- routing
        def _dataset_route(self, path: str):
            """(project, dataset, rest) for /p/<project>/<dataset><rest>, else None."""
            parts = path.split("/", 4)
            if len(parts) >= 4 and parts[1] == "p" and parts[2] and parts[3]:
                return parts[2], parts[3], "/" + (parts[4] if len(parts) == 5 else "")
            return None

        def do_GET(self):
            if not self._host_checked():
                return
            if self.headers.get("Transfer-Encoding") or (self.headers.get("Content-Length") or "0").strip() != "0":
                # Its body would otherwise be read as the next request on this connection.
                return self._json(400, {"error": "a GET request carries no body"})
            path = urlsplit(self.path).path
            try:
                return self._get(path)
            except Refused as exc:
                return self._json(exc.code, {"error": str(exc)})
            except StaleWrite as exc:
                return self._json(409, {"error": str(exc), "stale": True})
            except SystemExit as exc:
                return self._json(409, {"error": str(exc)})
            except Exception as exc:  # never drop a request without an answer
                return self._internal(exc)

        def _internal(self, exc: Exception):
            # The type only: an exception's text can quote a body or a transcript.
            sys.stderr.write(f"{now_stamp()} internal error on {urlsplit(self.path).path}: {type(exc).__name__}\n")
            return self._json(500, {"error": f"internal error ({type(exc).__name__}); see the server log"})

        def do_POST(self):
            if not self._host_checked():
                return
            path = urlsplit(self.path).path
            try:
                self._post_checks()
                return self._post(path)
            except Refused as exc:
                return self._json(exc.code, {"error": str(exc)})
            except StaleWrite as exc:
                return self._json(409, {"error": str(exc), "stale": True})
            except SystemExit as exc:
                return self._json(409, {"error": str(exc)})
            except Exception as exc:
                return self._internal(exc)

        def _shell(self, name: str):
            return self._send(200, asset_bytes(name), ASSET_TYPES[name.rsplit(".", 1)[1]])

        def _get(self, path: str):
            if path == "/healthz":
                return self._json(200, {"ok": True, "version": __version__})
            if path in ("/", "/index.html"):
                return self._shell("landing.html")
            if path == "/login":
                return self._shell("login.html")
            if path.startswith("/static/"):
                name = path[len("/static/"):]
                if name not in ("page.css", "app.js", "login.js", "landing.js"):
                    raise Refused(404, f"no such path: {path}")
                return self._shell(name)
            if path.startswith("/api/v1/"):
                if not self._bearer():
                    raise Refused(401, "this route needs the bearer token")
                from .agent_api import agent_get
                return self._json(*agent_get(svc, path, parse_qs(urlsplit(self.path).query)))
            if path == "/api/whoami":
                if not self._session():
                    raise Refused(401, "no valid session; open a new login link")
                return self._json(200, {"ok": True, "operator": svc.operator})
            if path == "/api/projects":
                if not self._session():
                    raise Refused(401, "no valid session; open a new login link")
                return self._json(200, projects_view(svc))
            route = self._dataset_route(path)
            if route is None:
                raise Refused(404, f"no such path: {path}")
            project, dataset, rest = route
            if rest == "/" and not path.endswith("/"):
                # The page's api paths are relative, so its address must end in a slash.
                return self._send(301, b"", "text/plain", {"Location": path + "/"})
            if rest in ("/", "/index.html"):
                return self._shell("page.html")
            if not self._session():
                raise Refused(401, "no valid session; open a new login link")
            ds = svc.dataset(project, dataset)
            if rest == "/api/page":
                return self._json(200, ds.page_data())
            if rest == "/api/answers":
                answers, v_answers = svc.answers_store(project, dataset, ds).load_versioned()
                review, v_review = svc.review_store(project, dataset, ds).load_versioned()
                return self._json(200, {**svc.reviewed(ds, answers), "review": review,
                                        "versions": {"answers": v_answers, "review": v_review}})
            if rest == "/api/transcript":
                key = parse_qs(urlsplit(self.path).query).get("key", [""])[0]
                if key not in ds.manifest["items"]:
                    raise Refused(404, "item is not in this dataset")
                try:
                    item = ds.item(key)
                except (OSError, ValueError):
                    raise Refused(409, "item unavailable; add it again")
                review = svc.review_store(project, dataset, ds).load()
                return self._json(200, transcript_view(ds, item, review))
            raise Refused(404, f"no such path: {path}")

        def _post(self, path: str):
            if path == "/api/login":
                msg = self._body(MAX_BODY)
                code = msg.get("code")
                try:
                    session, target = svc.auth.redeem(code if isinstance(code, str) else "")
                except AuthError as exc:
                    return self._json(409, {"error": str(exc), "target": svc.auth.code_target(code or "")})
                return self._json(200, {"session": session, "target": target})
            if path.startswith("/api/v1/"):
                if not self._bearer():
                    self._drain_body()
                    raise Refused(401, "this route needs the bearer token")
                from .agent_api import agent_post
                return self._json(*agent_post(svc, path, self._body(MAX_AGENT_BODY),
                                              self.headers.get("X-TL-Client") or "cli"))
            route = self._dataset_route(path)
            if route is None:
                raise Refused(404, f"no such path: {path}")
            project, dataset, rest = route
            if rest not in ("/api/answer", "/api/note", "/api/reveal", "/api/done"):
                raise Refused(404, f"no such path: {path}")
            if not self._session():
                # Browser-only writes: the bearer token is not enough, by design.
                self._drain_body()
                raise Refused(401, "answers, notes, reveals and done marks need a browser session")
            msg = self._body(MAX_BODY)
            ds = svc.dataset(project, dataset)
            handler = {"/api/answer": post_answer, "/api/note": post_note, "/api/reveal": post_reveal,
                       "/api/done": post_done}[rest]
            code, body, kind, ident, finished, sha = handler(svc, project, dataset, ds, msg)
            # The write has landed. A failure to log it or to note it for the committer must
            # not tell the page that its save failed; it is reported, and the page told.
            for what, step in (("event log", lambda: svc.events(project, dataset).append(
                                    kind, ident, svc.operator, "browser", sha)),
                               ("committer", lambda: svc.on_save(project, dataset, finished))):
                try:
                    step()
                except Exception as exc:
                    sys.stderr.write(f"{now_stamp()} saved, but the {what} failed: {type(exc).__name__}\n")
                    body = {**body, "warning": f"saved, but the {what} failed ({type(exc).__name__})"}
            return self._json(code, body)

    return Handler


# ------------------------------------------------------------------ views

def projects_view(svc: Service) -> dict:
    out = []
    for name, entry in sorted(svc.registry.load()["projects"].items()):
        datasets = []
        for d in sorted(entry["datasets"]):
            try:
                ds = svc.dataset(name, d)
                answers = svc.answers_store(name, d, ds).load()
                review = svc.review_store(name, d, ds).load()
            except (Refused, SystemExit) as exc:
                datasets.append({"name": d, "error": str(exc)})
                continue
            datasets.append({"name": d, "title": ds.manifest["title"] or d, "modes": ds.modes,
                             "items": len(ds.manifest["items"]), "with_ranges": len(answers["spans"]),
                             "done": len(review["done"]), "finished": review["finished"] is not None})
        out.append({"name": name, "title": entry["title"], "privacy": entry["privacy"], "datasets": datasets})
    return {"projects": out}


def transcript_view(ds: Dataset, item: dict, review: dict) -> dict:
    """An item as the page receives it: token kinds added, suggestions only once allowed.

    Under the blind-first policy (`reveal: after_answer`) an item's suggestions stay
    on the server until the owner reveals them for that item.
    """
    revealed = ds.manifest["reveal"] == "always" or item["key"] in review["reveals"]
    view = {k: v for k, v in item.items() if k != "text"}
    view["token_kinds"] = item_kinds(item)
    view["suggestions_hidden"] = 0 if revealed else len(item.get("suggestions", []))
    if not revealed:
        view["suggestions"] = []
    return view


# ------------------------------------------------------------------ browser writes

def page_version(msg: dict, which: str) -> str:
    """The version of `which` ("answers" or "review") the page loaded, from a write's body."""
    versions = msg.get("versions")
    v = versions.get(which) if isinstance(versions, dict) else None
    if not isinstance(v, str) or not v:
        raise Refused(400, f"a write must send versions.{which}, as api/answers gave it; reload the page")
    return v


def post_answer(svc: Service, project: str, dataset: str, ds: Dataset, msg: dict):
    version = page_version(msg, "answers")
    kind, ident, value = msg.get("kind"), msg.get("id"), msg.get("value")
    if not isinstance(ident, str):
        raise Refused(400, "id must be a string")
    if kind not in SECTIONS:
        raise Refused(400, f"kind must be one of {SECTIONS}, got {kind!r}")
    if kind == "attribution":
        raise Refused(409, "Quote results follow your word markings and are not saved directly. "
                           "Reload the page to continue.")
    if kind not in ds.sections():
        raise Refused(400, f"this dataset's modes ({', '.join(ds.modes)}) do not write {kind}")
    if ident not in ids_of(ds.page_data())[kind]:
        raise Refused(400, f"{ident!r} is not a {kind} id in this dataset")
    if not isinstance(value, dict):
        raise Refused(400, "value is not an object")
    # The server, not the page, says who checked.
    value = {**value, "checked_by": svc.operator}
    transcript = None
    if kind != "labels":
        try:
            transcript = ds.item(ident)
        except (OSError, ValueError) as exc:
            raise Refused(400, f"item unavailable: {exc}")
    if kind == "spans":
        try:
            validate_annotation(value, transcript, ds.speaker_ids())
        except ValueError as exc:
            raise Refused(400, str(exc))
    stored = {}

    def change(answers: dict) -> dict:
        new = value
        if kind == "corrections":
            new = update_corrections(value, answers["corrections"].get(ident), transcript)
        if kind == "labels":
            new = keep_assisted(value, answers["labels"].get(ident))
        stored["value"] = new
        return svc.reviewed(ds, {**answers, kind: {**answers[kind], ident: new}})

    try:
        answers, sha, _ = svc.answers_store(project, dataset, ds).update_hashed(change, version)
    except ValueError as exc:
        raise Refused(409, str(exc))
    body = {"ok": True, "saved": {s: len(answers[s]) for s in SECTIONS}, "attribution": answers["attribution"],
            **({"correction": stored["value"]} if kind == "corrections" else {})}
    return 200, body, "answer", ident, False, sha


def _note_target(ds: Dataset, ident) -> None:
    data = ds.page_data()
    ids = ids_of(data)
    if ident not in ids["spans"] | ids["labels"] | ids["attribution"]:
        raise Refused(400, f"{ident!r} is neither an item nor a passage in this dataset")


def post_note(svc, project, dataset, ds, msg):
    version = page_version(msg, "review")
    ident, text = msg.get("id"), msg.get("text")
    if not isinstance(ident, str):
        raise Refused(400, "id must be a string")
    _note_target(ds, ident)
    if not isinstance(text, str) or len(text) > NOTE_MAX:
        raise Refused(400, f"text must be a string of at most {NOTE_MAX} characters")

    def change(review):
        if text.strip():
            review["notes"][ident] = {"text": text, "updated_at": now_stamp(), "checked_by": svc.operator}
        else:
            review["notes"].pop(ident, None)
        return review
    _, sha, _ = svc.review_store(project, dataset, ds).update_hashed(change, version)
    return 200, {"ok": True}, "note", ident, False, sha


def post_reveal(svc, project, dataset, ds, msg):
    """Show an item's model suggestions, once, keeping the ranges marked blind before it."""
    version = page_version(msg, "review")
    key = msg.get("key")
    if not isinstance(key, str) or key not in ds.manifest["items"]:
        raise Refused(400, f"{key!r} is not an item in this dataset")
    answers = svc.answers_store(project, dataset, ds).load()
    before = (answers["spans"].get(key) or {}).get("ranges", [])
    if ds.manifest["reveal"] == "after_answer" and not before:
        # Blind first: suggestions show only after the owner's own marks on this item.
        raise Refused(409, "mark this item's speakers first; its suggestions show after your own answer")

    def change(review):
        # Once set, neither field can be removed or replaced, as with draft_shown.
        if key not in review["reveals"]:
            review["reveals"][key] = {"revealed_at": now_stamp(), "ranges_before_reveal": before,
                                      "checked_by": svc.operator}
        return review
    review, sha, _ = svc.review_store(project, dataset, ds).update_hashed(change, version)
    item = ds.item(key)
    return 200, {"ok": True, "reveal": review["reveals"][key],
                 "suggestions": item.get("suggestions", [])}, "reveal", key, False, sha


def post_done(svc, project, dataset, ds, msg):
    """Mark an item done (or not), or Finish the whole dataset with `{"finish": true}`."""
    version = page_version(msg, "review")
    if msg.get("finish") is True:
        def change(review):
            review["finished"] = {"at": now_stamp(), "checked_by": svc.operator}
            return review
        _, sha, _ = svc.review_store(project, dataset, ds).update_hashed(change, version)
        return 200, {"ok": True, "finished": True}, "finish", f"{project}/{dataset}", True, sha
    key, done = msg.get("key"), msg.get("done")
    if not isinstance(key, str) or key not in ds.manifest["items"] or not isinstance(done, bool):
        raise Refused(400, "send {key: <item>, done: true|false} or {finish: true}")

    def change(review):
        if done:
            review["done"][key] = {"at": now_stamp(), "checked_by": svc.operator}
        else:
            review["done"].pop(key, None)
        return review
    _, sha, _ = svc.review_store(project, dataset, ds).update_hashed(change, version)
    return 200, {"ok": True}, "done", key, False, sha


def build_service_server(svc: Service, port: int | None = None) -> ThreadingHTTPServer:
    """The service on 127.0.0.1. `port` 0 picks a free port (tests); None uses the config's."""
    httpd = ThreadingHTTPServer(("127.0.0.1", svc.port if port is None else port), make_handler(svc))
    if port == 0:
        # A test server on a free port answers to that port's loopback names.
        svc.port = httpd.server_address[1]
    httpd.svc = svc
    return httpd


def start_committer(svc: Service, interval_s: float = COMMIT_TICK_S) -> Committer:
    """Note every browser write for the committer, and tick it from a daemon thread.

    A restart forgets saves the last server noted but had not committed, so every
    git-private dataset whose label files differ from HEAD is noted again first.
    """
    committer = Committer(svc.registry)
    svc.committer = committer
    recover_pending(svc, committer)
    before = svc.on_save

    def on_save(project, dataset, finished):
        before(project, dataset, finished)
        committer.note_save(project, dataset, finished)
    svc.on_save = on_save

    def loop():
        while True:
            time.sleep(interval_s)
            commit_tick(svc, committer)
    threading.Thread(target=loop, name="tl-committer", daemon=True).start()
    return committer


def recover_pending(svc: Service, committer: Committer) -> list[tuple[str, str]]:
    """Note for commit every committing git-private dataset whose label files differ from HEAD."""
    noted = []
    for project, dataset in svc.registry.datasets():
        entry = svc.registry.project(project)
        if entry["privacy"] != "git-private" or not entry["git"].get("commit"):
            continue
        try:
            checkout = Path(entry["git"]["checkout"]).resolve()
            paths = [str((f.parent.resolve() / f.name).relative_to(checkout))
                     for f in svc.label_files(project, dataset) if f.exists()]
            if paths and not head_holds(checkout, paths):
                committer.note_save(project, dataset)
                noted.append((project, dataset))
        except (Exception, SystemExit) as exc:  # one dataset's problem never stops the others or the server
            print(f"{now_stamp()} could not check {project}/{dataset} for uncommitted labels: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
    return noted


def commit_tick(svc: Service, committer: Committer) -> list[dict]:
    """One committer tick; every result and any error goes to the log, never raised."""
    try:
        results = committer.tick(svc.label_files)
    except BaseException as exc:  # a dead thread would stop every commit without a trace
        print(f"{now_stamp()} commit tick failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return [{"error": f"{type(exc).__name__}: {exc}"}]
    for r in results:
        print(f"{now_stamp()} commit {json.dumps(r, sort_keys=True)}", file=sys.stderr)
    return results


#: The server's own log is rotated at this size, keeping LOG_BACKUPS older files.
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 3


class RotatingLog:
    """A stand-in for sys.stderr that appends to `path` and rotates it at `max_bytes`.

    The older files are `<name>.1` (newest) to `<name>.<backups>`. Every request line,
    commit result and traceback the server writes goes through here, so the log stays
    under (backups + 1) * max_bytes, apart from one write larger than max_bytes.
    """

    def __init__(self, path: Path, max_bytes: int = LOG_MAX_BYTES, backups: int = LOG_BACKUPS):
        self.path, self.max_bytes, self.backups = Path(path), max_bytes, backups
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def write(self, text: str) -> int:
        """Append `text`. Never raises: a log that cannot be written must not drop a request.

        A missing folder is made again and the write tried once more. If the log still
        cannot be written, one line saying why goes to the process's original stderr
        (launchd.log under launchd), and the text is lost.
        """
        data = text.encode("utf-8", "replace")
        with self._lock:
            for attempt in (1, 2):
                try:
                    self._append(data)
                    return len(text)
                except OSError as exc:
                    if attempt == 1:
                        try:  # logs/ only: a home deleted under the server is not made again
                            self.path.parent.mkdir(exist_ok=True, mode=0o700)
                        except OSError:
                            pass  # the second attempt fails and reports why
                        continue
                    fallback = sys.__stderr__
                    if fallback is not None:
                        try:
                            fallback.write(f"{now_stamp()} cannot write {self.path} ({type(exc).__name__}: "
                                           f"{exc}); one log write of {len(data)} bytes was lost\n")
                            fallback.flush()
                        except (OSError, ValueError):
                            pass  # nowhere left to say it
        return len(text)

    def _append(self, data: bytes) -> None:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            size = 0
        if size and size + len(data) > self.max_bytes:
            self._rotate()
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)

    def flush(self) -> None:
        pass  # every write is already in the file

    def _rotate(self) -> None:
        older = lambda i: self.path.with_name(f"{self.path.name}.{i}")  # noqa: E731
        for i in range(self.backups - 1, 0, -1):
            if older(i).exists():
                os.replace(older(i), older(i + 1))
        os.replace(self.path, older(1))


def install_log(home: Home) -> RotatingLog:
    """Point sys.stderr at the rotated server log, and move aside a large launchd log.

    launchd sends the process's own stdout and stderr to `logs/launchd.log`. It holds
    what is written before this runs (a crash at import, say), and afterwards one line
    for each write the rotated log could not take. launchd opens that file anew at each
    start, so renaming it here bounds it across restarts, not during one run.
    """
    launchd_log = home.logs_dir / "launchd.log"
    if launchd_log.exists() and launchd_log.stat().st_size > LOG_MAX_BYTES:
        os.replace(launchd_log, launchd_log.with_name("launchd.log.1"))
    log = RotatingLog(home.logs_dir / "server.log")
    print(f"transcript-labeling {__version__}: logging to {log.path}", file=sys.stderr)
    sys.stderr = log
    return log


def serve_forever(home: Home) -> None:  # pragma: no cover - run by launchd
    install_log(home)
    svc = Service(home)
    start_committer(svc)
    httpd = build_service_server(svc)
    print(f"transcript-labeling {__version__} on http://127.0.0.1:{svc.port}/", file=sys.stderr)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
