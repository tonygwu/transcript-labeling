"""The service's label commits, in scratch git repositories.

  OPT-IN     without `tl.syncLock` in the checkout's own config nothing is committed
  CHECKOUT   the top level of its work tree, however its path is spelled
  COMMIT     an untracked answers file is added and committed with only its own paths
  LOCKS      a held index.lock is IndexLocked; a held tl-sync.flock makes the commit wait,
             while the answers file can still be saved
  REFUSE     a staged foreign file, a merge, rebase, cherry-pick or revert in progress,
             a conflict without MERGE_HEAD, a detached HEAD, no label file at all
  TIMEOUT    a hung hook is stopped, and the sync lock and index.lock are freed; a hook
             gets the grace period to clean up; one that ignores SIGTERM is killed
  DYING      if the service dies mid-commit, git's holder keeps the sync lock until git
             exits, and nothing git starts (a hook's background job) inherits it
  ALREADY    labels another agent already committed count as success
  UNSTAGE    a failed commit unstages only its own paths
  TIMING     300 s idle, 1800 s maximum age, at once on Finish; backoff on index.lock;
             one dataset's error never stops another's commit; one tick at a time
"""
from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from transcript_labeling import gitcommit
from transcript_labeling.gitcommit import (MARKER, CommitRefused, Committer, IndexLocked,
                                           commit_message, commit_once, marker)
from transcript_labeling.home import Home
from transcript_labeling.registry import Registry
from transcript_labeling.store import write_atomic


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=check).stdout


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "data"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.invalid")
    git(r, "config", "user.name", "t")
    (r / "README").write_text("data\n")
    git(r, "add", "README")
    git(r, "commit", "-qm", "init")
    git(r, "config", MARKER, "1")
    (r / "labels" / "d").mkdir(parents=True)
    return r


def answers(repo, text='{"labels": {}}'):
    p = repo / "labels" / "d" / "human_answers.json"
    p.write_text(text)
    return p


def last_commit(repo):
    return git(repo, "log", "-1", "--format=%s"), git(repo, "show", "--name-only", "--format=", "HEAD").split()


def test_without_the_opt_in_nothing_is_committed(repo):
    git(repo, "config", "--unset", MARKER)
    a = answers(repo)
    with pytest.raises(CommitRefused, match="has not opted in"):
        commit_once(repo, "main", [a], "m")
    assert last_commit(repo)[0].strip() == "init"


def test_a_global_or_system_marker_does_not_count(repo, tmp_path, monkeypatch):
    git(repo, "config", "--unset", MARKER)
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[tl]\n\tsyncLock = 1\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(cfg))
    assert git(repo, "config", "--get", MARKER).strip() == "1"  # git itself sees it
    assert marker(repo) is None
    with pytest.raises(CommitRefused, match="has not opted in"):
        commit_once(repo, "main", [answers(repo)], "m")


def test_git_variables_pointing_elsewhere_are_ignored(repo, tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    git(other, "config", MARKER, "1")
    git(repo, "config", "--unset", MARKER)
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))  # as a hook or `submodule foreach` exports it
    with pytest.raises(CommitRefused, match="has not opted in"):
        commit_once(repo, "main", [answers(repo)], "m")
    git(repo, "--git-dir", str(repo / ".git"), "config", MARKER, "1")
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "stray-index"))
    hooks = tmp_path / "failing-hooks"  # `git -c core.hooksPath=...` from a parent git
    hooks.mkdir()
    (hooks / "pre-commit").write_text("#!/bin/sh\nexit 1\n")
    (hooks / "pre-commit").chmod(0o755)
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", f"'core.hooksPath'='{hooks}'")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(hooks))
    assert commit_once(repo, "main", [answers(repo)], "m")["committed"]
    assert not (tmp_path / "stray-index").exists()
    assert git(repo, "--git-dir", str(repo / ".git"), "log", "-1", "--format=%s").strip() == "m"


def test_the_opt_in_is_checked_again_once_the_lock_is_held(repo, monkeypatch):
    a = answers(repo)
    lock = open(repo / ".git" / "tl-sync.flock", "a")
    fcntl.flock(lock, fcntl.LOCK_EX)
    errors, waiting = [], threading.Event()

    class SignallingFcntl:  # tells the test when commit_once, past its first check, asks for the lock
        LOCK_EX, LOCK_NB, LOCK_UN = fcntl.LOCK_EX, fcntl.LOCK_NB, fcntl.LOCK_UN

        @staticmethod
        def flock(fh, op):
            if op == fcntl.LOCK_EX:
                waiting.set()
            return fcntl.flock(fh, op)
    monkeypatch.setattr(gitcommit, "fcntl", SignallingFcntl)

    def go():
        try:
            commit_once(repo, "main", [a], "m")
        except CommitRefused as exc:
            errors.append(str(exc))
    t = threading.Thread(target=go)
    t.start()
    assert waiting.wait(10)
    git(repo, "config", "--unset", MARKER)  # the sync tool backs out its opt-in
    fcntl.flock(lock, fcntl.LOCK_UN)
    lock.close()
    t.join(10)
    assert errors and "has not opted in" in errors[0]
    assert last_commit(repo)[0].strip() == "init"


def test_a_checkout_below_the_top_level_is_refused(repo):
    a = answers(repo)
    with pytest.raises(CommitRefused, match="not the top level"):
        commit_once(repo / "labels", "main", [a], "m")


def test_the_checkout_and_files_may_be_spelled_through_symlinks(repo, tmp_path):
    link = tmp_path / "spelled-differently"
    link.symlink_to(repo)
    a = answers(repo)
    assert commit_once(link, "main", [a], "m")["committed"]
    a.write_text('{"labels": {"y": {}}}')
    assert commit_once(repo, "main", [link / "labels" / "d" / "human_answers.json"], "m2")["committed"]


def test_odd_names_inside_and_missing_files_outside(repo, tmp_path):
    odd = repo / "..odd.json"
    odd.write_text("{}")
    assert commit_once(repo, "main", [odd], "m")["committed"]
    with pytest.raises(CommitRefused, match="are outside"):
        commit_once(repo, "main", [tmp_path / "missing.json"], "m")


def test_an_untracked_answers_file_is_added_and_committed_alone(repo):
    a = answers(repo)
    (repo / "other.txt").write_text("someone else's work\n")
    review = repo / "labels" / "d" / "review.json"  # absent: not listed
    res = commit_once(repo, "main", [a, review], commit_message("vi", "d", 3))
    assert res["committed"] and len(res["sha"]) == 40
    subject, files = last_commit(repo)
    assert subject.strip().startswith("Save human labels: vi/d (3 saves, tl ")
    assert files == ["labels/d/human_answers.json"]
    assert "other.txt" in git(repo, "status", "--porcelain")
    review.write_text("{}")
    a.write_text('{"labels": {"x": {}}}')
    commit_once(repo, "main", [a, review], "m2")
    assert sorted(last_commit(repo)[1]) == ["labels/d/human_answers.json", "labels/d/review.json"]


def test_a_held_index_lock_is_reported(repo):
    a = answers(repo)  # untracked: `git add` meets the lock
    (repo / ".git" / "index.lock").write_text("")
    with pytest.raises(IndexLocked):
        commit_once(repo, "main", [a], "m")
    (repo / ".git" / "index.lock").unlink()
    commit_once(repo, "main", [a], "m")
    a.write_text('{"labels": {"z": {}}}')  # tracked: `git commit` meets the lock
    (repo / ".git" / "index.lock").write_text("")
    with pytest.raises(IndexLocked):
        commit_once(repo, "main", [a], "m2")
    assert last_commit(repo)[0].strip() == "m"


def test_a_held_sync_lock_makes_the_commit_wait_while_saves_continue(repo):
    a = answers(repo)
    lock = open(repo / ".git" / "tl-sync.flock", "a")
    fcntl.flock(lock, fcntl.LOCK_EX)
    with pytest.raises(CommitRefused, match="sync lock is held"):
        commit_once(repo, "main", [a], "m", wait_s=0.2)
    done = []
    t = threading.Thread(target=lambda: done.append(commit_once(repo, "main", [a], "m")))
    t.start()
    time.sleep(0.3)
    write_atomic(a, '{"labels": {"saved": {}}}')  # a save while the commit waits
    assert done == []
    fcntl.flock(lock, fcntl.LOCK_UN)
    lock.close()
    t.join(10)
    assert done and done[0]["committed"]
    assert git(repo, "show", "HEAD:labels/d/human_answers.json") == '{"labels": {"saved": {}}}'


def test_refusals(repo):
    a = answers(repo)
    (repo / "foreign.txt").write_text("x")
    git(repo, "add", "foreign.txt")
    with pytest.raises(CommitRefused, match="other changes are staged"):
        commit_once(repo, "main", [a], "m")
    assert git(repo, "diff", "--cached", "--name-only").split() == ["foreign.txt"]
    git(repo, "reset", "-q", "--", "foreign.txt")
    (repo / ".git" / "MERGE_HEAD").write_text(git(repo, "rev-parse", "HEAD"))
    with pytest.raises(CommitRefused, match="a merge is in progress"):
        commit_once(repo, "main", [a], "m")
    (repo / ".git" / "MERGE_HEAD").unlink()
    git(repo, "checkout", "-q", "--detach")
    with pytest.raises(CommitRefused, match="not main"):
        commit_once(repo, "main", [a], "m")


@pytest.mark.parametrize("name, what", [("rebase-merge", "a rebase"), ("rebase-apply", "a rebase"),
                                        ("CHERRY_PICK_HEAD", "a cherry-pick"), ("REVERT_HEAD", "a revert")])
def test_an_operation_in_progress_is_refused(repo, name, what):
    a = answers(repo)
    if name.startswith("rebase"):
        (repo / ".git" / name).mkdir()
    else:
        (repo / ".git" / name).write_text(git(repo, "rev-parse", "HEAD"))
    with pytest.raises(CommitRefused, match=f"{what} is in progress"):
        commit_once(repo, "main", [a], "m")


def test_a_conflict_without_merge_head_is_refused(repo):
    a = answers(repo, '{"labels": {"base": {}}}')
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    a.write_text('{"labels": {"stashed": {}}}')
    git(repo, "stash", "-q")
    a.write_text('{"labels": {"committed": {}}}')
    git(repo, "commit", "-qam", "other")
    git(repo, "stash", "pop", check=False)  # conflicts; a stash pop leaves no MERGE_HEAD
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    with pytest.raises(CommitRefused, match="in conflict"):
        commit_once(repo, "main", [a], "m")


def test_no_label_file_at_all_is_refused(repo):
    with pytest.raises(CommitRefused, match="none of the label files"):
        commit_once(repo, "main", [repo / "labels" / "d" / "human_answers.json"], "m")


def hook_never_started(refused):
    """Fail naming the cause: the commit was stopped before git ran the hook."""
    pytest.fail(f"git did not start the hook within the {gitcommit.GIT_TIMEOUT_S:g} s timeout, so its stopping "
                f"was not tested; the refusal was: {refused.value}")


def test_a_hung_hook_is_stopped_and_frees_both_locks(repo, monkeypatch):
    # Long enough for git to reach the hook on a loaded machine: with 1 s, the commit
    # was sometimes stopped before the hook ran, and the hook's pid file never appeared.
    monkeypatch.setattr(gitcommit, "GIT_TIMEOUT_S", 3.0)
    a = answers(repo)
    hook = repo / ".git" / "hooks" / "pre-commit"
    pidfile = repo.parent / "hook.pid"
    hook.write_text(f"#!/bin/sh\necho $$ > {pidfile}\nexec sleep 30\n")
    hook.chmod(0o755)
    t0 = time.monotonic()
    with pytest.raises(CommitRefused, match="ran longer than 3 s") as refused:
        commit_once(repo, "main", [a], "m")
    assert time.monotonic() - t0 < 15
    assert not (repo / ".git" / "index.lock").exists()
    with open(repo / ".git" / "tl-sync.flock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises if still held
    assert git(repo, "diff", "--cached", "--name-only") == ""
    if not pidfile.exists():
        hook_never_started(refused)
    pid = int(pidfile.read_text())  # this hook's own process, not any `sleep 30` on the machine
    for _ in range(40):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"the hook's process {pid} is still running")


def test_labels_already_committed_by_someone_else_count_as_success(repo):
    a = answers(repo)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "an agent's commit -a")
    assert commit_once(repo, "main", [a], "m") == {"committed": False, "already": True}


def test_a_failed_commit_unstages_only_its_own_paths(repo):
    a = answers(repo)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    with pytest.raises(CommitRefused, match="git commit failed"):
        commit_once(repo, "main", [a], "m")
    assert git(repo, "diff", "--cached", "--name-only") == ""
    assert last_commit(repo)[0].strip() == "init"
    # Another agent stages its own file after the checks pass: the cleanup leaves it staged.
    (repo / "other.txt").write_text("theirs\n")
    wrap = repo.parent / "gitwrap"
    wrap.write_text('#!/bin/sh\nif [ "$3" = commit ]; then /usr/bin/git -C "$2" add other.txt; fi\n'
                    'exec /usr/bin/git "$@"\n')
    wrap.chmod(0o755)
    with pytest.raises(CommitRefused, match="git commit failed"):
        commit_once(repo, "main", [a], "m", git_bin=str(wrap))
    assert git(repo, "diff", "--cached", "--name-only").split() == ["other.txt"]


# ------------------------------------------------------------------ TIMING

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def committer(tmp_path, repo):
    home = Home(tmp_path / "h")
    home.init()
    reg = Registry(home)
    reg.create_project("vi", privacy="git-private", writers=["o/d"],
                       git={"checkout": str(repo), "branch": "main", "commit": True})
    reg.add_dataset("vi", "d", "labels/d")
    calls = []
    clock = Clock()

    def fake_commit(checkout, branch, files, message, wait_s=None, git_bin=None):
        calls.append(message)
        return {"committed": True, "sha": "f" * 40}
    c = Committer(reg, clock=clock, commit=fake_commit)
    c.calls, c.clock_obj = calls, clock
    return c


def files(project, dataset):
    return []


def test_commits_after_300_s_idle(committer):
    committer.note_save("vi", "d")
    committer.clock_obj.t += 299
    assert committer.tick(files) == []
    committer.clock_obj.t += 1
    assert committer.tick(files)[0]["committed"] and committer.calls[0].startswith("Save human labels: vi/d (1 save,")


def test_a_long_sitting_commits_every_1800_s(committer):
    for _ in range(40):
        committer.note_save("vi", "d")
        committer.clock_obj.t += 60  # never 300 s idle
        committer.tick(files)
    assert len(committer.calls) == 1 and "(30 saves" in committer.calls[0]


def test_finish_commits_at_once(committer):
    committer.note_save("vi", "d", finished=True)
    assert committer.tick(files)[0]["committed"]


def test_index_lock_backs_off_then_waits_for_the_next_save(committer):
    def locked(*a, **k):
        raise IndexLocked("index.lock held")
    committer.commit = locked
    committer.note_save("vi", "d", finished=True)
    gaps = []
    for i in range(6):
        if i:
            committer.clock_obj.t += gaps[-1]
        assert committer.tick(files)[0]["pending"]
        gaps.append(committer.state[("vi", "d")].next_try - committer.clock_obj.t)
    assert gaps == [2, 5, 10, 30, 60, 600]  # five retries over about two minutes, then a 10-minute wait
    assert committer.tick(files) == []
    committer.commit = lambda *a, **k: {"committed": True, "sha": "a" * 40}
    committer.note_save("vi", "d", finished=True)
    assert committer.tick(files)[0]["committed"]


def test_a_save_during_the_commit_stays_pending(committer):
    def slow_commit(*a, **k):
        committer.clock_obj.t += 5
        committer.note_save("vi", "d")  # lands while git runs; the commit may not hold it
        return {"committed": True, "sha": "b" * 40}
    committer.commit = slow_commit
    committer.note_save("vi", "d", finished=True)
    assert committer.tick(files)[0]["committed"]
    assert committer.status("vi", "d")["pending_saves"] == 1
    committer.commit = lambda *a, **k: {"committed": True, "sha": "c" * 40}
    assert committer.tick(files)[0]["committed"]  # Finish still set: the new save commits at once
    assert committer.status("vi", "d")["pending_saves"] == 0


def test_the_background_commit_waits_for_the_sync_lock_with_a_cap(committer):
    seen = []
    committer.commit = lambda *a, **k: seen.append(k["wait_s"]) or {"committed": True, "sha": "d" * 40}
    committer.note_save("vi", "d", finished=True)
    committer.tick(files)
    assert seen == [Committer.SYNC_WAIT_S]


def test_a_removed_project_is_dropped_loudly(committer):
    committer.note_save("vi", "d", finished=True)
    data = committer.registry.load()
    del data["projects"]["vi"]
    committer.registry.load = lambda: data
    assert committer.tick(files) == [{"project": "vi", "dataset": "d", "dropped": True,
                                      "error": "the project is gone or no longer commits"}]
    assert committer.state == {}


def test_one_datasets_error_never_stops_another(committer):
    committer.registry.add_dataset("vi", "e", "labels/e")
    committer.note_save("vi", "d", finished=True)
    committer.note_save("vi", "e", finished=True)

    def files_of(project, dataset):
        if dataset == "d":
            raise SystemExit("REFUSING: no dataset vi/d")
        return []
    results = {r["dataset"]: r for r in committer.tick(files_of)}
    assert results["d"]["pending"] and results["d"]["error"] == "SystemExit: REFUSING: no dataset vi/d"
    assert results["e"]["committed"]
    assert committer.tick(files_of) == []  # d now waits 10 minutes
    committer.clock_obj.t += 600
    assert committer.tick(files_of)[0]["error"].startswith("SystemExit")


def test_only_one_tick_runs_at_a_time(committer):
    inner = []

    def slow_commit(*a, **k):
        inner.append(committer.tick(files))
        return {"committed": True, "sha": "e" * 40}
    committer.commit = slow_commit
    committer.note_save("vi", "d", finished=True)
    assert committer.tick(files)[0]["committed"]
    assert inner == [[{"project": None, "dataset": None, "skipped": "another tick is still running"}]]
    assert committer.status("vi", "d")["pending_saves"] == 0


def test_a_missing_label_file_stays_pending_with_its_error(committer, repo):
    committer.commit = commit_once
    committer.note_save("vi", "d", finished=True)
    res = committer.tick(lambda p, d: [repo / "labels" / "d" / "answers.json"])
    assert res[0]["pending"] and "none of the label files ['answers.json']" in res[0]["error"]
    st = committer.status("vi", "d")
    assert st["pending_saves"] == 1 and "none of the label files" in st["last_error"]


def test_local_only_projects_and_commit_off_are_ignored(committer, tmp_path):
    committer.registry.create_project("loc", privacy="local-only", writers=["o/x"])
    committer.note_save("loc", "x", finished=True)
    assert committer.tick(files) == []
    assert committer.status("loc", "x") == {"commits": "not a git-private project"}
    st = committer.status("vi", "d")
    assert st["commits"] == "on" and st["pending_saves"] == 0


def test_a_hook_that_ignores_sigterm_is_killed(repo, monkeypatch):
    monkeypatch.setattr(gitcommit, "GIT_TIMEOUT_S", 3.0)  # time for git to reach the hook; see above
    monkeypatch.setattr(gitcommit, "STOP_GRACE_S", 1.0)
    pidfile = repo.parent / "stubborn.pid"
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntrap '' TERM\necho $$ > {pidfile}\nsleep 30\n")
    hook.chmod(0o755)
    with pytest.raises(CommitRefused, match="ran longer than 3 s") as refused:
        commit_once(repo, "main", [answers(repo)], "m")
    if not pidfile.exists():
        hook_never_started(refused)
    pid = int(pidfile.read_text())
    for _ in range(40):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"the hook's process {pid}, which ignores SIGTERM, is still running")


SERVICE = """
import sys
from transcript_labeling.gitcommit import commit_once
commit_once(sys.argv[1], "main", [sys.argv[2]], "label commit")
"""


def start_service(repo, a):
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    return subprocess.Popen([sys.executable, "-c", SERVICE, str(repo), str(a)], env=env, start_new_session=True)


def wait_for(path, seconds=20):
    deadline = time.monotonic() + seconds
    while not path.exists():
        assert time.monotonic() < deadline, f"{path.name} never appeared"
        time.sleep(0.02)


def take_lock_after(repo, killed, seconds=30):
    """Take the sync lock as the sync tool would; how long after `killed` it took."""
    lock = open(repo / ".git" / "tl-sync.flock", "a")
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lock, time.monotonic() - killed
        except BlockingIOError:
            assert time.monotonic() - killed < seconds, "the sync lock was never released"
            time.sleep(0.05)


def test_if_the_service_dies_mid_commit_git_keeps_the_sync_lock(repo, tmp_path):
    a = answers(repo)
    started = repo.parent / "hook.started"
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {started}\nsleep 2\n")
    hook.chmod(0o755)
    service = start_service(repo, a)
    wait_for(started)  # git is now running the hook, under the lock
    os.killpg(service.pid, signal.SIGKILL)  # as launchd kills the job's group when it exits
    service.wait()
    lock, _ = take_lock_after(repo, time.monotonic())
    held_at = git(repo, "rev-parse", "HEAD")
    time.sleep(4)  # long enough for an unlocked orphan's 2 s hook and commit to land
    assert git(repo, "rev-parse", "HEAD") == held_at, "a commit landed while the sync tool held the lock"
    fcntl.flock(lock, fcntl.LOCK_UN)
    lock.close()
    assert last_commit(repo)[0].strip() == "label commit"  # git finished the commit before letting go



def test_an_orphaned_git_add_keeps_the_sync_lock_too(repo):
    a = answers(repo)
    started = repo.parent / "filter.started"
    (repo / ".gitattributes").write_text("*.json filter=slow\n")
    git(repo, "config", "filter.slow.clean", f"touch {started}; sleep 3; cat")  # makes `git add` take 3 s
    service = start_service(repo, a)
    wait_for(started)  # `git add` is now running the filter, under the lock
    os.killpg(service.pid, signal.SIGKILL)
    service.wait()
    lock, waited = take_lock_after(repo, time.monotonic())
    fcntl.flock(lock, fcntl.LOCK_UN)
    lock.close()
    assert waited > 0.8, f"the sync tool got the lock {waited:.2f} s after the service died, while git add ran"


def test_a_hooks_background_job_never_inherits_the_sync_lock(repo):
    a = answers(repo)
    started, bg = repo.parent / "hook.started", repo.parent / "bg.pid"
    hooks = repo / ".git" / "hooks"
    (hooks / "pre-commit").write_text(f"#!/bin/sh\ntouch {started}\nsleep 2\n")
    (hooks / "post-commit").write_text(f"#!/bin/sh\nsleep 20 </dev/null >/dev/null 2>&1 &\necho $! > {bg}\n")
    for h in ("pre-commit", "post-commit"):
        (hooks / h).chmod(0o755)
    service = start_service(repo, a)
    wait_for(started)
    os.killpg(service.pid, signal.SIGKILL)  # the service dies; git finishes, and its hook leaves a job behind
    service.wait()
    try:
        lock, waited = take_lock_after(repo, time.monotonic())
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
        assert waited < 10, f"the sync tool waited {waited:.1f} s: the hook's 20 s job held the lock"
        assert last_commit(repo)[0].strip() == "label commit"
    finally:
        if bg.exists():  # never mask the assertion above with a cleanup failure
            try:
                os.kill(int(bg.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_a_hook_gets_the_grace_period_to_clean_up(repo, monkeypatch):
    monkeypatch.setattr(gitcommit, "GIT_TIMEOUT_S", 3.0)  # time for git to reach the hook; see above
    started, cleaned = repo.parent / "started", repo.parent / "cleaned"
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntrap 'sleep 0.5; touch {cleaned}; exit 1' TERM\ntouch {started}\n"
                    "sleep 30 &\nwait\n")
    hook.chmod(0o755)
    with pytest.raises(CommitRefused, match="ran longer than 3 s") as refused:
        commit_once(repo, "main", [answers(repo)], "m")
    if not started.exists():
        hook_never_started(refused)
    assert cleaned.exists(), "the hook was killed before its SIGTERM cleanup finished"


def test_every_write_call_runs_under_the_lock_holder(repo, monkeypatch):
    calls = []
    real_git = gitcommit.git

    def spy(checkout, *args, **kw):
        calls.append((args[0], kw.get("lock_fd")))
        return real_git(checkout, *args, **kw)
    monkeypatch.setattr(gitcommit, "git", spy)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")  # the commit fails, so the cleanup reset runs too
    hook.chmod(0o755)
    with pytest.raises(CommitRefused):
        commit_once(repo, "main", [answers(repo)], "m")
    writes = {name: fd for name, fd in calls if name in ("add", "commit", "reset")}
    assert set(writes) == {"add", "commit", "reset"} and all(isinstance(fd, int) for fd in writes.values())
    assert all(fd is None for name, fd in calls if name not in writes)


def test_stop_returns_as_soon_as_the_group_has_exited(repo, monkeypatch):
    monkeypatch.setattr(gitcommit, "GIT_TIMEOUT_S", 0.5)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexec sleep 30\n")  # dies on SIGTERM at once
    hook.chmod(0o755)
    t0 = time.monotonic()
    with pytest.raises(CommitRefused, match="ran longer than 0.5 s"):
        commit_once(repo, "main", [answers(repo)], "m")
    assert time.monotonic() - t0 < 4, "stop waited out the grace period for a group that had already exited"
