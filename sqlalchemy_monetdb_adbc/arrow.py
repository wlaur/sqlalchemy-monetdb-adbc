from __future__ import annotations

from collections.abc import Generator, Sequence
from contextlib import AbstractContextManager, contextmanager
from types import CapsuleType
from typing import Literal, Protocol, cast

import pyarrow as pa
import pyarrow.dataset as pads
from sqlalchemy.engine import Connection
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.elements import ClauseElement
from sqlalchemy.sql.expression import Executable

from sqlalchemy_monetdb_adbc.connection import raw_adbc_connection


class ArrowArrayExportable(Protocol):
    def __arrow_c_array__(
        self,
        requested_schema: object | None = None,
    ) -> tuple[object, object]: ...


class ArrowStreamExportable(Protocol):
    def __arrow_c_stream__(self, requested_schema: object | None = None) -> object: ...


type ArrowIngestData = (
    pa.RecordBatch
    | pa.Table
    | pa.RecordBatchReader
    | pads.Dataset
    | pads.Scanner
    | CapsuleType
    | ArrowArrayExportable
    | ArrowStreamExportable
)


class _ArrowCursor(Protocol):
    def execute(
        self,
        operation: bytes | str,
        parameters: Sequence[object] | None = None,
    ) -> object: ...

    def fetch_arrow_table(self) -> pa.Table: ...

    def fetch_record_batch(self) -> pa.RecordBatchReader: ...

    def adbc_ingest(
        self,
        table_name: str,
        data: ArrowIngestData,
        mode: Literal["append", "create", "replace", "create_append"],
        *,
        db_schema_name: str | None,
        temporary: bool,
    ) -> int: ...


def compile_arrow_statement(connection: Connection, statement: Executable | str) -> tuple[str, list[object]]:
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
    parameters: Sequence[object] | None = None,
) -> pa.Table:
    """Run a statement on ``connection`` and return a ``pyarrow.Table``.

    The query runs on the ADBC session backing ``connection``, so it sees that
    connection's uncommitted work and takes part in its transaction. Rows are
    never converted to Python objects.
    """
    sql, bound = compile_arrow_statement(connection, statement)
    adbc_connection = raw_adbc_connection(connection)

    with adbc_connection.cursor() as cursor:
        arrow_cursor = cast(_ArrowCursor, cursor)
        arrow_cursor.execute(sql, parameters if parameters is not None else bound)
        return arrow_cursor.fetch_arrow_table()


def open_arrow_batch_reader(
    connection: Connection,
    statement: Executable | str,
    parameters: Sequence[object] | None = None,
) -> AbstractContextManager[pa.RecordBatchReader]:
    """Open a ``pyarrow.RecordBatchReader`` for a statement's result.

    Like :func:`fetch_arrow_table`, but the result is not materialized at once.
    A context manager, because the reader stays valid only while the cursor
    behind it is open. MonetDB carries one result channel per session, so
    finish the stream before using ``connection`` again.
    """
    return _open_arrow_batch_reader(connection, statement, parameters)


@contextmanager
def _open_arrow_batch_reader(
    connection: Connection,
    statement: Executable | str,
    parameters: Sequence[object] | None,
) -> Generator[pa.RecordBatchReader]:
    sql, bound = compile_arrow_statement(connection, statement)
    adbc_connection = raw_adbc_connection(connection)

    with adbc_connection.cursor() as cursor:
        arrow_cursor = cast(_ArrowCursor, cursor)
        arrow_cursor.execute(sql, parameters if parameters is not None else bound)
        yield arrow_cursor.fetch_record_batch()


def ingest_arrow(
    connection: Connection,
    table_name: str,
    data: ArrowIngestData,
    *,
    mode: Literal["append", "create", "replace", "create_append"] = "append",
    schema_name: str | None = None,
    temporary: bool = False,
) -> int:
    """Bulk-load Arrow data into ``table_name`` on ``connection``'s transaction.

    ``data`` is anything ADBC accepts: a ``pyarrow`` table, record batch, or
    reader, or any object exposing the Arrow PyCapsule interface. Returns the
    number of rows written. Set ``temporary`` to address or create a local
    temporary table; it cannot be combined with ``schema_name``.
    """
    adbc_connection = raw_adbc_connection(connection)

    with adbc_connection.cursor() as cursor:
        return cast(_ArrowCursor, cursor).adbc_ingest(
            table_name,
            data,
            mode=mode,
            db_schema_name=schema_name,
            temporary=temporary,
        )


__all__ = [
    "ArrowArrayExportable",
    "ArrowIngestData",
    "ArrowStreamExportable",
    "compile_arrow_statement",
    "fetch_arrow_table",
    "ingest_arrow",
    "open_arrow_batch_reader",
]
