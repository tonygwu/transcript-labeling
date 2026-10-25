"""ops/install-launchd.sh, in --print mode only: the plist it would install, and its refusals.

The --install path writes to ~/Library/LaunchAgents and calls launchctl, so the tests
never run it; they check the plist --print shows with plutil, as --install does.
"""
from __future__ import annotations

import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "ops" / "install-launchd.sh"


@pytest.fixture
def env(tmp_path):
    assert shutil.which("plutil"), "plutil is missing; these tests and the script need macOS"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tl = bin_dir / "tl"
    tl.write_text("#!/bin/sh\nexit 0\n")
    tl.chmod(0o755)
    # Never reach the real launchd: if a broken check ever let --install run, it fails here.
    launchctl = bin_dir / "launchctl"
    launchctl.write_text("#!/bin/sh\necho 'launchctl stub: the tests must not reach launchd' >&2\nexit 99\n")
    launchctl.chmod(0o755)
    home = tmp_path / "home & co"  # an ampersand must come out escaped in the XML
    home.mkdir()
    (home / "config.json").write_text("{}")
    return {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path / "user"), "TL_HOME": str(home),
            "tl": str(tl), "home": home, "tmp": tmp_path}


def run(env, *args, **overrides):
    e = {k: v for k, v in {**env, **overrides}.items() if k in ("PATH", "HOME", "TL_HOME") and v is not None}
    return subprocess.run(["/bin/sh", str(SCRIPT), *args], capture_output=True, text=True, env=e)


def test_print_shows_a_valid_plist_that_runs_tl_serve_and_keeps_it_alive(env):
    r = run(env, "--print")
    assert r.returncode == 0, r.stderr
    out = env["tmp"] / "out.plist"
    out.write_text(r.stdout)
    assert subprocess.run(["plutil", "-lint", str(out)], capture_output=True).returncode == 0
    p = plistlib.loads(r.stdout.encode())
    assert p["Label"] == "local.transcript-labeling.server"
    assert p["ProgramArguments"] == [env["tl"], "serve"]
    assert p["KeepAlive"] is True and p["RunAtLoad"] is True
    assert p["EnvironmentVariables"] == {"TL_HOME": env["TL_HOME"], "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    assert p["StandardErrorPath"] == p["StandardOutPath"] == f"{env['TL_HOME']}/logs/launchd.log"
    assert "launchctl bootstrap" in r.stderr
    assert not (Path(env["HOME"]) / "Library").exists()  # --print changes nothing


def test_without_tl_home_the_default_home_is_used_and_not_passed(env):
    default = Path(env["HOME"]) / ".local" / "share" / "transcript-labeling"
    default.mkdir(parents=True)
    (default / "config.json").write_text("{}")
    r = run(env, "--print", TL_HOME=None)
    assert r.returncode == 0, r.stderr
    p = plistlib.loads(r.stdout.encode())
    assert "TL_HOME" not in p["EnvironmentVariables"]
    assert p["StandardOutPath"] == f"{default}/logs/launchd.log"


@pytest.mark.parametrize("change, code, expect", [
    ({"PATH": "/usr/bin:/bin"}, 1, "tl is not on PATH"),
    ({"TL_HOME": "relative/home"}, 1, "must be an absolute path"),
    ({"TL_HOME": "/nonexistent/tl-home"}, 1, "run `tl init` first"),
])
def test_refusals(env, change, code, expect):
    r = run(env, "--print", **change)
    assert r.returncode == code and expect in r.stderr and r.stdout == ""


def test_it_needs_exactly_one_mode(env):
    for args in ([], ["--install", "--print"], ["--go"]):
        r = run(env, *args)
        assert r.returncode == 2 and "usage:" in r.stderr



@pytest.mark.parametrize("where", [".venv/bin", "checkout/bin"])
def test_a_tl_from_a_virtual_environment_or_a_checkout_is_refused(env, where):
    import subprocess as sp
    folder = env["tmp"] / "proj" / where
    folder.mkdir(parents=True)
    if where.startswith("checkout"):
        sp.run(["git", "init", "-q", str(env["tmp"] / "proj" / "checkout")], check=True)
    (folder / "tl").write_text("#!/bin/sh\nexit 0\n")
    (folder / "tl").chmod(0o755)
    r = run(env, "--print", PATH=f"{folder}:{env['PATH']}")
    assert r.returncode == 1 and r.stdout == ""
    assert ("virtual environment" if ".venv" in where else "inside a git checkout") in r.stderr


def test_a_tl_that_is_a_shell_function_is_refused(env):
    r = subprocess.run(["/bin/bash", "-c", 'tl() { :; }; export -f tl; exec /bin/sh "$0" --print', str(SCRIPT)],
                       capture_output=True, text=True,
                       env={"PATH": f"{env['tmp'] / 'bin'}:/usr/bin:/bin", "HOME": env["HOME"],
                            "TL_HOME": env["TL_HOME"]})  # the launchctl stub stays on PATH
    assert r.returncode == 1 and "not a file" in r.stderr and r.stdout == ""
