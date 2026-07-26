import re
from collections.abc import Callable
from typing import Any, cast

from sqlalchemy import types as sqltypes
from sqlalchemy.sql import compiler, operators
from sqlalchemy.sql.ddl import CreateIndex, CreateSequence, DropSequence
from sqlalchemy.sql.elements import ClauseElement, UnaryExpression
from sqlalchemy.sql.expression import cast as cast_expression
from sqlalchemy.sql.schema import Column, ForeignKeyConstraint, Sequence, Table
from sqlalchemy.sql.selectable import Select

from sqlalchemy_monetdb_adbc.constants import (
    MONETDB_DEFAULT_DECIMAL_PRECISION,
    MONETDB_DEFAULT_DECIMAL_SCALE,
)
from sqlalchemy_monetdb_adbc.ddl import self_referential_foreign_keys

FK_ACTION = re.compile(r"^(?:RESTRICT|CASCADE|SET NULL|NO ACTION|SET DEFAULT)$", re.IGNORECASE)

ORDERING_MODIFIERS = frozenset({operators.asc_op, operators.desc_op, operators.nulls_first_op, operators.nulls_last_op})


def _strip_ordering(expression: Any) -> ClauseElement:
    """Drop ASC/DESC/NULLS modifiers from an index expression.

    MonetDB's CREATE INDEX grammar accepts only bare column expressions; its
    indexes carry no ordering to preserve.
    """
    while isinstance(expression, UnaryExpression) and expression.modifier in ORDERING_MODIFIERS:
        expression = expression.element

    return cast(ClauseElement, expression)


class MonetDBTypeCompiler(compiler.GenericTypeCompiler):
    def _decimal(self, type_: sqltypes.TypeEngine[Any]) -> str:
        """Render DECIMAL, always with an explicit precision and scale.

        MonetDB defaults a bare DECIMAL to (18, 3), so naming it changes
        nothing about the resulting column. It does avoid a server-side crash:
        casting a *parameter* to an unsized DECIMAL, as SQLAlchemy does when
        rendering true division, drops the connection with "unexpected end of
        file". ``CAST(? AS DECIMAL(18, 3))`` is fine, and so is an unsized cast
        of a literal.
        """
        precision = getattr(type_, "precision", None) or MONETDB_DEFAULT_DECIMAL_PRECISION
        scale = getattr(type_, "scale", None)
        if scale is None:
            scale = MONETDB_DEFAULT_DECIMAL_SCALE
        return f"DECIMAL({precision}, {scale})"

    def visit_NUMERIC(self, type_: sqltypes.TypeEngine[Any], **kw: Any) -> str:  # noqa: N802
        return self._decimal(type_)

    def visit_DECIMAL(self, type_: sqltypes.TypeEngine[Any], **kw: Any) -> str:  # noqa: N802
        return self._decimal(type_)

    def visit_numeric(self, type_: sqltypes.TypeEngine[Any], **kw: Any) -> str:
        return self._decimal(type_)

    def visit_TINYINT(self, type_: sqltypes.TypeEngine[Any], **kw: Any) -> str:  # noqa: N802
        return "TINYINT"

    def visit_HUGEINT(self, type_: sqltypes.TypeEngine[Any], **kw: Any) -> str:  # noqa: N802
        return "HUGEINT"

    def visit_INET(self, type_: sqltypes.TypeEngine[Any], **kw: Any) -> str:  # noqa: N802
        return "INET"

    def visit_URL(self, type_: sqltypes.TypeEngine[Any], **kw: Any) -> str:  # noqa: N802
        return "URL"

    def visit_MONTH_INTERVAL(self, type_: sqltypes.TypeEngine[Any], **kw: Any) -> str:  # noqa: N802
        return "INTERVAL MONTH"

    def visit_SECOND_INTERVAL(self, type_: sqltypes.TypeEngine[Any], **kw: Any) -> str:  # noqa: N802
        return "INTERVAL SECOND"

    def visit_DOUBLE_PRECISION(self, type_: sqltypes.TypeEngine[Any], **kw: Any) -> str:  # noqa: N802
        return "DOUBLE PRECISION"

    def visit_double(self, type_: sqltypes.TypeEngine[Any], **kw: Any) -> str:
        return "DOUBLE PRECISION"

    def visit_DOUBLE(self, type_: sqltypes.TypeEngine[Any], **kw: Any) -> str:  # noqa: N802
        return "DOUBLE PRECISION"

    def visit_FLOAT(self, type_: sqltypes.TypeEngine[Any], **kw: Any) -> str:  # noqa: N802
        precision = getattr(type_, "precision", None)
        if precision is None:
            return "DOUBLE PRECISION"
        return f"FLOAT({precision})"

    def visit_REAL(self, type_: sqltypes.TypeEngine[Any], **kw: Any) -> str:  # noqa: N802
        return "REAL"

    def visit_VARCHAR(self, type_: sqltypes.String, **kw: Any) -> str:  # noqa: N802
        if type_.length is None:
            return "CLOB"
        return super().visit_VARCHAR(type_, **kw)

    def visit_NVARCHAR(self, type_: sqltypes.String, **kw: Any) -> str:  # noqa: N802
        return self.visit_VARCHAR(type_, **kw)

    def visit_TEXT(self, type_: sqltypes.TypeEngine[Any], **kw: Any) -> str:  # noqa: N802
        return "CLOB"

    def visit_NTEXT(self, type_: sqltypes.TypeEngine[Any], **kw: Any) -> str:  # noqa: N802
        return "CLOB"

    def visit_unicode(self, type_: sqltypes.String, **kw: Any) -> str:
        return self.visit_VARCHAR(type_, **kw)

    def visit_unicode_text(self, type_: sqltypes.TypeEngine[Any], **kw: Any) -> str:
        return "CLOB"

    def visit_BLOB(self, type_: sqltypes.TypeEngine[Any], **kw: Any) -> str:  # noqa: N802
        return "BLOB"

    def visit_large_binary(self, type_: sqltypes.TypeEngine[Any], **kw: Any) -> str:
        return "BLOB"

    def visit_VARBINARY(self, type_: sqltypes.TypeEngine[Any], **kw: Any) -> str:  # noqa: N802
        return "BLOB"

    def visit_BINARY(self, type_: sqltypes.TypeEngine[Any], **kw: Any) -> str:  # noqa: N802
        return "BLOB"

    def visit_datetime(self, type_: sqltypes.TypeEngine[Any], **kw: Any) -> str:
        return self.visit_TIMESTAMP(type_, **kw)

    def visit_TIMESTAMP(self, type_: sqltypes.TypeEngine[Any], **kw: Any) -> str:  # noqa: N802
        if getattr(type_, "timezone", False):
            return "TIMESTAMP WITH TIME ZONE"
        return "TIMESTAMP"

    def visit_DATETIME(self, type_: sqltypes.TypeEngine[Any], **kw: Any) -> str:  # noqa: N802
        return self.visit_TIMESTAMP(type_, **kw)

    def visit_TIME(self, type_: sqltypes.TypeEngine[Any], **kw: Any) -> str:  # noqa: N802
        # A bare TIME is second-precision in MonetDB, unlike TIMESTAMP, so a
        # SQLAlchemy Time would silently lose microseconds without this.
        precision = getattr(type_, "precision", None)
        rendered = f"TIME({6 if precision is None else precision})"
        if getattr(type_, "timezone", False):
            return f"{rendered} WITH TIME ZONE"
        return rendered

    def visit_uuid(self, type_: sqltypes.Uuid[Any], **kw: Any) -> str:
        if type_.native_uuid:
            return "UUID"
        return super().visit_uuid(type_, **kw)

    def visit_UUID(self, type_: sqltypes.TypeEngine[Any], **kw: Any) -> str:  # noqa: N802
        return "UUID"

    def visit_JSON(self, type_: sqltypes.TypeEngine[Any], **kw: Any) -> str:  # noqa: N802
        return "JSON"

    def visit_json(self, type_: sqltypes.TypeEngine[Any], **kw: Any) -> str:
        return "JSON"


class MonetDBDDLCompiler(compiler.DDLCompiler):
    def visit_create_sequence(self, create: CreateSequence, prefix: str | None = None, **kw: Any) -> str:
        sequence = create.element
        text = f"CREATE SEQUENCE {self.preparer.format_sequence(sequence)} AS BIGINT"
        if sequence.start is not None:
            text += f" START WITH {sequence.start}"
        if sequence.increment is not None:
            text += f" INCREMENT BY {sequence.increment}"
        if sequence.minvalue is not None:
            text += f" MINVALUE {sequence.minvalue}"
        if sequence.maxvalue is not None:
            text += f" MAXVALUE {sequence.maxvalue}"
        if sequence.cache is not None:
            text += f" CACHE {sequence.cache}"
        if sequence.cycle:
            text += " CYCLE"
        return text

    def visit_drop_sequence(self, drop: DropSequence, **kw: Any) -> str:
        return f"DROP SEQUENCE {self.preparer.format_sequence(drop.element)}"

    def create_table_constraints(
        self,
        table: Table,
        _include_foreign_key_constraints: Any = None,
        **kw: Any,
    ) -> str:
        # MonetDB validates a FOREIGN KEY against the referenced PRIMARY KEY as
        # the CREATE TABLE is parsed, so a self-referential one cannot be
        # inlined. The after_create hook in ddl.py adds them by ALTER TABLE
        # once the table, and therefore its primary key, exists.
        deferred = set(self_referential_foreign_keys(table))
        included = _include_foreign_key_constraints
        if included is None:
            included = table.foreign_key_constraints

        return super().create_table_constraints(  # pyright: ignore[reportUnknownMemberType]
            table,
            _include_foreign_key_constraints={fkc for fkc in included if fkc not in deferred},
            **kw,
        )

    def _quoted_column_path(self, column: Column[Any]) -> str:
        # MonetDB's COMMENT ON COLUMN grammar rejects some unquoted identifiers
        # that are legal elsewhere ("id" lexes as its own token), so quote every
        # part unconditionally.
        quote = self.preparer.quote_identifier
        table = column.table
        parts = [quote(table.name), quote(column.name)]
        if table.schema is not None:
            parts.insert(0, quote(table.schema))
        return ".".join(parts)

    def visit_set_column_comment(self, create: Any, **kw: Any) -> str:
        column = cast(Column[Any], create.element)
        comment = self.sql_compiler.render_literal_value(column.comment, sqltypes.String())
        return f"COMMENT ON COLUMN {self._quoted_column_path(column)} IS {comment}"

    def visit_drop_column_comment(self, drop: Any, **kw: Any) -> str:
        column = cast(Column[Any], drop.element)
        return f"COMMENT ON COLUMN {self._quoted_column_path(column)} IS NULL"

    def define_constraint_cascades(self, constraint: ForeignKeyConstraint) -> str:
        text = ""
        validate = cast(
            Callable[[str, re.Pattern[str]], str],
            self.preparer.validate_sql_phrase,  # pyright: ignore[reportUnknownMemberType]
        )
        if constraint.ondelete is not None:
            text += f" ON DELETE {validate(constraint.ondelete, FK_ACTION)}"
        if constraint.onupdate is not None:
            text += f" ON UPDATE {validate(constraint.onupdate, FK_ACTION)}"
        return text

    def get_column_specification(self, column: Column[Any], **kw: Any) -> str:
        colspec = self.preparer.format_column(column)

        if (
            column.primary_key
            and column is column.table._autoincrement_column  # pyright: ignore[reportPrivateUsage]
            and column.identity is None
            and (column.default is None or (isinstance(column.default, Sequence) and column.default.optional))
        ):
            colspec += " " + self.dialect.type_compiler_instance.process(column.type, type_expression=column)
            colspec += " AUTO_INCREMENT"
        else:
            colspec += " " + self.dialect.type_compiler_instance.process(column.type, type_expression=column)
            default = self.get_column_default_string(column)
            if default is not None:
                colspec += " DEFAULT " + default

        if column.identity is not None:
            colspec += " " + self.process(column.identity)

        if not column.nullable:
            colspec += " NOT NULL"

        return colspec

    def visit_identity_column(self, identity: Any, **kw: Any) -> str:
        text = "GENERATED {} AS IDENTITY".format("ALWAYS" if identity.always else "BY DEFAULT")
        options = self.get_identity_options(identity)
        if options:
            text += f" ({options})"
        return text

    def visit_create_index(
        self,
        create: CreateIndex,
        include_schema: bool = False,
        include_table_schema: bool = True,
        **kw: Any,
    ) -> str:
        index = create.element
        preparer = self.preparer
        self._verify_index_table(index)

        table = cast(Table, index.table)
        columns = ", ".join(
            self.sql_compiler.process(_strip_ordering(expr), include_table=False, literal_binds=True)
            for expr in index.expressions
        )

        name = self._prepared_index_name(index, include_schema=False)
        formatted_table = preparer.format_table(table)

        # MonetDB has no CREATE UNIQUE INDEX; uniqueness is a table constraint.
        if index.unique:
            return f"ALTER TABLE {formatted_table} ADD CONSTRAINT {name} UNIQUE ({columns})"

        text = "CREATE INDEX "
        if create.if_not_exists:
            text += "IF NOT EXISTS "
        text += f"{name} ON {formatted_table} ({columns})"
        return text


class MonetDBCompiler(compiler.SQLCompiler):
    def visit_sequence(self, sequence: Sequence, **kw: Any) -> str:
        # self.preparer, not self.dialect.identifier_preparer: only the
        # compiler's own preparer applies a schema_translate_map, and without it
        # an inline sequence keeps the untranslated schema while the table
        # around it gets translated.
        return f"NEXT VALUE FOR {self.preparer.format_sequence(sequence)}"

    def returning_clause(
        self,
        stmt: Any,
        returning_cols: Any,
        *,
        populate_result_map: bool,
        **kw: Any,
    ) -> str:
        # MonetDB rejects table-qualified names in RETURNING.
        kw["include_table"] = False
        return super().returning_clause(stmt, returning_cols, populate_result_map=populate_result_map, **kw)

    def limit_clause(self, select: Select[Any], **kw: Any) -> str:
        text = ""
        if select._limit_clause is not None:  # pyright: ignore[reportPrivateUsage]
            text += "\n LIMIT " + self.process(select._limit_clause, **kw)  # pyright: ignore[reportPrivateUsage]
        if select._offset_clause is not None:  # pyright: ignore[reportPrivateUsage]
            text += "\n OFFSET " + self.process(select._offset_clause, **kw)  # pyright: ignore[reportPrivateUsage]
        return text

    def visit_empty_set_expr(self, element_types: list[sqltypes.TypeEngine[Any]], **kw: Any) -> str:
        types = element_types or [sqltypes.INTEGER()]
        casts = ", ".join(
            "CAST(NULL AS {})".format(
                self.dialect.type_compiler_instance.process(
                    sqltypes.INTEGER() if type_._isnull else type_  # pyright: ignore[reportPrivateUsage]
                )
            )
            for type_ in types
        )
        return f"SELECT {casts} WHERE 1 <> 1"

    def render_literal_value(self, value: Any, type_: sqltypes.TypeEngine[Any]) -> str:
        rendered = super().render_literal_value(value, type_)
        if isinstance(value, str):
            return rendered.replace("\\", "\\\\")
        return rendered

    def update_from_clause(
        self,
        update_stmt: Any,
        from_table: Any,
        extra_froms: Any,
        from_hints: Any,
        **kw: Any,
    ) -> str:
        return "FROM " + ", ".join(
            table._compiler_dispatch(self, asfrom=True, fromhints=from_hints, **kw) for table in extra_froms
        )

    def _json_extract(self, binary: Any, _cast_applied: bool = False, **kw: Any) -> str:
        if not _cast_applied and binary.type._type_affinity is not sqltypes.JSON:
            kw["_cast_applied"] = True
            return self.process(cast_expression(binary, binary.type), **kw)

        left = self.process(binary.left, **kw)
        # MonetDB infers no type for a bare parameter here and tries to coerce
        # it to HUGEINT, so the path is cast explicitly.
        right = f"CAST({self.process(binary.right, **kw)} AS STRING)"
        # JSON.FILTER yields '[]' when the path matches nothing; SQLAlchemy
        # expects NULL there, as on other backends.
        filtered = f"NULLIF(JSON.FILTER({left}, {right}), '[]')"

        if binary.type._type_affinity is sqltypes.JSON:
            return filtered

        # JSON.TEXT unwraps a scalar; JSON null must still come back as SQL NULL.
        return f"CASE {filtered} WHEN 'null' THEN NULL ELSE JSON.TEXT({filtered}) END"

    def visit_json_getitem_op_binary(self, binary: Any, operator: Any, **kw: Any) -> str:
        return self._json_extract(binary, **kw)

    def visit_json_path_getitem_op_binary(self, binary: Any, operator: Any, **kw: Any) -> str:
        return self._json_extract(binary, **kw)

    def visit_regexp_replace_op_binary(self, binary: Any, operator: Any, **kw: Any) -> str:
        flags = binary.modifiers["flags"]
        string = self.process(binary.left, **kw)
        pattern_replace = self.process(binary.right, **kw)
        if flags is None:
            return f"REGEXP_REPLACE({string}, {pattern_replace})"
        return f"REGEXP_REPLACE({string}, {pattern_replace}, {self.render_literal_value(flags, sqltypes.STRINGTYPE)})"
