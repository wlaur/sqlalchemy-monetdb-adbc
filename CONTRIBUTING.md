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

## Review matrix

A passing happy-path test is not evidence for adjacent states. Reflection work
must cover `schema=None`, explicit and missing schemas; absent, empty and
non-empty filters; default, temporary and combined scopes; permanent/temporary
name collisions; and same-schema, cross-schema and self-referential foreign
keys.

Connection work must cover the default transaction mode and autocommit,
commit, rollback and pool reset, complete and partial result consumption, and
raw ADBC operations sharing the SQLAlchemy session. Packaging changes must
exercise every declared dialect entry point from the built wheel outside the
checkout.

Treat test names, documentation claims, support metadata and repository
settings as assertions that need executable gates. A dependency release must
be available from its public registry before this lockfile adopts it, and the
driver must run this dialect against its candidate wheel before publishing.
