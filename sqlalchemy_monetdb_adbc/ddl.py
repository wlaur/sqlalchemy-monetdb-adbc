from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.schema import AddConstraint, ForeignKeyConstraint, Table


def self_referential_foreign_keys(table: Table) -> list[ForeignKeyConstraint]:
    return [
        constraint
        for constraint in table.foreign_key_constraints
        if constraint.referred_table is table and not constraint.use_alter
    ]


@event.listens_for(Table, "after_create")
def _add_self_referential_foreign_keys(  # pyright: ignore[reportUnusedFunction]
    target: Table,
    connection: Connection,
    **kw: Any,  # noqa: ARG001
) -> None:
    """Add self-referential foreign keys once the table exists.

    MonetDB rejects a self-referential FOREIGN KEY inside CREATE TABLE because
    the referenced PRIMARY KEY does not exist yet, but accepts the same
    constraint through ALTER TABLE afterwards. MonetDBDDLCompiler omits these
    constraints from the CREATE TABLE, so they are added here instead.
    """
    if connection.dialect.name != "monetdb":
        return

    for constraint in self_referential_foreign_keys(target):
        connection.execute(AddConstraint(constraint))
