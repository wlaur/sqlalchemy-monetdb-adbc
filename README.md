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

## Arrow and polars

Rows are converted to Python objects only if you ask for them. To keep data
columnar, run the query on the same connection and take Arrow back:

```python
from sqlalchemy_monetdb_adbc import fetch_arrow_table, fetch_record_batches, ingest_arrow

statement = select(trades.c.id, trades.c.symbol).where(trades.c.symbol == "AAPL")

with Session(engine) as session:
    table = fetch_arrow_table(session.connection(), statement)  # pyarrow.Table
    frame = polars.from_arrow(table)

    # streaming, for results that should not be materialized at once
    with fetch_record_batches(session.connection(), statement) as reader:
        for batch in reader:
            ...

    ingest_arrow(session.connection(), "trades", table, mode="append")
```

These run on the ADBC session backing the SQLAlchemy connection, so they see
that connection's uncommitted work, take part in its transaction, and roll back
with it. Use them rather than opening a second connection with
`polars.read_database`, which would be a separate session and transaction.

A SQLAlchemy statement is compiled for you, bind parameters included; a plain
SQL string works too. `raw_adbc_connection()` returns the underlying ADBC
connection if you need the driver API directly.

Reading 50,000 rows as Arrow takes about 7 ms, against about 40 ms to build
Python objects from the same result.

## JSON

A `JSON` column round-trips Python objects, as SQLAlchemy specifies: the driver
returns the document as a string and SQLAlchemy deserializes it, honouring
`json_serializer` and `json_deserializer` on `create_engine`.

```python
engine = create_engine("monetdb://...", json_deserializer=orjson.loads)
```

MonetDB validates and normalizes JSON on input, so a round-tripped document
keeps its values but not its original whitespace or key order.

Path indexing works as on other backends, including nested keys, array indexes
and the `as_string()`/`as_integer()` accessors. A path that matches nothing
returns `None`:

```python
select(t.c.doc["title"])  # 'hello'
select(t.c.doc[("sub", "k")])  # 'v'
select(t.c.doc[("arr", 1)])  # 20
select(t.c.doc["missing"])  # None
```

Whatever Python object you store is serialized, and you get that same object
back. Note that a `str` is itself a valid JSON value, so passing pre-serialized
text to a `JSON` column stores a JSON *string*, not an object:

```python
connection.execute(insert(t), [{"payload": {"a": 1}}])  # stored as {"a":1}
connection.execute(insert(t), [{"payload": '{"a": 1}'}])  # stored as "{\"a\": 1}"
```

This is SQLAlchemy's behavior on every backend, not a MonetDB quirk. To store
JSON text you already hold, use a `Text` column, or a type that controls the
codec as below.

### Storing a Pydantic model

`PydanticJSON` stores a Pydantic model in a MonetDB `JSON` column. The model is
serialized straight to JSON text and parsed straight back with
`model_validate_json`, so no intermediate `dict` is built in either direction:

```python
from pydantic import BaseModel
from sqlalchemy import Integer, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from sqlalchemy_monetdb_adbc import PydanticJSON


class Content(BaseModel):
    model_config = ConfigDict(frozen=True)

    title: str
    views: int


class Article(Base):
    __tablename__ = "article"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    content: Mapped[Content] = mapped_column(PydanticJSON(Content))
```

The attribute is the model itself, with no conversion at the call site:

```python
article: Article = session.scalars(select(Article)).one()
article.content.title  # Content instance, never a dict
```

Reading 1,000 documents of ~10 KB is 2.4x faster this way than deserializing to
`dict` and validating afterwards (23.9 ms versus 58.2 ms). A plain `JSON` column
cannot avoid the dict: by the time the attribute is read, SQLAlchemy has already
deserialized and the original text is gone.

#### Declare the model frozen

As with any `JSON` column, SQLAlchemy does not track mutation *inside* the
value, so an in-place edit is silently not persisted. Declaring the model
`frozen=True`, as above, turns that silent loss into a `ValidationError`, and
makes the model hashable. Persist a change by assigning a new value:

```python
article.content = article.content.model_copy(update={"views": 2})
```

Freezing is a property of your model, so the type cannot impose it; if you need
mutation to be tracked instead, use `sqlalchemy.ext.mutable`.

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

## Arrow and polars

Rows are converted to Python objects only if you ask for them. To keep data
columnar, run the query on the same connection and take Arrow back:

```python
from sqlalchemy_monetdb_adbc import fetch_arrow_table, fetch_record_batches, ingest_arrow

statement = select(trades.c.id, trades.c.symbol).where(trades.c.symbol == "AAPL")

with Session(engine) as session:
    table = fetch_arrow_table(session.connection(), statement)  # pyarrow.Table
    frame = polars.from_arrow(table)

    # streaming, for results that should not be materialized at once
    with fetch_record_batches(session.connection(), statement) as reader:
        for batch in reader:
            ...

    ingest_arrow(session.connection(), "trades", table, mode="append")
```

These run on the ADBC session backing the SQLAlchemy connection, so they see
that connection's uncommitted work, take part in its transaction, and roll back
with it. Use them rather than opening a second connection with
`polars.read_database`, which would be a separate session and transaction.

A SQLAlchemy statement is compiled for you, bind parameters included; a plain
SQL string works too. `raw_adbc_connection()` returns the underlying ADBC
connection if you need the driver API directly.

Reading 50,000 rows as Arrow takes about 7 ms, against about 40 ms to build
Python objects from the same result.

## JSON

A `JSON` column round-trips Python objects, as SQLAlchemy specifies: the driver
returns the document as a string and SQLAlchemy deserializes it, honouring
`json_serializer` and `json_deserializer` on `create_engine`.

```python
engine = create_engine("monetdb://...", json_deserializer=orjson.loads)
```

MonetDB validates and normalizes JSON on input, so a round-tripped document
keeps its values but not its original whitespace or key order.

Path indexing works as on other backends, including nested keys, array indexes
and the `as_string()`/`as_integer()` accessors. A path that matches nothing
returns `None`:

```python
select(t.c.doc["title"])  # 'hello'
select(t.c.doc[("sub", "k")])  # 'v'
select(t.c.doc[("arr", 1)])  # 20
select(t.c.doc["missing"])  # None
```

Whatever Python object you store is serialized, and you get that same object
back. Note that a `str` is itself a valid JSON value, so passing pre-serialized
text to a `JSON` column stores a JSON *string*, not an object:

```python
connection.execute(insert(t), [{"payload": {"a": 1}}])  # stored as {"a":1}
connection.execute(insert(t), [{"payload": '{"a": 1}'}])  # stored as "{\"a\": 1}"
```

This is SQLAlchemy's behavior on every backend, not a MonetDB quirk. To store
JSON text you already hold, use a `Text` column, or a type that controls the
codec as below.

### Parsing straight into a model

This package does not ship a Pydantic type, so as not to depend on Pydantic.
The following is a recipe to copy into your own code.

Because the driver hands back the raw JSON text, a type can skip the
intermediate dict entirely and hand that text to a parser that reads JSON
directly, such as `pydantic.BaseModel.model_validate_json`. Override
`bind_processor`/`result_processor` rather than
`process_bind_param`/`process_result_value`, so the underlying `JSON` codecs
never run:

```python
from sqlalchemy import JSON, TypeDecorator


class PydanticJSON(TypeDecorator):
    """Store a Pydantic model in a JSON column, without an intermediate dict."""

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

Map that type and the attribute is already the model, with no conversion at
the call site:

```python
class Article(Base):
    __tablename__ = "article"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    content: Mapped[Content] = mapped_column(PydanticJSON(Content))


article = session.scalars(select(Article)).one()
article.content.title  # Content instance, never a dict
```

The column is still `JSON` in MonetDB. Reading 1,000 documents of ~10 KB was
2.4x faster this way than deserializing to `dict` and validating afterwards
(23.9 ms versus 58.2 ms).

A plain `JSON` column cannot avoid the dict: by the time the attribute is read,
SQLAlchemy has already deserialized and the original text is gone. The type has
to produce the model.

As with any `JSON` column, in-place mutation is not tracked. Assign a new value
to persist a change, or use `sqlalchemy.ext.mutable`:

```python
article.content = article.content.model_copy(update={"views": 2})
```

## Alembic

Importing the dialect registers a MonetDB migration implementation, so Alembic
works without further configuration. Schema operations, autogenerate, and
`PydanticJSON` columns are covered by the test suite.

No configuration is needed: importing the dialect, which `create_engine` does
for you, registers the implementation. This works in offline mode too.

Changing a column's type is supported. MonetDB spells it
`ALTER TABLE t ALTER COLUMN c <type>`, without the `TYPE` keyword most backends
use. It refuses to alter a column that other objects depend on, such as one
carrying a primary key, and says so.

A `PydanticJSON` column autogenerates as `sa.JSON()`, since a migration
describes the database schema. Rendering the model instead would make
migrations import application code and break as soon as that model moved.

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
