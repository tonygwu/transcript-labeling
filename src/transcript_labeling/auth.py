"""Who may use the service: the CLI by bearer token, the browser by a one-time login link.

The bearer token in `$TL_HOME/token` (0600) is for the CLI and the MCP server. It
never reaches a browser.

The owner opens the page through a link `tl url` mints:
`http://127.0.0.1:<port>/login#code=<code>`. The code sits in the URL fragment, so
the browser never sends it in a request line, a Referer or a server log. The login
page's script posts it once to `/api/login`, receives a random session id, keeps it
in `localStorage` (scoped to this port), and sends it as `X-TL-Session` on every
fetch. A code is redeemable once within 7 days; a session lasts 30 days.

`sessions.json` (0600) stores only sha256 digests of codes and session ids, so a
reader of the file cannot log in with what it finds there.

Known limit: an agent running as the owner can read the token, mint a link and
redeem it. The service cannot tell that agent from the owner, so `checked_by` is
trustworthy against other macOS users and web pages, not against the owner's own
agents.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from .home import Home, private_write
from .store import locked, read_json_object

CODE_TTL = timedelta(days=7)
SESSION_TTL = timedelta(days=30)
#: A login target: a path on this server, in characters no browser reinterprets.
#: A backslash, tab or newline after the first slash would let a browser leave the site.
TARGET = re.compile(r"/(?!/)[A-Za-z0-9._~%/-]*(#[A-Za-z0-9._~%/-]*)?")


class AuthError(Exception):
    """A login code or session that is unknown, used or expired."""


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _now(now: datetime | None) -> datetime:
    if now is not None and now.tzinfo is None:
        raise ValueError("now must carry a time zone")
    return now or datetime.now(timezone.utc)


def _stamp(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


class Auth:
    def __init__(self, home: Home):
        home.require()
        self.home = home

    @contextmanager
    def _locked(self):
        # Checked on every use, not only at start: a home deleted while the server runs
        # must not be remade by the lock below. Checked again under the lock, because
        # the home may have gone while this call waited for it.
        self.home.require()
        with locked(self.home.root / "sessions.lock", make_parents=False):
            self.home.require()
            yield

    def _load(self) -> dict:
        path = self.home.sessions_path
        if not path.exists():
            return {"schema_version": 1, "codes": {}, "sessions": {}}
        data = read_json_object(path)
        if data.get("schema_version") != 1 or not isinstance(data.get("codes"), dict) \
                or not isinstance(data.get("sessions"), dict):
            raise SystemExit(f"REFUSING: {path} is not a version-1 sessions file")
        return data

    def _save(self, data: dict, now: datetime) -> None:
        # Drop what can never be used again, so the file does not grow without bound.
        data["codes"] = {k: v for k, v in data["codes"].items()
                         if datetime.fromisoformat(v["expires_at"]) > now - CODE_TTL}
        data["sessions"] = {k: v for k, v in data["sessions"].items()
                            if datetime.fromisoformat(v["expires_at"]) > now}
        private_write(self.home.sessions_path, json.dumps(data, indent=1, allow_nan=False) + "\n")

    def mint_code(self, target: str, now: datetime | None = None) -> str:
        """A one-time login code that opens `target` (a path such as `/p/demo/call-1/`)."""
        if not isinstance(target, str) or not TARGET.fullmatch(target):
            raise SystemExit(f"REFUSING: login target {target!r} must be a path on this server")
        now = _now(now)
        code = secrets.token_urlsafe(24)
        with self._locked():
            data = self._load()
            data["codes"][digest(code)] = {"target": target, "created_at": _stamp(now),
                                           "expires_at": _stamp(now + CODE_TTL), "used_at": None}
            self._save(data, now)
        return code

    def redeem(self, code: str, now: datetime | None = None) -> tuple[str, str]:
        """`(session id, target)` for an unused, unexpired code. Marks the code used."""
        now = _now(now)
        with self._locked():
            data = self._load()
            entry = data["codes"].get(digest(code)) if isinstance(code, str) and code else None
            if entry is None:
                raise AuthError("this login link is not known; ask for a new one (`tl url`)")
            if entry["used_at"] is not None:
                raise AuthError("this login link was already used")
            if datetime.fromisoformat(entry["expires_at"]) <= now:
                raise AuthError("this login link has expired; ask for a new one (`tl url`)")
            entry["used_at"] = _stamp(now)
            session = secrets.token_urlsafe(32)
            data["sessions"][digest(session)] = {"created_at": _stamp(now),
                                                 "expires_at": _stamp(now + SESSION_TTL)}
            self._save(data, now)
        return session, entry["target"]

    def code_target(self, code: str) -> str | None:
        """Where a code (used or not) points, so a used link can still say where it led."""
        if not isinstance(code, str) or not code:
            return None
        entry = self._load()["codes"].get(digest(code))
        return entry["target"] if entry else None

    def session_ok(self, session: str | None, now: datetime | None = None) -> bool:
        if not isinstance(session, str) or not session:
            return False
        entry = self._load()["sessions"].get(digest(session))
        return entry is not None and datetime.fromisoformat(entry["expires_at"]) > _now(now)


def token_ok(header: str | None, token: str) -> bool:
    """Whether an `Authorization` header carries the bearer token. Constant-time.

    An empty or short token is a broken server, never a token every request matches.
    """
    if not isinstance(token, str) or len(token) < 32:
        raise ValueError("the bearer token is missing or too short")
    if not header or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[len("Bearer "):].strip().encode(), token.encode())
