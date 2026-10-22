"""The home folder and the registry: private files, named projects, guarded dataset paths."""
from __future__ import annotations

import json
import os
import stat

import pytest

from transcript_labeling.home import DEFAULT_HOME, Home, home_path
from transcript_labeling.registry import Registry


def test_home_path_defaults_and_honours_tl_home(tmp_path):
    assert home_path({}) == home_path({"TL_HOME": ""}) != tmp_path
    assert str(home_path({})).endswith(DEFAULT_HOME.removeprefix("~"))
    assert home_path({"TL_HOME": str(tmp_path)}) == tmp_path


def test_init_makes_a_private_home_once(tmp_path):
    home = Home(tmp_path / "h")
    made = home.init()
    assert str(home.token_path) in made and str(home.config_path) in made
    assert stat.S_IMODE(home.root.stat().st_mode) == 0o700
    for f in (home.token_path, home.config_path):
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
    token = home.token()
    assert len(token) >= 32
    assert home.init() == [] and home.token() == token
    assert home.config() == {"port": 8790, "operator_id": "operator", "allowed_hosts": [], "access": {}}


def test_a_readable_token_or_a_broken_config_is_refused(tmp_path):
    home = Home(tmp_path / "h")
    with pytest.raises(SystemExit, match="run `tl init`"):
        home.token()
    with pytest.raises(SystemExit, match="run `tl init`"):
        home.config()
    home.init()
    os.chmod(home.token_path, 0o644)
    with pytest.raises(SystemExit, match="must be 0600"):
        home.token()
    home.config_path.write_text(json.dumps({"port": 1}))
    with pytest.raises(SystemExit, match="has no operator_id"):
        home.config()


@pytest.fixture
def reg(tmp_path):
    home = Home(tmp_path / "h")
    home.init()
    return Registry(home)


def test_local_only_project_and_dataset_live_under_tl_home(reg):
    reg.create_project("demo", privacy="local-only", writers=["me/repo"], now="2026-10-22T00:00:00+00:00")
    path = reg.add_dataset("demo", "call-1")
    assert path == reg.home.root / "data" / "demo" / "call-1"
    assert reg.dataset_dir("demo", "call-1") == path
    assert reg.datasets() == [("demo", "call-1")]
    with pytest.raises(SystemExit, match="always lives at"):
        reg.add_dataset("demo", "call-2", "elsewhere")
    with pytest.raises(SystemExit, match="already exists"):
        reg.add_dataset("demo", "call-1")
    with pytest.raises(SystemExit, match="already exists"):
        reg.create_project("demo", privacy="local-only", writers=["me/repo"])


def test_git_private_datasets_need_a_relative_path_inside_the_checkout(reg, tmp_path):
    git = {"checkout": str(tmp_path / "co"), "branch": "main", "commit": False}
    entry = reg.create_project("vi", privacy="git-private", writers=["o/data", "o/code"], git=git)
    assert entry["git"]["idle_s"] == 300 and entry["git"]["max_age_s"] == 1800
    with pytest.raises(SystemExit, match="needs --path"):
        reg.add_dataset("vi", "talk-1")
    for bad in ("/abs", "../up", "a/../b", "a//b", ""):
        with pytest.raises(SystemExit, match="must be relative"):
            reg.add_dataset("vi", "x", bad)
    assert reg.add_dataset("vi", "talk-1", "logs/talk-1") == tmp_path / "co" / "logs" / "talk-1"
    for overlap in ("logs/talk-1", "logs", "logs/talk-1/items"):
        with pytest.raises(SystemExit, match="would overlap dataset vi/talk-1"):
            reg.add_dataset("vi", "talk-1-again", overlap)
    with pytest.raises(SystemExit, match="inside .git"):
        reg.add_dataset("vi", "hooks", ".git/hooks")
    assert reg.add_dataset("vi", "talk-2", "logs/talk-2").name == "talk-2"


def put_git_project(reg, name, checkout):
    """Write a git-private project straight into the registry, as a hand edit would.

    create_project refuses a checkout that overlaps another project's, so this is the
    only way two such projects exist; add_dataset must still keep their datasets apart.
    """
    with reg.locked():
        r = reg.load()
        r["projects"][name] = {"title": name, "privacy": "git-private", "writers": ["o/data"],
                               "created_at": "2026-01-01T00:00:00+00:00", "datasets": {},
                               "git": {"checkout": str(checkout), "branch": "main", "commit": False,
                                       "idle_s": 300, "max_age_s": 1800}}
        reg._save(r)


def test_dataset_paths_cannot_nest_by_letter_case_or_across_projects_on_one_checkout(reg, tmp_path):
    (tmp_path / "co").mkdir()
    git = {"checkout": str(tmp_path / "co"), "branch": "main", "commit": False}
    reg.create_project("vi", privacy="git-private", writers=["o/data"], git=git)
    reg.add_dataset("vi", "talk-1", "logs/a")
    for overlap in ("Logs/A", "logs/A/items", "LOGS"):
        with pytest.raises(SystemExit, match="would overlap dataset vi/talk-1"):
            reg.add_dataset("vi", "x", overlap)
    for inside in (".GIT/hooks", "sub/.Git/x"):
        with pytest.raises(SystemExit, match="inside .git"):
            reg.add_dataset("vi", "x", inside)
    # A second project on the same checkout, named through a symlink to it.
    (tmp_path / "link").symlink_to(tmp_path / "co")
    put_git_project(reg, "vi2", tmp_path / "link")
    for overlap in ("logs", "logs/a/items", "Logs/A"):
        with pytest.raises(SystemExit, match="would overlap dataset vi/talk-1"):
            reg.add_dataset("vi2", "y", overlap)
    assert reg.add_dataset("vi2", "y", "logs/b").name == "b"
    # Projects on different folders never collide.
    reg.create_project("other", privacy="git-private", writers=["o/data"],
                       git={**git, "checkout": str(tmp_path / "elsewhere")})
    assert reg.add_dataset("other", "z", "logs/a").name == "a"


@pytest.mark.parametrize("kwargs,expect", [
    (dict(name="Bad Name", privacy="local-only", writers=["r"]), "must match"),
    (dict(name="ok", privacy="public", writers=["r"]), "privacy"),
    (dict(name="ok", privacy="local-only", writers=[]), "at least one writer"),
    (dict(name="ok", privacy="git-private", writers=["r"]), "needs its git settings"),
    (dict(name="ok", privacy="git-private", writers=["r"], git={"checkout": "rel", "branch": "m", "commit": False}),
     "absolute checkout"),
])
def test_project_creation_refusals(reg, kwargs, expect):
    kwargs = dict(kwargs)  # the parameter is shared by every run of this case
    name = kwargs.pop("name")
    with pytest.raises(SystemExit, match=expect):
        reg.create_project(name, **kwargs)
    assert reg.load()["projects"] == {}


def test_writer_check_is_a_guard_with_an_owner_override(reg):
    reg.create_project("demo", privacy="local-only", writers=["me/repo"])
    reg.check_writer("demo", "me/repo")
    with pytest.raises(SystemExit, match="not a writer"):
        reg.check_writer("demo", "someone/else")
    reg.check_writer("demo", "someone/else", owner_override=True)
    with pytest.raises(SystemExit, match="no project"):
        reg.check_writer("nope", "me/repo")


def test_a_registry_of_another_version_is_refused(reg):
    reg.home.registry_path.write_text(json.dumps({"schema_version": 9, "projects": {}}))
    with pytest.raises(SystemExit, match="not a version-1 registry"):
        reg.load()


def test_one_name_written_two_ways_is_one_folder(reg, tmp_path):
    (tmp_path / "co").mkdir()
    reg.create_project("vi", privacy="git-private", writers=["o/data"],
                       git={"checkout": str(tmp_path / "co"), "branch": "main", "commit": False})
    reg.add_dataset("vi", "a", "logs/café")  # é as one character
    for overlap in ("logs/café/items", "logs/CAFÉ"):  # e plus a combining accent
        with pytest.raises(SystemExit, match="would overlap dataset vi/a"):
            reg.add_dataset("vi", "b", overlap)


def test_nested_checkouts_and_the_service_home_are_compared_by_real_folder(reg, tmp_path):
    co = tmp_path / "co"
    (co / "logs").mkdir(parents=True)
    git = {"branch": "main", "commit": False}
    reg.create_project("p1", privacy="git-private", writers=["o/data"], git={**git, "checkout": str(co)})
    reg.add_dataset("p1", "a", "logs/a")
    put_git_project(reg, "p2", co / "logs")
    with pytest.raises(SystemExit, match="would overlap dataset p1/a"):
        reg.add_dataset("p2", "b", "a/items")
    assert reg.add_dataset("p2", "c", "c").name == "c"
    put_git_project(reg, "p3", reg.home.root)
    for path in ("state", "data/loc/ds", "logs"):
        with pytest.raises(SystemExit):
            reg.add_dataset("p3", "d", path)
    put_git_project(reg, "p4", reg.home.root.parent)
    with pytest.raises(SystemExit, match="overlaps the service's home"):
        reg.add_dataset("p4", "e", reg.home.root.name)


def test_a_symlink_inside_the_checkout_cannot_lead_around_the_guards(reg, tmp_path):
    co = tmp_path / "co"
    (co / "logs" / "a").mkdir(parents=True)
    (co / ".git").mkdir()
    (tmp_path / "outside").mkdir()
    (co / "alias").symlink_to(co / "logs" / "a")
    (co / "g").symlink_to(co / ".git")
    (co / "out").symlink_to(tmp_path / "outside")
    reg.create_project("vi", privacy="git-private", writers=["o/data"],
                       git={"checkout": str(co), "branch": "main", "commit": False})
    reg.add_dataset("vi", "a", "logs/a")
    with pytest.raises(SystemExit, match="would overlap dataset vi/a"):
        reg.add_dataset("vi", "b", "alias/items")
    with pytest.raises(SystemExit, match="into .git through a symlink"):
        reg.add_dataset("vi", "c", "g/hooks")
    with pytest.raises(SystemExit, match="out of .* through a symlink"):
        reg.add_dataset("vi", "d", "out/x")
    (co / "here").symlink_to(".")
    (co / "up").symlink_to("..")
    for whole in ("here", "up"):  # the checkout itself, or its parent
        with pytest.raises(SystemExit, match="out of .* through a symlink"):
            reg.add_dataset("vi", "e", whole)
    assert reg.add_dataset("vi", "f", "here/f").name == "f"  # resolves to a folder inside the checkout


def test_the_git_check_compares_folder_names_not_character_counts(reg, tmp_path):
    import unicodedata
    co = tmp_path / unicodedata.normalize("NFD", "éé")  # four characters
    (co / ".git").mkdir(parents=True)
    (co / "g").symlink_to(tmp_path / unicodedata.normalize("NFC", "éé") / ".git")  # two characters
    reg.create_project("vi", privacy="git-private", writers=["o/data"],
                       git={"checkout": str(co), "branch": "main", "commit": False})
    with pytest.raises(SystemExit, match="into .git through a symlink"):
        reg.add_dataset("vi", "c", "g/hooks")


def test_a_deleted_home_is_refused_not_made_again(reg):
    import shutil
    from transcript_labeling.auth import Auth
    auth = Auth(reg.home)
    auth.mint_code("/")
    shutil.rmtree(reg.home.root)
    with pytest.raises(SystemExit, match="run `tl init`"):
        reg.create_project("p", privacy="local-only", writers=["o/r"])
    with pytest.raises(SystemExit, match="run `tl init`"):
        auth.mint_code("/")
    assert not reg.home.root.exists()


def test_a_home_made_again_by_other_code_is_refused(reg):
    import shutil
    from transcript_labeling.auth import Auth
    from transcript_labeling.store import locked
    auth = Auth(reg.home)
    shutil.rmtree(reg.home.root)
    old = os.umask(0o022)  # the usual umask, so missing parents are made 0755
    try:
        with locked(reg.home.state_dir("p", "d") / ".tl.lock"):  # makes every missing parent
            pass
    finally:
        os.umask(old)
    assert reg.home.root.is_dir()
    with pytest.raises(SystemExit, match="mode 0o755, open to other users"):
        reg.create_project("p", privacy="local-only", writers=["o/r"])
    with pytest.raises(SystemExit, match="mode 0o755"):
        auth.mint_code("/")
    os.chmod(reg.home.root, 0o700)  # private, but still without its token
    with pytest.raises(SystemExit, match="has no token; run `tl init`"):
        reg.create_project("p", privacy="local-only", writers=["o/r"])
    with pytest.raises(SystemExit, match="has no token"):
        auth.mint_code("/")
    reg.home.init()
    assert reg.create_project("p", privacy="local-only", writers=["o/r"])
    assert auth.mint_code("/")


def test_a_home_open_to_the_group_or_not_a_folder_is_refused(reg, tmp_path):
    from transcript_labeling.home import Home
    os.chmod(reg.home.root, 0o750)  # the owner's group can hold other users of this Mac
    with pytest.raises(SystemExit, match="mode 0o750, open to other users"):
        reg.create_project("p", privacy="local-only", writers=["o/r"])
    (tmp_path / "file").write_text("x")
    with pytest.raises(SystemExit, match="is not a folder"):
        Registry(Home(tmp_path / "file")).create_project("p", privacy="local-only", writers=["o/r"])
    with pytest.raises(SystemExit, match="cannot be a folder: part of its path is a file"):
        Registry(Home(tmp_path / "file" / "h")).create_project("p", privacy="local-only", writers=["o/r"])
    with pytest.raises(SystemExit, match="cannot be made: part of its path is a file"):
        Home(tmp_path / "file" / "h").init()


def test_a_home_deleted_while_a_call_waits_for_its_lock_is_refused(reg, monkeypatch):
    import fcntl
    import shutil
    from transcript_labeling.auth import Auth
    auth = Auth(reg.home)
    real = fcntl.flock

    deleted = []

    def flock_then_delete(fd, op):  # another process deletes the home once while this one waits
        real(fd, op)
        if not op & fcntl.LOCK_UN and not deleted:
            deleted.append(1)
            shutil.rmtree(reg.home.root, ignore_errors=True)
    monkeypatch.setattr(fcntl, "flock", flock_then_delete)
    with pytest.raises(SystemExit, match="does not exist; run `tl init`"):
        reg.create_project("p", privacy="local-only", writers=["o/r"])
    assert not reg.home.root.exists()
    monkeypatch.setattr(fcntl, "flock", real)
    reg.home.init()
    auth = Auth(reg.home)
    deleted.clear()
    monkeypatch.setattr(fcntl, "flock", flock_then_delete)
    with pytest.raises(SystemExit, match="does not exist; run `tl init`"):
        auth.mint_code("/")
    assert not reg.home.root.exists()  # the session lock did not make the home again


def test_a_home_made_again_by_other_code_while_a_call_waits_is_refused_under_the_lock(reg, monkeypatch):
    # Only the check after the lock can see this: the home comes back open to others
    # and without its token (a dataset lock made its parents) while the call waits.
    import fcntl
    import shutil
    from transcript_labeling.auth import Auth
    auth = Auth(reg.home)
    real, swapped = fcntl.flock, []

    def flock_then_remake(fd, op):
        real(fd, op)
        if not op & fcntl.LOCK_UN and not swapped:
            swapped.append(1)
            shutil.rmtree(reg.home.root)
            old = os.umask(0o022)
            try:
                reg.home.root.mkdir()
            finally:
                os.umask(old)
    monkeypatch.setattr(fcntl, "flock", flock_then_remake)
    with pytest.raises(SystemExit, match="mode 0o755, open to other users"):
        reg.create_project("p", privacy="local-only", writers=["o/r"])
    assert not reg.home.registry_path.exists()
    monkeypatch.setattr(fcntl, "flock", real)
    shutil.rmtree(reg.home.root)
    reg.home.init()
    auth = Auth(reg.home)
    swapped.clear()
    monkeypatch.setattr(fcntl, "flock", flock_then_remake)
    with pytest.raises(SystemExit, match="mode 0o755, open to other users"):
        auth.mint_code("/")
    assert not reg.home.sessions_path.exists()


def test_a_registry_lock_file_replaced_while_waiting_is_taken_again(reg, monkeypatch):
    # The home was deleted and set up again by `tl init` while this call waited: the
    # lock it got is on a file nobody else will open, so it must lock the new one.
    import fcntl
    real, calls = fcntl.flock, []
    lock = reg.home.root / "registry.lock"

    def flock_then_replace(fd, op):
        real(fd, op)
        if not op & fcntl.LOCK_UN:
            calls.append(os.fstat(fd.fileno()).st_ino)
            if len(calls) == 1:
                lock.unlink()
                lock.write_text("")
    monkeypatch.setattr(fcntl, "flock", flock_then_replace)
    reg.create_project("p", privacy="local-only", writers=["o/r"])
    assert len(calls) == 2 and calls[1] == os.stat(lock).st_ino


def test_state_folders_are_private(tmp_path):
    import stat
    from transcript_labeling.home import private_write
    from transcript_labeling.store import EventLog, locked
    # A umask of 077 would make every folder 0700 whatever the code asks for.
    old = os.umask(0o022)
    try:
        with locked(tmp_path / "state" / "p" / "d" / ".tl.lock"):
            pass
        EventLog(tmp_path / "state2" / "events.jsonl").append("answer", "k", "operator", "browser", None)
        private_write(tmp_path / "secret" / "token", "x")
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "state" / "p" / "d").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "state2").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "secret").stat().st_mode) == 0o700


def test_a_folder_reached_through_another_spelling_of_the_checkout_is_refused(reg, tmp_path):
    # The commit path compares paths exactly, so such a folder's labels would never commit.
    import unicodedata
    co = tmp_path / unicodedata.normalize("NFD", "éé")
    (co / "logs").mkdir(parents=True)
    (co / "nfc").symlink_to(tmp_path / unicodedata.normalize("NFC", "éé") / "logs")
    (co / "upper").symlink_to(tmp_path / unicodedata.normalize("NFD", "ÉÉ") / "logs")
    reg.create_project("vi", privacy="git-private", writers=["o/data"],
                       git={"checkout": str(co), "branch": "main", "commit": False})
    for via in ("nfc/d", "upper/d"):
        with pytest.raises(SystemExit, match="spells .* differently"):
            reg.add_dataset("vi", "d", via)
    assert reg.add_dataset("vi", "d", "logs/d").name == "d"


def test_create_project_refuses_a_checkout_that_overlaps_another_projects(reg, tmp_path):
    # The check runs under the registry lock, so two creates racing on one checkout
    # cannot both pass; it used to run before the lock, in the CLI's project creation.
    import unicodedata
    co = tmp_path / unicodedata.normalize("NFC", "Café")
    (co / "inner").mkdir(parents=True)
    (tmp_path / "link").symlink_to(co)
    git = {"branch": "main", "commit": False}
    reg.create_project("vi", privacy="git-private", writers=["o/data"], git={**git, "checkout": str(co)})
    for overlap in (co, tmp_path / "link", co / "inner", tmp_path,
                    tmp_path / unicodedata.normalize("NFD", "café")):  # other case and Unicode form
        with pytest.raises(SystemExit, match="overlaps the checkout .* of project 'vi'"):
            reg.create_project("vi2", privacy="git-private", writers=["o/data"],
                               git={**git, "checkout": str(overlap)})
    assert sorted(reg.load()["projects"]) == ["vi"]
    assert reg.create_project("other", privacy="git-private", writers=["o/data"],
                              git={**git, "checkout": str(tmp_path / "Caféx")})


def test_a_rival_create_just_before_the_lock_is_seen_under_it(reg, tmp_path, monkeypatch):
    # The race the lock closes: another create on the same checkout lands between this
    # call's start and its lock. A check made before the lock would let both through.
    co = tmp_path / "co"
    co.mkdir()
    git = {"checkout": str(co), "branch": "main", "commit": False}
    real, fired = Registry.locked, []

    def rival_first(self):
        if not fired:
            fired.append(1)
            real_create(self, "rival", privacy="git-private", writers=["o/data"], git=git)
        return real(self)
    real_create = Registry.create_project
    monkeypatch.setattr(Registry, "locked", rival_first)
    with pytest.raises(SystemExit, match="overlaps the checkout .* of project 'rival'"):
        reg.create_project("mine", privacy="git-private", writers=["o/data"], git=git)
    assert sorted(reg.load()["projects"]) == ["rival"]


def test_create_project_refuses_a_checkout_that_overlaps_the_service_home(reg, tmp_path):
    git = {"branch": "main", "commit": False}
    home = reg.home.root
    (home / "data" / "inner").mkdir(parents=True)
    (tmp_path / "to-home").symlink_to(home)
    upper = home.parent.with_name(home.parent.name.upper())  # the same folder on the Mac's volume
    for overlap in (home, home.parent, home / "data" / "inner",  # is it, holds it, sits inside it
                    tmp_path / "to-home", upper):  # through a symlink, and in other letter case
        with pytest.raises(SystemExit, match="overlaps the service's home"):
            reg.create_project("vi", privacy="git-private", writers=["o/data"],
                               git={**git, "checkout": str(overlap)})
    assert reg.load()["projects"] == {}
    (home.parent / f"{home.name}-co").mkdir()  # a sibling whose name starts with the home's
    assert reg.create_project("vi", privacy="git-private", writers=["o/data"],
                              git={**git, "checkout": str(home.parent / f"{home.name}-co")})


def test_init_refuses_a_home_inside_a_git_work_tree(tmp_path):
    from transcript_labeling.home import Home
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    (tmp_path / "linked").mkdir()
    (tmp_path / "linked" / ".git").write_text("gitdir: /elsewhere\n")  # a linked worktree's .git file
    (tmp_path / "repo" / "a").mkdir()
    (tmp_path / "via").symlink_to(tmp_path / "repo" / "a")  # into the tree, below its top folder
    for home in (tmp_path / "repo" / ".tl", tmp_path / "repo" / "a" / "b", tmp_path / "linked" / "h",
                 tmp_path / "via" / "h", tmp_path / "repo"):
        with pytest.raises(SystemExit, match="is inside the git work tree"):
            Home(home).init()
        assert not home.exists() or home == tmp_path / "repo"
    assert Home(tmp_path / "plain" / "h").init()
    assert Home(tmp_path / "repo" / ".." / "plain2" / "h").init()  # leaves the tree through ..
