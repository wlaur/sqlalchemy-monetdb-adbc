import datetime
from types import ModuleType
from typing import Any, cast

import adbc_driver_manager
import pyarrow as pa
import pytest
from adbc_driver_manager.dbapi import Connection as ADBCConnection
from sqlalchemy import (
    JSON,
    Column,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Time,
    TypeDecorator,
    create_engine,
    literal,
    select,
)
from sqlalchemy.dialects import registry
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import SAWarning
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.pool import QueuePool, StaticPool
from sqlalchemy.schema import DropIndex, SetColumnComment
from sqlalchemy.sql import sqltypes

from sqlalchemy_monetdb_adbc import INET, MonetDBADBCDialect, ParquetArrowStream, ParquetEpochUnit
from sqlalchemy_monetdb_adbc._alembic import MonetDBImpl
from sqlalchemy_monetdb_adbc._convert import batch_to_rows
from sqlalchemy_monetdb_adbc.arrow import compile_arrow_statement
from sqlalchemy_monetdb_adbc.base import (
    RESERVED_WORDS,
    MonetDBCursor,
    MonetDBIdentifierPreparer,
    parameter_record_batch,
)
from sqlalchemy_monetdb_adbc.connection import MonetDBConnection
from sqlalchemy_monetdb_adbc.constants import DIALECT_NAMES
from sqlalchemy_monetdb_adbc.dialect import parse_server_version
from sqlalchemy_monetdb_adbc.reflection import resolve_type
from sqlalchemy_monetdb_adbc.types import MonetDBTime


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


def test_inet_columns_are_cast_for_binary_results() -> None:
    table = Table("network", MetaData(), Column("address", INET))

    compiled = select(table.c.address).compile(dialect=MonetDBADBCDialect())

    assert str(compiled) == "SELECT CAST(network.address AS VARCHAR(128)) AS address \nFROM network"


def test_import_dbapi_loads_monetdb_adbc_driver() -> None:
    dbapi = MonetDBADBCDialect.import_dbapi()

    assert isinstance(dbapi, ModuleType)
    assert dbapi.__name__ == "adbc_driver_monetdb.dbapi"
    assert dbapi.paramstyle == "qmark"


def test_parquet_stream_is_reexported_from_the_dialect() -> None:
    from adbc_driver_monetdb import ParquetArrowStream as DriverParquetArrowStream
    from adbc_driver_monetdb import ParquetEpochUnit as DriverParquetEpochUnit

    assert ParquetArrowStream is DriverParquetArrowStream
    assert ParquetEpochUnit is DriverParquetEpochUnit


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


def test_dec2025_scanner_only_reserved_words_are_quoted() -> None:
    preparer = MonetDBIdentifierPreparer(MonetDBADBCDialect())
    reserved = {
        "as",
        "both",
        "details",
        "fetch",
        "geometrycollection",
        "leading",
        "linestring",
        "multilinestring",
        "multipoint",
        "multipolygon",
        "point",
        "polygon",
        "qualify",
        "recursive",
        "returning",
        "show",
        "trailing",
        "unnest",
        "within",
    }

    assert reserved <= RESERVED_WORDS
    assert all(preparer.quote(word) == f'"{word}"' for word in reserved)
    assert all(preparer.quote(word) == word for word in {"field", "greatest", "ifnull", "least", "trim"})


def test_server_version_parser_accepts_suffixes_and_invalid_values() -> None:
    assert parse_server_version("11.55.7") == (11, 55, 7)
    assert parse_server_version("MonetDB 11.55.7 (Dec2025-SP3)") == (11, 55, 7)
    assert parse_server_version(None) == ()


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
    def __init__(self, dialect: MonetDBADBCDialect | None = None) -> None:
        self.dialect = dialect or MonetDBADBCDialect()

    def get_execution_options(self) -> dict[str, Any]:
        return {"schema_translate_map": {"source": "target"}}


def test_arrow_sql_expands_parameters_and_translates_schemas() -> None:
    table = Table("items", MetaData(), Column("id", Integer), schema="source")
    statement = select(table.c.id).where(table.c.id.in_([1, 2, 3]))

    sql, parameters = compile_arrow_statement(cast(Connection, _CompileConnection()), statement)

    assert "target.items" in sql
    assert "IN (?, ?, ?)" in sql
    assert parameters == [1, 2, 3]


class _PrefixedString(TypeDecorator[str]):
    impl = String
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Any) -> str | None:
        return None if value is None else f"processed:{value}"


def test_arrow_sql_applies_bind_processors_to_scalar_and_expanding_values() -> None:
    dialect = MonetDBADBCDialect(json_serializer=lambda value: f"json:{value['value']}")
    table = Table(
        "items",
        MetaData(),
        Column("doc", JSON),
        Column("at", Time(timezone=True)),
        Column("custom", _PrefixedString()),
    )
    statement = select(table).where(
        table.c.doc.in_([{"value": 1}, {"value": 2}]),
        table.c.at == datetime.time(1, 2, 3, tzinfo=datetime.timezone(datetime.timedelta(hours=2))),
        table.c.custom == "value",
    )

    sql, parameters = compile_arrow_statement(
        cast(Connection, _CompileConnection(dialect)),
        statement,
    )

    assert "doc IN (?, ?)" in sql
    assert parameters == ["json:1", "json:2", datetime.time(23, 2, 3), "processed:value"]


def test_column_comment_applies_schema_translation() -> None:
    table = Table("items", MetaData(), Column("value", Integer, comment="translated"), schema="source")

    compiled = SetColumnComment(table.c.value).compile(
        dialect=MonetDBADBCDialect(),
        schema_translate_map={"source": "target"},
        render_schema_translate=True,
    )

    assert str(compiled) == """COMMENT ON COLUMN "target"."items"."value" IS R'translated'"""


def test_unique_index_drop_uses_its_backing_constraint() -> None:
    table = Table("items", MetaData(), Column("value", Integer), schema="app")
    index = Index("uq_items_value", table.c.value, unique=True)

    compiled = DropIndex(index).compile(dialect=MonetDBADBCDialect())

    assert str(compiled) == "ALTER TABLE app.items DROP CONSTRAINT uq_items_value"


class _RenderedString(String):
    cache_ok = True

    def __init__(self, rendered: str) -> None:
        super().__init__()
        self.rendered = rendered

    def literal_processor(self, dialect: Any) -> Any:
        def process(_value: Any) -> str:
            return self.rendered

        return process


@pytest.mark.parametrize(
    ("rendered", "expected"),
    [
        ("CURRENT_USER", "SELECT CURRENT_USER AS anon_1"),
        ("R'already raw'", "SELECT R'already raw' AS anon_1"),
    ],
)
def test_literal_rendering_only_prefixes_plain_quoted_strings(rendered: str, expected: str) -> None:
    statement = select(literal("ignored", type_=_RenderedString(rendered)))

    compiled = statement.compile(
        dialect=MonetDBADBCDialect(),
        compile_kwargs={"literal_binds": True},
    )

    assert str(compiled) == expected


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
    closed = adbc_driver_manager.ProgrammingError(
        "INVALID_STATE: connection has been closed",
        status_code=adbc_driver_manager.AdbcStatusCode.INVALID_STATE,
        details=[("adbc.monetdb.connection_terminal", b"true")],
    )
    terminal_timeout = adbc_driver_manager.OperationalError(
        "operation timed out",
        status_code=adbc_driver_manager.AdbcStatusCode.TIMEOUT,
        details=[("adbc.monetdb.connection_terminal", b"true")],
    )
    server_timeout = adbc_driver_manager.OperationalError(
        "server timed out",
        status_code=adbc_driver_manager.AdbcStatusCode.TIMEOUT,
        sqlstate="HYT00",
    )

    assert dialect.is_disconnect(cast(Any, disconnected), None, None)
    assert dialect.is_disconnect(cast(Any, closed), None, None)
    assert dialect.is_disconnect(cast(Any, terminal_timeout), None, None)
    assert not dialect.is_disconnect(cast(Any, server_timeout), None, None)
    assert not dialect.is_disconnect(cast(Any, query_error), None, None)


class _ClosedRawConnection:
    adbc_connection: "_ClosedRawConnection"

    def __init__(self) -> None:
        self.adbc_connection = self
        self.close_calls = 0

    def get_option(self, _name: str) -> str:
        return "false"

    def rollback(self) -> None:
        raise adbc_driver_manager.ProgrammingError(
            "INVALID_STATE: connection has been closed",
            status_code=adbc_driver_manager.AdbcStatusCode.INVALID_STATE,
            details=[("adbc.monetdb.connection_terminal", b"true")],
        )

    def close(self) -> None:
        self.close_calls += 1


def test_closed_transport_propagates_rollback_and_close_is_idempotent() -> None:
    raw = _ClosedRawConnection()
    connection = MonetDBConnection(cast(ADBCConnection, cast(object, raw)))

    with pytest.raises(adbc_driver_manager.ProgrammingError, match="connection has been closed"):
        connection.rollback()
    connection.close()
    connection.close()

    assert connection.closed
    assert raw.close_calls == 1


def test_pool_checkin_invalidates_a_defunct_connection() -> None:
    created: list[_ClosedRawConnection] = []

    def create_connection() -> MonetDBConnection:
        raw = _ClosedRawConnection()
        created.append(raw)
        return MonetDBConnection(cast(ADBCConnection, cast(object, raw)))

    connection_pool = QueuePool(cast(Any, create_connection), reset_on_return="rollback")
    first = connection_pool.connect()
    first.close()
    second = connection_pool.connect()

    assert len(created) == 2
    assert created[0].close_calls == 1

    second.close()
    connection_pool.dispose()


class _HealthyRawConnection(_ClosedRawConnection):
    def rollback(self) -> None:
        pass


def test_queue_pool_reuses_healthy_connections_honors_bounds_and_disposes_idle() -> None:
    created: list[_HealthyRawConnection] = []

    def create_connection() -> MonetDBConnection:
        raw = _HealthyRawConnection()
        created.append(raw)
        return MonetDBConnection(cast(ADBCConnection, cast(object, raw)))

    connection_pool = QueuePool(
        cast(Any, create_connection),
        pool_size=1,
        max_overflow=1,
        timeout=0.01,
        reset_on_return="rollback",
    )
    first = connection_pool.connect()
    first.close()
    reused = connection_pool.connect()
    assert len(created) == 1
    overflow = connection_pool.connect()
    with pytest.raises(SATimeoutError):
        connection_pool.connect()
    reused.close()
    overflow.close()
    connection_pool.dispose()

    assert sum(raw.close_calls for raw in created) == 2


def test_static_pool_reconnects_after_a_terminal_reset_failure() -> None:
    created: list[_ClosedRawConnection] = []

    def create_connection() -> MonetDBConnection:
        raw = _ClosedRawConnection()
        created.append(raw)
        return MonetDBConnection(cast(ADBCConnection, cast(object, raw)))

    connection_pool = StaticPool(cast(Any, create_connection), reset_on_return="rollback")
    first = connection_pool.connect()
    first.close()
    second = connection_pool.connect()

    assert len(created) == 2
    second.close()
    connection_pool.dispose()


class _CommitFailureRawConnection(_ClosedRawConnection):
    def commit(self) -> None:
        raise adbc_driver_manager.ProgrammingError(
            "transaction is aborted",
            status_code=adbc_driver_manager.AdbcStatusCode.INVALID_STATE,
            sqlstate="25005",
        )

    def close(self) -> None:
        super().close()
        raise adbc_driver_manager.OperationalError(
            "cleanup failed",
            status_code=adbc_driver_manager.AdbcStatusCode.IO,
        )


def test_commit_error_is_not_masked_by_cleanup_error() -> None:
    raw = _CommitFailureRawConnection()
    connection = MonetDBConnection(cast(ADBCConnection, cast(object, raw)))

    with pytest.raises(adbc_driver_manager.ProgrammingError, match="transaction is aborted") as caught:
        connection.commit()

    assert caught.value.__notes__ == [
        "automatic rollback also failed: INVALID_STATE: connection has been closed",
        "connection cleanup also failed: cleanup failed",
    ]
    assert raw.close_calls == 1


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

    def adbc_prepare(self, operation: str) -> Any | None:
        return None

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


def test_compiled_parameters_use_the_prepared_arrow_schema() -> None:
    month = pa.field(
        "0",
        pa.int32(),
        metadata={b"ARROW:extension:name": b"monetdb.interval_month"},
    )

    class _PreparedParameterCursor(_ParameterCursor):
        def adbc_prepare(self, operation: str) -> Any:
            return pa.schema([month])

    inner = _PreparedParameterCursor()
    cursor = MonetDBCursor(inner)

    _, schema = cursor.execute_with_parameter_schema("SELECT ?", (14,), None)

    assert schema == pa.schema([month])
    assert inner.parameters[-1].column(0).type == pa.int32()


def test_parameter_batch_encodes_python_integers_wider_than_int64_as_hugeint() -> None:
    values = [-(2**63) - 1, 2**63]

    batch = parameter_record_batch([(value,) for value in values], None)

    assert batch is not None
    assert batch.schema.field(0).metadata == {b"ARROW:extension:name": b"monetdb.hugeint"}
    assert batch_to_rows(batch) == [(values[0],), (values[1],)]


def test_timezone_time_processors_normalize_to_utc() -> None:
    data_type = MonetDBTime(timezone=True)
    bind = data_type.bind_processor(MonetDBADBCDialect())
    result = data_type.result_processor(MonetDBADBCDialect(), None)
    assert bind is not None
    assert result is not None

    bound = bind(datetime.time(0, 30, 1, 234567, tzinfo=datetime.timezone(datetime.timedelta(hours=2))))

    assert bound == datetime.time(22, 30, 1, 234567)
    assert result(bound) == datetime.time(22, 30, 1, 234567, tzinfo=datetime.UTC)
