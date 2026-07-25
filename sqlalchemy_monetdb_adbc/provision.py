"""Provisioning hooks for SQLAlchemy's dialect compliance suite.

The hook functions are registered by decorator and never referenced directly,
and SQLAlchemy's testing API is untyped, hence the suppressions below.
"""

from typing import Any, cast

from sqlalchemy import Engine, MetaData, Table, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.schema import (
    sort_tables_and_constraints,  # pyright: ignore[reportUnknownVariableType]
)
from sqlalchemy.testing.provision import (
    delete_from_all_tables,
    drop_all_schema_objects_post_tables,
    post_configure_engine,
    temp_table_keyword_args,
)

from sqlalchemy_monetdb_adbc.ddl import self_referential_foreign_keys

TEST_SCHEMAS = ("test_schema", "test_schema_2")

# A stock MonetDB login lands in "sys", where system views already occupy many
# ordinary table names ("users", "columns", "comments", "keywords", ...). The
# suite creates a "users" table, so it must run in a schema of its own.
DEFAULT_SCHEMA = "sqlalchemy_test"


@temp_table_keyword_args.for_db("monetdb")  # pyright: ignore[reportUnknownMemberType]
def _monetdb_temp_table_keyword_args(  # pyright: ignore[reportUnusedFunction]
    cfg: Any,  # noqa: ARG001
    eng: Any,  # noqa: ARG001
) -> dict[str, list[str]]:
    return {"prefixes": ["TEMPORARY"]}


@post_configure_engine.for_db("monetdb")  # pyright: ignore[reportUnknownMemberType]
def _monetdb_post_configure_engine(  # pyright: ignore[reportUnusedFunction]
    url: Any,  # noqa: ARG001
    engine: Engine,
    follower_ident: Any,  # noqa: ARG001
) -> None:
    # MonetDB has no CREATE SCHEMA IF NOT EXISTS, so consult the catalog first.
    with engine.begin() as connection:
        existing = {row[0] for row in connection.execute(text("SELECT name FROM sys.schemas"))}
        for schema in (*TEST_SCHEMAS, DEFAULT_SCHEMA):
            if schema not in existing:
                connection.execute(text(f"CREATE SCHEMA {schema}"))

        user = connection.execute(text("SELECT current_user")).scalar()
        connection.execute(text(f'ALTER USER "{user}" SET SCHEMA "{DEFAULT_SCHEMA}"'))

    # ALTER USER only affects new sessions, and the connection above already
    # made the dialect cache "sys" as the default schema name.
    engine.dispose()
    with engine.connect() as connection:
        engine.dialect.default_schema_name = connection.exec_driver_sql("SELECT CURRENT_SCHEMA").scalar()


@drop_all_schema_objects_post_tables.for_db("monetdb")  # pyright: ignore[reportUnknownMemberType]
def _monetdb_drop_all_schema_objects_post_tables(  # pyright: ignore[reportUnusedFunction]
    cfg: Any,  # noqa: ARG001
    eng: Engine,
) -> None:
    # SQLAlchemy only drops views in the default and test_schema scopes; a view
    # left in test_schema_2 would block later table drops.
    with eng.begin() as connection:
        for schema in TEST_SCHEMAS:
            rows = connection.execute(
                text(
                    "SELECT t.name FROM sys.tables t JOIN sys.schemas s ON t.schema_id = s.id "
                    "WHERE s.name = :schema AND t.system = FALSE AND t.type = 1"
                ),
                {"schema": schema},
            ).all()
            for (view_name,) in rows:
                connection.execute(text(f"DROP VIEW {schema}.{view_name}"))


@delete_from_all_tables.for_db("monetdb")  # pyright: ignore[reportUnknownMemberType]
def _monetdb_delete_from_all_tables(  # pyright: ignore[reportUnusedFunction]
    connection: Connection,
    cfg: Any,  # noqa: ARG001
    metadata: MetaData,
) -> None:
    """Empty every table, working around self-referential foreign keys.

    MonetDB enforces a self-referential FOREIGN KEY per statement rather than
    at statement end, so ``DELETE FROM t`` is rejected whenever any surviving
    row of the statement still references a row it is deleting -- even when the
    statement removes the whole table, and even for TRUNCATE. Setting the
    referencing columns to NULL first makes the delete legal.
    """
    inspector = inspect(connection)

    sorted_tables = cast(
        list[tuple[Table | None, Any]],
        sort_tables_and_constraints(list(metadata.tables.values())),
    )
    tables = [
        table
        for (table, _fks) in sorted_tables
        if table is not None and table.name in inspector.get_table_names(schema=table.schema)
    ]

    for table in tables:
        columns = {
            column
            for constraint in self_referential_foreign_keys(table)
            for column in constraint.columns
            if column.nullable
        }
        if columns:
            connection.execute(table.update().values(dict.fromkeys(columns, None)))

    for table in reversed(tables):
        connection.execute(table.delete())
