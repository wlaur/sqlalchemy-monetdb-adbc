from collections.abc import Callable
from typing import Any, cast

from sqlalchemy import exc, text, util
from sqlalchemy.engine import Connection, reflection
from sqlalchemy.sql import sqltypes

from sqlalchemy_monetdb_adbc.constants import (
    TABLE_TYPE_LOCAL_TEMPORARY,
    TABLE_TYPE_MERGE,
    TABLE_TYPE_REMOTE,
    TABLE_TYPE_REPLICA,
    TABLE_TYPE_TABLE,
    TABLE_TYPE_VIEW,
)
from sqlalchemy_monetdb_adbc.types import MONETDB_TYPE_MAP


def resolve_type(type_name: str, digits: int, scale: int) -> sqltypes.TypeEngine[Any]:
    if type_name == "varchar" and digits == 0:
        return sqltypes.TEXT()

    impl = MONETDB_TYPE_MAP.get(type_name)
    if impl is None:
        util.warn(f"Did not recognize MonetDB type {type_name!r}")
        return sqltypes.NULLTYPE

    if type_name == "timestamptz":
        return sqltypes.TIMESTAMP(timezone=True)
    if type_name == "timetz":
        return sqltypes.TIME(timezone=True)

    constructor = cast(Callable[..., sqltypes.TypeEngine[Any]], impl)
    if type_name in {"char", "varchar"}:
        return constructor(digits)
    if type_name == "decimal":
        return constructor(digits, scale)

    return constructor()


class MonetDBReflection:
    """MonetDB catalog reflection.

    The ADBC ``GetObjects`` API is intentionally not used: it cannot express
    MonetDB indexes, sequences, check constraints, comments, or referential
    actions, and it would require a second Arrow round trip for data the
    catalog already returns on the same session.
    """

    @reflection.cache
    def _schema_id(self, connection: Connection, schema: str | None, **kw: Any) -> int:
        if schema is None:
            query = text("SELECT id FROM sys.schemas WHERE name = CURRENT_SCHEMA")
            schema_id = connection.execute(query).scalar()
        else:
            query = text("SELECT id FROM sys.schemas WHERE name = :schema")
            schema_id = connection.execute(query, {"schema": schema}).scalar()

        if schema_id is None:
            raise exc.InvalidRequestError(f"schema does not exist: {schema}")

        return int(schema_id)

    @reflection.cache
    def _table_id(self, connection: Connection, table_name: str, schema: str | None = None, **kw: Any) -> int:
        query = text("SELECT id FROM sys.tables WHERE name = :name AND schema_id = :schema_id")
        table_id = connection.execute(
            query,
            {"name": table_name, "schema_id": self._schema_id(connection, schema)},
        ).scalar()

        if table_id is None and schema is None:
            # MonetDB puts local temporary tables in the "tmp" schema rather
            # than the session's current schema. Match only genuine temporary
            # tables, so an unrelated table in "tmp" cannot shadow a miss.
            table_id = connection.execute(
                text("SELECT id FROM sys.tables WHERE name = :name AND schema_id = :schema_id AND type = :type"),
                {
                    "name": table_name,
                    "schema_id": self._schema_id(connection, "tmp"),
                    "type": TABLE_TYPE_LOCAL_TEMPORARY,
                },
            ).scalar()

        if table_id is None:
            raise exc.NoSuchTableError(f"{schema}.{table_name}" if schema else table_name)

        return int(table_id)

    @reflection.cache
    def get_schema_names(self, connection: Connection, **kw: Any) -> list[str]:
        rows = connection.execute(text("SELECT name FROM sys.schemas ORDER BY name"))
        return [row[0] for row in rows]

    @reflection.cache
    def get_table_names(self, connection: Connection, schema: str | None = None, **kw: Any) -> list[str]:
        query = text(
            "SELECT name FROM sys.tables "
            "WHERE system = FALSE AND type IN (:table, :merge, :remote, :replica) AND schema_id = :schema_id "
            "ORDER BY name"
        )
        rows = connection.execute(
            query,
            {
                "table": TABLE_TYPE_TABLE,
                "merge": TABLE_TYPE_MERGE,
                "remote": TABLE_TYPE_REMOTE,
                "replica": TABLE_TYPE_REPLICA,
                "schema_id": self._schema_id(connection, schema),
            },
        )
        return [row[0] for row in rows]

    @reflection.cache
    def get_temp_table_names(self, connection: Connection, schema: str | None = None, **kw: Any) -> list[str]:
        if schema is not None:
            # Temporary tables live in "tmp" and belong to no user schema.
            return []

        query = text(
            "SELECT name FROM sys.tables "
            "WHERE type = :type AND schema_id = (SELECT id FROM sys.schemas WHERE name = 'tmp') "
            "ORDER BY name"
        )
        rows = connection.execute(query, {"type": TABLE_TYPE_LOCAL_TEMPORARY})
        return [row[0] for row in rows]

    @reflection.cache
    def get_view_names(self, connection: Connection, schema: str | None = None, **kw: Any) -> list[str]:
        query = text(
            "SELECT name FROM sys.tables WHERE system = FALSE AND type = :type AND schema_id = :schema_id ORDER BY name"
        )
        rows = connection.execute(
            query,
            {"type": TABLE_TYPE_VIEW, "schema_id": self._schema_id(connection, schema)},
        )
        return [row[0] for row in rows]

    @reflection.cache
    def get_view_definition(self, connection: Connection, view_name: str, schema: str | None = None, **kw: Any) -> str:
        query = text("SELECT query FROM sys.tables WHERE type = :type AND name = :name AND schema_id = :schema_id")
        definition = connection.execute(
            query,
            {
                "type": TABLE_TYPE_VIEW,
                "name": view_name,
                "schema_id": self._schema_id(connection, schema),
            },
        ).scalar()

        if definition is None:
            raise exc.NoSuchTableError(f"{schema}.{view_name}" if schema else view_name)

        return str(definition)

    @reflection.cache
    def get_sequence_names(self, connection: Connection, schema: str | None = None, **kw: Any) -> list[str]:
        # An AUTO_INCREMENT column owns a generated sequence, named in that
        # column's default. Only sequences the user declared are listed.
        query = text(
            "SELECT s.name FROM sys.sequences s WHERE s.schema_id = :schema_id AND NOT EXISTS ("
            'SELECT 1 FROM sys.columns c WHERE c."default" = '
            "'next value for \"' || (SELECT n.name FROM sys.schemas n WHERE n.id = s.schema_id) "
            "|| '\".\"' || s.name || '\"') ORDER BY s.name"
        )
        rows = connection.execute(query, {"schema_id": self._schema_id(connection, schema)})
        return [row[0] for row in rows]

    @reflection.cache
    def has_table(self, connection: Connection, table_name: str, schema: str | None = None, **kw: Any) -> bool:
        try:
            self._table_id(connection, table_name, schema, **kw)
        except (exc.InvalidRequestError, exc.NoSuchTableError):
            return False
        return True

    @reflection.cache
    def has_sequence(self, connection: Connection, sequence_name: str, schema: str | None = None, **kw: Any) -> bool:
        query = text("SELECT 1 FROM sys.sequences WHERE name = :name AND schema_id = :schema_id")
        try:
            schema_id = self._schema_id(connection, schema)
        except exc.InvalidRequestError:
            return False
        return connection.execute(query, {"name": sequence_name, "schema_id": schema_id}).scalar() is not None

    def _resolve_type(self, type_name: str, digits: int, scale: int) -> sqltypes.TypeEngine[Any]:
        return resolve_type(type_name, digits, scale)
