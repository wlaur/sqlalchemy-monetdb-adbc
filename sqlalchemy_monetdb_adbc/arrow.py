from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal, cast

from sqlalchemy.engine import Connection
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.elements import ClauseElement
from sqlalchemy.sql.expression import Executable

from sqlalchemy_monetdb_adbc.connection import raw_adbc_connection

# pyarrow ships no type information, so the Arrow objects these helpers return
# and accept are annotated as Any. Their concrete types are given per function.


def compile_arrow_statement(connection: Connection, statement: Executable | str) -> tuple[str, list[Any]]:
    if isinstance(statement, str):
        return statement, []

    schema_translate_map = connection.get_execution_options().get("schema_translate_map")
    compiled = cast(
        SQLCompiler,
        cast(ClauseElement, statement).compile(
            dialect=connection.dialect,
            schema_translate_map=schema_translate_map,
            render_schema_translate=bool(schema_translate_map),
            compile_kwargs={"render_postcompile": True},
        ),
    )
    parameters = compiled.params
    # The dialect uses qmark, so bind values go positionally in the order the
    # compiler emitted them.
    ordered = [parameters[name] for name in compiled.positiontup or ()]
    return str(compiled), ordered


def fetch_arrow_table(
    connection: Connection,
    statement: Executable | str,
    parameters: list[Any] | None = None,
) -> Any:
    """Run a statement on ``connection`` and return a ``pyarrow.Table``.

    The query runs on the ADBC session backing ``connection``, so it sees that
    connection's uncommitted work and takes part in its transaction. Rows are
    never converted to Python objects.
    """
    sql, bound = compile_arrow_statement(connection, statement)
    adbc_connection = raw_adbc_connection(connection)

    with adbc_connection.cursor() as cursor:
        adbc_cursor = cast(Any, cursor)
        adbc_cursor.execute(sql, parameters if parameters is not None else bound)
        return adbc_cursor.fetch_arrow_table()


@contextmanager  # pyright: ignore[reportDeprecated]
def fetch_record_batches(
    connection: Connection,
    statement: Executable | str,
    parameters: list[Any] | None = None,
) -> Iterator[Any]:
    """Stream a statement's result as a ``pyarrow.RecordBatchReader``.

    Like :func:`fetch_arrow_table`, but the result is not materialized at once.
    A context manager, because the reader stays valid only while the cursor
    behind it is open. MonetDB carries one result channel per session, so
    finish the stream before using ``connection`` again.
    """
    sql, bound = compile_arrow_statement(connection, statement)
    adbc_connection = raw_adbc_connection(connection)

    with adbc_connection.cursor() as cursor:
        adbc_cursor = cast(Any, cursor)
        adbc_cursor.execute(sql, parameters if parameters is not None else bound)
        yield adbc_cursor.fetch_record_batch()


def ingest_arrow(
    connection: Connection,
    table_name: str,
    data: Any,
    *,
    mode: Literal["append", "create", "replace", "create_append"] = "append",
    schema_name: str | None = None,
) -> int:
    """Bulk-load Arrow data into ``table_name`` on ``connection``'s transaction.

    ``data`` is anything ADBC accepts: a ``pyarrow`` table, record batch, or
    reader, or any object exposing the Arrow PyCapsule interface. Returns the
    number of rows written.
    """
    adbc_connection = raw_adbc_connection(connection)

    with adbc_connection.cursor() as cursor:
        return cast(int, cast(Any, cursor).adbc_ingest(table_name, data, mode=mode, db_schema_name=schema_name))


__all__ = ["fetch_arrow_table", "fetch_record_batches", "ingest_arrow"]
