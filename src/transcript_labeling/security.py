"""Request checks that keep web pages and other machines out of the service.

  Host     must be a loopback name for this port, or one listed in config, so a
           DNS-rebinding page cannot reach the server under its own name.
  Origin   a POST from a browser must come from this server's own origin.
  Type     a POST must be `application/json`, which a cross-site page cannot send
           without a preflight this server never answers.
  Headers  no CORS headers, and a Content-Security-Policy that allows only this
           origin's scripts and connections and no framing.
"""
from __future__ import annotations

CSP = "default-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'"
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
LOOPBACK_NAMES = ("127.0.0.1", "localhost", "[::1]")


def allowed_hosts(port: int, extra: list[str]) -> set[str]:
    return {f"{name}:{port}" for name in LOOPBACK_NAMES} | {h.lower() for h in extra}


def host_ok(host: str | None, port: int, extra: list[str]) -> bool:
    return bool(host) and host.lower() in allowed_hosts(port, extra)


def origin_ok(origin: str | None, host: str | None, https_hosts: list[str] = ()) -> bool:
    """A missing Origin (a CLI client) is fine; a present one must be this server's.

    A host listed in config is reached over HTTPS (through a tunnel), so its origin is
    `https://`; the loopback names are plain `http://`.
    """
    if origin is None:
        return True
    if not host:
        return False
    scheme = "https" if host.lower() in {h.lower() for h in https_hosts} else "http"
    return origin.lower() == f"{scheme}://{host.lower()}"


def json_type_ok(content_type: str | None) -> bool:
    return (content_type or "").split(";")[0].strip().lower() == "application/json"
