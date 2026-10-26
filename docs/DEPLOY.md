# Deploy

Status: written, not yet run. None of these steps has been carried out on this
Mac. The first deployment needs the owner's go-ahead, because it creates the
public GitHub repository and installs a service that runs all the time.

## Where things live

- `~/Code/tools/labeling/transcript-labeling/` is a container of checkouts.
  `repo-0` and its siblings are working checkouts for agent sessions.
  `repo-prod` alone owns the live install. Never run `uv tool install` from a
  working checkout: it replaces the `tl` that every agent and the running
  service use.
- `$TL_HOME` (default `~/.local/share/transcript-labeling`, mode 0700) holds the
  config, the bearer token, the registry, local-only data, state, exports and
  logs.
- `repo-prod` is always at a release tag, never at a branch tip. A release is a
  tag on `main`, and each release raises `__version__` in
  `src/transcript_labeling/__init__.py`, because `tl doctor` finds a stale server
  by comparing versions.
- The launchd agent is `local.transcript-labeling.server`, in
  `~/Library/LaunchAgents/local.transcript-labeling.server.plist`.

## First install

1. Create the GitHub repository (public), push `main`, and tag `v0.1.0`.
2. Clone `repo-prod` next to `repo-0` and check out the release tag
   (`git checkout v0.1.0`).
3. In `repo-prod`, outside `uv run`: `uv tool install --force .`. Then check
   that `command -v tl` prints `~/.local/bin/tl` and that `tl --version`
   prints the version you meant to install.
4. `tl init`. This makes `$TL_HOME`, its config and its token.
5. Before the service starts for the first time, verbatim-index's judge
   sandboxes must already deny port 8790 and `$TL_HOME`, so a judged model
   cannot reach the labels.
6. `ops/install-launchd.sh --print` and read the plist. It runs `tl serve` by
   absolute path, because launchd's `PATH` lacks `~/.local/bin`, and it refuses
   a `tl` whose real path is in a `.venv` or `venv` folder or a git checkout
   (what `uv run` puts first on `PATH`). Then
   `ops/install-launchd.sh --install`.
7. Check, and keep the output:
   - `tl doctor` exits 0 and reports the installed version.
   - `curl -s http://127.0.0.1:8790/healthz` answers with that version.
   - `curl -s -o /dev/null -w '%{http_code}' -H 'Host: evil.example' http://127.0.0.1:8790/healthz`
     prints 421.
   - `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8790/api/v1/projects`
     prints 401.
   - Kill the server's process; `launchctl print gui/$(id -u)/local.transcript-labeling.server`
     shows a new PID within about 10 seconds.

## Codex

Back up `~/.codex/config.toml`, then add:

```toml
[mcp_servers.tl]
command = "/Users/<you>/.local/bin/tl"
args = ["mcp"]
```

If the service uses a `TL_HOME` other than the default, add
`env = { TL_HOME = "<that path>" }` to the table. A second Codex home whose
`config.toml` is a symlink to this file gets the same table. Codex judges run with
`--ignore-user-config`, so they never see the server. Measure Codex's per-tool
timeout once with a real `wait` call: it must be longer than the 50 seconds one
`wait` call can take, plus a status call.

## Upgrade

```sh
cd ~/Code/tools/labeling/transcript-labeling/repo-prod
git fetch --tags
git checkout v0.1.1          # the new release tag
uv tool install --force --reinstall .
tl --version                 # must print the new version
launchctl kickstart -k gui/$(id -u)/local.transcript-labeling.server
tl doctor
```

`--reinstall` is needed: without it, `uv tool install --force` can reuse its
cached build of the same path and install the old version again, and then
`tl doctor` passes because the server and the install agree on the old
version. `tl doctor` fails while the running service's version differs from
the installed one.

## Remove

```sh
launchctl bootout gui/$(id -u)/local.transcript-labeling.server
rm ~/Library/LaunchAgents/local.transcript-labeling.server.plist
```

This leaves `$TL_HOME` and its labels in place.

## Logs

The server writes `$TL_HOME/logs/server.log` itself and rotates it at 5 MB,
keeping 3 older files. It holds one line per POST and per refused request, each
commit result, and tracebacks; never transcript text. launchd's own output goes
to `logs/launchd.log`. It holds what is written before the server takes over its
log, and one line for each write that `server.log` could not take later, such as
when that file is read-only or its folder was deleted; the text of such a write is
lost. The server moves `launchd.log` aside at start once it passes 5 MB. If
`server.log` stops growing, look in `launchd.log`.

## Known gaps

- A crash that happens before the server takes over its log, at every restart,
  grows `launchd.log` by one traceback every 10 seconds until someone notices.
- While `server.log` cannot be written, every log write adds a line or two to
  `launchd.log` for as long as the server runs, because it is moved aside only at
  start.
- The event log (`state/<project>/<dataset>/events.jsonl`) is not rotated.
  `tl status` reports its line count.
- Local-only data has no backup beyond Time Machine.
