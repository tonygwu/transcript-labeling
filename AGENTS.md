# Notes for coding agents working in this repo

This repository is the transcript labeling service: one always-on server on the
owner's Mac where coding agents create projects and datasets, push transcripts,
send the owner a login link, and read the labels back. It was extracted from the
annotation tool in verbatim-index (`scripts/annotations*.py`) at verbatim commit
`d5a3a8bf`.

`CLAUDE.md` is a symlink to this file. Edit `AGENTS.md`.

## The repository is public

It holds code and synthetic fixtures only.

- **No transcript, answers file or export ever enters this repository.** Real
  labeling data lives in `$TL_HOME` (default `~/.local/share/transcript-labeling`)
  or in a private data checkout that a project names. `.gitignore` blocks the
  common file names, but it is a second line of defence, not the rule.
- **Fixtures are synthetic.** No real person's name, no private project name,
  no real recording id. Invent them (`Jane Roe`, `talk-1`).
- Commit as `446441+tonygwu@users.noreply.github.com`. GitHub rejects pushes that
  carry another address for this account.

## The gate

```sh
uv run pytest
```

Run the gate before every commit and quote its output in the commit's review.
The page tests run the page's script under `node`, so the gate needs `node` on
PATH; without it those tests fail with the install command, never skip.
`.python-version` pins 3.12, the oldest version the package supports and the one
verbatim-index runs, so the gate catches code that only works on a newer Python.

## Runtime rules

- **Standard library only at runtime.** `dependencies = []` in `pyproject.toml`
  is checked by `tests/test_package.py`. Test-only tools go in the `dev` dependency group.
- Python 3.12 or newer.

## Checkouts and the live install

`~/Code/tools/labeling/transcript-labeling/` is a container. `repo-0` and its
siblings are working checkouts for agent sessions; `repo-prod`, once it exists,
alone owns the live install of `tl` and the launchd service. `uv tool install`
from a working checkout replaces the binary every agent and the running service
use, so run it only in `repo-prod`.
