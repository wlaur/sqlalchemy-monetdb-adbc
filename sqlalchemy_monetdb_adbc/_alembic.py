"""Alembic support.

Alembic resolves a migration implementation by dialect name from the subclasses
of ``DefaultImpl`` that have been imported, so defining this class registers
MonetDB. Alembic is not a runtime dependency, so ``dialect`` imports this module
inside a ``try``.
"""

from typing import Any

from alembic.ddl.impl import DefaultImpl
from sqlalchemy.sql.type_api import TypeEngine


class MonetDBImpl(DefaultImpl):
    __dialect__ = "monetdb"

    transactional_ddl = True

    def alter_column(
        self,
        table_name: str,
        column_name: str,
        *,
        type_: TypeEngine[Any] | None = None,
        existing_type: TypeEngine[Any] | None = None,
        **kw: Any,
    ) -> None:
        if type_ is not None:
            # MonetDB has no ALTER TABLE ... ALTER COLUMN ... TYPE. Reaching the
            # server would only produce a syntax error, so say what is wrong and
            # what to do instead.
            raise NotImplementedError(
                f"MonetDB cannot change the type of an existing column "
                f"({table_name}.{column_name} to {type_}). Add a new column, copy the "
                f"values across, drop the old column, then rename the new one."
            )

        super().alter_column(  # pyright: ignore[reportUnknownMemberType]
            table_name, column_name, type_=type_, existing_type=existing_type, **kw
        )

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
            rendered_inspector_default = rendered_inspector_default.strip("'")
        if rendered_metadata_default is not None:
            rendered_metadata_default = rendered_metadata_default.strip("'")
        return rendered_inspector_default != rendered_metadata_default
