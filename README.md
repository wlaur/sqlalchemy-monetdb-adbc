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

SQLAlchemy's own dialect suite runs from `suite/` (see Development). 1127 of its
tests pass; the known failures cluster in multi-table reflection
(`get_multi_*`), and in a few places where MonetDB will not infer a type for a
bare parameter, such as `WHERE ? = ?` or `LIMIT 1 + 2`.

Common table expressions are fully supported, including recursive CTEs, CTEs
over `VALUES`, and CTEs driving `UPDATE`/`DELETE`. MonetDB's `WITH` accepts only
`SELECT` or `VALUES`, so a CTE cannot itself be an `INSERT`/`UPDATE`/`DELETE`.

The dialect requires `adbc-driver-monetdb` 0.8.2 or newer, which reports
truthful row counts and exports the PEP 249 `Binary` constructor. One
`DRIVER-WORKAROUND` remains in the source, for an upstream Apache arrow-adbc
behavior: ADBC always returns an Arrow stream, so the DB-API layer reports an
empty `description` rather than `None` for statements that produce no result
set, and SQLAlchemy needs `None` to decide that a statement returned no rows.

### MonetDB behaviors worth knowing

- A stock login lands in the `sys` schema, where system views already occupy
  ordinary table names such as `users` and `columns`. Create and use a schema of
  your own.
- Self-referential foreign keys are added by `ALTER TABLE` after the table
  exists, because MonetDB cannot declare them inline. MonetDB then enforces
  them one statement at a time rather than at statement end, so on such a table
  `DELETE FROM t` and `TRUNCATE t` are both rejected, and a multi-row `INSERT`
  cannot reference a row added by the same statement. Declare the foreign key
  with `ondelete="CASCADE"` if you need bulk deletes, or clear the referencing
  column first:

  ```python
  connection.execute(update(tree).values(parent_id=None))
  connection.execute(delete(tree))
  ```
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

## JSON

A `JSON` column round-trips Python objects, as SQLAlchemy specifies: the driver
returns the document as a string and SQLAlchemy deserializes it, honouring
`json_serializer` and `json_deserializer` on `create_engine`.

```python
engine = create_engine("monetdb://...", json_deserializer=orjson.loads)
```

MonetDB validates and normalizes JSON on input, so a round-tripped document
keeps its values but not its original whitespace or key order.

### Parsing straight into a model

Because the driver hands back the raw JSON text, a type can skip the
intermediate dict entirely and hand that text to a parser that reads JSON
directly, such as `pydantic.BaseModel.model_validate_json`. Override
`bind_processor`/`result_processor` rather than
`process_bind_param`/`process_result_value`, so the underlying `JSON` codecs
never run:

```python
class PydanticJSON(TypeDecorator):
    impl = JSON
    cache_ok = True

    def __init__(self, model, *args, **kw):
        self.model = model
        super().__init__(*args, **kw)

    def bind_processor(self, dialect):
        def process(value):
            return None if value is None else value.model_dump_json()

        return process

    def result_processor(self, dialect, coltype):
        model = self.model

        def process(value):
            return None if value is None else model.model_validate_json(value)

        return process
```

The column is still `JSON` in MonetDB. Reading 1,000 documents of ~10 KB was
2.4x faster this way than deserializing to `dict` and validating afterwards
(23.9 ms versus 58.2 ms).

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
