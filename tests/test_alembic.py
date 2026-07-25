from collections.abc import Iterator
from typing import Any

import pytest
from alembic.autogenerate import produce_migrations, render_python_code
from alembic.migration import MigrationContext
from alembic.operations import Operations
from pydantic import BaseModel
from sqlalchemy import JSON, Column, Engine, Integer, MetaData, String, Table, insert, inspect, select, text

from sqlalchemy_monetdb_adbc import PydanticJSON

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def clean_schema(engine: Engine) -> Iterator[None]:
    yield
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT t.name FROM sys.tables t JOIN sys.schemas s ON t.schema_id = s.id "
                "WHERE s.name = CURRENT_SCHEMA AND t.system = FALSE AND t.type = 0"
            )
        ).all()
        for (name,) in rows:
            connection.exec_driver_sql(f'DROP TABLE "{name}" CASCADE')


class Doc(BaseModel):
    title: str
    views: int


def _autogenerate(engine: Engine, target: MetaData) -> str:
    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        upgrade_ops = produce_migrations(context, target).upgrade_ops
        assert upgrade_ops is not None
        return render_python_code(upgrade_ops)


@pytest.fixture
def operations(engine: Engine) -> Iterator[tuple[Operations, Any]]:
    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        yield Operations(context), connection
        connection.commit()


def test_migration_impl_is_registered(engine: Engine) -> None:
    with engine.connect() as connection:
        context = MigrationContext.configure(connection)

    assert context.impl.__dialect__ == "monetdb"


def test_schema_operations(operations: tuple[Operations, Any], engine: Engine) -> None:
    op, connection = operations

    op.create_table("alb", Column("id", Integer, primary_key=True), Column("name", String(20)))
    op.add_column("alb", Column("extra", Integer))
    op.alter_column("alb", "extra", nullable=False)
    op.create_index("ix_alb_name", "alb", ["name"])
    connection.commit()

    inspector = inspect(engine)
    assert {column["name"] for column in inspector.get_columns("alb")} == {"id", "name", "extra"}
    assert "ix_alb_name" in {index["name"] for index in inspector.get_indexes("alb")}

    op.drop_index("ix_alb_name", table_name="alb")
    op.drop_column("alb", "extra")
    op.rename_table("alb", "alb_renamed")
    connection.commit()

    inspector = inspect(engine)
    assert "alb_renamed" in inspector.get_table_names()
    assert {column["name"] for column in inspector.get_columns("alb_renamed")} == {"id", "name"}


def test_alter_column_type_reports_the_monetdb_limitation(operations: tuple[Operations, Any]) -> None:
    op, connection = operations
    op.create_table("alb_type", Column("id", Integer, primary_key=True), Column("name", String(20)))
    connection.commit()

    # MonetDB has no ALTER TABLE ... ALTER COLUMN ... TYPE, so this must say so
    # rather than reaching the server and failing with a syntax error.
    with pytest.raises(NotImplementedError, match="cannot change the type"):
        op.alter_column("alb_type", "name", type_=String(50))


def test_autogenerate_detects_schema_differences(engine: Engine) -> None:
    existing = MetaData()
    Table("ag_keep", existing, Column("id", Integer, primary_key=True))
    Table("ag_drop", existing, Column("id", Integer, primary_key=True))
    existing.create_all(engine)

    target = MetaData()
    Table("ag_keep", target, Column("id", Integer, primary_key=True), Column("added", String(20)))
    Table("ag_new", target, Column("id", Integer, primary_key=True))

    code = _autogenerate(engine, target)

    assert "op.create_table('ag_new'" in code
    assert "op.drop_table('ag_drop')" in code
    assert "op.add_column('ag_keep'" in code
    # ag_keep.id already exists and must not be re-added.
    assert code.count("op.add_column('ag_keep'") == 1


def test_autogenerate_renders_pydantic_json_with_its_model(engine: Engine) -> None:
    target = MetaData()
    Table("ag_pyd", target, Column("id", Integer, primary_key=True), Column("doc", PydanticJSON(Doc)))

    code = _autogenerate(engine, target)

    # Without the model the generated migration would not run.
    assert "PydanticJSON(Doc)" in code


def test_migration_creates_a_usable_pydantic_json_column(operations: tuple[Operations, Any], engine: Engine) -> None:
    op, connection = operations
    op.create_table("alb_pyd", Column("id", Integer, primary_key=True), Column("doc", PydanticJSON(Doc)))
    connection.commit()

    # The migration created a real JSON column that the type can round-trip.
    assert isinstance(inspect(engine).get_columns("alb_pyd")[1]["type"], JSON)

    table = Table("alb_pyd", MetaData(), Column("id", Integer, primary_key=True), Column("doc", PydanticJSON(Doc)))
    document = Doc(title="migrated", views=3)

    with engine.begin() as write:
        write.execute(insert(table), [{"id": 1, "doc": document}])

    with engine.connect() as read:
        assert read.execute(select(table.c.doc)).scalar() == document
