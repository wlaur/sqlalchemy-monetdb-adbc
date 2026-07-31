import datetime
import decimal
import uuid
import warnings
from collections.abc import Iterator
from typing import Any, cast

import adbc_driver_manager
import numpy as np
import pytest
from pydantic import BaseModel
from sqlalchemy import (
    JSON,
    TIMESTAMP,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Engine,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    Sequence,
    String,
    Table,
    Time,
    UniqueConstraint,
    Uuid,
    delete,
    exc,
    func,
    insert,
    inspect,
    literal,
    select,
    text,
    update,
)
from sqlalchemy.engine import ObjectScope
from sqlalchemy.exc import SAWarning
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from sqlalchemy_monetdb_adbc import (
    HUGEINT,
    PydanticJSON,
    fetch_arrow_table,
    ingest_arrow,
    open_arrow_batch_reader,
    raw_adbc_connection,
)
from sqlalchemy_monetdb_adbc.base import RESERVED_WORDS

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


def test_cross_schema_foreign_key_keeps_referred_schema(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE SCHEMA xfk_other")
        try:
            connection.exec_driver_sql("CREATE TABLE xfk_other.xfk_parent (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql(
                "CREATE TABLE xfk_other.xfk_same_child "
                "(id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES xfk_other.xfk_parent (id))"
            )
            connection.exec_driver_sql(
                "CREATE TABLE xfk_child (id INTEGER PRIMARY KEY, "
                "parent_id INTEGER REFERENCES xfk_other.xfk_parent (id))"
            )

            inspector = inspect(connection)
            foreign_key = inspector.get_foreign_keys("xfk_child")[0]
            assert foreign_key["referred_schema"] == "xfk_other"
            assert foreign_key["referred_table"] == "xfk_parent"
            same_schema = inspector.get_foreign_keys("xfk_same_child", schema="xfk_other")[0]
            assert same_schema["referred_schema"] == "xfk_other"

            reflected = Table("xfk_child", MetaData(), autoload_with=connection)
            target = next(iter(reflected.foreign_key_constraints)).referred_table
            assert target.schema == "xfk_other"
            assert target.name == "xfk_parent"
        finally:
            connection.exec_driver_sql("DROP TABLE IF EXISTS xfk_child")
            connection.exec_driver_sql("DROP SCHEMA xfk_other CASCADE")


def test_temporary_table_existence_constraints_and_name_collision(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE reflection_collision (permanent_value INTEGER)")
        connection.exec_driver_sql(
            "CREATE LOCAL TEMPORARY TABLE temp_probe "
            "(value INTEGER, CONSTRAINT ck_temp_probe_positive CHECK (value > 0))"
        )
        connection.exec_driver_sql("CREATE LOCAL TEMPORARY TABLE reflection_collision (temporary_value INTEGER)")
        try:
            inspector = inspect(connection)
            assert inspector.has_table("temp_probe")
            assert [column["name"] for column in inspector.get_columns("temp_probe")] == ["value"]
            assert [constraint["name"] for constraint in inspector.get_check_constraints("temp_probe")] == [
                "ck_temp_probe_positive"
            ]

            assert [column["name"] for column in inspector.get_columns("reflection_collision")] == ["permanent_value"]
            temporary = inspector.get_multi_columns(
                scope=ObjectScope.TEMPORARY,
                filter_names=["reflection_collision"],
            )
            assert [column["name"] for column in temporary[(None, "reflection_collision")]] == ["temporary_value"]
        finally:
            connection.exec_driver_sql("DROP TABLE tmp.temp_probe")
            connection.exec_driver_sql("DROP TABLE tmp.reflection_collision")


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
        batch = pa.record_batch(
            [
                pa.array([2, 3], type=pa.int32()),
                pa.array(["arrow-a", "arrow-b"], type=pa.string()),
            ],
            schema=schema,
        )
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


def test_ingest_arrow_creates_a_translated_sqlalchemy_table(engine: Engine) -> None:
    import pyarrow as pa

    metadata = MetaData()
    table = Table(
        "arrow_created",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("label", String(20), nullable=False, unique=True),
        schema="arrow_source",
    )
    data = pa.table(
        {
            "id": pa.array([1, 2], type=pa.int32()),
            "label": pa.array(["first", "second"], type=pa.string()),
        }
    )

    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE SCHEMA arrow_target")
        mapped = connection.execution_options(schema_translate_map={"arrow_source": "arrow_target"})
        assert ingest_arrow(mapped, table, data, create=True) == 2
        assert mapped.exec_driver_sql("SELECT id, label FROM arrow_target.arrow_created ORDER BY id").all() == [
            (1, "first"),
            (2, "second"),
        ]
        inspector = inspect(mapped)
        assert inspector.get_pk_constraint("arrow_created", schema="arrow_target")["constrained_columns"] == ["id"]
        assert inspector.get_unique_constraints("arrow_created", schema="arrow_target")[0]["column_names"] == ["label"]
        connection.exec_driver_sql("DROP SCHEMA arrow_target CASCADE")


def test_ingest_arrow_rejects_table_ddl_through_arrow_schema(engine: Engine) -> None:
    import pyarrow as pa

    table = Table(
        "arrow_schema_only_create",
        MetaData(),
        Column("id", Integer, primary_key=True),
    )
    with engine.begin() as connection, pytest.raises(ValueError, match="would discard its constraints"):
        ingest_arrow(connection, table, pa.table({"id": [1]}), mode="create")


def test_ingest_arrow_quotes_reserved_and_mixed_case_names(engine: Engine) -> None:
    import pyarrow as pa

    table = Table(
        "Select",
        MetaData(),
        Column("From", Integer, primary_key=True),
        Column("MixedCase", String(20), nullable=False),
    )
    table.create(engine)
    try:
        with engine.begin() as connection:
            assert (
                ingest_arrow(
                    connection,
                    table,
                    pa.table(
                        {
                            "From": pa.array([1], type=pa.int32()),
                            "MixedCase": ["value"],
                        }
                    ),
                )
                == 1
            )
        with engine.connect() as connection:
            assert connection.execute(select(table)).one() == (1, "value")
    finally:
        table.drop(engine)


@pytest.mark.parametrize("rows", [1, 500])
def test_ingest_arrow_appends_by_name_at_every_stream_size(engine: Engine, rows: int) -> None:
    # the driver routes small streams through a prepared INSERT and larger ones through COPY, and
    # matches by name on both. the dialect must not reorder or fill anything of its own
    import pyarrow as pa

    table = Table(
        "align_by_name",
        MetaData(),
        Column("ts", TIMESTAMP),
        Column("a", Float),
        Column("b", Integer, server_default=text("7")),
        Column("c", String(8)),
    )
    table.create(engine)
    try:
        with engine.begin() as connection:
            assert (
                ingest_arrow(
                    connection,
                    table,
                    pa.table(
                        {
                            "c": pa.array(["x"] * rows),
                            "a": pa.array([1.5] * rows, type=pa.float64()),
                            "ts": pa.array([datetime.datetime(2026, 7, 31)] * rows, type=pa.timestamp("us")),
                        }
                    ),
                )
                == rows
            )
        with engine.connect() as connection:
            assert connection.execute(select(table).distinct()).all() == [(datetime.datetime(2026, 7, 31), 1.5, 7, "x")]
    finally:
        table.drop(engine)


def test_ingest_arrow_partial_failure_blocks_sqlalchemy_commit(engine: Engine) -> None:
    import pyarrow as pa

    table = Table(
        "arrow_partial_failure",
        MetaData(),
        Column("value", Integer),
    )
    table.create(engine)
    batch = pa.record_batch({"value": pa.array([2, 3], type=pa.int32())})

    def batches() -> Iterator[pa.RecordBatch]:
        yield batch
        raise RuntimeError("intentional upstream failure")

    with engine.connect() as connection:
        connection.execute(insert(table), {"value": 1})
        reader = pa.RecordBatchReader.from_batches(batch.schema, batches())
        with pytest.raises(Exception, match="intentional upstream failure"):
            ingest_arrow(
                connection,
                table,
                reader,
                statement_options={"adbc.monetdb.write_batch_rows": 2},
            )
        assert connection.execute(select(table.c.value).order_by(table.c.value)).scalars().all() == [
            1,
            2,
            3,
        ]
        with pytest.raises(exc.ProgrammingError, match="ROLLBACK is required"):
            connection.commit()
        assert not connection.in_transaction()
        connection.rollback()
        connection.execute(insert(table), {"value": 4})
        connection.commit()

    with engine.connect() as connection:
        assert connection.execute(select(table.c.value)).scalars().all() == [4]


def test_ingest_arrow_savepoint_atomicity_preserves_prior_work(engine: Engine) -> None:
    import pyarrow as pa

    table = Table(
        "arrow_savepoint_failure",
        MetaData(),
        Column("value", Integer),
    )
    table.create(engine)
    batch = pa.record_batch({"value": pa.array([2, 3], type=pa.int32())})

    def batches() -> Iterator[pa.RecordBatch]:
        yield batch
        raise RuntimeError("intentional upstream failure")

    with engine.connect() as connection:
        connection.execute(insert(table), {"value": 1})
        reader = pa.RecordBatchReader.from_batches(batch.schema, batches())
        with pytest.raises(Exception, match="intentional upstream failure"):
            ingest_arrow(
                connection,
                table,
                reader,
                statement_options={
                    "adbc.monetdb.write_batch_rows": 2,
                    "adbc.monetdb.ingest_atomicity": "savepoint",
                },
            )
        assert connection.execute(select(table.c.value)).scalars().all() == [1]
        connection.commit()

    with engine.connect() as connection:
        assert connection.execute(select(table.c.value)).scalars().all() == [1]


def test_ingest_arrow_failure_is_atomic_in_autocommit(engine: Engine) -> None:
    import pyarrow as pa

    table = Table(
        "arrow_autocommit_failure",
        MetaData(),
        Column("value", Integer),
    )
    table.create(engine)
    batch = pa.record_batch({"value": pa.array([2, 3], type=pa.int32())})

    def batches() -> Iterator[pa.RecordBatch]:
        yield batch
        raise RuntimeError("intentional upstream failure")

    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(insert(table), {"value": 1})
        reader = pa.RecordBatchReader.from_batches(batch.schema, batches())
        with pytest.raises(Exception, match="intentional upstream failure"):
            ingest_arrow(
                connection,
                table,
                reader,
                statement_options={"adbc.monetdb.write_batch_rows": 2},
            )
        assert connection.execute(select(table.c.value)).scalars().all() == [1]
        connection.execute(insert(table), {"value": 4})

    with engine.connect() as connection:
        assert connection.execute(select(table.c.value).order_by(table.c.value)).scalars().all() == [1, 4]


def test_polars_read_database_shares_the_sqlalchemy_transaction(engine: Engine) -> None:
    import polars as pl

    metadata = MetaData()
    table = Table("polars_target", metadata, Column("id", BigInteger), Column("label", String(20)))
    metadata.create_all(engine)

    with Session(engine) as session:
        session.execute(insert(table), {"id": 1, "label": "uncommitted"})
        assert (
            ingest_arrow(
                session.connection(),
                table.name,
                pl.DataFrame({"id": [2], "label": ["polars"]}),
                mode="append",
            )
            == 1
        )
        df = pl.read_database(
            query="SELECT id, label FROM polars_target ORDER BY id",
            connection=raw_adbc_connection(session.connection()),
        )

        assert df.to_dicts() == [
            {"id": 1, "label": "uncommitted"},
            {"id": 2, "label": "polars"},
        ]
        session.rollback()

    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(table)) == 0


def test_arrow_helpers_establish_sqlalchemy_transaction_ownership(engine: Engine) -> None:
    import pyarrow as pa

    data = pa.table({"id": pa.array([1, 2], type=pa.int64())})

    with engine.connect() as connection:
        assert not connection.in_transaction()
        assert ingest_arrow(connection, "arrow_autobegin_commit", data, mode="create") == 2
        assert connection.in_transaction()
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM arrow_autobegin_commit").scalar_one() == 2
        connection.commit()
        assert not connection.in_transaction()

        result = fetch_arrow_table(connection, "SELECT id FROM arrow_autobegin_commit ORDER BY id")
        assert result.column("id").to_pylist() == [1, 2]
        assert connection.in_transaction()
        connection.rollback()

        assert not connection.in_transaction()
        with open_arrow_batch_reader(connection, "SELECT id FROM arrow_autobegin_commit ORDER BY id") as reader:
            assert [value.as_py() for batch in reader for value in batch.column(0)] == [1, 2]
        assert connection.in_transaction()
        connection.rollback()

    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM arrow_autobegin_commit").scalar_one() == 2


def test_arrow_helper_rollback_and_pool_return_undo_raw_work(engine: Engine) -> None:
    import pyarrow as pa

    data = pa.table({"id": pa.array([1], type=pa.int64())})

    with engine.connect() as connection:
        assert ingest_arrow(connection, "arrow_autobegin_rollback", data, mode="create") == 1
        connection.rollback()

    with engine.connect() as connection:
        assert (
            connection.exec_driver_sql(
                "SELECT COUNT(*) FROM sys.tables WHERE name = 'arrow_autobegin_rollback'"
            ).scalar_one()
            == 0
        )
        connection.rollback()

    with engine.connect() as connection:
        assert ingest_arrow(connection, "arrow_pool_return_rollback", data, mode="create") == 1

    with engine.connect() as connection:
        assert (
            connection.exec_driver_sql(
                "SELECT COUNT(*) FROM sys.tables WHERE name = 'arrow_pool_return_rollback'"
            ).scalar_one()
            == 0
        )


def test_ingest_arrow_supports_temporary_tables_and_merge(engine: Engine) -> None:
    import pyarrow as pa

    created = pa.table(
        {
            "id": pa.array([1, 2], type=pa.int64()),
            "label": pa.array(["created-a", "created-b"], type=pa.string()),
        }
    )
    staged = pa.table(
        {
            "id": pa.array([1, 3], type=pa.int64()),
            "label": pa.array(["updated", "inserted"], type=pa.string()),
        }
    )

    with engine.connect() as connection:
        assert (
            ingest_arrow(
                connection,
                "arrow_created_temporary",
                created,
                mode="create",
                temporary=True,
            )
            == 2
        )
        connection.commit()
        assert connection.exec_driver_sql("SELECT id, label FROM arrow_created_temporary ORDER BY id").all() == [
            (1, "created-a"),
            (2, "created-b"),
        ]
        connection.exec_driver_sql("DROP TABLE tmp.arrow_created_temporary")
        connection.commit()

    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE arrow_merge_target(id BIGINT PRIMARY KEY, label CLOB NOT NULL)")
        connection.exec_driver_sql("INSERT INTO arrow_merge_target VALUES (1, 'original')")
        connection.exec_driver_sql(
            "CREATE LOCAL TEMPORARY TABLE arrow_merge_stage(id BIGINT PRIMARY KEY, label CLOB NOT NULL)"
        )
        assert (
            ingest_arrow(
                connection,
                "arrow_merge_stage",
                staged,
                mode="append",
                temporary=True,
            )
            == 2
        )
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM arrow_merge_stage").scalar_one() == 2
        connection.exec_driver_sql(
            "MERGE INTO arrow_merge_target AS target "
            "USING arrow_merge_stage AS stage ON target.id = stage.id "
            "WHEN MATCHED THEN UPDATE SET label = stage.label "
            "WHEN NOT MATCHED THEN INSERT (id, label) VALUES (stage.id, stage.label)"
        )
        assert connection.exec_driver_sql("SELECT id, label FROM arrow_merge_target ORDER BY id").all() == [
            (1, "updated"),
            (3, "inserted"),
        ]
        connection.exec_driver_sql("DROP TABLE tmp.arrow_merge_stage")


def test_ingest_arrow_rejects_temporary_schema_combination(engine: Engine) -> None:
    import pyarrow as pa

    with (
        engine.connect() as connection,
        pytest.raises(
            adbc_driver_manager.ProgrammingError,
            match="temporary ingestion cannot specify a schema",
        ),
    ):
        ingest_arrow(
            connection,
            "invalid_temporary_schema",
            pa.table({"id": pa.array([1], type=pa.int64())}),
            mode="create",
            schema_name="tmp",
            temporary=True,
        )


def test_ingest_arrow_streams_one_record_batch_reader_in_the_sqlalchemy_transaction(
    engine: Engine,
) -> None:
    import pyarrow as pa

    batches = [
        pa.record_batch({"id": pa.array([1, 2], type=pa.int64())}),
        pa.record_batch({"id": pa.array([3, 4], type=pa.int64())}),
    ]
    reader = pa.RecordBatchReader.from_batches(batches[0].schema, batches)

    with engine.connect() as connection:
        assert ingest_arrow(connection, "arrow_reader_ingest", reader, mode="create") == 4
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM arrow_reader_ingest").scalar_one() == 4
        connection.commit()

    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT SUM(id) FROM arrow_reader_ingest").scalar_one() == 10


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


def test_temporal_timezone_round_trip_normalizes_aware_values_to_utc(engine: Engine) -> None:
    metadata = MetaData()
    table = Table(
        "temporal_timezone_round_trip",
        metadata,
        Column("time_tz", Time(timezone=True)),
        Column("time_naive", Time()),
        Column("datetime_tz", DateTime(timezone=True)),
        Column("datetime_naive", DateTime()),
        Column("timestamp_tz", TIMESTAMP(timezone=True)),
        Column("timestamp_naive", TIMESTAMP()),
    )
    metadata.create_all(engine)
    offset = datetime.timezone(datetime.timedelta(hours=2))
    aware_time = datetime.time(0, 30, 1, 234567, tzinfo=offset)
    naive_time = datetime.time(0, 30, 1, 234567)
    aware_datetime = datetime.datetime(2024, 1, 2, 0, 30, 1, 234567, tzinfo=offset)
    naive_datetime = datetime.datetime(2024, 1, 2, 0, 30, 1, 234567)

    with engine.begin() as connection:
        connection.execute(
            insert(table),
            {
                "time_tz": aware_time,
                "time_naive": naive_time,
                "datetime_tz": aware_datetime,
                "datetime_naive": naive_datetime,
                "timestamp_tz": aware_datetime,
                "timestamp_naive": naive_datetime,
            },
        )
        row = connection.execute(select(table)).one()

    assert row.time_tz == datetime.time(22, 30, 1, 234567, tzinfo=datetime.UTC)
    assert row.time_naive == naive_time
    assert row.datetime_tz == aware_datetime.astimezone(datetime.UTC)
    assert row.datetime_naive == naive_datetime
    assert row.timestamp_tz == aware_datetime.astimezone(datetime.UTC)
    assert row.timestamp_naive == naive_datetime


def test_wide_python_integers_round_trip_as_hugeint(engine: Engine) -> None:
    metadata = MetaData()
    table = Table("hugeint_round_trip", metadata, Column("value", HUGEINT))
    metadata.create_all(engine)
    values = [-(10**38) + 1, -(2**63) - 1, 2**63, 10**38 - 1]

    with engine.begin() as connection:
        connection.execute(insert(table), [{"value": value} for value in values])
        stored = connection.execute(select(table.c.value).order_by(table.c.value)).scalars().all()

    assert stored == values
    assert all(type(value) is int for value in stored)


def test_dec2025_reserved_words_are_quoted_in_ddl(engine: Engine) -> None:
    with engine.connect() as connection:
        catalog_words = {
            str(word).lower() for word in connection.exec_driver_sql("SELECT keyword FROM sys.keywords").scalars()
        }
    scanner_only = sorted(RESERVED_WORDS - catalog_words)
    assert {"as", "details", "fetch", "point", "qualify", "returning", "show"} <= set(scanner_only)

    metadata = MetaData()
    Table(
        "reserved_word_columns",
        metadata,
        *(Column(word, Integer) for word in scanner_only),
    )
    metadata.create_all(engine)

    reflected = {column["name"] for column in inspect(engine).get_columns("reserved_word_columns")}
    assert reflected == set(scanner_only)


def test_system_catalog_table_can_be_reflected_when_named(engine: Engine) -> None:
    inspector = inspect(engine)

    assert inspector.has_table("keys", schema="sys")
    assert {column["name"] for column in inspector.get_columns("keys", schema="sys")} >= {"id", "table_id", "type"}
    assert "keys" in inspector.get_table_names(schema="sys")


def test_unique_index_drop_removes_the_backing_constraint(engine: Engine) -> None:
    metadata = MetaData()
    table = Table("unique_index_drop", metadata, Column("value", Integer))
    metadata.create_all(engine)
    index = Index("uq_unique_index_drop_value", table.c.value, unique=True)

    index.create(engine)
    assert {item["name"] for item in inspect(engine).get_unique_constraints(table.name)} == {index.name}

    index.drop(engine)
    assert inspect(engine).get_unique_constraints(table.name) == []


def test_column_comment_schema_translation_reaches_the_mapped_table(engine: Engine) -> None:
    metadata = MetaData()
    Table(
        "translated_comment",
        metadata,
        Column("value", Integer, comment="mapped comment"),
        schema="comment_source",
    )

    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE SCHEMA comment_target")
        try:
            mapped = connection.execution_options(schema_translate_map={"comment_source": "comment_target"})
            metadata.create_all(mapped)
            columns = inspect(connection).get_columns("translated_comment", schema="comment_target")
            assert columns[0].get("comment") == "mapped comment"
        finally:
            connection.exec_driver_sql("DROP SCHEMA comment_target CASCADE")


def test_sequence_defaults_and_generated_identity_options_are_reflected(engine: Engine) -> None:
    metadata = MetaData()
    sequence = Sequence("explicit_reflection_sequence", start=5, increment=2)
    Table(
        "sequence_default_reflection",
        metadata,
        Column("value", Integer, sequence, server_default=sequence.next_value()),
    )
    Table(
        "identity_reflection",
        metadata,
        Column("value", Integer, Identity(start=10, increment=3, cycle=True)),
    )
    metadata.create_all(engine)

    try:
        inspector = inspect(engine)
        sequence_column = inspector.get_columns("sequence_default_reflection")[0]
        assert sequence_column.get("autoincrement") is False
        assert sequence_column["default"] is not None
        assert "explicit_reflection_sequence" in sequence_column["default"]
        assert sequence_column.get("identity") is None

        identity_column = inspector.get_columns("identity_reflection")[0]
        assert identity_column.get("autoincrement") is True
        assert identity_column["default"] is None
        identity = identity_column.get("identity")
        assert identity is not None
        assert identity["start"] == 10
        assert identity["increment"] == 3
        assert identity["cycle"] is True

        reflected = Table("identity_reflection", MetaData(), autoload_with=engine)
        assert reflected.c.value.identity is not None
        assert reflected.c.value.identity.start == 10
        assert reflected.c.value.identity.increment == 3
        assert reflected.c.value.identity.cycle is True
    finally:
        metadata.drop_all(engine)


def test_server_errors_keep_dbapi_classification_and_sqlstate(engine: Engine) -> None:
    from adbc_driver_monetdb import dbapi

    assert issubclass(dbapi.ProgrammingError, dbapi.DatabaseError)
    assert issubclass(dbapi.IntegrityError, dbapi.DatabaseError)
    assert issubclass(dbapi.DatabaseError, dbapi.Error)

    with engine.connect() as connection:
        with pytest.raises(exc.ProgrammingError) as caught:
            connection.exec_driver_sql("SELEC 1")

        assert isinstance(caught.value.orig, dbapi.ProgrammingError)
        assert caught.value.orig.sqlstate == "42000"
        assert caught.value.connection_invalidated is False

        connection.rollback()
        assert connection.exec_driver_sql("SELECT 1").scalar() == 1

        connection.exec_driver_sql("CREATE TABLE error_classification (id INTEGER PRIMARY KEY)")
        connection.commit()
        connection.exec_driver_sql("INSERT INTO error_classification VALUES (1)")
        connection.commit()

        with pytest.raises(exc.IntegrityError) as caught:
            connection.exec_driver_sql("INSERT INTO error_classification VALUES (1)")

        assert isinstance(caught.value.orig, dbapi.IntegrityError)
        assert caught.value.orig.sqlstate == "40002"
        assert caught.value.connection_invalidated is False
        connection.rollback()
        assert connection.exec_driver_sql("SELECT 1").scalar() == 1


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


def test_server_errors_preserve_dbapi_class_and_sqlstate(engine: Engine) -> None:
    metadata = MetaData()
    table = Table(
        "server_error_target",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=False),
    )
    metadata.create_all(engine)

    with engine.connect() as connection:
        with pytest.raises(exc.ProgrammingError) as syntax:
            connection.exec_driver_sql("SELEC 1")
        assert isinstance(syntax.value.orig, adbc_driver_manager.ProgrammingError)
        assert syntax.value.orig.status_code == adbc_driver_manager.AdbcStatusCode.INVALID_ARGUMENT
        assert syntax.value.orig.sqlstate == "42000"
        assert not syntax.value.connection_invalidated
        connection.rollback()

    with engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(exc.IntegrityError) as constraint:
            connection.execute(insert(table), [{"id": 1}, {"id": 1}, {"id": 2}])
        assert isinstance(constraint.value.orig, adbc_driver_manager.IntegrityError)
        assert constraint.value.orig.status_code == adbc_driver_manager.AdbcStatusCode.INTEGRITY
        assert constraint.value.orig.sqlstate == "40002"
        assert not constraint.value.connection_invalidated
        transaction.rollback()

    with engine.connect() as connection:
        assert connection.execute(select(table.c.id)).all() == []


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


def test_arrow_helpers_apply_sqlalchemy_bind_processors(engine: Engine) -> None:
    metadata = MetaData()
    table = Table(
        "arrow_bind_processors",
        metadata,
        Column("id", Integer),
        Column("doc", JSON),
        Column("model", PydanticJSON(_Content)),
        Column("at", Time(timezone=True)),
    )
    metadata.create_all(engine)
    content = _Content(title="Arrow", tags=["typed"], views=3)
    at = datetime.time(1, 2, 3, tzinfo=datetime.timezone(datetime.timedelta(hours=2)))

    with engine.begin() as connection:
        connection.execute(insert(table), [{"id": 1, "doc": {"value": 1}, "model": content, "at": at}])
        for statement in (
            select(table.c.id).where(table.c.doc == {"value": 1}),
            select(table.c.id).where(table.c.model == content),
            select(table.c.id).where(table.c.at == at),
        ):
            assert fetch_arrow_table(connection, statement).column("id").to_pylist() == [1]


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


def test_arrow_helpers_run_on_the_sqlalchemy_transaction(engine: Engine) -> None:
    import pyarrow as pa

    metadata = MetaData()
    table = Table("arrow_helpers", metadata, Column("id", Integer), Column("sym", String(10)))
    metadata.create_all(engine)

    with Session(engine) as session:
        session.execute(insert(table), [{"id": i, "sym": ("AAPL", "MSFT")[i % 2]} for i in range(100)])

        # A SQLAlchemy Select is compiled for us, bind parameters included.
        statement = select(table.c.id, table.c.sym).where(table.c.sym == "AAPL").order_by(table.c.id)

        arrow_table = fetch_arrow_table(session.connection(), statement)
        assert isinstance(arrow_table, pa.Table)
        assert arrow_table.num_rows == 50
        assert arrow_table.schema.names == ["id", "sym"]
        with pytest.raises(ValueError, match="cannot override bind values"):
            fetch_arrow_table(session.connection(), statement, ["MSFT"])
        with pytest.raises(ValueError, match="cannot override bind values"):
            fetch_arrow_table(session.connection(), select(literal(1)), ())

        expanded = select(table.c.id).where(table.c.id.in_([1, 3, 5])).order_by(table.c.id)
        assert fetch_arrow_table(session.connection(), expanded).column("id").to_pylist() == [1, 3, 5]

        with open_arrow_batch_reader(session.connection(), statement) as reader:
            assert sum(batch.num_rows for batch in reader) == 50

        assert ingest_arrow(session.connection(), "arrow_helpers", arrow_table, mode="append") == 50
        # The ingest joined the same uncommitted transaction.
        assert len(session.execute(select(table)).all()) == 150

        session.rollback()

    with engine.connect() as connection:
        assert connection.execute(select(table)).all() == []


def test_arrow_helpers_accept_raw_sql(engine: Engine) -> None:
    metadata = MetaData()
    table = Table("arrow_raw", metadata, Column("id", Integer))
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(insert(table), [{"id": i} for i in range(10)])
        assert fetch_arrow_table(connection, "SELECT id FROM arrow_raw").num_rows == 10


def test_batched_reflection_matches_per_table_reflection(engine: Engine) -> None:
    metadata = MetaData()
    Table(
        "batch_parent",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("code", String(10)),
        UniqueConstraint("code"),
    )
    Table(
        "batch_child",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("parent_id", Integer, ForeignKey("batch_parent.id")),
        Column("qty", Integer),
        CheckConstraint("qty > 0", name="batch_qty_positive"),
        comment="a child table",
    )
    metadata.create_all(engine)

    inspector = inspect(engine)
    names = ["batch_parent", "batch_child"]

    def normalize(value: Any) -> Any:
        # Reflected types are fresh instances, so compare their rendering.
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, dict):
            return {key: str(item) if key == "type" else normalize(item) for key, item in value.items()}
        return value

    for name in names:
        key = (None, name)
        assert normalize(inspector.get_multi_columns(filter_names=names)[key]) == normalize(inspector.get_columns(name))
        assert inspector.get_multi_pk_constraint(filter_names=names)[key] == inspector.get_pk_constraint(name)
        assert inspector.get_multi_foreign_keys(filter_names=names)[key] == inspector.get_foreign_keys(name)
        assert inspector.get_multi_indexes(filter_names=names)[key] == inspector.get_indexes(name)
        assert inspector.get_multi_unique_constraints(filter_names=names)[key] == inspector.get_unique_constraints(name)
        assert inspector.get_multi_check_constraints(filter_names=names)[key] == inspector.get_check_constraints(name)
        assert inspector.get_multi_table_comment(filter_names=names)[key] == inspector.get_table_comment(name)


def test_batched_reflection_empty_filter_returns_nothing(engine: Engine) -> None:
    metadata = MetaData()
    Table("empty_filter_guard", metadata, Column("id", Integer))
    metadata.create_all(engine)

    assert inspect(engine).get_multi_columns(filter_names=[]) == {}


def test_batched_reflection_issues_few_statements(engine: Engine) -> None:
    import logging

    metadata = MetaData()
    for index in range(10):
        Table(f"batch_perf_{index}", metadata, Column("id", Integer, primary_key=True), Column("a", String(10)))
    metadata.create_all(engine)

    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("sqlalchemy.engine.Engine")
    handler = Capture()
    previous = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        MetaData().reflect(engine)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)

    statements = sum(1 for record in records if str(record.msg).lstrip().startswith(("SELECT", "WITH")))
    # One query per reflected kind, not per table; the per-table loop took 20x this.
    assert statements < 40, f"reflection issued {statements} statements"


@pytest.mark.parametrize(
    "values",
    [
        [15.7563, decimal.Decimal("15.7563")],
        [decimal.Decimal("15.7563"), 15.7563],
        [1, decimal.Decimal("2.5"), 3.5],
        [np.int64(1), decimal.Decimal("2.5"), np.float32(3.5)],
        [decimal.Decimal("2.5"), None, 3.5],
    ],
    ids=["float-first", "decimal-first", "int-mixed", "numpy-mixed", "with-null"],
)
def test_numeric_binds_mixed_python_types(engine: Engine, values: list[Any]) -> None:
    """A Numeric column accepts float, int and Decimal in one executemany.

    adbc_driver_manager builds the parameter batch with
    pyarrow.RecordBatch.from_pydict, which takes each column's Arrow type from
    its first value and then rejects any row that does not fit. MonetDBNumeric
    normalises the column so callers can mix them.
    """
    metadata = MetaData()
    table = Table("numeric_mixed_binds", metadata, Column("value", Numeric(18, 4)))
    metadata.drop_all(engine)
    metadata.create_all(engine)

    try:
        with engine.begin() as connection:
            connection.execute(insert(table), [{"value": value} for value in values])

        with engine.connect() as connection:
            stored = sorted(row[0] for row in connection.execute(select(table.c.value)) if row[0] is not None)

        assert stored == sorted(decimal.Decimal(str(value)) for value in values if value is not None)
        assert all(isinstance(value, decimal.Decimal) for value in stored)
    finally:
        metadata.drop_all(engine)
