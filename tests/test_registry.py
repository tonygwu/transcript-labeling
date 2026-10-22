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


@pytest.mark.parametrize("kwargs,expect", [
    (dict(name="Bad Name", privacy="local-only", writers=["r"]), "must match"),
    (dict(name="ok", privacy="public", writers=["r"]), "privacy"),
    (dict(name="ok", privacy="local-only", writers=[]), "at least one writer"),
    (dict(name="ok", privacy="git-private", writers=["r"]), "needs its git settings"),
    (dict(name="ok", privacy="git-private", writers=["r"], git={"checkout": "rel", "branch": "m", "commit": False}),
     "absolute checkout"),
])
def test_project_creation_refusals(reg, kwargs, expect):
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
