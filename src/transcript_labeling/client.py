"""The `tl` CLI's and the MCP server's connection to the running service.

Every call goes to `http://127.0.0.1:<port>/api/v1/...` with the bearer token from
`$TL_HOME/token`. `client` names the caller in `X-TL-Client`: the service refuses
an `mcp` caller any write to a git-private project.

A service that does not answer, and every refusal, stops the caller with the
service's own message; nothing is retried or guessed.
"""
from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from urllib.parse import urlencode

from .home import Home


class ServiceError(SystemExit):
    """The service refused a call. `status` is its HTTP status; the exit message is the refusal."""

    def __init__(self, status: int, message: str):
        super().__init__(f"REFUSING ({status}): {message.removeprefix('REFUSING: ')}")
        self.status = status


class ServiceClient:
    def __init__(self, home: Home, client: str = "cli", timeout: float = 60.0):
        cfg = home.config()
        self.port = cfg["port"]
        self.base = f"http://127.0.0.1:{self.port}"
        self.token = home.token()
        self.client = client
        self.timeout = timeout

    def get(self, route: str, **query) -> dict:
        q = {k: v for k, v in query.items() if v is not None}
        return self._call(urllib.request.Request(f"{self.base}/api/v1/{route}" + (f"?{urlencode(q)}" if q else ""),
                                                 method="GET", headers=self._headers()))

    def post(self, route: str, body: dict) -> dict:
        try:
            data = json.dumps(body, allow_nan=False).encode()
        except ValueError:  # the service would refuse it, and no stored file may hold it
            raise SystemExit("REFUSING: the request holds NaN or Infinity, which JSON does not allow") from None
        return self._call(urllib.request.Request(f"{self.base}/api/v1/{route}", data=data,
                                                 method="POST",
                                                 headers={**self._headers(), "Content-Type": "application/json"}))

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "X-TL-Client": self.client}

    def _call(self, req: urllib.request.Request) -> dict:
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                message = json.loads(raw).get("error") or raw.decode(errors="replace")
            except ValueError:
                message = raw.decode(errors="replace")[:300]
            raise ServiceError(exc.code, message)
        except urllib.error.URLError as exc:
            raise SystemExit(f"REFUSING: the labeling service is not answering on 127.0.0.1:{self.port} "
                             f"({exc.reason}); run `tl doctor`. It runs under launchd, so do not start it yourself.")
        except (OSError, http.client.HTTPException) as exc:  # dropped, reset or timed out mid-call
            raise SystemExit(f"REFUSING: the labeling service on 127.0.0.1:{self.port} broke off the call "
                             f"({type(exc).__name__}: {exc}); run `tl doctor` and try again.")
        except ValueError as exc:  # a 200 whose body is not JSON
            raise SystemExit(f"REFUSING: the labeling service on 127.0.0.1:{self.port} sent a reply that is not "
                             f"JSON ({exc}); run `tl doctor`.")


def healthz(home: Home, timeout: float = 5.0) -> dict | None:
    """The running service's `/healthz`, or None when nothing answers on the configured port."""
    url = f"http://127.0.0.1:{home.config()['port']}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None
