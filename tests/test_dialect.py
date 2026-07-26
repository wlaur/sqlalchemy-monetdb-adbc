from types import ModuleType
from typing import Any, cast

import adbc_driver_manager
import pyarrow as pa
import pytest
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, select
from sqlalchemy.dialects import registry
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import SAWarning
from sqlalchemy.sql import sqltypes

from sqlalchemy_monetdb_adbc import MonetDBADBCDialect
from sqlalchemy_monetdb_adbc._alembic import MonetDBImpl
from sqlalchemy_monetdb_adbc.arrow import compile_arrow_statement
from sqlalchemy_monetdb_adbc.base import MonetDBCursor
from sqlalchemy_monetdb_adbc.constants import DIALECT_NAMES
from sqlalchemy_monetdb_adbc.reflection import resolve_type


def test_entry_point_loads_dialect() -> None:
    for dialect_name in DIALECT_NAMES:
        assert registry.load(dialect_name) is MonetDBADBCDialect


def test_create_engine_loads_dialect_without_connecting() -> None:
    urls = (
        "monetdb://monetdb:secret@localhost:50000/demo",
        "monetdb+adbc://monetdb:secret@localhost:50000/demo",
        "monetdbs://monetdb:secret@localhost:50000/demo",
        "monetdbs+adbc://monetdb:secret@localhost:50000/demo",
    )

    for url in urls:
        engine = create_engine(url)
        assert isinstance(engine.dialect, MonetDBADBCDialect)
        engine.dispose()


def test_import_dbapi_loads_monetdb_adbc_driver() -> None:
    dbapi = MonetDBADBCDialect.import_dbapi()

    assert isinstance(dbapi, ModuleType)
    assert dbapi.__name__ == "adbc_driver_monetdb.dbapi"
    assert dbapi.paramstyle == "qmark"


def test_create_connect_args_translates_sqlalchemy_scheme() -> None:
    url = make_url(
        "monetdb+adbc://monetdb:secret@localhost:50000/demo?client_application=sqlalchemy-monetdb-adbc&read_timeout=30"
    )

    args, kwargs = MonetDBADBCDialect().create_connect_args(url)

    assert args == (
        "monetdb://monetdb:secret@localhost:50000/demo?client_application=sqlalchemy-monetdb-adbc&read_timeout=30",
    )
    assert kwargs == {}


def test_create_connect_args_preserves_tls_scheme() -> None:
    url = make_url("monetdbs+adbc://monetdb:secret@localhost:50000/demo")

    args, kwargs = MonetDBADBCDialect().create_connect_args(url)

    assert args == ("monetdbs://monetdb:secret@localhost:50000/demo",)
    assert kwargs == {}


def test_unknown_reflected_type_warns_and_uses_nulltype() -> None:
    with pytest.warns(SAWarning, match="Did not recognize MonetDB type 'future_type'"):
        data_type = resolve_type("future_type", 0, 0)

    assert data_type is sqltypes.NULLTYPE


def test_alembic_unquotes_exactly_one_sql_string_layer() -> None:
    implementation = MonetDBImpl.__new__(MonetDBImpl)
    assert not implementation.compare_server_default(None, None, "'x'", "'x'")
    assert implementation.compare_server_default(None, None, "'x'", "'''x'''")
    assert not implementation.compare_server_default(
        None,
        None,
        "CURRENT_TIMESTAMP",
        "CURRENT_TIMESTAMP",
    )


class _CompileConnection:
    dialect = MonetDBADBCDialect()

    def get_execution_options(self) -> dict[str, Any]:
        return {"schema_translate_map": {"source": "target"}}


def test_arrow_sql_expands_parameters_and_translates_schemas() -> None:
    table = Table("items", MetaData(), Column("id", Integer), schema="source")
    statement = select(table.c.id).where(table.c.id.in_([1, 2, 3]))

    sql, parameters = compile_arrow_statement(cast(Connection, _CompileConnection()), statement)

    assert "target.items" in sql
    assert "IN (?, ?, ?)" in sql
    assert parameters == [1, 2, 3]


def test_disconnect_detection_uses_adbc_io_status() -> None:
    dialect = MonetDBADBCDialect()
    disconnected = adbc_driver_manager.OperationalError(
        "connection reset",
        status_code=adbc_driver_manager.AdbcStatusCode.IO,
    )
    query_error = adbc_driver_manager.ProgrammingError(
        "syntax error",
        status_code=adbc_driver_manager.AdbcStatusCode.INVALID_ARGUMENT,
    )

    assert dialect.is_disconnect(cast(Any, disconnected), None, None)
    assert not dialect.is_disconnect(cast(Any, query_error), None, None)


class _Reader:
    def __init__(self, batches: list[Any]) -> None:
        self._batches = iter(batches)

    def read_next_batch(self) -> Any:
        return next(self._batches)


class _Cursor:
    def __init__(self) -> None:
        self.arraysize = 2
        self.description = [("value",)]
        self._readers = iter(
            [
                _Reader(
                    [
                        pa.record_batch({"value": [1, 2]}),
                        pa.record_batch({"value": [3, 4]}),
                    ]
                ),
                _Reader([pa.record_batch({"value": [10, 11]})]),
            ]
        )
        self._reader: _Reader | None = None

    def execute(self, operation: str, parameters: Any) -> None:
        self._reader = next(self._readers)

    def executemany(self, operation: str, parameters: Any) -> None:
        self._reader = next(self._readers)

    def fetch_record_batch(self) -> _Reader:
        assert self._reader is not None
        return self._reader

    def close(self) -> None:
        pass


def test_cursor_fetchmany_arraysize_and_executemany_reset() -> None:
    inner = _Cursor()
    cursor = MonetDBCursor(inner)
    cursor.arraysize = 3
    cursor.execute("SELECT value")

    assert cursor.arraysize == 3
    assert cursor.fetchmany() == [(1,), (2,), (3,)]
    assert cursor.fetchone() == (4,)

    cursor.executemany("SELECT ?", [(10,), (11,)])
    assert cursor.fetchall() == [(10,), (11,)]


class _ParameterCursor:
    def __init__(self) -> None:
        self.parameters: list[Any] = []

    def execute(self, operation: str, parameters: Any) -> None:
        self.parameters.append(parameters)

    def executemany(self, operation: str, parameters: Any) -> None:
        self.parameters.append(parameters)

    def close(self) -> None:
        pass


def test_compiled_parameters_reuse_arrow_schema() -> None:
    inner = _ParameterCursor()
    cursor = MonetDBCursor(inner)

    _, schema = cursor.execute_with_parameter_schema("SELECT ?", (1,), None)
    assert schema is not None
    cursor.execute_with_parameter_schema("SELECT ?", (2,), schema)
    cursor.executemany_with_parameter_schema("INSERT INTO t VALUES (?)", [(3,), (4,)], schema)

    assert [batch.column(0).to_pylist() for batch in inner.parameters] == [
        [1],
        [2],
        [3, 4],
    ]

    cursor.execute_with_parameter_schema("SELECT 1", (), None)
    assert inner.parameters[-1] == ()

    _, null_schema = cursor.execute_with_parameter_schema("SELECT ?", (None,), None)
    cursor.execute_with_parameter_schema("SELECT ?", (5,), null_schema)
    assert inner.parameters[-1].column(0).to_pylist() == [5]
