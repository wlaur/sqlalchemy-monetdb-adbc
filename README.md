# sqlalchemy-monetdb-adbc

A SQLAlchemy dialect for MonetDB backed by
[`adbc-driver-monetdb`](https://github.com/wlaur/adbc-driver-monetdb).

The package is pure Python. It gives SQLAlchemy and Arrow-native ADBC operations a
single MonetDB connection and transaction boundary, without depending on `pymonetdb`
or `sqlalchemy-monetdb`.

## Status

The dialect covers SQL compilation, types, reflection, transactions, and the ORM,
and is validated against a live MonetDB server. It is not released to PyPI yet.

### Dialect compliance suite

SQLAlchemy's own dialect suite runs from `suite/` (see Development). 1052 of its
tests pass; the known failures are tracked and mostly cluster in reflection
edge cases, `RETURNING` variants, CTEs, and numeric precision.

Two behaviors are limited by `adbc-driver-monetdb` rather than by MonetDB, and
are marked `DRIVER-WORKAROUND` in the source:

- `CursorResult.rowcount` is not reliable for single-statement DML, so
  `supports_sane_rowcount` is off.
- `LargeBinary` binds through a dialect-specific processor, because the DB-API
  module does not export the PEP 249 `Binary` constructor.

### MonetDB behaviors worth knowing

- A stock login lands in the `sys` schema, where system views already occupy
  ordinary table names such as `users` and `columns`. Create and use a schema of
  your own.
- Self-referential foreign keys are added by `ALTER TABLE` after the table
  exists, because MonetDB cannot declare them inline.
- Indexes carry no ordering, so `ASC`/`DESC` on an index expression is dropped.
- A `UNIQUE` constraint is backed by an index of the same name, so it is
  reflected both by `get_unique_constraints()` and by `get_indexes()`.

## Development installation

```console
uv add git+https://github.com/wlaur/sqlalchemy-monetdb-adbc
```

## Usage

Existing SQLAlchemy MonetDB URLs work unchanged:

```python
from sqlalchemy import create_engine

engine = create_engine(
    "monetdb://monetdb:monetdb@localhost:50000/demo",
)

with engine.connect() as connection:
    rows = connection.exec_driver_sql("SELECT 1").all()
```

The explicit `monetdb+adbc://` form resolves to the same dialect. Do not install
`sqlalchemy-monetdb-adbc` and `sqlalchemy-monetdb` in the same environment: both
packages register the bare `monetdb://` SQLAlchemy entry point.

TLS URLs work with either the unchanged `monetdbs://` form or the explicit
`monetdbs+adbc://` form. Both preserve the secure driver URI.

The driver accepts the same URI query options after the database name:

```python
engine = create_engine(
    "monetdb://monetdb:monetdb@localhost:50000/demo?client_application=my_app",
)
```

## Development

Python 3.13 or newer and `uv` are required.

```console
uv sync --all-groups
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
uv build
```

Integration tests and the dialect compliance suite need a server. `compose.yaml`
pins the same native ARM64 MonetDB image the driver is developed against:

```console
docker compose up -d
MONETDB_TEST_URI=monetdb://monetdb:monetdb@localhost:50001/test uv run pytest tests
uv run pytest suite -o addopts=""
docker compose down -v
```

`uv run pytest` alone skips every test that needs a server, so the unit gate
runs without one.

## License

MIT
