# sqlalchemy-monetdb-adbc

A SQLAlchemy dialect for MonetDB backed by
[`adbc-driver-monetdb`](https://github.com/wlaur/adbc-driver-monetdb).

The package is pure Python. It gives SQLAlchemy and Arrow-native ADBC operations a
single MonetDB connection and transaction boundary, without depending on `pymonetdb`
or `sqlalchemy-monetdb`.

## Status

The repository currently contains the installable package and connection bootstrap.
Compiler, reflection, transaction, and live-server coverage are under development; it
is not ready for a PyPI release yet.

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

## License

MIT
