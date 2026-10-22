# transcript-labeling

An always-on transcript labeling service for one person's Mac, built so that
coding agents can start a labeling task on demand.

An agent creates a project and a dataset, uploads transcripts, and sends the
owner a login link. The owner labels in the browser: who said which words, what
was actually said, and any checklist questions. The agent waits for the owner to
finish and reads the labels back as JSON.

Status: under construction. The core labeling code is being ported from the
verbatim-index annotation tool; see `AGENTS.md`.

## Development

```sh
uv run pytest
```

Runtime dependencies: none. The service, the CLI and the MCP server use only the
Python standard library.

## License

MIT
