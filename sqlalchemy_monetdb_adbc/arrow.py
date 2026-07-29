from __future__ import annotations

from collections.abc import Generator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from types import CapsuleType
from typing import Literal, Protocol, cast

import pyarrow as pa
import pyarrow.dataset as pads
from adbc_driver_monetdb import (
    DEFAULT_PARQUET_RECLAIM_BYTES,
    ParquetArrowStream,
    ParquetEpochUnit,
    StatementOptionValues,
)
from sqlalchemy.engine import Connection
from sqlalchemy.sql.elements import ClauseElement
from sqlalchemy.sql.expression import Executable
from sqlalchemy.sql.schema import Table

from sqlalchemy_monetdb_adbc.compiler import MonetDBCompiler
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
        MonetDBCompiler,
        cast(ClauseElement, statement).compile(
            dialect=connection.dialect,
            schema_translate_map=schema_translate_map,
            render_schema_translate=bool(schema_translate_map),
        ),
    )
    expanded = compiled.construct_expanded_state()
    processors = dict(compiled.arrow_bind_processors())
    processors.update(expanded.processors)
    ordered = [
        processors[name](expanded.parameters[name]) if name in processors else expanded.parameters[name]
        for name in expanded.positiontup or ()
    ]
    return expanded.statement, ordered


def _arrow_execute_parameters(
    statement: Executable | str,
    compiled: list[object],
    explicit: Sequence[object] | None,
) -> Sequence[object]:
    if explicit is not None and not isinstance(statement, str) and compiled:
        raise ValueError("parameters cannot override bind values in a compiled SQLAlchemy statement")
    return explicit if explicit is not None else compiled


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
        arrow_cursor.execute(sql, _arrow_execute_parameters(statement, bound, parameters))
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
        arrow_cursor.execute(sql, _arrow_execute_parameters(statement, bound, parameters))
        yield arrow_cursor.fetch_record_batch()


def ingest_arrow(
    connection: Connection,
    table: str | Table,
    data: ArrowIngestData,
    *,
    mode: Literal["append", "create", "replace", "create_append"] = "append",
    schema_name: str | None = None,
    temporary: bool = False,
    create: bool = False,
    statement_options: (StatementOptionValues | Mapping[str, bytes | float | int | str | None] | None) = None,
) -> int:
    """Bulk-load Arrow data into ``table`` on ``connection``'s transaction.

    ``data`` is anything ADBC accepts: a ``pyarrow`` table, record batch, or
    reader, or any object exposing the Arrow PyCapsule interface. Returns the
    number of rows written. A SQLAlchemy :class:`~sqlalchemy.Table` supplies its
    name and translated schema. With ``create=True``, its normal SQLAlchemy DDL
    is emitted before appending, preserving constraints and dialect types.

    Set ``temporary`` to address or create a local temporary table; it cannot
    be combined with ``schema_name``. ``statement_options`` passes advanced
    ADBC driver options to the ingest statement.
    """
    adbc_connection = raw_adbc_connection(connection)

    if isinstance(table, Table):
        if schema_name is not None:
            raise ValueError("schema_name cannot override a SQLAlchemy Table schema")
        if mode != "append" and not create:
            raise ValueError(
                "non-append ADBC modes with a SQLAlchemy Table would discard its constraints; "
                "use create=True with mode='append' or pass the table name as a string"
            )
        table_name = table.name
        schema_name = connection.schema_for_object(table)
        if create:
            if mode != "append":
                raise ValueError("create=True requires mode='append'")
            if temporary:
                raise ValueError("create=True cannot be combined with temporary=True")
            table.create(connection)
    else:
        table_name = table
        if create:
            raise TypeError("create=True requires a SQLAlchemy Table")

    with adbc_connection.cursor(adbc_stmt_kwargs=dict(statement_options or {})) as cursor:
        return cast(_ArrowCursor, cursor).adbc_ingest(
            table_name,
            data,
            mode=mode,
            db_schema_name=schema_name,
            temporary=temporary,
        )


__all__ = [
    "DEFAULT_PARQUET_RECLAIM_BYTES",
    "ArrowArrayExportable",
    "ArrowIngestData",
    "ArrowStreamExportable",
    "ParquetArrowStream",
    "ParquetEpochUnit",
    "compile_arrow_statement",
    "fetch_arrow_table",
    "ingest_arrow",
    "open_arrow_batch_reader",
]
