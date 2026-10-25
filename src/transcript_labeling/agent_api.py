"""The agent API under `/api/v1/`, used by `tl` and the MCP server with the bearer token.

  GET  projects                                   registry summary
  GET  status?project=P&dataset=D                 progress, done marks, Finish, event count
  GET  answers?project=P&dataset=D                answers with derived results, and review.json
  POST projects  {name, writers, repo, title}      a local-only project (git-private: CLI only)
  POST datasets  {project, dataset, config, path?, repo}
  POST items     {project, dataset, repo, upsert, items: [item spec]}
  POST new       {project, dataset, repo, create_project, speakers, modes, items, ...}
  POST url       {project, dataset, item?}        a one-time login link for the owner

An item spec is `{name, text, group?, format?, row?, focus?, suggest_map?}`. The
server parses the text (`formats.py`), so every client gets the same items.

Writes name the calling repository, and a project refuses repositories its
`writers` do not list unless `owner_override` is set. That guards against
mistakes; it is not authentication. A request marked `X-TL-Client: mcp` may not
create or write a git-private project, so a Codex agent cannot make the service
rewrite tracked files in a private checkout.
"""
from __future__ import annotations

import shutil
import sys
from urllib.parse import quote

from .dataset3 import Dataset
from .formats import parse, parse_suggest_map, segment_suggestions
from .items import add_items, make_item
from .registry import NAME

ITEM_SPEC = {"name", "text", "group", "format", "row", "focus", "suggest_map", "source"}
#: The type each item spec field must have, when present (None counts as absent).
ITEM_SPEC_TYPES = {"name": str, "text": str, "group": str, "format": str, "row": dict, "focus": list,
                   "suggest_map": (str, dict), "source": dict}


class ApiError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def _q(query: dict, key: str) -> str:
    v = query.get(key, [""])[0]
    if not v:
        raise ApiError(400, f"missing query parameter {key!r}")
    return v


def _need(body: dict, *keys):
    missing = [k for k in keys if k not in body]
    if missing:
        raise ApiError(400, f"missing {missing}")
    return [body[k] for k in keys]


def _writable(svc, project: str, repo, client: str, owner_override: bool) -> dict:
    entry = svc.registry.project(project)
    if client == "mcp" and entry["privacy"] == "git-private":
        raise ApiError(403, f"project {project!r} is git-private; the MCP tools write only local-only projects")
    svc.registry.check_writer(project, repo, owner_override)
    return entry


def item_entry(ds: Dataset, spec: dict) -> dict:
    """An add_items entry from an item spec: the text parsed, suggestions from the suggest map."""
    if not isinstance(spec, dict) or set(spec) - ITEM_SPEC:
        raise ApiError(400, f"an item spec takes only {sorted(ITEM_SPEC)}")
    for field, kind in ITEM_SPEC_TYPES.items():
        if spec.get(field) is not None and not isinstance(spec[field], kind):
            raise ApiError(400, f"item spec field {field!r} has the wrong type ({type(spec[field]).__name__})")
    if not all(isinstance(p, dict) for p in spec.get("focus") or []):
        raise ApiError(400, "item spec field 'focus' must be a list of objects")
    name, text = spec.get("name"), spec.get("text")
    group = spec.get("group") or "item"
    if not isinstance(text, str) or not text.strip():
        raise ApiError(400, f"item {name!r} has no text")
    for part in (name, group):
        if not isinstance(part, str) or not NAME.fullmatch(part):
            raise ApiError(400, f"item name and group must match {NAME.pattern}, got {part!r}")
    parsed = parse(text, spec.get("format"))
    mapping = spec.get("suggest_map") or {}
    if isinstance(mapping, str):
        mapping = parse_suggest_map(mapping)
    suggestions = segment_suggestions(parsed["segments"], mapping, ds.speaker_ids())
    source = {**(spec.get("source") or {}), "format": parsed["format"]}  # the parser's word, never the caller's
    item = make_item(f"{group}/{name}", text, suggestions=suggestions, segments=parsed["segments"],
                     meta=parsed["meta"], source=source)
    row = {"title": name, **(spec.get("row") or {})}
    return {"item": item, "row": row, "focus": spec.get("focus") or []}


def login_link(svc, project: str, dataset: str, item: str | None = None) -> str:
    target = f"/p/{project}/{dataset}/" + (f"#{quote(item, safe='')}" if item else "")
    code = svc.auth.mint_code(target)
    return f"http://127.0.0.1:{svc.port}/login#code={code}"


def status_view(svc, project: str, dataset: str) -> dict:
    ds = svc.dataset(project, dataset)
    answers = svc.answers_store(project, dataset, ds).load()
    review = svc.review_store(project, dataset, ds).load()
    events = svc.events(project, dataset).read()
    items = ds.manifest["items"]
    return {"project": project, "dataset": dataset, "modes": ds.modes, "items": len(items),
            "items_with_ranges": sorted(k for k in answers["spans"] if answers["spans"][k].get("ranges")),
            "items_with_corrections": sorted(answers["corrections"]), "labels": len(answers["labels"]),
            "passages_answered": sum(1 for a in answers["attribution"].values() if a.get("answer")),
            "passages": sum(len(r.get("quotes", [])) for r in ds.catalog()["quote_rows"]),
            "done": sorted(review["done"]), "notes": len(review["notes"]), "revealed": sorted(review["reveals"]),
            "finished": review["finished"], "events": len(events),
            "last_event": events[-1]["time"] if events else None,
            "commits": svc.committer.status(project, dataset) if svc.committer
            else {"commits": "the committer is not running in this server"}}


def agent_get(svc, path: str, query: dict) -> tuple[int, dict]:
    try:
        route = path[len("/api/v1/"):]
        if route == "projects":
            return 200, svc.registry.load()
        if route == "status":
            return 200, status_view(svc, _q(query, "project"), _q(query, "dataset"))
        if route == "answers":
            project, dataset = _q(query, "project"), _q(query, "dataset")
            ds = svc.dataset(project, dataset)
            answers = svc.reviewed(ds, svc.answers_store(project, dataset, ds).load())
            return 200, {**answers, "review": svc.review_store(project, dataset, ds).load()}
        if route in ("export", "verify", "schema"):
            from .export3 import agent_export, agent_schema, agent_verify
            handler = {"export": agent_export, "verify": agent_verify, "schema": agent_schema}[route]
            return 200, handler(svc, query)
        raise ApiError(404, f"no such route: {path}")
    except ApiError as exc:
        return exc.code, {"error": str(exc)}
    except SystemExit as exc:
        return 409, {"error": str(exc)}
    except (ValueError, OSError) as exc:  # stored data that no longer fits, or an unreadable file
        return 409, {"error": f"{type(exc).__name__}: {exc}"}


def agent_post(svc, path: str, body: dict, client: str) -> tuple[int, dict]:
    try:
        route = path[len("/api/v1/"):]
        override = body.get("owner_override") is True
        if route == "projects":
            name, writers, repo = _need(body, "name", "writers", "repo")
            if not isinstance(writers, list) or not all(isinstance(w, str) and w for w in writers):
                raise ApiError(400, "writers must be a list of repository names")
            if body.get("privacy", "local-only") != "local-only":
                raise ApiError(403, "git-private projects are created with the CLI only (tl project create)")
            if not isinstance(body.get("title", ""), str):
                raise ApiError(400, "title must be a string")
            entry = svc.registry.create_project(name, privacy="local-only", writers=[repo, *writers],
                                                title=body.get("title", ""))
            return 200, {"project": name, **entry}
        if route == "datasets":
            project, dataset, config, repo = _need(body, "project", "dataset", "config", "repo")
            if not isinstance(config, dict):
                raise ApiError(400, "config must be an object (the dataset configuration)")
            entry = _writable(svc, project, repo, client, override)
            root = svc.registry.add_dataset(project, dataset, body.get("path"))
            existed = root.exists()
            try:
                Dataset.create(root, project, dataset, config)
            except BaseException:  # whatever failed, leave no half-made dataset behind
                _undo_dataset(svc, project, dataset, root, existed)
                raise
            return 200, {"project": project, "dataset": dataset, "privacy": entry["privacy"], "path": str(root)}
        if route == "items":
            project, dataset, repo, items = _need(body, "project", "dataset", "repo", "items")
            if not isinstance(items, list):
                raise ApiError(400, "items must be a list of item specs")
            _writable(svc, project, repo, client, override)
            ds = svc.dataset(project, dataset)
            entries = [item_entry(ds, spec) for spec in items]
            res = add_items(ds, svc.home.state_dir(project, dataset), entries, upsert=body.get("upsert") is True)
            return 200, res
        if route == "new":
            return 200, agent_new(svc, body, client)
        if route == "url":
            project, dataset = _need(body, "project", "dataset")
            svc.dataset(project, dataset)
            item = body.get("item")
            if item is not None and not isinstance(item, str):
                raise ApiError(400, "item must be an item key")
            return 200, {"url": login_link(svc, project, dataset, item)}
        raise ApiError(404, f"no such route: {path}")
    except ApiError as exc:
        return exc.code, {"error": str(exc)}
    except SystemExit as exc:
        return 409, {"error": str(exc)}


def agent_new(svc, body: dict, client: str) -> dict:
    """Project (if asked), dataset, items and a login link, in one call. Local-only projects only."""
    project, dataset, repo, items = _need(body, "project", "dataset", "repo", "items")
    if not isinstance(items, list):
        raise ApiError(400, "items must be a list of item specs")
    config = body.get("config")
    if not isinstance(body.get("title", ""), str) or not (config is None or isinstance(config, dict)):
        raise ApiError(400, "title must be a string and config an object")
    projects = svc.registry.load()["projects"]
    created = project not in projects
    if created:
        if body.get("create_project") is not True:
            raise ApiError(404, f"no project {project!r}; pass --create-project to make it (a typo would "
                                "otherwise make a new one)")
        svc.registry.create_project(project, privacy="local-only", writers=[repo], title=body.get("title", ""))
    elif projects[project]["privacy"] != "local-only":
        raise ApiError(403, f"tl new writes local-only projects; {project!r} is git-private")
    try:
        _writable(svc, project, repo, client, body.get("owner_override") is True)
        config = {"speakers": body.get("speakers") or [], "modes": body.get("modes") or ["speakers", "captions"],
                  **({"title": body["title"]} if body.get("title") else {}), **(body.get("config") or {})}
        root = svc.registry.add_dataset(project, dataset)
        existed = root.exists()
        try:
            ds = Dataset.create(root, project, dataset, config)
            entries = [item_entry(ds, spec) for spec in items]
            res = add_items(ds, svc.home.state_dir(project, dataset), entries)
        except BaseException:  # whatever failed, leave no half-made dataset behind
            _undo_dataset(svc, project, dataset, root, existed)
            raise
    except BaseException:  # and no project this call made, so a corrected retry is not refused
        if created:
            try:
                svc.registry.remove_project(project)
            except SystemExit as exc:  # it holds a dataset by now (another call's, or ours left by a failed
                # undo), or the home or registry refused the call: it stays, and the call's own error is raised
                print(f"new: project {project!r} was not rolled back: {exc}", file=sys.stderr)  # the server log
        raise
    return {"project": project, "dataset": dataset, **res, "url": login_link(svc, project, dataset)}


def _undo_dataset(svc, project: str, dataset: str, root, existed: bool) -> None:
    """Forget a dataset whose creation failed, and remove its folder only if this call made it."""
    svc.registry.remove_dataset(project, dataset)
    if not existed:
        shutil.rmtree(root, ignore_errors=True)
