"""`tl mcp`: a stdio MCP server for agents whose sandbox blocks loopback (Codex).

Codex starts MCP servers outside its sandbox, so this process can reach the service
on 127.0.0.1 when the agent's own shell cannot. It speaks JSON-RPC 2.0, one message
per line, on stdin and stdout; anything else it has to say goes to stderr.

Every call goes through `ServiceClient(client="mcp")`. The service refuses an `mcp`
caller any write to a git-private project, so a Codex agent can create and fill
local-only projects only.

  ABSOLUTE   a stdio server has no per-call working directory, so `file`, `focus_file`
             and `repo` must be absolute paths. `repo` is the caller's own git
             checkout; its `origin` names the writing repository.
  NO TEXT    a tool never returns transcript text. `export` writes the file under
             `$TL_HOME/exports/<project>/<dataset>/` and returns its path and counts.
  WAIT       `wait` returns "still waiting" after at most WAIT_CAP_S seconds, so the
             agent calls it again instead of running into its per-tool timeout.
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

from . import __version__
from .home import Home
from .store import strict_loads

#: The longest one `wait` call blocks. Codex's per-tool timeout must be longer.
WAIT_CAP_S = 50.0
#: Protocol versions this server speaks, newest first. A client asking for another
#: gets the newest, as the MCP lifecycle rules say.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = ("Have the owner label transcripts. Flow: new (prints a login link to send the owner), then wait "
                "or status, then export (writes a file and returns its path), then verify. Transcripts never go "
                "into a repository; read exports from the path returned. Unmarked words are unknown and "
                "suggestions are never labels.")

STR, BOOL, NUM = {"type": "string"}, {"type": "boolean"}, {"type": "number"}
REF = {"project": STR, "dataset": STR}
ITEM = {"file": {"type": "string", "description": "absolute path of the transcript file"},
        "repo": {"type": "string", "description": "absolute path of your own git checkout; its origin is the writer"},
        "name": {"type": "string", "description": "item name (default: the file name made safe)"},
        "group": {"type": "string", "description": "item group; the key is GROUP/NAME (default: item)"},
        "suggest_map": {"type": "string", "description": "'Label=speaker_id,...': file labels become suggestions"},
        "focus_file": {"type": "string", "description": "absolute path of a JSON file of focus passages"}}
SPEAKER = {"type": "object", "properties": {"id": STR, "label": STR, "key": STR}, "required": ["id", "label", "key"]}

TOOLS = {
    "new": ("Make a local-only dataset from one transcript file and return the owner's login link.",
            {**REF, **ITEM, "create_project": BOOL, "speakers": {"type": "array", "items": SPEAKER},
             "modes": {"type": "array", "items": STR}, "title": STR},
            ["project", "dataset", "file", "repo"]),
    "items_add": ("Add one transcript file to an existing local-only dataset.",
                  {**REF, **ITEM, "upsert": BOOL}, ["project", "dataset", "file", "repo"]),
    "url": ("A new one-time login link for the owner (works once, within 7 days).",
            {**REF, "item": STR}, ["project", "dataset"]),
    "status": ("Progress of a dataset: items, ranges, done marks, Finish, commits.", REF, ["project", "dataset"]),
    "wait": (f"Wait until the owner presses Finish, at most {WAIT_CAP_S:g} s per call; call again while it "
             "says still_waiting.", {**REF, "timeout_s": NUM}, ["project", "dataset"]),
    "export": ("Write an export under $TL_HOME/exports and return its path and counts, never its text.",
               {**REF, "kind": {"type": "string", "enum": ["spans", "segments", "focus", "corrections", "labels",
                                                         "notes", "all"]},
                "no_text": BOOL, "fill": {"type": "string", "enum": ["suggestions"]}},
               ["project", "dataset", "kind"]),
    "verify": ("Check a dataset end to end.", {**REF, "expect_answers_sha256": STR}, ["project", "dataset"]),
    "schema": ("The shape of an item, dataset, answers file or export.",
               {"name": {"type": "string", "enum": ["item", "dataset", "answers", "export"]}}, ["name"]),
    "doctor": ("Check the service home, the running service and every project.", {}, []),
}
JSON_TYPES = {"string": str, "boolean": bool, "number": (int, float), "array": list, "object": dict}


class ToolError(Exception):
    """A refusal to report to the agent as the tool's error result."""


def check_arguments(name: str, args) -> dict:
    """`args` checked against the tool's schema: known keys, required keys, JSON types."""
    _, props, required = TOOLS[name]
    if not isinstance(args, dict):
        raise ToolError("arguments must be an object")
    unknown = sorted(set(args) - set(props))
    missing = [k for k in required if k not in args]
    if unknown or missing:
        raise ToolError(f"unknown arguments {unknown}; missing {missing}; this tool takes {sorted(props)}")
    for key, value in args.items():
        kind = JSON_TYPES[props[key]["type"]]
        if not isinstance(value, kind) or (kind is not bool and isinstance(value, bool)):
            raise ToolError(f"{key} must be a JSON {props[key]['type']}")
        if "enum" in props[key] and value not in props[key]["enum"]:
            raise ToolError(f"{key} must be one of {props[key]['enum']}")
    return args


def absolute(args: dict, key: str) -> Path:
    path = Path(args[key])
    if not path.is_absolute():
        raise ToolError(f"{key} must be an absolute path, got {args[key]!r}")
    return path


def calling_repo(args: dict) -> str:
    from .gitproject import own_repo
    checkout = absolute(args, "repo")
    repo = own_repo(checkout) if checkout.is_dir() else None
    if not repo:
        raise ToolError(f"repo {str(checkout)!r} must be your git checkout, with an origin on GitHub")
    return repo


def item_spec(args: dict) -> dict:
    from .cli import item_name, read_json_file, read_transcript
    path, text = read_transcript(str(absolute(args, "file")))
    spec = {"name": item_name(path, args.get("name")), "text": text, "group": args.get("group", "item")}
    if "suggest_map" in args:
        spec["suggest_map"] = args["suggest_map"]
    if "focus_file" in args:
        spec["focus"] = read_json_file(str(absolute(args, "focus_file")))
    return spec


#: What `export` counts in each kind's data, so the agent can check the file without reading text.
COUNTERS = {
    "spans": lambda d: {"items": len(d), "ranges": sum(len(v["ranges"]) for v in d.values())},
    "segments": lambda d: {"items": len(d), "captions": sum(len(rows) for rows in d.values())},
    "focus": lambda d: {"passages": len(d)},
    "corrections": lambda d: {"items": len(d), "edits": sum(len(v.get("edits", [])) for v in d.values())},
    "labels": lambda d: {"labels": len(d["labels"]), "partial": len(d["partial"]), "unlabelled": len(d["unlabelled"])},
    "notes": lambda d: {"notes": len(d)},
}


def counts(export: dict) -> dict:
    """The counts of each exported kind (one kind under `data`, or every kind for `all`)."""
    body = {export["kind"]: export["data"]} if "data" in export else {k: export[k] for k in COUNTERS if k in export}
    return {k: COUNTERS[k](v) for k, v in body.items()}


def call_tool(home: Home, name: str, args: dict) -> dict:
    from .client import ServiceClient
    args = check_arguments(name, args)
    if name == "doctor":
        from .cli import doctor_report
        notes, problems = doctor_report(home)
        return {"ok": not problems, "ok_checks": notes, "problems": problems}
    if name == "schema":
        return ServiceClient(home, client="mcp").get("schema", name=args["name"])
    client = ServiceClient(home, client="mcp")
    project, dataset = args["project"], args["dataset"]
    if name == "new":
        body = {"project": project, "dataset": dataset, "repo": calling_repo(args),
                "create_project": args.get("create_project", False), "speakers": args.get("speakers", []),
                "items": [item_spec(args)], "owner_override": False}
        for key in ("modes", "title"):
            if key in args:
                body[key] = args[key]
        res = client.post("new", body)
        return {"send_the_owner_this_link": res["url"], **{k: v for k, v in res.items() if k != "url"}}
    if name == "items_add":
        return client.post("items", {"project": project, "dataset": dataset, "repo": calling_repo(args),
                                     "upsert": args.get("upsert", False), "items": [item_spec(args)],
                                     "owner_override": False})
    if name == "url":
        return client.post("url", {"project": project, "dataset": dataset, "item": args.get("item")})
    if name == "status":
        return client.get("status", project=project, dataset=dataset)
    if name == "wait":
        from .cli import WAIT_POLL_S
        limit = args.get("timeout_s", WAIT_CAP_S)
        if not 0 <= limit <= WAIT_CAP_S:
            raise ToolError(f"timeout_s must be between 0 and {WAIT_CAP_S:g}")
        start = time.monotonic()
        while True:
            st = client.get("status", project=project, dataset=dataset)
            if st["finished"]:
                return {"finished": st["finished"]}
            left = limit - (time.monotonic() - start)
            if left <= 0:
                return {"still_waiting": True, "waited_s": round(time.monotonic() - start, 1),
                        "next": "call wait again, or check later with status"}
            time.sleep(min(WAIT_POLL_S, left))
    if name == "export":
        from .textguard import check_text_target
        no_text = args.get("no_text", False)
        target = home.exports_dir(project, dataset) / f"{args['kind']}{'-notext' if no_text else ''}.json"
        dest = check_text_target(target, no_text)
        data = client.get("export", project=project, dataset=dataset, kind=args["kind"],
                          no_text="1" if no_text else None, fill=args.get("fill"))
        text = json.dumps(data, indent=1, ensure_ascii=False, allow_nan=False) + "\n"
        from .cli import write_private
        write_private(dest, text)
        return {"path": str(dest), "bytes": len(text.encode()), "kind": args["kind"], "no_text": no_text,
                "counts": counts(data)}
    if name == "verify":
        return client.get("verify", project=project, dataset=dataset,
                          expect_answers_sha256=args.get("expect_answers_sha256"))
    raise AssertionError(name)  # check_arguments knows every tool


def handle(home: Home, msg) -> dict | None:
    """The response to one JSON-RPC message, or None for a notification."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return {"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                "error": {"code": -32600, "message": "not a JSON-RPC 2.0 request"}}
    if "id" not in msg:
        return None  # notifications/initialized, notifications/cancelled: nothing to answer
    mid, method, params = msg["id"], msg["method"], msg.get("params", {})

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def err(code, message):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

    if not isinstance(params, dict):
        return err(-32602, "params must be an object")
    if method == "initialize":
        asked = params.get("protocolVersion")
        return ok({"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "transcript-labeling", "version": __version__},
                   "instructions": INSTRUCTIONS})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [{"name": n, "description": d,
                              "inputSchema": {"type": "object", "properties": p, "required": r,
                                              "additionalProperties": False}}
                             for n, (d, p, r) in TOOLS.items()]})
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str) or name not in TOOLS:
            return err(-32602, f"unknown tool {name!r}; tools: {sorted(TOOLS)}")
        try:
            result, is_error = call_tool(home, name, params.get("arguments") or {}), False
        except ToolError as exc:
            result, is_error = {"error": f"REFUSING: {exc}"}, True
        except SystemExit as exc:  # the service's refusal, or a local check's, already worded for the agent
            result, is_error = {"error": str(exc.code)}, True
        except Exception as exc:  # report it and keep serving; the traceback goes to stderr only
            traceback.print_exc(file=sys.stderr)
            result, is_error = {"error": f"internal error in tl mcp: {type(exc).__name__}; see its stderr"}, True
        return ok({"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                   "isError": is_error})
    return err(-32601, f"no such method: {method}")


def serve_stdio(home: Home, stdin=None, stdout=None) -> None:
    """Answer one JSON-RPC message per line until stdin closes.

    Messages are UTF-8 whatever the locale says, as MCP requires. One request is
    handled at a time, so a `wait` holds up the calls behind it for up to WAIT_CAP_S;
    notifications/cancelled is not acted on.
    """
    stdin = stdin if stdin is not None else sys.stdin.buffer
    stdout = stdout if stdout is not None else sys.stdout.buffer
    for line in stdin:
        if not line.strip():
            continue
        try:
            msg = strict_loads(line.decode("utf-8") if isinstance(line, bytes) else line)
        except (ValueError, RecursionError):  # not UTF-8, not JSON (a key written twice, NaN), too deep
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "not JSON"}}
        else:
            try:
                reply = handle(home, msg)
            except Exception as exc:  # a message no rule above foresaw: answer it and keep serving
                traceback.print_exc(file=sys.stderr)
                reply = {"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                         "error": {"code": -32603, "message": f"internal error in tl mcp: {type(exc).__name__}"}}
        if reply is not None:
            stdout.write((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
            stdout.flush()
