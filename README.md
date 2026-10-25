# transcript-labeling

An always-on transcript labeling service for one person's Mac, built so that
coding agents can start a labeling task on demand.

An agent creates a project and a dataset, uploads transcripts, and sends the
owner a login link. The owner labels in the browser: who said which words, what
was actually said, and any checklist questions. The agent waits for the owner to
finish and reads the labels back as JSON.

Status: not deployed yet. [docs/SPEC.md](docs/SPEC.md) says what the service
stores and promises, [docs/API.md](docs/API.md) lists the calls, `tl` and the
MCP tools, [docs/DEPLOY.md](docs/DEPLOY.md) is the install procedure, and
[docs/HISTORY.md](docs/HISTORY.md) records what was taken from verbatim-index.

## Development

```sh
uv run pytest
```

The browser check drives the real page in Chromium and is outside the default
run:

```sh
uv run --group browser playwright install chromium   # once
uv run --group browser pytest -m browser
```

Runtime dependencies: none. The service, the CLI and the MCP server use only the
Python standard library.

## License

MIT
