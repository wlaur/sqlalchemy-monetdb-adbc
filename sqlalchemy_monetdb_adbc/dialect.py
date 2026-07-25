from collections.abc import Callable
from typing import Any, ClassVar, cast

from sqlalchemy import pool
from sqlalchemy.engine import default
from sqlalchemy.engine.interfaces import (
    DBAPIConnection,
    DBAPICursor,
    DBAPIModule,
    IsolationLevel,
    PoolProxiedConnection,
)
from sqlalchemy.engine.url import URL
from sqlalchemy.sql import sqltypes

from sqlalchemy_monetdb_adbc.base import MonetDBExecutionContext, MonetDBIdentifierPreparer
from sqlalchemy_monetdb_adbc.compiler import MonetDBCompiler, MonetDBDDLCompiler, MonetDBTypeCompiler
from sqlalchemy_monetdb_adbc.reflection import MonetDBReflection
from sqlalchemy_monetdb_adbc.types import (
    DOUBLE_PRECISION,
    HUGEINT,
    INET,
    TINYINT,
    MonetDBBinary,
    MonetDBJSON,
    MonetDBJSONIndexType,
    MonetDBJSONPathType,
)
from sqlalchemy_monetdb_adbc.types import URL as MONETDB_URL

SECURE_BACKEND_NAMES = frozenset({"monetdbs"})

AUTOCOMMIT_OPTION = "adbc.connection.autocommit"


class MonetDBADBCDialect(MonetDBReflection, default.DefaultDialect):
    name = "monetdb"
    driver = "adbc"

    supports_statement_cache = True

    statement_compiler = MonetDBCompiler
    ddl_compiler = MonetDBDDLCompiler
    type_compiler_cls = MonetDBTypeCompiler
    preparer = MonetDBIdentifierPreparer
    execution_ctx_cls = MonetDBExecutionContext
    poolclass = pool.QueuePool

    default_paramstyle = "qmark"

    # MonetDB folds unquoted identifiers to lower case, like PostgreSQL.
    requires_name_normalize = False

    supports_native_boolean = True
    supports_native_decimal = True
    supports_native_uuid = True
    supports_sequences = True
    sequences_optional = True
    supports_multivalues_insert = True
    supports_is_distinct_from = True
    supports_comments = True
    supports_default_values = False
    supports_empty_insert = False
    supports_default_metavalue = False

    # MonetDB has no lastrowid over ADBC, but it does support RETURNING for
    # INSERT, UPDATE and DELETE, which is how generated values are fetched.
    postfetch_lastrowid = False
    insert_returning = True
    update_returning = True
    delete_returning = True
    # executemany discards the RETURNING result set, so it cannot be used there.
    # insert_executemany_returning is a memoized_property on DefaultDialect; a
    # plain class attribute shadows it, which is how other dialects pin it too.
    insert_executemany_returning: bool = False  # pyright: ignore[reportIncompatibleVariableOverride]
    insert_executemany_returning_sort_by_parameter_order: bool = False  # pyright: ignore[reportIncompatibleVariableOverride]
    update_executemany_returning = False
    delete_executemany_returning = False

    # DRIVER-WORKAROUND(adbc-driver-monetdb #1): ExecuteQuery reports
    # rows_affected as 0 for DML, while ExecuteUpdate reports it correctly.
    # The DB-API layer calls ExecuteQuery from execute() and ExecuteUpdate
    # from executemany(). Set both back to True once the driver is fixed.
    supports_sane_rowcount = False
    supports_sane_multi_rowcount = False

    ischema_names: ClassVar[dict[str, type[sqltypes.TypeEngine[Any]]]] = {
        "double": DOUBLE_PRECISION,
        "hugeint": HUGEINT,
        "inet": INET,
        "tinyint": TINYINT,
        "url": MONETDB_URL,
    }

    colspecs: ClassVar[dict[type[sqltypes.TypeEngine[Any]], type[sqltypes.TypeEngine[Any]]]] = {  # pyright: ignore[reportIncompatibleVariableOverride]
        sqltypes.JSON: MonetDBJSON,
        sqltypes.LargeBinary: MonetDBBinary,
        sqltypes.JSON.JSONPathType: MonetDBJSONPathType,
        sqltypes.JSON.JSONIndexType: MonetDBJSONIndexType,
    }

    def __init__(
        self,
        json_serializer: Callable[[Any], str] | None = None,
        json_deserializer: Callable[[str], Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._json_serializer = json_serializer
        self._json_deserializer = json_deserializer

    @classmethod
    def import_dbapi(cls) -> DBAPIModule:
        from adbc_driver_monetdb import dbapi

        return cast(DBAPIModule, dbapi)

    def create_connect_args(self, url: URL) -> tuple[tuple[str], dict[str, Any]]:
        driver_url = url.set(drivername=url.get_backend_name())
        return (driver_url.render_as_string(hide_password=False),), {}

    def _get_server_version_info(self, connection: Any) -> tuple[int, ...]:
        version = connection.exec_driver_sql("SELECT value FROM sys.environment WHERE name = 'monet_version'").scalar()
        return tuple(int(part) for part in str(version).split("."))

    def _get_default_schema_name(self, connection: Any) -> str:
        return str(connection.exec_driver_sql("SELECT CURRENT_SCHEMA").scalar())

    def do_ping(self, dbapi_connection: DBAPIConnection) -> bool:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("SELECT 1")
            cursor.fetchall()
        finally:
            cursor.close()
        return True

    def get_isolation_level_values(self, dbapi_conn: DBAPIConnection) -> tuple[IsolationLevel, ...]:
        # MonetDB runs optimistic-concurrency snapshot transactions and rejects
        # SET TRANSACTION ISOLATION LEVEL; the ADBC isolation option is waived
        # by the driver. Only the two levels that map to real behavior are
        # accepted, so an unsupported request fails loudly.
        return ("SERIALIZABLE", "AUTOCOMMIT")

    def get_isolation_level(self, dbapi_connection: DBAPIConnection) -> IsolationLevel:
        connection = cast(Any, dbapi_connection)
        value = connection.adbc_connection.get_option(AUTOCOMMIT_OPTION)
        return "AUTOCOMMIT" if value == "true" else "SERIALIZABLE"

    def set_isolation_level(self, dbapi_connection: DBAPIConnection, level: IsolationLevel) -> None:
        connection = cast(Any, dbapi_connection)
        autocommit = level == "AUTOCOMMIT"

        if connection._autocommit == autocommit:
            return

        connection._conn.set_autocommit(autocommit)
        connection._autocommit = autocommit
        connection._commit_supported = not autocommit

    def do_commit(self, dbapi_connection: PoolProxiedConnection) -> None:
        dbapi_connection.commit()

    def do_rollback(self, dbapi_connection: PoolProxiedConnection) -> None:
        dbapi_connection.rollback()

    def do_execute(
        self,
        cursor: DBAPICursor,
        statement: str,
        parameters: Any,
        context: Any = None,
    ) -> None:
        cursor.execute(statement, parameters)

    def do_executemany(
        self,
        cursor: DBAPICursor,
        statement: str,
        parameters: Any,
        context: Any = None,
    ) -> None:
        cursor.executemany(statement, parameters)
