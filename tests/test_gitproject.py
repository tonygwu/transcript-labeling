"""Creating a git-private project, against scratch checkouts and local bare mirrors.

  PRIVATE    every fetch and push URL of every remote must be PRIVATE
  PUSH       push settings must name checked remotes; legacy remote files are refused
  CHECKOUT   absolute, the top level of a work tree, overlapping no other project's
  WRITERS    the checkout's own repositories, the creator's, and extras
  COMMITS    git.commit follows the tl.syncLock marker
  GH         the default visibility check asks gh about github.com/owner/repo and fails closed
"""
from __future__ import annotations

import json
import subprocess

import pytest

from transcript_labeling import gitcommit
from transcript_labeling.gitcommit import MARKER
from transcript_labeling.gitproject import (check_checkout, create_git_project, gh_visibility, github_slug, own_repo,
                                            redact)
from transcript_labeling.home import Home
from transcript_labeling.registry import Registry

PRIVATE_URL = "git@github.com:owner/labels-data.git"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def private_only(url):
    return "PRIVATE" if url in (PRIVATE_URL, "https://github.com/owner/other-private") else "PUBLIC"


@pytest.fixture
def checkout(tmp_path):
    r = tmp_path / "data"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "remote", "add", "origin", PRIVATE_URL)
    return r


@pytest.fixture
def registry(tmp_path):
    home = Home(tmp_path / "h")
    home.init()
    return Registry(home)


def test_a_private_checkout_becomes_a_project(registry, checkout):
    entry = create_git_project(registry, "vi", checkout, creator_repo="owner/code", extra_writers=["owner/x"],
                               visibility=private_only)
    assert entry["privacy"] == "git-private"
    assert entry["writers"] == ["owner/code", "owner/labels-data", "owner/x"]
    assert entry["git"] == {"idle_s": 300, "max_age_s": 1800, "checkout": str(checkout.resolve()),
                            "branch": "main", "commit": False}
    assert json.loads(registry.home.registry_path.read_text())["projects"]["vi"] == entry


def test_commits_turn_on_only_with_the_sync_lock_marker(registry, checkout):
    git(checkout, "config", MARKER, "1")
    assert create_git_project(registry, "vi", checkout, creator_repo=None, visibility=private_only)["git"]["commit"]


@pytest.mark.parametrize("setup, why", [
    (lambda r: git(r, "remote", "set-url", "origin", "https://github.com/owner/code"), "PUBLIC"),
    (lambda r: git(r, "remote", "add", "mirror", "https://github.com/owner/code.git"), "mirror"),
    (lambda r: git(r, "remote", "set-url", "--push", "origin", "git@github.com:owner/code.git"), "PUBLIC"),
    (lambda r: git(r, "config", "url.https://github.com/owner/code.insteadOf", PRIVATE_URL), "PUBLIC"),
    (lambda r: git(r, "remote", "remove", "origin"), "has no remote"),
])
def test_any_remote_that_is_not_private_is_refused(registry, checkout, setup, why):
    setup(checkout)
    with pytest.raises(SystemExit, match=why):
        create_git_project(registry, "vi", checkout, creator_repo=None, visibility=private_only)
    assert registry.load()["projects"] == {}


def test_a_public_second_push_url_is_refused(registry, checkout):
    git(checkout, "remote", "set-url", "--add", "--push", "origin", PRIVATE_URL)
    git(checkout, "remote", "set-url", "--add", "--push", "origin", "https://github.com/owner/code")
    with pytest.raises(SystemExit, match="origin https://github.com/owner/code: PUBLIC"):
        create_git_project(registry, "vi", checkout, creator_repo=None, visibility=private_only)


@pytest.mark.parametrize("key", ["branch.main.pushRemote", "remote.pushDefault", "branch.main.remote"])
def test_a_push_setting_that_names_a_url_is_refused(registry, checkout, key):
    git(checkout, "config", key, "https://github.com/owner/code.git")
    with pytest.raises(SystemExit, match="names no checked remote"):
        create_git_project(registry, "vi", checkout, creator_repo=None, visibility=private_only)


def test_push_settings_that_name_remotes_are_fine(registry, checkout):
    git(checkout, "config", "branch.main.remote", "origin")
    git(checkout, "config", "branch.main.pushRemote", "origin")
    git(checkout, "config", "remote.pushDefault", "origin")
    git(checkout, "config", "branch.topic.remote", ".")
    assert create_git_project(registry, "vi", checkout, creator_repo=None, visibility=private_only)


@pytest.mark.parametrize("legacy", ["remotes", "branches"])
def test_legacy_remote_files_are_refused(registry, checkout, legacy):
    (checkout / ".git" / legacy).mkdir()
    (checkout / ".git" / legacy / "old").write_text("https://github.com/owner/code\n")
    with pytest.raises(SystemExit, match="legacy remote files"):
        create_git_project(registry, "vi", checkout, creator_repo=None, visibility=private_only)


def test_git_dir_in_the_environment_does_not_redirect_the_check(registry, checkout, tmp_path, monkeypatch):
    public = tmp_path / "public"
    public.mkdir()
    git(public, "init", "-q", "-b", "main")
    git(public, "remote", "add", "origin", "https://github.com/owner/code")
    monkeypatch.setenv("GIT_DIR", str(checkout / ".git"))  # private, while the named checkout is public
    with pytest.raises(SystemExit, match="PUBLIC"):
        create_git_project(registry, "vi", public, creator_repo=None, visibility=private_only)


def test_credentials_never_reach_the_message(registry, checkout):
    git(checkout, "remote", "add", "tok", "https://x:ghp_SECRET@github.com/owner/code")
    git(checkout, "config", "branch.main.pushRemote", "https://y:ghp_OTHER@github.com/owner/code")
    with pytest.raises(SystemExit) as exc:
        create_git_project(registry, "vi", checkout, creator_repo=None, visibility=private_only)
    assert "ghp_" not in str(exc.value) and "tok https://github.com/owner/code: PUBLIC" in str(exc.value)


def test_a_remote_name_with_a_space_is_checked(registry, checkout):
    git(checkout, "config", "remote.a b.url", "https://github.com/owner/code")
    with pytest.raises(SystemExit, match="a b https://github.com/owner/code: PUBLIC"):
        create_git_project(registry, "vi", checkout, creator_repo=None, visibility=private_only)


def test_a_git_failure_is_a_refusal_not_a_crash(registry, checkout, tmp_path):
    fake = tmp_path / "git"
    fake.write_text('#!/bin/sh\nfor a; do [ "$a" = get-url ] && { echo "error: boom" >&2; exit 2; }; done\n'
                    'exec /usr/bin/git "$@"\n')
    fake.chmod(0o755)
    with pytest.raises(SystemExit, match="REFUSING: .*git remote get-url failed: error: boom"):
        create_git_project(registry, "vi", checkout, creator_repo=None, visibility=private_only,
                           git_bin=str(fake))


def test_a_second_private_push_url_is_allowed(registry, checkout):
    git(checkout, "remote", "set-url", "--add", "--push", "origin", "https://github.com/owner/other-private")
    git(checkout, "remote", "set-url", "--add", "--push", "origin", PRIVATE_URL)
    entry = create_git_project(registry, "vi", checkout, creator_repo=None, visibility=private_only)
    assert entry["writers"] == ["owner/labels-data", "owner/other-private"]


def test_a_local_bare_mirror_passes_only_through_the_test_override(registry, tmp_path):
    bare = tmp_path / "mirror.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    r = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(bare), str(r)], check=True, capture_output=True)
    with pytest.raises(SystemExit, match="not a github.com repository"):
        create_git_project(registry, "vi", r, creator_repo="owner/code")
    entry = create_git_project(registry, "vi", r, creator_repo="owner/code",
                               visibility=lambda url: "PRIVATE" if url == str(bare) else "PUBLIC")
    assert entry["writers"] == ["owner/code"]


def test_the_checkout_path_is_checked(registry, checkout, tmp_path):
    with pytest.raises(SystemExit, match="must be an absolute path"):
        create_git_project(registry, "vi", "data", creator_repo=None, visibility=private_only)
    (checkout / "sub").mkdir()
    with pytest.raises(SystemExit, match="name its top level"):
        create_git_project(registry, "vi", checkout / "sub", creator_repo=None, visibility=private_only)
    with pytest.raises(SystemExit, match="is not a git checkout"):
        create_git_project(registry, "vi", tmp_path / "nowhere", creator_repo=None, visibility=private_only)
    (tmp_path / "plain").mkdir()
    with pytest.raises(SystemExit, match="is not a git checkout"):
        create_git_project(registry, "vi", tmp_path / "plain", creator_repo=None, visibility=private_only)


def test_one_checkout_belongs_to_one_project(registry, checkout, tmp_path):
    create_git_project(registry, "vi", checkout, creator_repo=None, visibility=private_only)
    link = tmp_path / "link"
    link.symlink_to(checkout)
    with pytest.raises(SystemExit, match="overlaps the checkout .* of project 'vi'"):
        create_git_project(registry, "vi2", link, creator_repo=None, visibility=private_only)


def test_nested_checkouts_are_refused_both_ways(registry, checkout):
    inner = checkout / "inner"
    inner.mkdir()
    git(inner, "init", "-q", "-b", "main")
    git(inner, "remote", "add", "origin", PRIVATE_URL)
    create_git_project(registry, "outer", checkout, creator_repo=None, visibility=private_only)
    with pytest.raises(SystemExit, match="overlaps the checkout"):
        create_git_project(registry, "inner", inner, creator_repo=None, visibility=private_only)
    registry2 = Registry(Home(registry.home.root.parent / "h2"))
    registry2.home.init()
    create_git_project(registry2, "inner", inner, creator_repo=None, visibility=private_only)
    with pytest.raises(SystemExit, match="overlaps the checkout"):
        create_git_project(registry2, "outer", checkout, creator_repo=None, visibility=private_only)


def test_a_symlinked_checkout_is_stored_resolved(registry, checkout, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(checkout)
    entry = create_git_project(registry, "vi", link, creator_repo=None, visibility=private_only)
    assert entry["git"]["checkout"] == str(checkout.resolve()) != str(link)


def test_a_detached_head_needs_a_named_branch(registry, checkout):
    (checkout / "f").write_text("x")
    git(checkout, "add", "f")
    git(checkout, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qm", "init")
    git(checkout, "checkout", "-q", "--detach")
    with pytest.raises(SystemExit, match="detached HEAD"):
        create_git_project(registry, "vi", checkout, creator_repo=None, visibility=private_only)
    assert create_git_project(registry, "vi", checkout, creator_repo=None, branch="main",
                              visibility=private_only)["git"]["branch"] == "main"


@pytest.mark.parametrize("url, slug", [
    ("git@github.com:owner/repo.git", "owner/repo"),
    ("git@github.com:owner/repo", "owner/repo"),
    ("ssh://git@github.com/owner/repo.git", "owner/repo"),
    ("https://github.com/owner/repo.git", "owner/repo"),
    ("https://github.com/owner/repo/", "owner/repo"),
    ("https://token@github.com/owner/my.repo.git\n", "owner/my.repo"),
    ("https://github.com.evil.example/owner/repo", None),
    ("https://gitlab.com/owner/repo", None),
    ("/srv/mirror.git", None),
    ("https://github.com/owner/repo/extra", None),
])
def test_github_slug(url, slug):
    assert github_slug(url) == slug


def test_own_repo(checkout, tmp_path):
    assert own_repo(checkout) == "owner/labels-data"
    (tmp_path / "plain").mkdir()
    subprocess.run(["git", "init", "-q", str(tmp_path / "plain")], check=True)
    assert own_repo(tmp_path / "plain") is None


def test_gh_visibility_asks_gh_and_fails_closed(tmp_path):
    args = tmp_path / "args"
    fake = tmp_path / "gh"
    fake.write_text(f'#!/bin/sh\necho "$@" > {args}\necho PRIVATE\n')
    fake.chmod(0o755)
    assert gh_visibility(PRIVATE_URL, gh_bin=str(fake)) == "PRIVATE"
    assert args.read_text().split() == ["repo", "view", "github.com/owner/labels-data", "--json", "visibility",
                                        "-q", ".visibility"]  # the host is explicit, so GH_HOST cannot redirect it
    fake.write_text("#!/bin/sh\necho 'could not resolve' >&2\nexit 1\n")
    assert gh_visibility(PRIVATE_URL, gh_bin=str(fake)).startswith("UNKNOWN (gh exited 1: could not resolve")
    fake.write_text("#!/bin/sh\nexit 0\n")
    assert gh_visibility(PRIVATE_URL, gh_bin=str(fake)).startswith("UNKNOWN")
    assert gh_visibility(PRIVATE_URL, gh_bin=str(tmp_path / "missing")).startswith("UNKNOWN (gh failed")
    assert gh_visibility("/srv/mirror.git", gh_bin=str(fake)) == "UNKNOWN (not a github.com repository)"


def test_gh_visibility_fails_closed_on_non_ascii_output_under_an_ascii_locale(tmp_path):
    import os
    import sys
    fake = tmp_path / "gh"
    fake.write_bytes(b"#!/bin/sh\nprintf 'erreur: d\\303\\251p\\303\\264t introuvable\\n' >&2\nexit 1\n")
    fake.chmod(0o755)
    code = ("import sys, locale\n"
            "from transcript_labeling.gitproject import gh_visibility\n"
            f"print(locale.getpreferredencoding(False), gh_visibility({PRIVATE_URL!r}, gh_bin=sys.argv[1]))\n")
    env = {**os.environ, "LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONIOENCODING": "utf-8"}
    out = subprocess.run([sys.executable, "-c", code, str(fake)], env=env, capture_output=True, text=True,
                         encoding="utf-8")
    assert out.returncode == 0, out.stderr[-500:]
    encoding, result = out.stdout.split(" ", 1)
    assert encoding.replace("-", "").lower() != "utf8", "the child did not run under a non-UTF-8 locale"
    assert result.startswith("UNKNOWN (gh exited 1: erreur: d\u00e9p\u00f4t introuvable")


def test_check_checkout_reports_every_bad_remote(checkout):
    git(checkout, "remote", "add", "a", "https://github.com/owner/code")
    git(checkout, "remote", "add", "b", "/srv/mirror.git")
    with pytest.raises(SystemExit) as exc:
        check_checkout(checkout, visibility=lambda url: gh_visibility(url, gh_bin="/nonexistent/gh")
                       if url.startswith("/") else private_only(url))
    msg = str(exc.value)
    assert "a https://github.com/owner/code: PUBLIC" in msg and "b /srv/mirror.git: UNKNOWN" in msg


@pytest.mark.parametrize("vis", ["INTERNAL", "private", "", "UNKNOWN (gh exited 1: )"])
def test_only_exactly_private_passes(registry, checkout, vis):
    with pytest.raises(SystemExit, match="must be a private repository"):
        create_git_project(registry, "vi", checkout, creator_repo=None, visibility=lambda url: vis)


def test_legacy_files_in_a_linked_worktrees_main_git_dir_are_refused(registry, checkout, tmp_path):
    (checkout / "f").write_text("x")
    git(checkout, "add", "f")
    git(checkout, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qm", "init")
    linked = tmp_path / "linked"
    git(checkout, "worktree", "add", "-q", "-b", "side", str(linked))
    (checkout / ".git" / "remotes").mkdir()
    (checkout / ".git" / "remotes" / "old").write_text("https://github.com/owner/code\n")
    with pytest.raises(SystemExit, match="legacy remote files"):
        create_git_project(registry, "vi", linked, creator_repo=None, visibility=private_only)


def test_a_sibling_whose_name_extends_another_checkout_is_not_an_overlap(registry, tmp_path):
    for name in ("data", "data2"):
        r = tmp_path / name
        r.mkdir()
        git(r, "init", "-q", "-b", "main")
        git(r, "remote", "add", "origin", PRIVATE_URL)
    create_git_project(registry, "one", tmp_path / "data", creator_repo=None, visibility=private_only)
    assert create_git_project(registry, "two", tmp_path / "data2", creator_repo=None, visibility=private_only)


def fake_git(tmp_path, body):
    fake = tmp_path / "fakegit"
    fake.write_text(f'#!/bin/sh\nfor a; do {body}; done\nexec /usr/bin/git "$@"\n')
    fake.chmod(0o755)
    return str(fake)


def test_a_failure_reading_push_settings_is_a_refusal(registry, checkout, tmp_path):
    git(checkout, "config", "branch.main.pushRemote", "/srv/public.git")
    fake = fake_git(tmp_path, '[ "$a" = --get-regexp ] && { echo "fatal: bad config" >&2; exit 128; }')
    with pytest.raises(SystemExit, match="git config --get-regexp failed: fatal: bad config"):
        create_git_project(registry, "vi", checkout, creator_repo=None, visibility=private_only, git_bin=fake)


@pytest.mark.parametrize("hang_on, branch", [("symbolic-ref", None), ("tl.syncLock", "main")])
def test_a_hung_git_after_the_checks_is_a_refusal(registry, checkout, tmp_path, monkeypatch, hang_on, branch):
    monkeypatch.setattr(gitcommit, "GIT_TIMEOUT_S", 1.0)
    fake = fake_git(tmp_path, f'[ "$a" = {hang_on} ] && exec sleep 29')
    with pytest.raises(SystemExit, match="REFUSING: .*ran longer than 1 s"):
        create_git_project(registry, "vi", checkout, creator_repo=None, branch=branch, visibility=private_only,
                           git_bin=fake)
    assert registry.load()["projects"] == {}


@pytest.mark.parametrize("url, shown", [
    ("https://x:pa@ss@github.com/owner/code", "https://github.com/owner/code"),
    ("https://tok@github.com/owner/a@b", "https://github.com/owner/a@b"),
    ("ssh://u:p@host/x", "ssh://host/x"),
    ("git@github.com:owner/code.git", "git@github.com:owner/code.git"),
])
def test_redact(url, shown):
    assert redact(url) == shown


def test_remote_and_push_problems_are_reported_under_their_own_headings(registry, checkout):
    git(checkout, "remote", "add", "pub", "https://github.com/owner/code")
    git(checkout, "config", "remote.pushDefault", "https://github.com/owner/code")
    with pytest.raises(SystemExit) as exc:
        create_git_project(registry, "vi", checkout, creator_repo=None, visibility=private_only)
    msg = str(exc.value)
    assert msg.startswith("REFUSING: every remote of a git-private checkout must be a private repository; pub ")
    assert ". Also, a plain git push must go only to those remotes; remote.pushdefault " in msg
