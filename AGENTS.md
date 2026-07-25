# Working on sqlalchemy-monetdb-adbc

This repository provides the SQLAlchemy dialect for `adbc-driver-monetdb`. It is a
pure-Python integration package: native protocol and Arrow behavior belong in the
driver, while SQLAlchemy compilation, connection adaptation, and reflection belong
here.

## Python

- Support Python 3.13 and newer.
- Use `uv` only, never `pip`, and commit `uv.lock`.
- Use full typing. Strict pyright, Ruff check/format, and pytest must pass.
- Do not add `pymonetdb` or `sqlalchemy-monetdb` as runtime dependencies.
- Do not add unnecessary comments, docstrings, or compatibility fallbacks.

## Connection and transaction boundaries

- `monetdb://` and `monetdb+adbc://` must create one ADBC DBAPI connection per
  SQLAlchemy connection.
- DDL, SQL execution, Arrow reads, and `adbc_ingest` participating in one unit of work
  must use that same physical connection. Do not introduce a second MonetDB session.
- Preserve SQLAlchemy's transaction contract while exposing a documented way to reach
  the raw ADBC connection for Arrow-native operations.
- The driver owns ADBC behavior and MonetDB wire semantics. Do not duplicate or patch
  those layers in this package.

## Testing

- Unit gates: `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run pyright`, and `uv run pytest`.
- Integration tests use the pinned native ARM64 MonetDB image documented by
  `adbc-driver-monetdb` and skip when no test URI is configured.
- Add regression tests for transaction ownership before changing connection or pool
  behavior.

## Packaging

- The distribution is `sqlalchemy-monetdb-adbc`, the import package is
  `sqlalchemy_monetdb_adbc`, and the SQLAlchemy entry points are `monetdb` and
  `monetdb.adbc`.
- Do not install this package alongside `sqlalchemy-monetdb`; both distributions
  register the bare `monetdb` entry point.
- Build both wheel and sdist with `uv build` and smoke-test the built wheel outside the
  repository before release.
- Keep the public repository self-contained. Do not commit private planning notes,
  benchmark results, credentials, or local infrastructure details.
