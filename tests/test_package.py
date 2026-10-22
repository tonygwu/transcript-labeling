"""The package imports, carries one version, and the `tl` entry point answers."""
from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import transcript_labeling
from transcript_labeling import cli

ROOT = Path(__file__).resolve().parent.parent


def test_version_is_a_plain_string():
    assert isinstance(transcript_labeling.__version__, str)
    assert transcript_labeling.__version__.count(".") == 2


def test_pyproject_reads_the_version_from_the_package():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert meta["tool"]["setuptools"]["dynamic"]["version"] == {"attr": "transcript_labeling.__version__"}
    assert meta["project"]["scripts"]["tl"] == "transcript_labeling.cli:main"


def test_no_runtime_dependencies():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert meta["project"]["dependencies"] == []


def test_cli_version(capsys):
    try:
        cli.main(["--version"])
    except SystemExit as exc:
        assert exc.code == 0
    assert transcript_labeling.__version__ in capsys.readouterr().out


def test_module_runs_as_a_script():
    out = subprocess.run([sys.executable, "-m", "transcript_labeling.cli", "--version"], capture_output=True,
                         text=True, cwd=ROOT, env={"PYTHONPATH": str(ROOT / "src")})
    assert out.returncode == 0 and transcript_labeling.__version__ in out.stdout
