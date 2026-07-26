"""Batched reflection.

SQLAlchemy asks a dialect for one kind of object across every table at once, and
falls back to looping the single-table methods when the dialect does not
implement it. On MonetDB that loop is expensive: reflecting twenty tables took
443 statements, each paying a full server-side PREPARE. These methods answer the
same questions with one statement per kind.
"""

from collections import defaultdict
from collections.abc import Collection, Iterator
from typing import Any, cast

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection, ObjectKind, ObjectScope, reflection
from sqlalchemy.engine.interfaces import (
    ReflectedCheckConstraint,
    ReflectedColumn,
    ReflectedForeignKeyConstraint,
    ReflectedIndex,
    ReflectedPrimaryKeyConstraint,
    ReflectedTableComment,
    ReflectedUniqueConstraint,
    TableKey,
)
from sqlalchemy.sql.elements import BindParameter

from sqlalchemy_monetdb_adbc.constants import (
    AUTOINCREMENT_DEFAULT,
    FK_ACTIONS,
    KEY_TYPE_CHECK,
    KEY_TYPE_PRIMARY,
    KEY_TYPE_UNIQUE,
    TABLE_TYPE_LOCAL_TEMPORARY,
    TABLE_TYPE_VIEW,
    TABLE_TYPES,
)
from sqlalchemy_monetdb_adbc.reflection import resolve_type


class MonetDBMultiReflection:
    def _resolve_request(
        self, connection: Connection, kw: dict[str, Any]
    ) -> tuple[str | None, dict[int, str], frozenset[int]]:
        """Pull the reflection arguments SQLAlchemy passes and resolve the tables."""
        schema = cast(str | None, kw.get("schema"))
        filter_names = cast("Collection[str] | None", kw.get("filter_names"))
        tables, temporary = self._resolve_tables(
            connection,
            schema,
            tuple(filter_names) if filter_names else None,
            cast(ObjectScope, kw.get("scope", ObjectScope.DEFAULT)),
            cast(ObjectKind, kw.get("kind", ObjectKind.TABLE)),
            info_cache=kw.get("info_cache"),
        )
        return schema, tables, temporary

    @reflection.cache
    def _resolve_tables(
        self,
        connection: Connection,
        schema: str | None,
        filter_names: tuple[str, ...] | None,
        scope: ObjectScope,
        kind: ObjectKind,
        **kw: Any,
    ) -> tuple[dict[int, str], frozenset[int]]:
        """Resolve the requested tables, and which of them are temporary.

        Temporary tables keep their keys and indexes in the "tmp" catalog
        rather than "sys". Querying each catalog separately lets the common
        case, where nothing temporary is in scope, skip "tmp" entirely.
        """
        types: list[int] = []
        if ObjectKind.TABLE in kind:
            types.extend(TABLE_TYPES)
        if ObjectKind.VIEW in kind:
            types.append(TABLE_TYPE_VIEW)
        # MonetDB has no materialized views.

        scopes: list[str] = []
        parameters: dict[str, Any] = {}

        if types and ObjectScope.DEFAULT in scope:
            scopes.append("(s.name = :schema AND t.type IN :types)")
            parameters["schema"] = schema if schema is not None else self._current_schema(connection)
            parameters["types"] = list(types)
        if ObjectScope.TEMPORARY in scope and ObjectKind.TABLE in kind and schema is None:
            # MonetDB keeps local temporary tables in "tmp", scoped to the
            # session. They belong to no user schema, so they are reported
            # only when the caller asked for the default one.
            scopes.append("(s.name = 'tmp' AND t.type = :temp_type)")
            parameters["temp_type"] = TABLE_TYPE_LOCAL_TEMPORARY

        if not scopes:
            return {}, frozenset()

        sql = (
            "SELECT t.id, t.name, t.type FROM sys.tables t JOIN sys.schemas s ON t.schema_id = s.id "
            f"WHERE t.system = FALSE AND ({' OR '.join(scopes)})"
        )
        binds: list[BindParameter[Any]] = [bindparam("types", expanding=True)] if "types" in parameters else []

        if filter_names:
            sql += " AND t.name IN :names"
            parameters["names"] = list(filter_names)
            binds.append(bindparam("names", expanding=True))

        statement = text(sql).bindparams(*binds) if binds else text(sql)
        rows = connection.execute(statement, parameters).all()
        tables = {row[0]: row[1] for row in rows}
        temporary = frozenset(row[0] for row in rows if row[2] == TABLE_TYPE_LOCAL_TEMPORARY)
        return tables, temporary

    def _current_schema(self, connection: Connection) -> str:
        return str(connection.execute(text("SELECT CURRENT_SCHEMA")).scalar())

    def _fetch(
        self,
        connection: Connection,
        sql: str,
        tables: dict[int, str],
        temporary: frozenset[int] = frozenset(),
        **extra: Any,
    ) -> list[Any]:
        """Run a catalog query, reading "tmp" as well when temp tables are in scope.

        ``sql`` names its catalog tables as ``{c}keys``, ``{c}objects`` and
        ``{c}idxs`` so the same statement serves both catalogs.
        """
        rows: list[Any] = []
        groups = [("sys.", [i for i in tables if i not in temporary])]
        if temporary:
            groups.append(("tmp.", [i for i in tables if i in temporary]))

        for prefix, ids in groups:
            if not ids:
                continue
            statement = text(sql.format(c=prefix)).bindparams(bindparam("ids", expanding=True))
            rows.extend(connection.execute(statement, {"ids": ids, **extra}).all())

        return rows

    def get_multi_columns(
        self,
        connection: Connection,
        **kw: Any,
    ) -> Iterator[tuple[TableKey, list[ReflectedColumn]]]:
        schema, tables, temporary = self._resolve_request(connection, kw)
        if not tables:
            return

        rows = self._fetch(
            connection,
            'SELECT c.table_id, c.name, c.type, c.type_digits, c.type_scale, c."null", c."default", cm.remark '
            "FROM sys.columns c LEFT JOIN sys.comments cm ON c.id = cm.id "
            "WHERE c.table_id IN :ids ORDER BY c.table_id, c.number",
            tables,
            temporary,
        )

        grouped: dict[int, list[ReflectedColumn]] = defaultdict(list)
        for table_id, name, type_name, digits, scale, nullable, default, comment in rows:
            autoincrement = default is not None and AUTOINCREMENT_DEFAULT.match(default) is not None
            grouped[table_id].append(
                ReflectedColumn(
                    name=name,
                    type=resolve_type(type_name, digits, scale),
                    nullable=bool(nullable),
                    default=None if autoincrement else default,
                    autoincrement=autoincrement,
                    comment=comment,
                )
            )

        for table_id, name in tables.items():
            yield (schema, name), grouped[table_id]

    def get_multi_pk_constraint(
        self,
        connection: Connection,
        **kw: Any,
    ) -> Iterator[tuple[TableKey, ReflectedPrimaryKeyConstraint]]:
        schema, tables, temporary = self._resolve_request(connection, kw)
        if not tables:
            return

        rows = self._fetch(
            connection,
            "SELECT k.table_id, k.name, o.name FROM {c}keys k JOIN {c}objects o ON k.id = o.id "
            "WHERE k.table_id IN :ids AND k.type = :key_type ORDER BY k.table_id, o.nr",
            tables,
            temporary,
            key_type=KEY_TYPE_PRIMARY,
        )

        grouped: dict[int, ReflectedPrimaryKeyConstraint] = {}
        for table_id, name, column in rows:
            constraint = grouped.setdefault(table_id, ReflectedPrimaryKeyConstraint(constrained_columns=[], name=name))
            constraint["constrained_columns"].append(column)

        for table_id, name in tables.items():
            yield (
                (schema, name),
                grouped.get(table_id, ReflectedPrimaryKeyConstraint(constrained_columns=[], name=None)),
            )

    def get_multi_foreign_keys(
        self,
        connection: Connection,
        **kw: Any,
    ) -> Iterator[tuple[TableKey, list[ReflectedForeignKeyConstraint]]]:
        schema, tables, temporary = self._resolve_request(connection, kw)
        if not tables:
            return

        rows = self._fetch(
            connection,
            "SELECT fkk.table_id, fkk.name, fkc.name, ps.name, pkt.name, pkc.name, fkk.action "
            "FROM {c}keys fkk "
            "JOIN {c}objects fkc ON fkk.id = fkc.id "
            "JOIN {c}keys pkk ON fkk.rkey = pkk.id "
            "JOIN {c}objects pkc ON pkk.id = pkc.id AND fkc.nr = pkc.nr "
            "JOIN sys.tables pkt ON pkk.table_id = pkt.id "
            "JOIN sys.schemas ps ON pkt.schema_id = ps.id "
            "WHERE fkk.table_id IN :ids ORDER BY fkk.table_id, fkk.name, fkc.nr",
            tables,
            temporary,
        )

        grouped: dict[int, dict[str, ReflectedForeignKeyConstraint]] = defaultdict(dict)
        for table_id, name, column, referred_schema, referred_table, referred_column, action in rows:
            constraints = grouped[table_id]
            if name not in constraints:
                options: dict[str, Any] = {}
                on_delete = FK_ACTIONS.get(action & 255, "NO ACTION")
                on_update = FK_ACTIONS.get((action >> 8) & 255, "NO ACTION")
                if on_delete != "NO ACTION":
                    options["ondelete"] = on_delete
                if on_update != "NO ACTION":
                    options["onupdate"] = on_update
                constraints[name] = ReflectedForeignKeyConstraint(
                    name=name,
                    constrained_columns=[],
                    referred_schema=referred_schema if schema is not None else None,
                    referred_table=referred_table,
                    referred_columns=[],
                    options=options,
                )
            constraints[name]["constrained_columns"].append(column)
            constraints[name]["referred_columns"].append(referred_column)

        for table_id, name in tables.items():
            yield (schema, name), list(grouped[table_id].values())

    def get_multi_indexes(
        self,
        connection: Connection,
        **kw: Any,
    ) -> Iterator[tuple[TableKey, list[ReflectedIndex]]]:
        schema, tables, temporary = self._resolve_request(connection, kw)
        if not tables:
            return

        rows = self._fetch(
            connection,
            "SELECT i.table_id, i.name, o.name, k.type FROM {c}idxs i "
            "JOIN {c}objects o ON i.id = o.id "
            "LEFT JOIN {c}keys k ON k.name = i.name AND k.table_id = i.table_id "
            "WHERE i.table_id IN :ids AND (k.type IS NULL OR k.type = :unique) "
            "ORDER BY i.table_id, i.name, o.nr",
            tables,
            temporary,
            unique=KEY_TYPE_UNIQUE,
        )

        grouped: dict[int, dict[str, ReflectedIndex]] = defaultdict(dict)
        for table_id, name, column, key_type in rows:
            indexes = grouped[table_id]
            if name not in indexes:
                index = ReflectedIndex(name=name, column_names=[], unique=key_type == KEY_TYPE_UNIQUE)
                if key_type == KEY_TYPE_UNIQUE:
                    index["duplicates_constraint"] = name
                indexes[name] = index
            indexes[name]["column_names"].append(column)

        for table_id, name in tables.items():
            yield (schema, name), list(grouped[table_id].values())

    def get_multi_unique_constraints(
        self,
        connection: Connection,
        **kw: Any,
    ) -> Iterator[tuple[TableKey, list[ReflectedUniqueConstraint]]]:
        schema, tables, temporary = self._resolve_request(connection, kw)
        if not tables:
            return

        rows = self._fetch(
            connection,
            "SELECT k.table_id, k.name, o.name FROM {c}keys k JOIN {c}objects o ON k.id = o.id "
            "WHERE k.table_id IN :ids AND k.type = :key_type ORDER BY k.table_id, k.name, o.nr",
            tables,
            temporary,
            key_type=KEY_TYPE_UNIQUE,
        )

        grouped: dict[int, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        for table_id, name, column in rows:
            grouped[table_id][name].append(column)

        for table_id, name in tables.items():
            yield (
                (schema, name),
                [
                    ReflectedUniqueConstraint(name=constraint, column_names=columns)
                    for constraint, columns in grouped[table_id].items()
                ],
            )

    def get_multi_check_constraints(
        self,
        connection: Connection,
        **kw: Any,
    ) -> Iterator[tuple[TableKey, list[ReflectedCheckConstraint]]]:
        schema, tables, temporary = self._resolve_request(connection, kw)
        if not tables:
            return

        resolved = schema if schema is not None else self._current_schema(connection)
        rows = self._fetch(
            connection,
            "SELECT k.table_id, k.name, sys.check_constraint(:schema, k.name) FROM {c}keys k "
            "WHERE k.table_id IN :ids AND k.type = :key_type ORDER BY k.table_id, k.name",
            tables,
            temporary,
            key_type=KEY_TYPE_CHECK,
            schema=resolved,
        )

        grouped: dict[int, list[ReflectedCheckConstraint]] = defaultdict(list)
        for table_id, name, sqltext in rows:
            grouped[table_id].append(ReflectedCheckConstraint(name=name, sqltext=sqltext))

        for table_id, name in tables.items():
            yield (schema, name), grouped[table_id]

    def get_multi_table_comment(
        self,
        connection: Connection,
        **kw: Any,
    ) -> Iterator[tuple[TableKey, ReflectedTableComment]]:
        schema, tables, temporary = self._resolve_request(connection, kw)
        if not tables:
            return

        rows = self._fetch(
            connection,
            "SELECT c.id, c.remark FROM sys.comments c WHERE c.id IN :ids",
            tables,
            temporary,
        )
        comments = dict(rows)

        for table_id, name in tables.items():
            yield (schema, name), ReflectedTableComment(text=comments.get(table_id))
