"""The service's home folder, `$TL_HOME`: config, token, registry, data, state, logs.

    $TL_HOME/
      config.json    {port, operator_id, allowed_hosts, access}
      token          0600; the bearer token for the CLI and MCP server, never sent to a browser
      registry.json  projects and their datasets (`registry.py`)
      sessions.json  0600; browser login codes and sessions (`auth.py`)
      data/<project>/<dataset>/     local-only datasets
      state/<project>/<dataset>/    events.jsonl, .tl.lock, staging folders
      exports/<project>/<dataset>/  default export target for local-only projects
      logs/                         server logs; never transcript text, tokens or session ids

The folder is 0700 and the token and session files 0600, so other macOS users on
the machine cannot read them. One sandbox deny on this folder covers every
local-only transcript, the token, the logs and the state.
"""
from __future__ import annotations

import json
import os
import secrets
import stat
from pathlib import Path


DEFAULT_HOME = "~/.local/share/transcript-labeling"
DEFAULT_PORT = 8790
CONFIG_KEYS = ("port", "operator_id", "allowed_hosts", "access")


def home_path(environ=None) -> Path:
    """`$TL_HOME`, or the default home folder when it is unset or empty."""
    env = os.environ if environ is None else environ
    return Path(env.get("TL_HOME") or DEFAULT_HOME).expanduser()


def private_write(path: Path, text: str) -> None:
    """Write `path` atomically, readable by its owner only from the first byte."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class Home:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else home_path()

    # ------------------------------------------------------------ paths
    @property
    def config_path(self) -> Path:
        return self.root / "config.json"

    @property
    def token_path(self) -> Path:
        return self.root / "token"

    @property
    def registry_path(self) -> Path:
        return self.root / "registry.json"

    @property
    def sessions_path(self) -> Path:
        return self.root / "sessions.json"

    def data_dir(self, project: str) -> Path:
        return self.root / "data" / project

    def state_dir(self, project: str, dataset: str) -> Path:
        return self.root / "state" / project / dataset

    def exports_dir(self, project: str, dataset: str) -> Path:
        return self.root / "exports" / project / dataset

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    def require(self) -> None:
        """Refuse a home that `tl init` did not make: missing, not a folder, open to its
        group or to others, or without its token.

        Checking only that the folder exists is not enough. Code that makes missing
        parent folders (a lock, a log) would make a deleted home again, with the default
        mode and no token, and every later check would pass.
        """
        try:
            st = self.root.stat()
        except (FileNotFoundError, NotADirectoryError):
            raise SystemExit(f"REFUSING: {self.root} does not exist; run `tl init`") from None
        if not stat.S_ISDIR(st.st_mode):
            raise SystemExit(f"REFUSING: {self.root} is not a folder")
        mode = stat.S_IMODE(st.st_mode)
        if mode & 0o077:
            raise SystemExit(f"REFUSING: {self.root} is mode {oct(mode)}, open to other users; it must be "
                             f"0700. If it was deleted and made again, run `tl init`")
        if not self.token_path.is_file():
            raise SystemExit(f"REFUSING: {self.root} has no token; run `tl init`")

    # ------------------------------------------------------------ setup
    def init(self, port: int = DEFAULT_PORT, operator_id: str = "operator") -> list[str]:
        """Create the folder, config and token if missing. Returns what it created."""
        made = []
        for d in (self.root, self.root / "data", self.root / "state", self.root / "exports", self.logs_dir):
            if not d.exists():
                d.mkdir(parents=True, mode=0o700)
                made.append(str(d))
        os.chmod(self.root, 0o700)
        if not self.config_path.exists():
            private_write(self.config_path, json.dumps(
                {"port": port, "operator_id": operator_id, "allowed_hosts": [], "access": {}}, indent=1) + "\n")
            made.append(str(self.config_path))
        if not self.token_path.exists():
            private_write(self.token_path, secrets.token_urlsafe(32) + "\n")
            made.append(str(self.token_path))
        return made

    def config(self) -> dict:
        """The config, every key present. A missing file or key stops the caller."""
        if not self.config_path.exists():
            raise SystemExit(f"REFUSING: {self.config_path} does not exist; run `tl init`")
        cfg = json.loads(self.config_path.read_text())
        missing = [k for k in CONFIG_KEYS if k not in cfg]
        if missing:
            raise SystemExit(f"REFUSING: {self.config_path} has no {', '.join(missing)}")
        if type(cfg["port"]) is not int or not isinstance(cfg["allowed_hosts"], list):
            raise SystemExit(f"REFUSING: {self.config_path}: port must be an integer and allowed_hosts a list")
        return cfg

    def token(self) -> str:
        """The bearer token. A token file other users could read stops the caller."""
        if not self.token_path.exists():
            raise SystemExit(f"REFUSING: {self.token_path} does not exist; run `tl init`")
        mode = stat.S_IMODE(self.token_path.stat().st_mode)
        if mode & 0o077:
            raise SystemExit(f"REFUSING: {self.token_path} is mode {oct(mode)}; it must be 0600")
        token = self.token_path.read_text().strip()
        if len(token) < 32:
            raise SystemExit(f"REFUSING: {self.token_path} holds no usable token")
        return token
