import re
from collections import defaultdict
from collections.abc import Callable
from typing import Any, cast

from sqlalchemy import exc, text
from sqlalchemy.engine import Connection, reflection
from sqlalchemy.engine.interfaces import (
    ReflectedCheckConstraint,
    ReflectedColumn,
    ReflectedForeignKeyConstraint,
    ReflectedIndex,
    ReflectedPrimaryKeyConstraint,
    ReflectedTableComment,
    ReflectedUniqueConstraint,
)
from sqlalchemy.sql import sqltypes

from sqlalchemy_monetdb_adbc.types import MONETDB_TYPE_MAP

AUTOINCREMENT_DEFAULT = re.compile(r'^next value for "(?P<schema>[^"]+)"\."(?P<sequence>[^"]+)"$')

TABLE_TYPE_TABLE = 0
TABLE_TYPE_VIEW = 1
TABLE_TYPE_MERGE = 3
TABLE_TYPE_REMOTE = 5
TABLE_TYPE_REPLICA = 6
TABLE_TYPE_LOCAL_TEMPORARY = 30

KEY_TYPE_PRIMARY = 0
KEY_TYPE_UNIQUE = 1
KEY_TYPE_FOREIGN = 2
KEY_TYPE_CHECK = 4

FK_ACTIONS = {
    0: "NO ACTION",
    1: "CASCADE",
    2: "RESTRICT",
    3: "SET NULL",
    4: "SET DEFAULT",
}


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
        query = text("SELECT name FROM sys.sequences WHERE schema_id = :schema_id ORDER BY name")
        rows = connection.execute(query, {"schema_id": self._schema_id(connection, schema)})
        return [row[0] for row in rows]

    @reflection.cache
    def has_table(self, connection: Connection, table_name: str, schema: str | None = None, **kw: Any) -> bool:
        query = text("SELECT 1 FROM sys.tables WHERE name = :name AND schema_id = :schema_id")
        try:
            schema_id = self._schema_id(connection, schema)
        except exc.InvalidRequestError:
            return False
        return connection.execute(query, {"name": table_name, "schema_id": schema_id}).scalar() is not None

    @reflection.cache
    def has_sequence(self, connection: Connection, sequence_name: str, schema: str | None = None, **kw: Any) -> bool:
        query = text("SELECT 1 FROM sys.sequences WHERE name = :name AND schema_id = :schema_id")
        try:
            schema_id = self._schema_id(connection, schema)
        except exc.InvalidRequestError:
            return False
        return connection.execute(query, {"name": sequence_name, "schema_id": schema_id}).scalar() is not None

    @reflection.cache
    def has_index(
        self,
        connection: Connection,
        table_name: str,
        index_name: str,
        schema: str | None = None,
        **kw: Any,
    ) -> bool:
        try:
            indexes = self.get_indexes(connection, table_name, schema, **kw)
        except exc.NoSuchTableError:
            return False
        return any(index["name"] == index_name for index in indexes)

    def _resolve_type(self, type_name: str, digits: int, scale: int) -> sqltypes.TypeEngine[Any]:
        if type_name == "varchar" and digits == 0:
            return sqltypes.TEXT()

        impl = MONETDB_TYPE_MAP.get(type_name)
        if impl is None:
            raise exc.InvalidRequestError(f"unknown MonetDB type: {type_name}")

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

    @reflection.cache
    def get_columns(
        self, connection: Connection, table_name: str, schema: str | None = None, **kw: Any
    ) -> list[ReflectedColumn]:
        table_id = self._table_id(connection, table_name, schema)
        query = text(
            'SELECT c.name, c.type, c.type_digits, c.type_scale, c."null", c."default", c.number, cm.remark '
            "FROM sys.columns c LEFT JOIN sys.comments cm ON c.id = cm.id "
            "WHERE c.table_id = :table_id ORDER BY c.number"
        )
        rows = connection.execute(query, {"table_id": table_id}).all()

        columns: list[ReflectedColumn] = []
        for row in rows:
            name, type_name, digits, scale, nullable, default, _number, comment = row
            autoincrement = False
            column_default = default

            if default is not None and AUTOINCREMENT_DEFAULT.match(default) is not None:
                autoincrement = True
                column_default = None

            columns.append(
                ReflectedColumn(
                    name=name,
                    type=self._resolve_type(type_name, digits, scale),
                    nullable=bool(nullable),
                    default=column_default,
                    autoincrement=autoincrement,
                    comment=comment,
                )
            )

        return columns

    @reflection.cache
    def get_pk_constraint(
        self, connection: Connection, table_name: str, schema: str | None = None, **kw: Any
    ) -> ReflectedPrimaryKeyConstraint:
        table_id = self._table_id(connection, table_name, schema)
        query = text(
            "SELECT k.name, o.name FROM sys.keys k JOIN sys.objects o ON k.id = o.id "
            "WHERE k.table_id = :table_id AND k.type = :key_type ORDER BY o.nr"
        )
        rows = connection.execute(query, {"table_id": table_id, "key_type": KEY_TYPE_PRIMARY}).all()

        if not rows:
            return ReflectedPrimaryKeyConstraint(constrained_columns=[], name=None)

        return ReflectedPrimaryKeyConstraint(
            constrained_columns=[row[1] for row in rows],
            name=rows[0][0],
        )

    @reflection.cache
    def get_foreign_keys(
        self, connection: Connection, table_name: str, schema: str | None = None, **kw: Any
    ) -> list[ReflectedForeignKeyConstraint]:
        table_id = self._table_id(connection, table_name, schema)
        query = text(
            "SELECT fkk.name, fkc.name, ps.name, pkt.name, pkc.name, fkk.action, fkc.nr "
            "FROM sys.keys fkk "
            "JOIN sys.objects fkc ON fkk.id = fkc.id "
            "JOIN sys.keys pkk ON fkk.rkey = pkk.id "
            "JOIN sys.objects pkc ON pkk.id = pkc.id AND fkc.nr = pkc.nr "
            "JOIN sys.tables pkt ON pkk.table_id = pkt.id "
            "JOIN sys.schemas ps ON pkt.schema_id = ps.id "
            "WHERE fkk.table_id = :table_id ORDER BY fkk.name, fkc.nr"
        )
        rows = connection.execute(query, {"table_id": table_id}).all()

        grouped: dict[str, ReflectedForeignKeyConstraint] = {}
        for name, local_column, referred_schema, referred_table, referred_column, action, _nr in rows:
            constraint = grouped.get(name)
            if constraint is None:
                on_delete = FK_ACTIONS.get(action & 255, "NO ACTION")
                on_update = FK_ACTIONS.get((action >> 8) & 255, "NO ACTION")
                options: dict[str, Any] = {}
                if on_delete != "NO ACTION":
                    options["ondelete"] = on_delete
                if on_update != "NO ACTION":
                    options["onupdate"] = on_update

                constraint = ReflectedForeignKeyConstraint(
                    name=name,
                    constrained_columns=[],
                    referred_schema=referred_schema if schema is not None else None,
                    referred_table=referred_table,
                    referred_columns=[],
                    options=options,
                )
                grouped[name] = constraint

            constraint["constrained_columns"].append(local_column)
            constraint["referred_columns"].append(referred_column)

        return list(grouped.values())

    @reflection.cache
    def get_unique_constraints(
        self, connection: Connection, table_name: str, schema: str | None = None, **kw: Any
    ) -> list[ReflectedUniqueConstraint]:
        table_id = self._table_id(connection, table_name, schema)
        query = text(
            "SELECT k.name, o.name FROM sys.keys k JOIN sys.objects o ON k.id = o.id "
            "WHERE k.table_id = :table_id AND k.type = :key_type ORDER BY k.name, o.nr"
        )
        rows = connection.execute(query, {"table_id": table_id, "key_type": KEY_TYPE_UNIQUE}).all()

        grouped: dict[str, list[str]] = defaultdict(list)
        for name, column in rows:
            grouped[name].append(column)

        return [ReflectedUniqueConstraint(name=name, column_names=columns) for name, columns in grouped.items()]

    @reflection.cache
    def get_indexes(
        self, connection: Connection, table_name: str, schema: str | None = None, **kw: Any
    ) -> list[ReflectedIndex]:
        table_id = self._table_id(connection, table_name, schema)
        # A MonetDB UNIQUE constraint owns both a key and an index of the same
        # name; report those as unique indexes. Primary and foreign key indexes
        # are internal and are reported through their own constraint methods.
        query = text(
            "SELECT i.name, o.name, o.nr, k.type FROM sys.idxs i "
            "JOIN sys.objects o ON i.id = o.id "
            "LEFT JOIN sys.keys k ON k.name = i.name AND k.table_id = i.table_id "
            "WHERE i.table_id = :table_id AND (k.type IS NULL OR k.type = :unique) "
            "ORDER BY i.name, o.nr"
        )
        rows = connection.execute(query, {"table_id": table_id, "unique": KEY_TYPE_UNIQUE}).all()

        grouped: dict[str, list[str]] = defaultdict(list)
        unique_names: set[str] = set()
        for name, column, _nr, key_type in rows:
            grouped[name].append(column)
            if key_type == KEY_TYPE_UNIQUE:
                unique_names.add(name)

        indexes: list[ReflectedIndex] = []
        for name, columns in grouped.items():
            index = ReflectedIndex(name=name, column_names=list(columns), unique=name in unique_names)
            if name in unique_names:
                index["duplicates_constraint"] = name
            indexes.append(index)

        return indexes

    @reflection.cache
    def get_check_constraints(
        self, connection: Connection, table_name: str, schema: str | None = None, **kw: Any
    ) -> list[ReflectedCheckConstraint]:
        table_id = self._table_id(connection, table_name, schema)
        resolved_schema = schema
        if resolved_schema is None:
            resolved_schema = connection.execute(text("SELECT CURRENT_SCHEMA")).scalar()

        query = text(
            "SELECT k.name, sys.check_constraint(:schema, k.name) FROM sys.keys k "
            "WHERE k.table_id = :table_id AND k.type = :key_type ORDER BY k.name"
        )
        rows = connection.execute(
            query, {"table_id": table_id, "schema": resolved_schema, "key_type": KEY_TYPE_CHECK}
        ).all()

        return [ReflectedCheckConstraint(name=name, sqltext=sqltext) for name, sqltext in rows]

    @reflection.cache
    def get_table_comment(
        self, connection: Connection, table_name: str, schema: str | None = None, **kw: Any
    ) -> ReflectedTableComment:
        table_id = self._table_id(connection, table_name, schema)
        query = text("SELECT remark FROM sys.comments WHERE id = :table_id")
        return ReflectedTableComment(text=connection.execute(query, {"table_id": table_id}).scalar())
