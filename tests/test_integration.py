import datetime
import decimal
import uuid
import warnings
from collections.abc import Iterator
from typing import Any, cast

import pytest
from pydantic import BaseModel
from sqlalchemy import (
    JSON,
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
    delete,
    exc,
    insert,
    inspect,
    literal,
    select,
    text,
    update,
)
from sqlalchemy.exc import SAWarning
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from sqlalchemy_monetdb_adbc import PydanticJSON, raw_adbc_connection

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


def test_recursive_cte(engine: Engine) -> None:
    metadata = MetaData()
    table = Table(
        "cte_tree",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("parent_id", Integer),
        Column("name", String(20)),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(
            insert(table),
            [
                {"id": 1, "parent_id": None, "name": "root"},
                {"id": 2, "parent_id": 1, "name": "a"},
                {"id": 3, "parent_id": 2, "name": "a1"},
            ],
        )

    base = select(table.c.id, literal(0).label("depth")).where(table.c.parent_id.is_(None)).cte("tree", recursive=True)
    tree = base.union_all(select(table.c.id, base.c.depth + 1).join(base, table.c.parent_id == base.c.id))

    with engine.connect() as connection:
        rows = connection.execute(select(tree).order_by(tree.c.id)).all()

    assert [(row[0], int(row[1])) for row in rows] == [(1, 0), (2, 1), (3, 2)]


def test_cte_driving_delete(engine: Engine) -> None:
    metadata = MetaData()
    table = Table("cte_dml", metadata, Column("id", Integer), Column("keep", Integer))
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(insert(table), [{"id": 1, "keep": 1}, {"id": 2, "keep": 0}])
        doomed = select(table.c.id).where(table.c.keep == 0).cte("doomed")
        connection.execute(delete(table).where(table.c.id.in_(select(doomed.c.id))))
        assert connection.execute(select(table.c.id)).scalars().all() == [1]


def test_bulk_delete_on_self_referential_table(engine: Engine) -> None:
    """MonetDB enforces a self-referential FK per statement, not at its end."""
    metadata = MetaData()
    table = Table(
        "selfref_delete",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=False),
        Column("parent_id", Integer, ForeignKey("selfref_delete.id")),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(insert(table), [{"id": 1, "parent_id": None}])
        connection.execute(insert(table), [{"id": 2, "parent_id": 1}])

    with engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(exc.IntegrityError):
            connection.execute(delete(table))
        # MonetDB aborts the transaction, so it can only be rolled back.
        transaction.rollback()

    # Clearing the referencing column first makes the delete legal.
    with engine.begin() as connection:
        connection.execute(update(table).values(parent_id=None))
        connection.execute(delete(table))
        assert connection.execute(select(table.c.id)).scalars().all() == []


def test_rowcount_reports_affected_rows(engine: Engine) -> None:
    metadata = MetaData()
    table = Table("rowcount_target", metadata, Column("id", Integer), Column("label", String(10)))
    metadata.create_all(engine)

    with engine.begin() as connection:
        assert connection.execute(insert(table), [{"id": i, "label": "x"} for i in range(5)]).rowcount == 5
        assert connection.execute(update(table).values(label="y")).rowcount == 5
        assert connection.execute(update(table).where(table.c.id == 999).values(label="z")).rowcount == 0
        assert connection.execute(delete(table).where(table.c.id < 2)).rowcount == 2
        assert connection.execute(delete(table)).rowcount == 3


def test_orm_versioned_update_and_delete(engine: Engine) -> None:
    """Both paths depend on a truthful rowcount."""

    class Base(DeclarativeBase):
        pass

    class Versioned(Base):
        __tablename__ = "orm_versioned"

        id: Mapped[int] = mapped_column(primary_key=True)
        name: Mapped[str] = mapped_column(String(20))
        version_id: Mapped[int] = mapped_column(Integer, nullable=False)

        __mapper_args__ = {"version_id_col": version_id}  # noqa: RUF012

    Base.metadata.create_all(engine)

    with warnings.catch_warnings():
        warnings.simplefilter("error", SAWarning)
        with Session(engine) as session:
            row = Versioned(name="first")
            session.add(row)
            session.commit()

            row.name = "second"
            session.commit()
            assert row.version_id == 2

            session.delete(row)
            session.commit()
            assert session.scalars(select(Versioned.id)).all() == []


def test_large_binary_uses_the_dbapi_binary_constructor(engine: Engine) -> None:
    from adbc_driver_monetdb import dbapi

    assert hasattr(dbapi, "Binary")

    metadata = MetaData()
    table = Table("binary_target", metadata, Column("payload", LargeBinary))
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(insert(table), [{"payload": b"\x00\xff binary"}])
        assert connection.execute(select(table.c.payload)).scalar() == b"\x00\xff binary"


class _Content(BaseModel):
    title: str
    tags: list[str]
    views: int


class _Other(BaseModel):
    title: str


def test_pydantic_json_round_trip(engine: Engine) -> None:
    metadata = MetaData()
    table = Table("pyd_docs", metadata, Column("id", Integer), Column("doc", PydanticJSON(_Content)))
    metadata.create_all(engine)

    content = _Content(title="Hello", tags=["a", "b"], views=1)
    with engine.begin() as connection:
        connection.execute(insert(table), [{"id": 1, "doc": content}, {"id": 2, "doc": None}])

    with engine.connect() as connection:
        rows: dict[int, _Content | None] = {
            row.id: row.doc for row in connection.execute(select(table.c.id, table.c.doc).order_by(table.c.id))
        }

    assert isinstance(rows[1], _Content)
    assert rows[1] == content
    assert rows[2] is None


def test_pydantic_json_creates_a_json_column(engine: Engine) -> None:
    metadata = MetaData()
    Table("pyd_ddl", metadata, Column("doc", PydanticJSON(_Content)))
    metadata.create_all(engine)

    columns = {column["name"]: column for column in inspect(engine).get_columns("pyd_ddl")}
    assert isinstance(columns["doc"]["type"], JSON)


def test_pydantic_json_cache_key_distinguishes_models() -> None:
    # cache_ok is True, so two column types differing only by model must not
    # share a compiled-statement cache entry.
    def cache_key(model: type[BaseModel]) -> Any:
        return cast(Any, PydanticJSON(model))._static_cache_key

    assert cache_key(_Content) != cache_key(_Other)
    assert cache_key(_Content) == cache_key(_Content)


def test_pydantic_json_orm_attribute_is_the_model(engine: Engine) -> None:
    class Base(DeclarativeBase):
        pass

    class Article(Base):
        __tablename__ = "pyd_article"

        id: Mapped[int] = mapped_column(Integer, primary_key=True)
        content: Mapped[_Content] = mapped_column(PydanticJSON(_Content))

    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Article(content=_Content(title="Hello", tags=[], views=1)))
        session.commit()

    with Session(engine) as session:
        article = session.scalars(select(Article)).one()
        assert isinstance(article.content, _Content)
        assert article.content.title == "Hello"

        article.content = article.content.model_copy(update={"views": 2})
        session.commit()

    with Session(engine) as session:
        assert session.scalars(select(Article)).one().content.views == 2


class _EmptyDoc(BaseModel):
    """Serializes to ``{}``, which is falsy."""


def test_pydantic_json_distinguishes_an_empty_model_from_null(engine: Engine) -> None:
    # A truthiness check here would collapse {} to None and lose the model.
    metadata = MetaData()
    table = Table("pyd_empty", metadata, Column("id", Integer), Column("doc", PydanticJSON(_EmptyDoc)))
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(insert(table), [{"id": 1, "doc": _EmptyDoc()}, {"id": 2, "doc": None}])

    with engine.connect() as connection:
        rows: dict[int, _EmptyDoc | None] = {
            row.id: row.doc for row in connection.execute(select(table.c.id, table.c.doc).order_by(table.c.id))
        }

    assert rows[1] == _EmptyDoc()
    assert rows[2] is None


def test_json_path_indexing(engine: Engine) -> None:
    metadata = MetaData()
    table = Table("json_paths", metadata, Column("id", Integer), Column("doc", JSON))
    metadata.create_all(engine)

    document = {"title": "hello", "n": 7, "nul": None, "sub": {"k": "v"}, "arr": [10, 20]}
    with engine.begin() as connection:
        connection.execute(insert(table), [{"id": 1, "doc": document}])

    with engine.connect() as connection:
        scalar = connection.scalar
        assert connection.execute(select(table.c.doc["title"])).scalar() == "hello"
        assert connection.execute(select(table.c.doc["n"])).scalar() == 7
        assert connection.execute(select(table.c.doc[("sub", "k")])).scalar() == "v"
        assert connection.execute(select(table.c.doc[("arr", 1)])).scalar() == 20
        assert connection.execute(select(table.c.doc["title"].as_string())).scalar() == "hello"
        assert connection.execute(select(table.c.doc["n"].as_integer())).scalar() == 7
        # A JSON null and a path that matches nothing both come back as None,
        # as on other backends. MonetDB's JSON.FILTER returns '[]' for a miss.
        assert connection.execute(select(table.c.doc["nul"])).scalar() is None
        assert connection.execute(select(table.c.doc["missing"])).scalar() is None
        assert scalar is not None


def test_pydantic_json_reports_schema_drift(engine: Engine) -> None:
    from pydantic import ValidationError

    metadata = MetaData()
    table = Table("pyd_drift", metadata, Column("id", Integer), Column("doc", PydanticJSON(_Content)))
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.exec_driver_sql("""INSERT INTO pyd_drift (id, doc) VALUES (1, '{"title": "only"}')""")

    # Stored JSON that no longer matches the model must fail loudly.
    with engine.connect() as connection, pytest.raises(ValidationError):
        connection.execute(select(table.c.doc)).scalar()
