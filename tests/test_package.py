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


def _decodes_in_the_locale(node) -> bool:
    """Whether a call reads or writes text in the locale's encoding: read_text() or
    write_text(), builtin open() or os.fdopen() in a text mode, or a subprocess call
    with text=True, universal_newlines=True or errors= (each turns on text mode), each
    with no encoding keyword (or encoding=None). Not checked: Path.open(), io.open(),
    os.popen(), TextIOWrapper, tempfile in a text mode, subprocess.getoutput(), a
    function imported from subprocess by name, and text= given as a variable."""
    import ast
    if not isinstance(node, ast.Call):
        return False
    kw = {k.arg: k.value for k in node.keywords}
    if "encoding" in kw:
        return isinstance(kw["encoding"], ast.Constant) and kw["encoding"].value is None
    f = node.func
    if isinstance(f, ast.Attribute) and f.attr in ("read_text", "write_text"):
        return True
    opener = (isinstance(f, ast.Name) and f.id == "open") or (
        isinstance(f, ast.Attribute) and f.attr == "fdopen" and isinstance(f.value, ast.Name) and f.value.id == "os")
    if opener:
        mode = kw.get("mode", node.args[1] if len(node.args) > 1 else None)
        return not (isinstance(mode, ast.Constant) and "b" in str(mode.value))
    text = kw.get("text", kw.get("universal_newlines"))
    return (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "subprocess"
            and ("errors" in kw or (isinstance(text, ast.Constant) and text.value is True)))


def test_every_text_read_and_write_names_its_encoding():
    # Files are written as UTF-8; text I/O without an encoding uses the locale's, which
    # garbles non-ASCII text under a Latin-1 locale without a word, and raises under an
    # ASCII one.
    import ast
    bare = [f"{p.relative_to(ROOT)}:{node.lineno}" for p in sorted((ROOT / "src").rglob("*.py"))
            for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))) if _decodes_in_the_locale(node)]
    assert bare == []


def test_the_encoding_check_sees_each_shape():
    import ast
    flagged = ["p.read_text()", "p.write_text(s)", "open(p)", "open(p, 'a')", "os.fdopen(fd, 'w')",
               "subprocess.run(a, text=True)", "subprocess.run(a, universal_newlines=True)",
               "subprocess.run(a, errors='replace')", "open(p, encoding=None)"]
    passed = ["p.read_text(encoding='utf-8')", "open(p, 'ab')", "open(p, mode='rb')", "os.open(p, 0)",
              "os.fdopen(fd, 'w', encoding='utf-8')", "subprocess.run(a, text=True, encoding='utf-8')",
              "subprocess.run(a)", "Dataset.open(root)"]
    assert [c for c in flagged if not _decodes_in_the_locale(ast.parse(c).body[0].value)] == []
    assert [c for c in passed if _decodes_in_the_locale(ast.parse(c).body[0].value)] == []
