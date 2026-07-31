from collections.abc import Iterator
from itertools import product
from typing import Any, cast

import pytest
from alembic.autogenerate import produce_migrations, render_python_code
from alembic.migration import MigrationContext
from alembic.operations import Operations
from pydantic import BaseModel
from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    Engine,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    String,
    Table,
    exc,
    insert,
    inspect,
    select,
    text,
)

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
        # The migration context carries the dialect's render_type hook.
        return render_python_code(upgrade_ops, migration_context=context)


def _autogenerate_diffs(engine: Engine, target: MetaData) -> list[Any]:
    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        upgrade_ops = produce_migrations(context, target).upgrade_ops
        assert upgrade_ops is not None
        return upgrade_ops.as_diffs()


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


def test_alter_column_type(operations: tuple[Operations, Any], engine: Engine) -> None:
    op, connection = operations
    op.create_table(
        "alb_type",
        Column("id", Integer, primary_key=True),
        Column("name", String(20)),
        Column("qty", Integer),
    )
    connection.commit()

    # MonetDB spells this "ALTER COLUMN <name> <type>", without TYPE.
    op.alter_column("alb_type", "name", type_=String(50), existing_type=String(20))
    op.alter_column("alb_type", "qty", type_=BigInteger(), existing_type=Integer())
    connection.commit()

    columns = {column["name"]: column["type"] for column in inspect(engine).get_columns("alb_type")}
    assert isinstance(columns["name"], String)
    assert columns["name"].length == 50
    assert isinstance(columns["qty"], BigInteger)


def test_alter_column_type_is_refused_for_a_depended_on_column(
    operations: tuple[Operations, Any],
) -> None:
    op, connection = operations
    op.create_table("alb_pk", Column("id", Integer, primary_key=True))
    connection.commit()

    # MonetDB will not alter a column that other objects depend on, such as a
    # primary key. The error names the reason rather than being a syntax error.
    with pytest.raises(exc.OperationalError, match="depend on it"):
        op.alter_column("alb_pk", "id", type_=BigInteger(), existing_type=Integer())
    connection.rollback()


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


def test_autogenerate_compares_all_foreign_key_actions(engine: Engine) -> None:
    actions: tuple[str | None, ...] = (None, "RESTRICT", "NO ACTION", "CASCADE", "SET NULL", "SET DEFAULT")
    database = MetaData()
    Table("fk_parent", database, Column("id", Integer, primary_key=True))
    target = MetaData()
    Table("fk_parent", target, Column("id", Integer, primary_key=True))

    expected_changed: set[str] = set()
    for database_index, model_index in product(range(len(actions)), repeat=2):
        database_action = actions[database_index]
        model_action = actions[model_index]
        table_name = f"fk_{database_index}_{model_index}"
        Table(
            table_name,
            database,
            Column("id", Integer, primary_key=True),
            Column(
                "parent_id",
                Integer,
                ForeignKey("fk_parent.id", ondelete=database_action, onupdate=database_action),
            ),
        )
        Table(
            table_name,
            target,
            Column("id", Integer, primary_key=True),
            Column(
                "parent_id",
                Integer,
                ForeignKey("fk_parent.id", ondelete=model_action, onupdate=model_action),
            ),
        )
        if (database_action or "RESTRICT") != (model_action or "RESTRICT"):
            expected_changed.add(table_name)

    database.create_all(engine)
    diffs = _autogenerate_diffs(engine, target)
    foreign_key_diffs = [diff for diff in diffs if diff[0] in {"add_fk", "remove_fk"}]
    changed = {cast(ForeignKeyConstraint, diff[1]).table.name for diff in foreign_key_diffs}

    assert changed == expected_changed
    assert len(foreign_key_diffs) == len(expected_changed) * 2
    assert len(diffs) == len(foreign_key_diffs)


def test_foreign_key_actions_are_reflected_independently(engine: Engine) -> None:
    metadata = MetaData()
    Table("fk_parent", metadata, Column("id", Integer, primary_key=True))
    Table(
        "fk_asymmetric",
        metadata,
        Column("id", Integer, primary_key=True),
        Column(
            "parent_id",
            Integer,
            ForeignKey("fk_parent.id", ondelete="CASCADE", onupdate="SET NULL"),
        ),
    )
    metadata.create_all(engine)

    [foreign_key] = inspect(engine).get_foreign_keys("fk_asymmetric")

    assert foreign_key.get("options", {}) == {"ondelete": "CASCADE", "onupdate": "SET NULL"}


def test_autogenerate_round_trips_all_delete_update_action_pairs(engine: Engine) -> None:
    actions: tuple[str | None, ...] = (None, "RESTRICT", "NO ACTION", "CASCADE", "SET NULL", "SET DEFAULT")
    database = MetaData()
    Table("fk_parent", database, Column("id", Integer, primary_key=True))
    target = MetaData()
    Table("fk_parent", target, Column("id", Integer, primary_key=True))

    for delete_index, update_index in product(range(len(actions)), repeat=2):
        ondelete = actions[delete_index]
        onupdate = actions[update_index]
        table_name = f"fk_pair_{delete_index}_{update_index}"
        for metadata in (database, target):
            Table(
                table_name,
                metadata,
                Column("id", Integer, primary_key=True),
                Column(
                    "parent_id",
                    Integer,
                    ForeignKey("fk_parent.id", ondelete=ondelete, onupdate=onupdate),
                ),
            )

    database.create_all(engine)

    assert _autogenerate_diffs(engine, target) == []


def test_autogenerate_renders_pydantic_json_as_a_plain_json_column(engine: Engine) -> None:
    target = MetaData()
    Table("ag_pyd", target, Column("id", Integer, primary_key=True), Column("doc", PydanticJSON(Doc)))

    code = _autogenerate(engine, target)

    # A migration describes the database schema. Rendering the model would make
    # the migration import application code and break once that model moves.
    assert "sa.JSON()" in code
    assert "PydanticJSON" not in code
    assert "Doc" not in code


def test_autogenerate_sees_no_change_for_an_unchanged_pydantic_json_column(engine: Engine) -> None:
    metadata = MetaData()
    Table("ag_stable", metadata, Column("id", Integer, primary_key=True), Column("doc", PydanticJSON(Doc)))
    metadata.create_all(engine)

    # The column is already JSON in the database, so there is nothing to migrate.
    code = _autogenerate(engine, metadata)

    assert "op.add_column" not in code
    assert "op.alter_column" not in code


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
