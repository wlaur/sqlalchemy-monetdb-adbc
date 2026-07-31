"""Alembic support.

Alembic resolves a migration implementation by dialect name from the subclasses
of ``DefaultImpl`` that have been imported, so defining this class registers
MonetDB. Alembic is not a runtime dependency, so ``dialect`` imports this module
inside a ``try``.
"""

from typing import Any, Literal, cast

from alembic.ddl.base import (
    ColumnType,
    alter_column,
    alter_table,
    format_type,  # pyright: ignore[reportUnknownVariableType]
)
from alembic.ddl.impl import DefaultImpl
from sqlalchemy import ForeignKeyConstraint
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.compiler import DDLCompiler
from sqlalchemy.sql.type_api import TypeEngine

from sqlalchemy_monetdb_adbc.types import PydanticJSON


def _unquote_string_default(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


class MonetDBImpl(DefaultImpl):
    __dialect__ = "monetdb"

    transactional_ddl = True

    def correct_for_autogen_foreignkeys(
        self,
        conn_fks: set[ForeignKeyConstraint],
        metadata_fks: set[ForeignKeyConstraint],
    ) -> None:
        conn_fk_by_sig = {
            self._create_reflected_constraint_sig(foreign_key).unnamed_no_options: foreign_key
            for foreign_key in conn_fks
        }
        metadata_fk_by_sig = {
            self._create_metadata_constraint_sig(foreign_key).unnamed_no_options: foreign_key
            for foreign_key in metadata_fks
        }

        for signature in conn_fk_by_sig.keys() & metadata_fk_by_sig.keys():
            conn_fk = conn_fk_by_sig[signature]
            metadata_fk = metadata_fk_by_sig[signature]
            if metadata_fk.ondelete is None and conn_fk.ondelete == "RESTRICT":
                conn_fk.ondelete = None
            if metadata_fk.onupdate is None and conn_fk.onupdate == "RESTRICT":
                conn_fk.onupdate = None

    def render_type(self, type_obj: Any, autogen_context: Any) -> str | Literal[False]:
        # A migration describes the database schema, so a PydanticJSON column
        # renders as the JSON column it actually is. Rendering the model would
        # make migrations import application code and break as soon as that
        # model is renamed or removed.
        if isinstance(type_obj, PydanticJSON):
            autogen_context.imports.add("import sqlalchemy as sa")
            return "sa.JSON()"
        return False

    def compare_server_default(
        self,
        inspector_column: Any,
        metadata_column: Any,
        rendered_metadata_default: str | None,
        rendered_inspector_default: str | None,
    ) -> bool:
        # MonetDB echoes a string default back with its quotes, so compare the
        # unquoted forms rather than reporting every such column as changed.
        if rendered_inspector_default is not None:
            rendered_inspector_default = _unquote_string_default(rendered_inspector_default)
        if rendered_metadata_default is not None:
            rendered_metadata_default = _unquote_string_default(rendered_metadata_default)
        return rendered_inspector_default != rendered_metadata_default


@compiles(ColumnType, "monetdb")
def _monetdb_column_type(  # pyright: ignore[reportUnusedFunction]
    element: ColumnType,
    compiler: DDLCompiler,
    **kw: Any,  # noqa: ARG001
) -> str:
    # MonetDB spells this "ALTER COLUMN <name> <type>", without the TYPE keyword
    # the default rendering emits.
    table = alter_table(compiler, element.table_name, element.schema)
    column = alter_column(compiler, element.column_name)
    column_type = cast(TypeEngine[Any], element.type_)  # pyright: ignore[reportUnknownMemberType]
    rendered = format_type(compiler, column_type)
    return f"{table} {column} {rendered}"
