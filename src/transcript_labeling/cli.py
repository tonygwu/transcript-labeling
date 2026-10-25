"""The `tl` command: set up and run the service, and the client every agent uses.

  tl init | serve | doctor | mcp
  tl new P/D --from-file PATH [--create-project] [--speakers "id=Label:key,..."] [--suggest-map ...]
             [--focus FILE] [--modes speakers,captions] [--name N] [--group G]
  tl project create NAME --git-checkout ABS_PATH [--writer REPO ...] [--branch B]   (git-private)
  tl project list | tl dataset list
  tl dataset create P/D --config FILE [--path REL]
  tl items add P/D --from-file PATH [--upsert] [--name N] [--group G] [--suggest-map ...] [--focus FILE]
  tl url P/D [--item KEY] | tl status P/D [--json] | tl wait P/D --until done --timeout SECONDS
  tl export P/D KIND [--out FILE] [--no-text] [--fill suggestions]
  tl verify P/D [--expect-answers-sha256 H] | tl schema item|dataset|answers|export

Writes name the calling repository: `--repo owner/name`, or the `origin` of the git
checkout `tl` runs in. Transcripts reach the service only through `--from-file`,
read with the caller's own permissions; `tl` never writes them anywhere else.

Exports of a local-only project go to `$TL_HOME/exports/<project>/<dataset>/` unless
`--out` says otherwise; a git-private project's go to stdout. Text bound for a file
inside a git work tree is refused unless `--no-text` (see `textguard`).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from pathlib import Path

from . import __version__
from .home import Home, git_work_tree_above
from .store import strict_loads

#: How often `tl wait` asks the service whether the owner pressed Finish.
WAIT_POLL_S = 5.0
#: `tl wait`'s exit status when the owner has not pressed Finish in time. argparse
#: already uses 2 for a usage error, and a loop on "still waiting" must not spin on a typo.
WAIT_TIMEOUT_EXIT = 3


def split_ref(ref: str) -> tuple[str, str]:
    if not isinstance(ref, str) or ref.count("/") != 1:
        raise SystemExit(f"REFUSING: {ref!r} must be PROJECT/DATASET")
    project, dataset = ref.split("/")
    return project, dataset


def parse_speakers(spec: str | None) -> list[dict]:
    """`"jane=Jane Roe:1,sam=Sam Poe:2"` as the dataset's speakers list."""
    out = []
    for part in (spec or "").split(","):
        if not part.strip():
            continue
        if "=" not in part or ":" not in part.split("=", 1)[1]:
            raise SystemExit(f"REFUSING: speaker {part!r} must be `id=Label:key`")
        sid, rest = part.split("=", 1)
        label, key = rest.rsplit(":", 1)
        out.append({"id": sid.strip(), "label": label.strip(), "key": key.strip()})
    return out


def item_name(path: Path, name: str | None) -> str:
    """The item name: `--name`, or the file name made safe (lowercase, other characters as `-`)."""
    if name:
        return name
    derived = re.sub(r"[^a-z0-9_-]+", "-", path.stem.lower()).strip("-_")[:63]
    if not derived:
        raise SystemExit(f"REFUSING: cannot make an item name from {path.name!r}; pass --name")
    return derived


def read_json_file(path: str | None):
    if path is None:
        return None
    try:
        return strict_loads(Path(path).read_text(encoding="utf-8"))  # as the service reads it
    except (OSError, ValueError) as exc:
        raise SystemExit(f"REFUSING: {path} is not a readable JSON file ({exc})")


def read_transcript(path: str) -> tuple[Path, str]:
    p = Path(path).expanduser()
    if not p.is_file():
        raise SystemExit(f"REFUSING: {p} is not a file")
    try:
        return p, p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise SystemExit(f"REFUSING: {p} is not UTF-8 text")


def calling_repo(args) -> str:
    from .gitproject import own_repo
    repo = args.repo or own_repo(Path.cwd())
    if not repo:
        raise SystemExit("REFUSING: this command writes, so it must name the calling repository: run it inside "
                         "your git checkout whose origin is on GitHub, or pass --repo owner/name")
    return repo


def write_private(dest: Path, text: str) -> None:
    """Write `text` to `dest` in place (as check_text_target requires), readable by the owner only."""
    if dest.is_dir():
        raise SystemExit(f"REFUSING: {dest} is a folder; --out names a file")
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        os.fchmod(fh.fileno(), 0o600)  # a file that already existed keeps its mode otherwise
        fh.write(text)


def item_spec(args) -> dict:
    path, text = read_transcript(args.from_file)
    spec = {"name": item_name(path, args.name), "text": text, "group": args.group}
    if args.suggest_map:
        spec["suggest_map"] = args.suggest_map
    focus = read_json_file(args.focus)
    if focus is not None:
        spec["focus"] = focus
    return spec


def out(data) -> None:
    print(json.dumps(data, indent=1, ensure_ascii=False))


# ------------------------------------------------------------------ commands

def cmd_init(args, home: Home) -> int:
    made = home.init()
    print("\n".join(f"created {m}" for m in made) or f"{home.root} is already set up")
    return 0


def cmd_serve(args, home: Home) -> int:  # pragma: no cover - run by launchd
    from .service import serve_forever
    serve_forever(home)
    return 0


def cmd_doctor(args, home: Home) -> int:
    notes, problems = doctor_report(home)
    for n in notes:
        print(f"ok   {n}")
    for p in problems:
        print(f"FAIL {p}")
    return 1 if problems else 0


def doctor_report(home: Home) -> tuple[list[str], list[str]]:
    """What is fine and what is wrong with the home, the running service and every project."""
    from .client import healthz
    from .gitcommit import marker
    from .registry import Registry
    problems, notes = [], []
    if not home.root.is_dir():
        return [], [f"{home.root} does not exist; run `tl init`"]
    mode = home.root.stat().st_mode & 0o777
    (problems if mode != 0o700 else notes).append(f"{home.root} mode {oct(mode)}" + ("" if mode == 0o700
                                                                                       else ", must be 0o700"))
    for check in (home.config, home.token):
        try:
            check()
        except SystemExit as exc:  # each names its file and what is wrong with it
            problems.append(str(exc))
    tree = git_work_tree_above(home.root)
    if tree is not None:  # every registry and session lock refuses such a home (Home.require)
        problems.append(f"{home.root} is inside the git work tree {tree}; the service refuses to use it. "
                        "Move the home outside every git checkout")
    if not problems:
        health = healthz(home)
        if health is None:
            problems.append(f"nothing answers on 127.0.0.1:{home.config()['port']}; the launchd service is down")
        elif health.get("version") != __version__:
            problems.append(f"the service runs {health.get('version')}, but {__version__} is installed; "
                            "restart it (launchctl kickstart -k)")
        else:
            from .client import ServiceClient
            try:  # /healthz needs no token; an agent call does, so prove the token works too
                ServiceClient(home).get("projects")
                notes.append(f"service {__version__} answers on port {home.config()['port']} and accepts the token")
            except SystemExit as exc:
                problems.append(f"the service answers /healthz but refuses an agent call: {exc}")
    try:
        projects = Registry(home).load()["projects"]
    except SystemExit as exc:
        problems.append(str(exc))
        projects = {}
    service_up = not problems
    for name, entry in sorted(projects.items()):
        if entry["privacy"] != "git-private":
            notes.append(f"project {name}: local-only, {len(entry['datasets'])} datasets")
            continue
        checkout = Path(entry["git"]["checkout"])
        if not checkout.is_dir():
            problems.append(f"project {name}: checkout {checkout} is missing")
            continue
        opted = marker(checkout)
        if entry["git"].get("commit") and not opted:
            problems.append(f"project {name}: commits are on, but {checkout} lacks the tl.syncLock marker, so "
                            "labels are saved and not committed")
        else:
            notes.append(f"project {name}: git-private, commits {'on' if opted and entry['git'].get('commit') else 'off'}")
        if service_up and opted and entry["git"].get("commit"):
            from .client import ServiceClient
            for dataset in sorted(entry["datasets"]):
                try:
                    c = ServiceClient(home).get("status", project=name, dataset=dataset)["commits"]
                except SystemExit as exc:
                    problems.append(f"{name}/{dataset}: {exc}")
                    continue
                if c.get("last_error"):
                    problems.append(f"{name}/{dataset}: {c['pending_saves']} saves not committed; "
                                    f"last git error: {c['last_error']}")
                elif c.get("pending_saves"):
                    notes.append(f"{name}/{dataset}: {c['pending_saves']} saves waiting for the next commit")
    return notes, problems


def cmd_mcp(args, home: Home) -> int:  # pragma: no cover - started by Codex; tests drive serve_stdio
    from .mcp import serve_stdio
    serve_stdio(home)
    return 0


def cmd_project(args, home: Home) -> int:
    from .registry import Registry
    if args.action == "list":
        out({name: {"privacy": e["privacy"], "writers": e["writers"], "datasets": sorted(e["datasets"])}
             for name, e in sorted(Registry(home).load()["projects"].items())})
        return 0
    from . import gitproject
    if not args.git_checkout:
        raise SystemExit("REFUSING: tl project create makes git-private projects and needs --git-checkout; "
                         "local-only projects come from `tl new ... --create-project`")
    entry = gitproject.create_git_project(Registry(home), args.name, args.git_checkout,
                                          creator_repo=calling_repo(args),
                                          extra_writers=args.writer or [], branch=args.branch,
                                          title=args.title or "", visibility=gitproject.gh_visibility)
    out({"project": args.name, **entry})
    if not entry["git"]["commit"]:
        print("note: the checkout lacks the tl.syncLock marker, so labels will be saved, not committed",
              file=sys.stderr)
    return 0


def cmd_dataset(args, home: Home) -> int:
    from .registry import Registry
    if args.action == "list":
        out([f"{p}/{d}" for p, d in Registry(home).datasets()])
        return 0
    from .client import ServiceClient
    project, dataset = split_ref(args.ref)
    config = read_json_file(args.config)
    out(ServiceClient(home).post("datasets", {"project": project, "dataset": dataset, "config": config,
                                              "path": args.path, "repo": calling_repo(args),
                                              "owner_override": args.owner_override}))
    return 0


def cmd_items(args, home: Home) -> int:
    from .client import ServiceClient
    project, dataset = split_ref(args.ref)
    out(ServiceClient(home).post("items", {"project": project, "dataset": dataset, "repo": calling_repo(args),
                                           "upsert": args.upsert, "items": [item_spec(args)],
                                           "owner_override": args.owner_override}))
    return 0


def cmd_new(args, home: Home) -> int:
    from .client import ServiceClient
    project, dataset = split_ref(args.ref)
    modes = [m.strip() for m in (args.modes or "speakers,captions").split(",") if m.strip()]
    if "speakers" in modes and not args.speakers:
        raise SystemExit("REFUSING: the speakers mode needs --speakers 'id=Label:key,...' (for example "
                         "--speakers 'jane=Jane Roe:1,sam=Sam Poe:2'); pass --modes captions to label captions only")
    body = {"project": project, "dataset": dataset, "repo": calling_repo(args), "create_project": args.create_project,
            "speakers": parse_speakers(args.speakers), "items": [item_spec(args)],
            "owner_override": args.owner_override}
    if args.modes:
        body["modes"] = modes
    if args.title:
        body["title"] = args.title
    res = ServiceClient(home).post("new", body)
    print(f"Send the owner this link (it works once, within 7 days):\n{res['url']}")
    print(json.dumps({k: v for k, v in res.items() if k != "url"}, ensure_ascii=False), file=sys.stderr)
    return 0


def cmd_url(args, home: Home) -> int:
    from .client import ServiceClient
    project, dataset = split_ref(args.ref)
    print(ServiceClient(home).post("url", {"project": project, "dataset": dataset, "item": args.item})["url"])
    return 0


def cmd_status(args, home: Home) -> int:
    from .client import ServiceClient
    project, dataset = split_ref(args.ref)
    st = ServiceClient(home).get("status", project=project, dataset=dataset)
    if args.json:
        out(st)
        return 0
    c = st["commits"]
    commits = c.get("commits")
    if c.get("pending_saves"):
        commits += f", {c['pending_saves']} saves not yet committed"
    if c.get("last_error"):
        commits += f"; last git error: {c['last_error']}"
    print(f"{project}/{dataset}: {st['items']} items, {len(st['items_with_ranges'])} with ranges, "
          f"{len(st['done'])} done, {st['passages_answered']}/{st['passages']} passages, {st['notes']} notes; "
          f"{'FINISHED' if st['finished'] else 'not finished'}; {st['events']} events logged; commits: {commits}")
    return 0


def cmd_wait(args, home: Home) -> int:
    from .client import ServiceClient
    project, dataset = split_ref(args.ref)
    if not (math.isfinite(args.timeout) and args.timeout >= 0):
        raise SystemExit(f"REFUSING: --timeout must be a number of seconds, 0 or more, got {args.timeout}")
    client = ServiceClient(home)
    deadline = time.monotonic() + args.timeout
    while True:
        st = client.get("status", project=project, dataset=dataset)
        if st["finished"]:
            print(f"{project}/{dataset} finished at {st['finished']['at']}")
            return 0
        left = deadline - time.monotonic()
        if left <= 0:
            print(f"{project}/{dataset} is not finished after {args.timeout:g} s; still waiting is fine, "
                  "or check later with tl status", file=sys.stderr)
            return WAIT_TIMEOUT_EXIT
        time.sleep(min(WAIT_POLL_S, left))


def cmd_export(args, home: Home) -> int:
    from .client import ServiceClient
    from .registry import Registry
    from .textguard import check_text_target
    project, dataset = split_ref(args.ref)
    target = args.out
    if target == "":
        raise SystemExit("REFUSING: --out is empty; name a file, or - for stdout")
    if target is None and Registry(home).project(project)["privacy"] == "local-only":
        target = home.exports_dir(project, dataset) / f"{args.kind}{'-notext' if args.no_text else ''}.json"
    dest = check_text_target(target, args.no_text)
    if dest is not None and dest.is_dir():
        raise SystemExit(f"REFUSING: {dest} is a folder; --out names a file")
    data = ServiceClient(home).get("export", project=project, dataset=dataset, kind=args.kind,
                                   no_text="1" if args.no_text else None, fill=args.fill)
    text = json.dumps(data, indent=1, ensure_ascii=False, allow_nan=False) + "\n"
    if dest is None:
        sys.stdout.write(text)
        return 0
    write_private(dest, text)
    print(f"wrote {dest}", file=sys.stderr)
    return 0


def cmd_verify(args, home: Home) -> int:
    from .client import ServiceClient
    project, dataset = split_ref(args.ref)
    report = ServiceClient(home).get("verify", project=project, dataset=dataset,
                                     expect_answers_sha256=args.expect_answers_sha256)
    out(report)
    return 0 if report.get("ok") else 1


def cmd_schema(args, home: Home) -> int:
    from .client import ServiceClient
    out(ServiceClient(home).get("schema", name=args.name))
    return 0


# ------------------------------------------------------------------ parser

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tl", description="Transcript labeling service and client.")
    parser.add_argument("--version", action="version", version=f"tl {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def writer(p):
        p.add_argument("--repo", help="the calling repository, owner/name (default: this checkout's origin)")
        p.add_argument("--owner-override", action="store_true",
                       help="write to a project that does not list the calling repository (owner's request only)")

    def item(p):
        p.add_argument("--from-file", required=True, help="the transcript file, read with your permissions")
        p.add_argument("--name", help="item name (default: the file name made safe)")
        p.add_argument("--group", default="item", help="item group; the key is GROUP/NAME (default: item)")
        p.add_argument("--suggest-map", help="'Label=speaker_id,...': file labels become model suggestions")
        p.add_argument("--focus", help="a JSON file of focus passages")

    sub.add_parser("init", help="create $TL_HOME, its config and token").set_defaults(fn=cmd_init)
    sub.add_parser("serve", help="run the service (launchd does this)").set_defaults(fn=cmd_serve)
    sub.add_parser("doctor", help="check the home, the service and every project").set_defaults(fn=cmd_doctor)
    sub.add_parser("mcp", help="stdio MCP server for Codex (registered in ~/.codex/config.toml)").set_defaults(
        fn=cmd_mcp)

    p = sub.add_parser("new", help="dataset, item and login link in one step (local-only projects)")
    p.add_argument("ref", metavar="PROJECT/DATASET")
    item(p)
    writer(p)
    p.add_argument("--create-project", action="store_true", help="make the project if it does not exist")
    p.add_argument("--speakers", help="'id=Label:key,...'")
    p.add_argument("--modes", help="comma-separated modes (default: speakers,captions)")
    p.add_argument("--title")
    p.set_defaults(fn=cmd_new)

    p = sub.add_parser("project", help="create (git-private) or list projects")
    p.add_argument("action", choices=("create", "list"))
    p.add_argument("name", nargs="?")
    p.add_argument("--git-checkout", help="absolute path to the private checkout's top level")
    p.add_argument("--writer", action="append", help="another repository allowed to write (repeatable)")
    p.add_argument("--branch")
    p.add_argument("--title")
    p.add_argument("--repo", help="the creating repository (default: this checkout's origin)")
    p.set_defaults(fn=cmd_project)

    p = sub.add_parser("dataset", help="create or list datasets")
    p.add_argument("action", choices=("create", "list"))
    p.add_argument("ref", nargs="?", metavar="PROJECT/DATASET")
    p.add_argument("--config", help="the dataset's configuration, a JSON file")
    p.add_argument("--path", help="the dataset's folder inside a git-private checkout")
    writer(p)
    p.set_defaults(fn=cmd_dataset)

    p = sub.add_parser("items", help="add items to a dataset")
    p.add_argument("action", choices=("add",))
    p.add_argument("ref", metavar="PROJECT/DATASET")
    item(p)
    writer(p)
    p.add_argument("--upsert", action="store_true", help="replace an item with the same key")
    p.set_defaults(fn=cmd_items)

    p = sub.add_parser("url", help="a one-time login link for the owner")
    p.add_argument("ref", metavar="PROJECT/DATASET")
    p.add_argument("--item")
    p.set_defaults(fn=cmd_url)

    p = sub.add_parser("status", help="progress of a dataset")
    p.add_argument("ref", metavar="PROJECT/DATASET")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("wait", help="wait until the owner presses Finish")
    p.add_argument("ref", metavar="PROJECT/DATASET")
    p.add_argument("--until", choices=("done",), required=True)
    p.add_argument("--timeout", type=float, required=True,
                   help=f"seconds; exit {WAIT_TIMEOUT_EXIT} if still not finished")
    p.set_defaults(fn=cmd_wait)

    p = sub.add_parser("export", help="export labels")
    p.add_argument("ref", metavar="PROJECT/DATASET")
    p.add_argument("kind", choices=("spans", "segments", "focus", "corrections", "labels", "notes", "all"))
    p.add_argument("--out", help="a file, or - for stdout")
    p.add_argument("--no-text", action="store_true", help="drop every transcript word (safe to commit)")
    p.add_argument("--fill", choices=("suggestions",))
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("verify", help="check a dataset end to end")
    p.add_argument("ref", metavar="PROJECT/DATASET")
    p.add_argument("--expect-answers-sha256")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("schema", help="the shape of an item, dataset, answers file or export")
    p.add_argument("name", choices=("item", "dataset", "answers", "export"))
    p.set_defaults(fn=cmd_schema)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "project" and args.action == "create" and not args.name:
        raise SystemExit("REFUSING: tl project create needs a NAME")
    if args.command == "dataset" and args.action == "create" and (not args.ref or not args.config):
        raise SystemExit("REFUSING: tl dataset create needs PROJECT/DATASET and --config FILE")
    return args.fn(args, Home())


if __name__ == "__main__":
    raise SystemExit(main())
