# sqlalchemy-monetdb-adbc

A SQLAlchemy dialect for MonetDB backed by
[`adbc-driver-monetdb`](https://github.com/wlaur/adbc-driver-monetdb).

The package is pure Python. It gives SQLAlchemy and Arrow-native ADBC operations a
single MonetDB connection and transaction boundary.

## Status

The dialect covers SQL compilation, types, reflection, transactions, and the ORM,
and is validated against a live MonetDB server.

### Dialect compliance suite

SQLAlchemy's own dialect suite runs from `suite/` (see Development) and reports
no failures: 1237 passed, 16 xfailed, 388 skipped. Each xfail is a MonetDB or
driver limitation, listed with its reason in `suite/known_failures.py`. They are
marked xfail rather than skipped so that the tests still run, and report XPASS
if a future release gains the behaviour:

- MonetDB infers no type for a bare parameter, so `WHERE ? = ?` is rejected
  outright. A cast resolves it, but SQLAlchemy emits the parameter alone. This
  is most of the remainder.
- Untyped parameters also change arithmetic: `SELECT ? / ?` with `(15, 10)`
  returns `1`, not `1.5`, and dividing an untyped parameter by a decimal is
  read as interval arithmetic.
- No lastrowid, which is why generated values come back through `RETURNING`.
- `RETURNING *` needs an explicit column list.
- JSON is normalized on input, so a document does not round-trip byte for byte.
- Some identifiers legal elsewhere are rejected, such as one containing `%`.

Common table expressions are fully supported, including recursive CTEs, CTEs
over `VALUES`, and CTEs driving `UPDATE`/`DELETE`. MonetDB's `WITH` accepts only
`SELECT` or `VALUES`, so a CTE cannot itself be an `INSERT`/`UPDATE`/`DELETE`.

Regular expression matching is not available: MonetDB's `~` is `mbr_contains`,
a geometry operator, so `regexp_match()` raises rather than producing SQL that
means something else. `regexp_replace()` works.

The dialect requires `adbc-driver-monetdb` 0.8.9 or newer, which coalesces
producer batches into adaptive byte-budgeted COPY windows, streams bounded
uploads, and prevents client-side ingest failures from committing partial
appends. It also reports truthful row counts, caches prepared statements per
connection, executes one-row bound DML without a savepoint, and returns small
results from MonetDB's initial reply. One
`DRIVER-WORKAROUND` remains in the source for upstream Apache Arrow ADBC
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
- MonetDB rejects the all-zero (nil) UUID, `00000000-0000-0000-0000-000000000000`,
  even as a plain literal, because it uses that value as its internal `NULL` for
  the `uuid` type. A `Uuid` column therefore cannot store `uuid.UUID(int=0)`.
- A `DECIMAL` declared without precision means `DECIMAL(18, 3)`. The dialect
  always renders that explicitly: MonetDB drops the connection when a
  *parameter* is cast to an unsized `DECIMAL`, which is what SQLAlchemy emits
  for true division.
- A `Numeric` column accepts `float`, `int` and `Decimal` in the same
  `executemany`. ADBC would otherwise take the column's Arrow type from the
  first value and reject the rest, so the dialect normalises the column first.
- A `UNIQUE` constraint is backed by an index of the same name, so it is
  reflected both by `get_unique_constraints()` and by `get_indexes()`.

## Installation

```console
uv add sqlalchemy-monetdb-adbc
```

PyArrow is a required dependency because the dialect's row-conversion layer and
public Arrow helpers use concrete `pyarrow` types. Polars is not required by the
dialect; install it only when using the Polars examples or integration.

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

The explicit `monetdb+adbc://` form resolves to this dialect. It can coexist
with `sqlalchemy-monetdb` when every URL is explicit:
`monetdb+adbc://` selects this package and `monetdb+pymonetdb://` selects the
legacy dialect. Both packages register bare `monetdb://`, so its winner is
entry-point-order dependent while both are installed.

TLS URLs work with either the unchanged `monetdbs://` form or the explicit
`monetdbs+adbc://` form. Both preserve the secure driver URI.

The driver accepts the same URI query options after the database name:

```python
engine = create_engine(
    "monetdb://monetdb:monetdb@localhost:50000/demo?client_application=my_app",
)
```

## Arrow and Polars

Rows are converted to Python objects only if you ask for them, and that
conversion is done a column at a time through NumPy rather than element by
element. Ordinary row-returning queries with meaningful result sizes avoid
per-cell Arrow scalar boxing.

PyArrow and NumPy are runtime dependencies for the dialect's Arrow-native
result layer and optimized Arrow-to-Python object conversion. Polars is
optional. Arrow-native reads and writes do not convert through NumPy.

The driver removes the avoidable fixed costs found in the release review:
one-row parameterized DML no longer adds a savepoint pair, and results of up to
100 rows no longer force a second fetch. The driver also batches `executemany`
parameters into one multi-row statement when MonetDB can preserve the DB-API
semantics, and repeated single-batch appends no longer create redundant
savepoints. Tiny row-oriented SELECTs still carry the fixed cost of receiving
a canonical Arrow stream across the native boundary; use the Arrow helpers
below when keeping the result columnar matters.

To skip the conversion entirely and keep data columnar, run the query on the
same connection and take Arrow back:

```python
import polars as pl

from sqlalchemy_monetdb_adbc import (
    fetch_arrow_table,
    ingest_arrow,
    open_arrow_batch_reader,
)

statement = select(trades.c.id, trades.c.symbol).where(trades.c.symbol == "AAPL")

with Session(engine) as session:
    table = fetch_arrow_table(session.connection(), statement)  # pyarrow.Table
    df = pl.from_arrow(table)

    # streaming, for results that should not be materialized at once
    with open_arrow_batch_reader(session.connection(), statement) as reader:
        for batch in reader:
            ...

    ingest_arrow(session.connection(), trades, table, mode="append")
```

These run on the ADBC session backing the SQLAlchemy connection, so they see
that connection's uncommitted work, take part in its transaction, and roll back
with it.

For a large incremental write, pass one `pyarrow.RecordBatchReader` to one
`ingest_arrow` call. The reader remains streaming while the driver keeps one
operation-level transaction scope. Passing a SQLAlchemy `Table` uses its name
and translated schema. With `create=True`, the helper emits normal dialect DDL
first, including primary keys, nullability, constraints, custom types, and
schema translation:

```python
ingest_arrow(connection, trades, reader, create=True)
```

For a Polars lazy query, install `adbc-driver-monetdb[polars]` and use its
backpressured Arrow-stream adapter. The dialect remains Polars-independent:

```python
import polars as pl

from adbc_driver_monetdb import PolarsArrowStream
from sqlalchemy_monetdb_adbc import ingest_arrow

stream = PolarsArrowStream(pl.scan_parquet("trades/*.parquet"))
ingest_arrow(connection, trades, stream, mode="append")
assert stream.rows_read > 0
```

Advanced ADBC statement options can be passed without moving ingest policy
into the dialect:

```python
ingest_arrow(
    connection,
    trades,
    reader,
    statement_options={"adbc.monetdb.ingest_atomicity": "savepoint"},
)
```

By default, a client-side failure after completed COPY windows leaves the
SQLAlchemy transaction readable but makes commit fail. SQLAlchemy deactivates
a transaction after any failed commit, so the dialect rolls the physical ADBC
transaction back before propagating that commit error; the pool and SQLAlchemy
then agree that no transaction remains. Calling `rollback()` instead of
attempting commit also recovers normally.

`pl.read_database` can use that same session too. Pass it the underlying
ADBC connection owned by SQLAlchemy:

```python
import polars as pl

from sqlalchemy_monetdb_adbc import raw_adbc_connection

with Session(engine) as session:
    session.execute(insert(trades), {"id": 1, "symbol": "AAPL"})

    df = pl.read_database(
        query="SELECT id, symbol FROM trades ORDER BY id",
        connection=raw_adbc_connection(session.connection()),
    )

    session.commit()
```

Do not close the returned ADBC connection or change its transaction state;
SQLAlchemy owns both. Passing a URL to `pl.read_database` instead would
open a separate session and would not see uncommitted work.

Do not pass the raw ADBC connection to `DataFrame.write_database`: Polars'
ADBC write path commits the connection after ingesting, which would also
commit any pending SQLAlchemy work. Use the transaction-aware helper for
columnar writes:

```python
ingest_arrow(session.connection(), "trades", df, mode="append")
```

A SQLAlchemy statement is compiled for you, bind parameters included; a plain
SQL string works too. `raw_adbc_connection()` returns the underlying ADBC
connection if you need the driver API directly.

The driver prefetches large results by default. Closing a partially consumed
result—such as calling `.first()`—waits briefly and then lets an in-flight fetch
finish without killing the pooled session. The next statement on that
connection can wait for that fetch or its configured timeout. Set
`read_prefetch=false` in the connection URI when prompt pool reuse matters more
than fetch/decode overlap:

```python
engine = create_engine("monetdb://localhost/demo?read_prefetch=false")
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
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Integer, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sqlalchemy_monetdb_adbc import PydanticJSON


class Content(BaseModel):
    model_config = ConfigDict(frozen=True)

    title: str
    views: int


class Base(DeclarativeBase):
    pass


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

This avoids deserializing to an intermediate `dict` before validation. A plain
`JSON` column cannot avoid the dict: by the time the attribute is read,
SQLAlchemy has already deserialized and the original text is gone.

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
docker compose up --wait
MONETDB_TEST_URI=monetdb://monetdb:monetdb@localhost:50001/test uv run pytest tests
uv run pytest suite -o addopts=""
docker compose down -v
```

`uv run pytest` alone skips every test that needs a server, so the unit gate
runs without one.

## License

MIT
