import json
import sys
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from transcript_labeling.cli import main
from transcript_labeling.contextual import build_request
from transcript_labeling.contextual_cli import run_adapter
from transcript_labeling.items import make_item


def setup(tmp_path):
    item = make_item("calls/example", "Hello there how are you today yes fine thanks goodbye")
    req = build_request(item, [{"id": "speaker", "description": "Jane Roe"}], unclear="unclear")
    raw = json.dumps({"turns": [{"marker": 0, "first_words": "Hello there how are you today yes fine",
                                "speaker": "speaker"}]})
    envelope = {"response": raw, "requested_model": "test-model", "served_model": None,
                "model_identity": "unverified"}
    script = tmp_path / "adapter.py"
    script.write_text("import json,sys\np=json.load(sys.stdin)\n"
                      "assert set(p)=={'prompt','response_schema','requested_model'}\n"
                      f"print({json.dumps(envelope)!r})\n")
    return req, [sys.executable, str(script)]


def test_adapter_success_replay_and_tamper_refusal(tmp_path):
    req, argv = setup(tmp_path)
    first = run_adapter(req, "test-model", argv, tmp_path / "runs", timeout=10)
    assert {k: first[k] for k in ("attempted", "succeeded", "failed", "cached")} == {
        "attempted": 1, "succeeded": 1, "failed": 0, "cached": 0}
    assert json.loads(Path(first["suggestions"]).read_text())["ranges"] == [
        {"start": 0, "end": 10, "speaker": "speaker", "source": "contextual_model"}]
    replay = run_adapter(req, "test-model", argv, tmp_path / "runs", timeout=10)
    assert replay["cached"] == 1 and replay["attempted"] == 0
    folder = Path(first["suggestions"]).parent
    assert (folder / "attempt-0001.stdout").read_text().strip().startswith('{"response":')
    Path(first["suggestions"]).write_text("{}")
    with pytest.raises(ValueError, match="cached suggestions"):
        run_adapter(req, "test-model", argv, tmp_path / "runs", timeout=10)


def test_failed_attempts_are_retained_and_not_cached(tmp_path):
    req, argv = setup(tmp_path)
    Path(argv[1]).write_text("import sys\nprint('failure detail',file=sys.stderr)\nsys.exit(7)\n")
    first = run_adapter(req, "test-model", argv, tmp_path / "runs", timeout=10)
    assert first["failed"] == 1 and first["errors"] == {"ChildProcessError": 1}
    assert "failure detail" in (Path(first["artifacts"]) / "attempt-0001.stderr").read_text()
    req, argv = setup(tmp_path)
    second = run_adapter(req, "test-model", argv, tmp_path / "runs", timeout=10)
    assert second["succeeded"] == 1 and second["cached"] == 0
    assert (Path(second["suggestions"]).parent / "attempt-0002.stdout").exists()


def test_interrupted_receipt_never_reuses_or_overwrites_raw_output(tmp_path):
    req, argv = setup(tmp_path)
    Path(argv[1]).write_text("import sys\nprint('first diagnostic')\nsys.exit(1)\n")
    first = run_adapter(req, "test-model", argv, tmp_path / "runs", timeout=10)
    root = Path(first["artifacts"])
    (root / "attempt-0001.json").unlink()  # crash before a completion receipt
    (root / "attempt-0009.stderr").write_text("interrupted late attempt")
    Path(argv[1]).write_text("import sys\nprint('replacement diagnostic')\nsys.exit(1)\n")
    second = run_adapter(req, "test-model", argv, tmp_path / "runs", timeout=10)
    assert second["failed"] == 1
    assert (root / "attempt-0001.stdout").read_text() == "first diagnostic\n"
    assert (root / "attempt-0009.stderr").read_text() == "interrupted late attempt"
    assert (root / "attempt-0010.stdout").read_text() == "replacement diagnostic\n"


def running(pid):
    result = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True)
    return bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")


def test_timeout_stops_child_even_when_parent_exits_first(tmp_path):
    req, argv = setup(tmp_path)
    pidfile = tmp_path / "owned-child.pid"
    child = ("import os,signal,time; from pathlib import Path; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
             f"Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(20)")
    Path(argv[1]).write_text(f"import subprocess,sys,time\nsubprocess.Popen([sys.executable,'-c',{child!r}])\ntime.sleep(20)\n")
    result = run_adapter(req, "test-model", argv, tmp_path / "runs", timeout=1)
    assert result["errors"] == {"TimeoutError": 1}
    pid = int(pidfile.read_text())
    try:
        deadline = time.monotonic() + 2
        while running(pid) and time.monotonic() < deadline:
            time.sleep(.01)
        assert not running(pid)
    finally:
        if running(pid):
            os.kill(pid, signal.SIGKILL)


def test_keyboard_cancellation_stops_owned_adapter_and_records_failure(tmp_path):
    req, argv = setup(tmp_path)
    pidfile = tmp_path / "adapter.pid"
    Path(argv[1]).write_text(f"import os,time\nfrom pathlib import Path\nPath({str(pidfile)!r}).write_text(str(os.getpid()))\ntime.sleep(20)\n")
    driver = tmp_path / "driver.py"
    source = str(Path(__file__).resolve().parents[1] / "src")
    driver.write_text(f"import sys,json\nsys.path.insert(0,{source!r})\nfrom pathlib import Path\nfrom transcript_labeling.contextual_cli import run_adapter\n"
                      f"print(json.dumps(run_adapter({req!r},'test-model',{argv!r},Path({str(tmp_path / 'runs')!r}),timeout=20)))\n")
    process = subprocess.Popen([sys.executable, str(driver)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert pidfile.exists()
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=10)
        result = json.loads(stdout)
        assert result["errors"] == {"KeyboardInterrupt": 1}, stderr
        assert not running(int(pidfile.read_text()))
        receipts = list(Path(result["artifacts"]).glob("attempt-*.json"))
        assert len(receipts) == 1 and json.loads(receipts[0].read_text())["status"] == "failed"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_timeout_and_bad_result_have_distinct_errors(tmp_path):
    req, argv = setup(tmp_path)
    Path(argv[1]).write_text("import time\ntime.sleep(5)\n")
    assert run_adapter(req, "test-model", argv, tmp_path / "runs", timeout=.01)["errors"] == {"TimeoutError": 1}
    Path(argv[1]).write_text("print('not a transport response')\n")
    result = run_adapter(req, "test-model", argv, tmp_path / "runs", timeout=10)
    assert result["failed"] == 1 and result["errors"] == {"JSONDecodeError": 1}


def test_prepare_retains_crlf_and_rejects_git_output(tmp_path, capsys):
    source = tmp_path / "source.txt"
    source.write_bytes(b"Hello there\r\nHow are you?\r\n")
    identities = tmp_path / "speakers.json"
    identities.write_text('[{"id":"me","description":"Jane Roe, candidate"}]')
    target = tmp_path / "request.json"
    args = ["contextual", "prepare", "--from-file", str(source), "--speakers-file", str(identities),
            "--unclear", "unclear", "--out", str(target)]
    assert main(args) == 0
    assert json.loads(target.read_text())["source"]["text"].encode() == source.read_bytes()
    assert "Hello" not in capsys.readouterr().out
    (tmp_path / ".git").mkdir()
    with pytest.raises(SystemExit, match="git work tree"):
        main(args)


def test_run_refuses_private_artifacts_in_git(tmp_path):
    req, argv = setup(tmp_path)
    (tmp_path / ".git").mkdir()
    with pytest.raises(SystemExit, match="git work tree"):
        run_adapter(req, "test-model", argv, tmp_path / "runs", timeout=10)
