import datetime
import decimal
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Engine,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    String,
    Table,
    Time,
    UniqueConstraint,
    Uuid,
    exc,
    insert,
    inspect,
    select,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from sqlalchemy_monetdb_adbc import raw_adbc_connection

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


NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def test_naming_convention_is_applied_and_reflected(engine: Engine) -> None:
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    Table("nc_parent", metadata, Column("id", Integer, primary_key=True))
    child = Table(
        "nc_child",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("parent_id", Integer, ForeignKey("nc_parent.id")),
        Column("code", String(10)),
        Column("qty", Integer),
        UniqueConstraint("code"),
        CheckConstraint("qty > 0", name="positive"),
    )
    Index(None, child.c.qty)

    metadata.create_all(engine)
    inspector = inspect(engine)

    assert inspector.get_pk_constraint("nc_child")["name"] == "pk_nc_child"
    assert [fk["name"] for fk in inspector.get_foreign_keys("nc_child")] == ["fk_nc_child_parent_id_nc_parent"]
    assert [uq["name"] for uq in inspector.get_unique_constraints("nc_child")] == ["uq_nc_child_code"]
    assert [ck["name"] for ck in inspector.get_check_constraints("nc_child")] == ["ck_nc_child_positive"]
    assert "ix_nc_child_qty" in {index["name"] for index in inspector.get_indexes("nc_child")}


def test_self_referential_foreign_key(engine: Engine) -> None:
    metadata = MetaData()
    Table(
        "tree",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("parent_id", Integer, ForeignKey("tree.id")),
    )
    metadata.create_all(engine)

    foreign_keys = inspect(engine).get_foreign_keys("tree")
    assert len(foreign_keys) == 1
    assert foreign_keys[0]["referred_table"] == "tree"
    assert foreign_keys[0]["constrained_columns"] == ["parent_id"]


def test_statements_without_a_result_set_do_not_return_rows(engine: Engine) -> None:
    with engine.begin() as connection:
        result = connection.execute(text("CREATE TABLE no_rows (id INTEGER)"))
        assert result.returns_rows is False
        with pytest.raises(exc.ResourceClosedError):
            result.all()

        assert connection.execute(text("INSERT INTO no_rows VALUES (1)")).returns_rows is False
        assert connection.execute(text("UPDATE no_rows SET id = 2")).returns_rows is False
        assert connection.execute(text("DELETE FROM no_rows")).returns_rows is False
        assert connection.execute(text("SELECT * FROM no_rows")).returns_rows is True


def test_raw_adbc_connection_shares_the_sqlalchemy_transaction(engine: Engine) -> None:
    import pyarrow as pa

    metadata = MetaData()
    table = Table("ingest_target", metadata, Column("id", Integer), Column("label", String(10)))
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(insert(table), [{"id": 1, "label": "sqlalchemy"}])

        adbc_connection = raw_adbc_connection(connection)
        schema = pa.schema([("id", pa.int32()), ("label", pa.string())])
        batch = pa.record_batch([[2, 3], ["arrow-a", "arrow-b"]], schema=schema)
        with adbc_connection.cursor() as cursor:
            cursor.adbc_ingest("ingest_target", batch, mode="append")

        # Both writes must be visible to the same SQLAlchemy connection before
        # the transaction commits, which proves they share one session.
        rows = connection.execute(select(table).order_by(table.c.id)).all()
        assert [row.id for row in rows] == [1, 2, 3]

    with engine.connect() as connection:
        assert connection.execute(select(table).order_by(table.c.id)).all() == [
            (1, "sqlalchemy"),
            (2, "arrow-a"),
            (3, "arrow-b"),
        ]


def test_rollback_discards_sqlalchemy_and_arrow_writes(engine: Engine) -> None:
    metadata = MetaData()
    table = Table("rollback_target", metadata, Column("id", Integer))
    metadata.create_all(engine)

    with engine.connect() as connection:
        with connection.begin():
            connection.execute(insert(table), [{"id": 1}])
        with pytest.raises(RuntimeError), connection.begin():
            connection.execute(insert(table), [{"id": 2}])
            raise RuntimeError("roll this back")

    with engine.connect() as connection:
        assert connection.execute(select(table.c.id).order_by(table.c.id)).scalars().all() == [1]


def test_orm_session_lifecycle(engine: Engine) -> None:
    class Base(DeclarativeBase):
        pass

    class Widget(Base):
        __tablename__ = "orm_widget"

        id: Mapped[int] = mapped_column(primary_key=True)
        name: Mapped[str] = mapped_column(String(20))

    Base.metadata.create_all(engine)

    with Session(engine) as session:
        widget = Widget(name="first")
        session.add(widget)
        session.flush()
        # RETURNING populates the generated key without a lastrowid.
        assert widget.id is not None
        session.commit()

        widget.name = "renamed"
        session.commit()
        assert session.scalars(select(Widget.name)).all() == ["renamed"]

        session.delete(widget)
        session.commit()
        assert session.scalars(select(Widget.name)).all() == []


def test_isolation_level_autocommit(engine: Engine) -> None:
    metadata = MetaData()
    table = Table("autocommit_target", metadata, Column("id", Integer))
    metadata.create_all(engine)

    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        assert connection.get_isolation_level() == "AUTOCOMMIT"
        connection.execute(insert(table), [{"id": 1}])

    # No explicit commit was issued, so this only survives under autocommit.
    with engine.connect() as connection:
        assert connection.execute(select(table.c.id)).scalars().all() == [1]


def test_rejects_unsupported_isolation_level(engine: Engine) -> None:
    with pytest.raises(exc.ArgumentError), engine.connect().execution_options(isolation_level="REPEATABLE READ"):
        pass


@pytest.mark.parametrize(
    ("column_type", "value"),
    [
        (Integer, 42),
        (String(20), "text"),
        (Boolean, True),
        (Numeric(18, 4), decimal.Decimal("1.2345")),
        (Float, 1.5),
        (Date, datetime.date(2024, 1, 2)),
        (DateTime, datetime.datetime(2024, 1, 2, 3, 4, 5)),
        (Time, datetime.time(3, 4, 5)),
        (LargeBinary, b"\x00\x01"),
        (Uuid, uuid.UUID("12345678-1234-5678-1234-567812345678")),
    ],
)
def test_type_round_trip(engine: Engine, column_type: Any, value: Any) -> None:
    metadata = MetaData()
    table = Table("type_round_trip", metadata, Column("value", column_type))
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(insert(table), [{"value": value}])
        assert connection.execute(select(table.c.value)).scalar() == value


def test_null_round_trip(engine: Engine) -> None:
    metadata = MetaData()
    table = Table("null_round_trip", metadata, Column("id", Integer), Column("value", String(20)))
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(insert(table), [{"id": 1, "value": None}])
        assert connection.execute(select(table.c.value)).scalar() is None
