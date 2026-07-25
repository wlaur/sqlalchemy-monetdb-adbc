"""Provisioning hooks for SQLAlchemy's dialect compliance suite.

The hook functions are registered by decorator and never referenced directly,
and SQLAlchemy's testing API is untyped, hence the suppressions below.
"""

from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.testing.provision import (
    drop_all_schema_objects_post_tables,
    post_configure_engine,
    temp_table_keyword_args,
)

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
