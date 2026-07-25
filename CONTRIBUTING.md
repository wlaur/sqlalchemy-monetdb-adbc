# Contributing

Create a branch from `main`, keep changes focused, and include tests for behavior
changes.

Set up the environment and run every local gate:

```console
uv sync --all-groups
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
uv build
```

Integration work must use the MonetDB image and connection rules documented in
`AGENTS.md`. Do not run live tests against an unrelated development container.
