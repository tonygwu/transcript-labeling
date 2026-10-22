"""The registry: every project, where its datasets live, and who may write to it.

    registry.json  {"schema_version": 1, "projects": {<name>: {
        title, privacy ("local-only" | "git-private"), writers: [<repo>...], created_at,
        git: {checkout, branch, commit, idle_s, max_age_s},   # git-private only
        datasets: {<dataset>: "<path relative to the project folder or checkout root>"}}}}

A `local-only` project always lives under `$TL_HOME/data/<project>/`, so one
sandbox deny on `$TL_HOME` covers it. A `git-private` project names a private git
checkout, and each dataset names its folder inside it.

`writers` lists the repositories allowed to write to a project. The check guards
against mistakes, such as an agent in one repository writing into another's
project. It is not authentication: a caller can name any repository.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .home import Home
from .store import locked, read_json_object, write_atomic

NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
PRIVACY = ("local-only", "git-private")
SCHEMA_VERSION = 1
DEFAULT_IDLE_S = 300
DEFAULT_MAX_AGE_S = 1800


def check_name(kind: str, name: str) -> str:
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise SystemExit(f"REFUSING: {kind} name {name!r} must match {NAME.pattern}")
    return name


def check_relpath(rel: str) -> str:
    """A dataset path inside its project folder: relative, no `..`, no empty parts, not in `.git`."""
    p = PurePosixPath(rel) if isinstance(rel, str) else None
    if not rel or p is None or p.is_absolute() or any(part in ("", ".", "..") for part in rel.split("/")):
        raise SystemExit(f"REFUSING: dataset path {rel!r} must be relative to the project folder, without `..`")
    if ".git" in name_key(rel).split("/"):  # .GIT is the same folder on the Mac's filesystem
        raise SystemExit(f"REFUSING: dataset path {rel!r} is inside .git")
    return str(p)


def name_key(path: str) -> str:
    """`path` as the Mac's default filesystem (APFS) compares names: without letter case,
    and with é written as one character or as e plus an accent counted as the same.
    On a case-sensitive volume this makes the overlap and .git checks refuse a little
    more; `add_dataset` also requires a dataset folder to sit inside its checkout as
    spelled, so no volume lets one lead outside."""
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", path).casefold())


def nests(a: str, b: str) -> bool:
    """Whether one dataset folder would sit inside the other (or be the same folder)."""
    pa, pb = name_key(a).split("/"), name_key(b).split("/")
    n = min(len(pa), len(pb))
    return pa[:n] == pb[:n]


class Registry:
    def __init__(self, home: Home):
        self.home = home

    @contextlib.contextmanager
    def locked(self):
        """Hold the registry lock while reading, changing and writing it.

        A home that is gone is refused, never made again: `tl init` makes it private
        (0700) with its token, and a silently remade one would be neither.
        """
        self.home.require()
        with locked(self.home.root / "registry.lock", make_parents=False):
            # Again under the lock: the home may have gone while this call waited for it.
            self.home.require()
            yield

    def load(self) -> dict:
        path = self.home.registry_path
        if not path.exists():
            return {"schema_version": SCHEMA_VERSION, "projects": {}}
        reg = read_json_object(path)
        if reg.get("schema_version") != SCHEMA_VERSION or not isinstance(reg.get("projects"), dict):
            raise SystemExit(f"REFUSING: {path} is not a version-{SCHEMA_VERSION} registry")
        return reg

    def _save(self, reg: dict) -> None:
        write_atomic(self.home.registry_path, json.dumps(reg, indent=1, ensure_ascii=False) + "\n")

    def project(self, name: str) -> dict:
        projects = self.load()["projects"]
        if name not in projects:
            raise SystemExit(f"REFUSING: no project {name!r}; known: {sorted(projects)}")
        return projects[name]

    def create_project(self, name: str, *, privacy: str, writers: list[str], title: str = "",
                       git: dict | None = None, now: str | None = None) -> dict:
        check_name("project", name)
        if privacy not in PRIVACY:
            raise SystemExit(f"REFUSING: privacy {privacy!r} must be one of {PRIVACY}")
        if (privacy == "git-private") != (git is not None):
            raise SystemExit("REFUSING: a git-private project needs its git settings, and only it may have them")
        if git is not None:
            missing = [k for k in ("checkout", "branch", "commit") if k not in git]
            if missing or not Path(git["checkout"]).is_absolute():
                raise SystemExit(f"REFUSING: git settings need an absolute checkout, a branch and commit; missing {missing}")
            git = {"idle_s": DEFAULT_IDLE_S, "max_age_s": DEFAULT_MAX_AGE_S, **git}
        if not writers or not all(isinstance(w, str) and w for w in writers):
            raise SystemExit("REFUSING: a project needs at least one writer repository")
        with self.locked():
            reg = self.load()
            if name in reg["projects"]:
                raise SystemExit(f"REFUSING: project {name!r} already exists")
            if git is not None:
                # Under the lock, so two creates racing on one checkout cannot both pass,
                # and by real folder compared as the filesystem compares names.
                folder = os.path.realpath(git["checkout"])
                for other, other_entry in reg["projects"].items():
                    taken = other_entry.get("git", {}).get("checkout")
                    if taken and nests(folder, os.path.realpath(taken)):
                        raise SystemExit(f"REFUSING: {git['checkout']} overlaps the checkout {taken} of project "
                                         f"{other!r}; add datasets there")
                # The token, the registry and the state must stay outside every work tree
                # that pushes, so a `git add -A` there can never stage them.
                if nests(folder, os.path.realpath(self.home.root)):
                    raise SystemExit(f"REFUSING: {git['checkout']} overlaps the service's home {self.home.root}; "
                                     "a git-private checkout must be outside it, and it outside the checkout")
            entry = {"title": title or name, "privacy": privacy, "writers": sorted(set(writers)),
                     "created_at": now or datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     **({"git": git} if git else {}), "datasets": {}}
            reg["projects"][name] = entry
            self._save(reg)
        if privacy == "local-only":
            self.home.data_dir(name).mkdir(parents=True, exist_ok=True, mode=0o700)
        return entry

    def project_root(self, name: str) -> Path:
        """The folder a project's dataset paths are relative to."""
        p = self.project(name)
        return Path(p["git"]["checkout"]) if p["privacy"] == "git-private" else self.home.data_dir(name)

    def add_dataset(self, project: str, dataset: str, rel: str | None = None) -> Path:
        """Register `dataset` in `project` and return its folder. Refuses a name already taken."""
        check_name("dataset", dataset)
        with self.locked():
            reg = self.load()
            if project not in reg["projects"]:
                raise SystemExit(f"REFUSING: no project {project!r}")
            entry = reg["projects"][project]
            if dataset in entry["datasets"]:
                raise SystemExit(f"REFUSING: dataset {project}/{dataset} already exists")
            if entry["privacy"] == "local-only":
                if rel not in (None, dataset):
                    raise SystemExit("REFUSING: a local-only dataset always lives at $TL_HOME/data/<project>/<dataset>")
                rel = dataset
            elif rel is None:
                raise SystemExit("REFUSING: a git-private dataset needs --path, its folder inside the checkout")
            rel = check_relpath(rel)
            root = self._root_of(project, entry)
            # The real folder, so a symlink inside the checkout cannot lead out of it,
            # into .git, or onto another dataset.
            folder = os.path.realpath(root / rel)
            # Compared folder by folder: the two may spell one name with a different
            # number of characters (é as one character or two), so slicing one by the
            # other's length can cut a name in half.
            root_parts, parts = name_key(str(root)).split("/"), name_key(folder).split("/")
            if parts[:len(root_parts)] != root_parts or len(parts) <= len(root_parts):
                raise SystemExit(f"REFUSING: dataset path {rel!r} leads out of {root} through a symlink")
            if ".git" in parts[len(root_parts):]:
                raise SystemExit(f"REFUSING: dataset path {rel!r} leads into .git through a symlink")
            # Exact containment, for every project: on a case-sensitive volume a path that
            # differs only in letter case can be a different folder, and the commit path
            # checks containment exactly, so a folder reached through another spelling of the
            # checkout's path (é in the other form, other letter case) would save labels that
            # are never committed.
            if not Path(folder).is_relative_to(root):
                raise SystemExit(f"REFUSING: dataset path {rel!r} reaches {folder}, which spells {root} "
                                 f"differently; name a folder inside the checkout as it is spelled")
            if entry["privacy"] == "git-private" and nests(folder, os.path.realpath(self.home.root)):
                raise SystemExit(f"REFUSING: dataset path {rel!r} overlaps the service's home {self.home.root}")
            # Every dataset of every project, compared by real folder: two git-private
            # projects may name one checkout, or one checkout inside another.
            for other_project, other_entry in reg["projects"].items():
                other_root = self._root_of(other_project, other_entry)
                for other, path in other_entry["datasets"].items():
                    if nests(folder, os.path.realpath(other_root / path)):
                        raise SystemExit(f"REFUSING: {rel} would overlap dataset {other_project}/{other} at {path}")
            entry["datasets"][dataset] = rel
            self._save(reg)
        return self.dataset_dir(project, dataset)

    def _root_of(self, name: str, entry: dict) -> Path:
        """A project's folder, resolved, so two spellings of one checkout compare equal."""
        if entry["privacy"] == "git-private":
            return Path(os.path.realpath(entry["git"]["checkout"]))
        return Path(os.path.realpath(self.home.data_dir(name)))

    def dataset_dir(self, project: str, dataset: str) -> Path:
        p = self.project(project)
        if dataset not in p["datasets"]:
            raise SystemExit(f"REFUSING: no dataset {project}/{dataset}; known: {sorted(p['datasets'])}")
        return self.project_root(project) / p["datasets"][dataset]

    def check_writer(self, project: str, repo: str | None, owner_override: bool = False) -> None:
        """Refuse a write from a repository the project does not list. A guard against mistakes only."""
        if owner_override:
            return
        writers = self.project(project)["writers"]
        if repo not in writers:
            raise SystemExit(f"REFUSING: repository {repo!r} is not a writer of project {project!r} "
                             f"(writers: {writers}); pass --owner-override if the owner asked for this")

    def datasets(self) -> list[tuple[str, str]]:
        return sorted((p, d) for p, e in self.load()["projects"].items() for d in e["datasets"])
