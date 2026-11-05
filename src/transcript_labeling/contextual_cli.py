"""Private files in, source-aligned proposals out. No provider is hardwired.

An adapter is an explicitly selected executable argv, not a shell command. It
receives only {prompt,response_schema,requested_model} on stdin and returns the
transport envelope documented in contextual.resolve_response on stdout.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import re
import signal
import subprocess
from pathlib import Path

from .contextual import build_request, canonical, resolve_response, response_schema, validate_request
from .store import strict_loads
from .textguard import check_text_target
from .tokens import text_hash


TERMINATE_GRACE_SECONDS = 5


def _stop_adapter(proc):
    """Let an adapter clean up its own children before enforcing the deadline."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    # The parent can exit while an owned child ignores SIGTERM. Always finish
    # cleaning the isolated process group, even if wait() already returned.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()

def _read(path):
    return strict_loads(Path(path).read_text(encoding="utf-8"))


def _write(path, value):
    from .cli import write_private
    target = check_text_target(path, False)
    if target is None:
        raise ValueError("contextual artifacts require a private file path, not stdout")
    write_private(target, canonical(value) + "\n")


def prepare(args, home):
    from .formats import parse
    from .items import make_item
    if args.item_file:
        item = _read(args.item_file)
    else:
        text = Path(args.from_file).read_bytes().decode("utf-8")
        parsed = parse(text, args.format)
        item = make_item("source/transcript", text, segments=parsed["segments"], meta=parsed["meta"])
    request = build_request(item, _read(args.speakers_file), unclear=args.unclear,
                            marker_every=args.marker_every, anchor_words=args.anchor_words)
    _write(args.out, request)
    print(canonical({"request": str(Path(args.out).resolve()), "request_sha256": request["request_sha256"],
                     "text_sha256": request["text_sha256"], "speech_words": len(request["speech"]),
                     "token_count": request["token_count"]}))
    return 0


def resolve(args, home):
    bundle = resolve_response(_read(args.request), _read(args.response), requested_model=args.model,
                              run_id=args.run_id)
    _write(args.out, bundle)
    print(canonical({"suggestions": str(Path(args.out).resolve()), "ranges": len(bundle["ranges"]),
                     "model_identity": bundle["provenance"]["model_identity"]}))
    return 0


def run_adapter(request: dict, model: str, argv: list[str], work_dir: Path, *, timeout: float) -> dict:
    """One attempt, raw receipts retained, successful results content-addressed.

    A caller can explicitly retry a failed attempt. No automatic model fallback,
    semantic repair, truncation, or retry occurs here. An unchanged success is
    resolved again from its raw response without spending another request.
    """
    from .cli import write_private
    validate_request(request)
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model must be explicitly selected")
    if not isinstance(argv, list) or not argv or any(not isinstance(s, str) or not s for s in argv):
        raise ValueError("adapter must be a nonempty JSON argv list")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    identity = {"request_sha256": request["request_sha256"], "model": model, "adapter": argv}
    run_id = text_hash(canonical(identity))
    root = Path(work_dir).resolve() / run_id
    check_text_target(root / "request.json", False)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / ".lock").open("a") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        success = root / "success.json"
        if success.exists():
            saved = _read(success)
            if saved["identity"] != identity:
                raise ValueError("cached identity differs from requested model, source or adapter")
            raw_path = root / saved["raw_response"]
            bundle = resolve_response(request, strict_loads(raw_path.read_text(encoding="utf-8")),
                                      requested_model=model, run_id=run_id)
            if bundle != _read(root / "suggestions.json"):
                raise ValueError("cached suggestions differ from retained raw response")
            return {"attempted": 0, "succeeded": 0, "failed": 0, "cached": 1, "errors": {},
                    "suggestions": str(root / "suggestions.json"), "run_id": run_id}
        _write(root / "request.json", request)
        _write(root / "identity.json", identity)
        # A killed writer may leave only stdout/stderr or a partial receipt.
        # Every observed attempt name reserves its number permanently.
        numbers = [int(m[1]) for p in root.glob("attempt-*")
                   if (m := re.fullmatch(r"attempt-(\d+)\.(?:json|stdout|stderr)", p.name))]
        attempt = max(numbers, default=0) + 1
        stem = f"attempt-{attempt:04d}"
        raw_path, err_path = root / f"{stem}.stdout", root / f"{stem}.stderr"
        check_text_target(raw_path, False)
        check_text_target(err_path, False)
        _write(root / f"{stem}.json", {"attempt": attempt, "status": "running",
                                      "stdout": raw_path.name, "stderr": err_path.name})
        payload = {"prompt": request["prompt"], "response_schema": response_schema(request), "requested_model": model}
        error = None
        exit_code = None
        try:
            with raw_path.open("w", encoding="utf-8") as out, err_path.open("w", encoding="utf-8") as err:
                os.fchmod(out.fileno(), 0o600)
                os.fchmod(err.fileno(), 0o600)
                proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=out, stderr=err,
                                        text=True, encoding="utf-8", cwd=root, start_new_session=True)
                try:
                    proc.communicate(canonical(payload), timeout=timeout)
                except subprocess.TimeoutExpired:
                    _stop_adapter(proc)
                    raise TimeoutError(f"adapter exceeded {timeout:g} seconds")
                except BaseException:
                    _stop_adapter(proc)
                    raise
                exit_code = proc.returncode
            if exit_code != 0:
                raise ChildProcessError(f"adapter exited {exit_code}; see retained stderr")
            result = strict_loads(raw_path.read_text(encoding="utf-8"))
            bundle = resolve_response(request, result, requested_model=model, run_id=run_id)
        except (OSError, ValueError, ChildProcessError, TimeoutError, KeyboardInterrupt) as exc:
            error = {"category": type(exc).__name__, "message": str(exc) or type(exc).__name__}
        _write(root / f"{stem}.json", {"attempt": attempt, "status": "failed" if error else "succeeded",
                                      "exit_code": exit_code, "error": error,
                                      "stdout": raw_path.name, "stderr": err_path.name})
        if error:
            return {"attempted": 1, "succeeded": 0, "failed": 1, "cached": 0,
                    "errors": {error["category"]: 1}, "error": error["message"], "run_id": run_id,
                    "artifacts": str(root)}
        _write(root / "suggestions.json", bundle)
        # Completion marker is last. Failed/partial attempts can never be cache hits.
        _write(success, {"identity": identity, "raw_response": raw_path.name})
        return {"attempted": 1, "succeeded": 1, "failed": 0, "cached": 0, "errors": {},
                "suggestions": str(root / "suggestions.json"), "run_id": run_id}


def run(args, home):
    previous = signal.getsignal(signal.SIGTERM)
    def interrupted(signum, frame):
        raise InterruptedError("adapter run cancelled by SIGTERM")
    signal.signal(signal.SIGTERM, interrupted)
    try:
        result = run_adapter(_read(args.request), args.model, _read(args.adapter_file), Path(args.work_dir),
                             timeout=args.timeout)
    finally:
        signal.signal(signal.SIGTERM, previous)
    print(canonical(result))
    return 1 if result["failed"] else 0


def register(sub):
    from .contextual import ANCHOR_WORDS, MARKER_EVERY
    from .formats import FORMATS
    p = sub.add_parser("contextual", help="prepare, run and resolve contextual speaker proposals in private files")
    commands = p.add_subparsers(dest="action", required=True)
    prep = commands.add_parser("prepare", help="build a source-pinned prompt without human labels")
    source = prep.add_mutually_exclusive_group(required=True)
    source.add_argument("--item-file", help="original item JSON, retaining any existing source geometry")
    source.add_argument("--from-file", help="original UTF-8 transcript")
    prep.add_argument("--format", choices=FORMATS)
    prep.add_argument("--speakers-file", required=True, help="private JSON list of {id,description} identities")
    prep.add_argument("--unclear", required=True, help="the dataset's uncertain speaker id")
    prep.add_argument("--marker-every", type=int, default=MARKER_EVERY)
    prep.add_argument("--anchor-words", type=int, default=ANCHOR_WORDS)
    prep.add_argument("--out", required=True)
    prep.set_defaults(fn=prepare)
    resolved = commands.add_parser("resolve", help="validate a retained response and produce an import bundle")
    resolved.add_argument("--request", required=True)
    resolved.add_argument("--response", required=True, help="adapter envelope, including original response text")
    resolved.add_argument("--model", required=True)
    resolved.add_argument("--run-id", required=True)
    resolved.add_argument("--out", required=True)
    resolved.set_defaults(fn=resolve)
    running = commands.add_parser("run", help="one model attempt using an explicit external adapter")
    running.add_argument("--request", required=True)
    running.add_argument("--model", required=True)
    running.add_argument("--adapter-file", required=True, help="JSON argv list; no shell interpolation")
    running.add_argument("--work-dir", required=True, help="private directory outside every Git checkout")
    running.add_argument("--timeout", required=True, type=float, help="explicit per-attempt wall-time limit in seconds")
    running.set_defaults(fn=run)
