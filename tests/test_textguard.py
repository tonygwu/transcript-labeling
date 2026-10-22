"""Transcript text refused for files inside a git work tree, through --out or a redirected stdout."""
from __future__ import annotations

import errno
import os
import subprocess
import sys
from pathlib import Path

import pytest

from transcript_labeling.textguard import check_text_target, destination, stdout_path, work_tree_of

PROBE = ("import sys; from transcript_labeling.textguard import check_text_target; "
         "check_text_target(None, '--no-text' in sys.argv); print('TEXT')")


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "deep" / "er").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    return r


def test_work_tree_of(repo, tmp_path):
    assert work_tree_of(repo / "deep" / "er" / "out.json") == repo.resolve()
    assert work_tree_of(repo / "not" / "made" / "yet.json") == repo.resolve()
    assert work_tree_of(repo) == repo.resolve()
    assert work_tree_of(repo / ".git" / "x") == repo.resolve()
    (tmp_path / "plain").mkdir()
    assert work_tree_of(tmp_path / "plain" / "out.json") is None


def test_a_worktree_or_submodule_counts(repo, tmp_path):
    wt = tmp_path / "linked"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: /somewhere/else\n")  # how worktrees and submodules look
    assert work_tree_of(wt / "out.json") == wt.resolve()


def test_out_inside_a_work_tree_is_refused_unless_no_text(repo, tmp_path):
    with pytest.raises(SystemExit, match="--out .* is inside the git work tree"):
        check_text_target(repo / "deep" / "out.json", no_text=False)
    check_text_target(repo / "deep" / "out.json", no_text=True)
    (tmp_path / "plain").mkdir()
    check_text_target(tmp_path / "plain" / "out.json", no_text=False)


def test_a_symlink_out_of_a_repo_into_one_is_refused(repo, tmp_path):
    (tmp_path / "plain").mkdir()
    (tmp_path / "plain" / "link").symlink_to(repo / "deep")
    with pytest.raises(SystemExit, match="inside the git work tree"):
        check_text_target(tmp_path / "plain" / "link" / "out.json", no_text=False)


def test_a_gitignored_path_is_still_refused(repo):
    (repo / ".gitignore").write_text("exports/\n")
    with pytest.raises(SystemExit):
        check_text_target(repo / "exports" / "out.json", no_text=False)


def run_probe(stdout, *args, cwd):
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    return subprocess.run([sys.executable, "-c", PROBE, *args], stdout=stdout, stderr=subprocess.PIPE,
                          text=True, cwd=cwd, env=env)


def test_stdout_redirected_into_a_work_tree_is_refused(repo, tmp_path):
    target = repo / "deep" / "dump.json"
    with open(target, "w") as fh:
        r = run_probe(fh, cwd=tmp_path)
    assert r.returncode == 1 and "REFUSING: stdout" in r.stderr and str(repo.resolve()) in r.stderr
    assert target.read_text() == ""
    with open(target, "w") as fh:
        assert run_probe(fh, "--no-text", cwd=tmp_path).returncode == 0
    assert target.read_text() == "TEXT\n"


def test_stdout_to_a_file_outside_or_to_a_pipe_is_allowed(tmp_path, repo):
    with open(tmp_path / "dump.json", "w") as fh:
        assert run_probe(fh, cwd=repo).returncode == 0
    assert (tmp_path / "dump.json").read_text() == "TEXT\n"
    r = run_probe(subprocess.PIPE, cwd=repo)  # a pipe gets past the check, as documented
    assert r.returncode == 0 and r.stdout == "TEXT\n"


def test_stdout_path_names_the_file(tmp_path):
    with open(tmp_path / "f.txt", "w") as fh:
        assert stdout_path(fh.fileno()) == (tmp_path / "f.txt").resolve()
    rd, wr = os.pipe()
    try:
        assert stdout_path(wr) is None
    finally:
        os.close(rd)
        os.close(wr)


def test_a_file_whose_path_cannot_be_found_is_refused(monkeypatch, tmp_path):
    import transcript_labeling.textguard as tg
    monkeypatch.delattr(tg.fcntl, "F_GETPATH")
    with open(tmp_path / "f.txt", "w") as fh:
        with pytest.raises(SystemExit, match="cannot be checked"):
            check_text_target(None, no_text=False, stdout_fd=fh.fileno())


def test_out_dash_means_stdout(repo, tmp_path):
    with open(repo / "deep" / "dump.json", "w") as fh:
        with pytest.raises(SystemExit, match="REFUSING: stdout"):
            check_text_target("-", no_text=False, stdout_fd=fh.fileno())
    with open(tmp_path / "dump.json", "w") as fh:
        check_text_target("-", no_text=False, stdout_fd=fh.fileno())


def test_an_out_path_that_cannot_be_checked_is_refused(tmp_path):
    locked = tmp_path / "locked"
    (locked / "inner").mkdir(parents=True)
    locked.chmod(0)
    try:
        with pytest.raises(SystemExit, match="cannot be checked"):
            check_text_target(locked / "inner" / "out.json", no_text=False)
    finally:
        locked.chmod(0o755)
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    with pytest.raises(SystemExit, match="cannot be checked"):
        check_text_target(loop / "out.json", no_text=False)


def test_a_path_too_long_for_f_getpath_says_so(monkeypatch, tmp_path):
    import transcript_labeling.textguard as tg

    def too_small(fd, cmd, arg):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(tg.fcntl, "fcntl", too_small)
    with open(tmp_path / "f.txt", "w") as fh:
        with pytest.raises(SystemExit, match="does not fit in 1024 bytes"):
            check_text_target(None, no_text=False, stdout_fd=fh.fileno())


def test_the_guard_returns_where_the_writer_must_write(repo, tmp_path):
    with open(tmp_path / "dump.json", "w") as fh:
        assert check_text_target("-", no_text=False, stdout_fd=fh.fileno()) is None
        assert check_text_target(None, no_text=False, stdout_fd=fh.fileno()) is None
    assert check_text_target(tmp_path / "x.json", no_text=False) == tmp_path / "x.json"
    assert check_text_target(repo / "x.json", no_text=True) == repo / "x.json"
    assert check_text_target("-", no_text=True) is None
    # `./-` as a string names a file; as a Path it prints as `-`, and pathlib cannot tell them apart.
    assert destination("./-") == Path("./-") and destination(Path("./-")) is None


def test_a_symlink_to_a_file_not_yet_made_inside_a_repo_is_refused(repo, tmp_path):
    (tmp_path / "plain").mkdir()
    (tmp_path / "plain" / "out.json").symlink_to(repo / "deep" / "leak.json")  # dangling until written
    with pytest.raises(SystemExit, match="inside the git work tree"):
        check_text_target(tmp_path / "plain" / "out.json", no_text=False)


def test_dot_dot_after_a_missing_folder_is_applied_like_the_os(repo, tmp_path):
    (tmp_path / "plain").mkdir()
    sneaky = tmp_path / "plain" / "missing" / ".." / ".." / "repo" / "x.json"
    with pytest.raises(SystemExit, match="inside the git work tree"):
        check_text_target(sneaky, no_text=False)
