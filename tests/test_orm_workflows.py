from __future__ import annotations

import datetime
import decimal
import os
import subprocess
import time
import uuid
from collections.abc import Iterator
from typing import Any, cast

import adbc_driver_manager
import pyarrow as pa
import pytest
from orm_models import IngestIdentity, ORMBase, Reading, Sensor, SensorTags, TagDetails, TypeMatrix
from pydantic import ValidationError
from sqlalchemy import JSON, Engine, Integer, Table, create_engine, delete, func, inspect, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from sqlalchemy_monetdb_adbc import (
    DOUBLE_PRECISION,
    HUGEINT,
    INET,
    MONTH_INTERVAL,
    SECOND_INTERVAL,
    URL,
    fetch_arrow_table,
    ingest_arrow,
)

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def orm_schema(engine: Engine, request: pytest.FixtureRequest) -> Iterator[None]:
    if request.node.get_closest_marker("disconnect") is not None:
        yield
        return
    ORMBase.metadata.create_all(engine)
    yield
    ORMBase.metadata.drop_all(engine)


def _tags(label: str = "primary") -> SensorTags:
    return SensorTags(
        labels=[label],
        details=TagDetails(owner="ops", calibrated=True),
        observed_at=datetime.datetime(2026, 7, 28, 10, 0, 0, 123456),
    )


def _reading_table(rows: list[tuple[int, datetime.datetime]]) -> pa.Table:
    month_field = pa.field(
        "age",
        pa.int32(),
        metadata={b"ARROW:extension:name": b"monetdb.interval_month"},
    )
    schema = pa.schema(
        [
            pa.field("sensor_id", pa.int32(), nullable=False),
            pa.field("ts", pa.timestamp("us"), nullable=False),
            pa.field("value", pa.float64()),
            pa.field("raw", pa.large_binary()),
            pa.field("quality", pa.int8()),
            pa.field("delta", pa.duration("ms")),
            month_field,
            pa.field("note", pa.large_string()),
        ]
    )
    return pa.Table.from_arrays(
        [
            pa.array([row[0] for row in rows], type=pa.int32()),
            pa.array([row[1] for row in rows], type=pa.timestamp("us")),
            pa.array([1.25] * len(rows), type=pa.float64()),
            pa.array([b"\x00raw"] * len(rows), type=pa.large_binary()),
            pa.array([7] * len(rows), type=pa.int8()),
            pa.array([datetime.timedelta(milliseconds=1250)] * len(rows), type=pa.duration("ms")),
            pa.array([14] * len(rows), type=pa.int32()),
            pa.array(["bulk"] * len(rows), type=pa.large_string()),
        ],
        schema=schema,
    )


def test_orm_ddl_and_reflection_round_trip(engine: Engine) -> None:
    inspector = inspect(engine)
    assert {"orm_sensor", "orm_reading", "orm_type_matrix"} <= set(inspector.get_table_names())

    sensor_columns = {column["name"]: column for column in inspector.get_columns("orm_sensor")}
    assert sensor_columns["name"]["nullable"] is False
    assert sensor_columns["active"]["nullable"] is False
    assert isinstance(sensor_columns["url"]["type"], URL)
    assert isinstance(sensor_columns["addr"]["type"], INET)
    assert isinstance(sensor_columns["tags"]["type"], JSON)

    matrix_columns = {column["name"]: column for column in inspector.get_columns("orm_type_matrix")}
    assert isinstance(matrix_columns["huge_value"]["type"], HUGEINT)
    assert isinstance(matrix_columns["double_value"]["type"], DOUBLE_PRECISION)
    assert isinstance(matrix_columns["second_interval_value"]["type"], SECOND_INTERVAL)
    assert isinstance(matrix_columns["month_interval_value"]["type"], MONTH_INTERVAL)

    assert inspector.get_pk_constraint("orm_reading")["constrained_columns"] == ["sensor_id", "ts"]
    foreign_key = inspector.get_foreign_keys("orm_reading")[0]
    assert foreign_key["constrained_columns"] == ["sensor_id"]
    assert foreign_key["referred_table"] == "orm_sensor"
    assert foreign_key["referred_columns"] == ["id"]
    assert foreign_key.get("options", {}).get("ondelete") == "CASCADE"
    assert [constraint["column_names"] for constraint in inspector.get_unique_constraints("orm_sensor")] == [["name"]]
    assert {constraint["name"] for constraint in inspector.get_check_constraints("orm_reading")} == {
        "ck_orm_reading_quality"
    }
    assert {index["name"] for index in inspector.get_indexes("orm_reading")} == {"ix_orm_reading_sensor_quality"}

    multi_columns = inspector.get_multi_columns(filter_names=["orm_sensor", "orm_reading", "orm_type_matrix"])
    assert {name for (_schema, name) in multi_columns} == {"orm_sensor", "orm_reading", "orm_type_matrix"}


def test_orm_type_matrix_matches_arrow_fetch(engine: Engine) -> None:
    timestamp = datetime.datetime(2026, 7, 28, 12, 34, 56, 123456)
    values: dict[str, Any] = {
        "small_value": -(2**15) + 1,
        "int_value": 2**31 - 1,
        "big_value": -(2**63) + 1,
        "huge_value": 10**38 - 1,
        "float_value": 3.25e38,
        "double_value": -1.0e308,
        "numeric_value": decimal.Decimal("123456789012.345678"),
        "string_value": "",
        "text_value": "text \U0001f680",
        "bool_value": True,
        "date_value": datetime.date(2026, 7, 28),
        "time_value": datetime.time(23, 59, 59, 123456),
        "datetime_value": timestamp,
        "binary_value": b"\x00\xffpayload",
        "uuid_value": uuid.UUID("12345678-1234-5678-1234-567812345678"),
        "json_value": {"nested": {"items": [1, "two", None]}},
        "second_interval_value": datetime.timedelta(days=1, microseconds=123000),
        "month_interval_value": 14,
        "inet_value": "127.0.0.1",
        "url_value": "https://example.com/\U0001f680",
    }

    with Session(engine) as session:
        stored = TypeMatrix(**values)
        nulls = TypeMatrix()
        session.add_all([stored, nulls])
        session.commit()
        stored_id, null_id = stored.id, nulls.id

        for non_finite in (float("nan"), float("inf"), float("-inf")):
            session.add(TypeMatrix(double_value=non_finite))
            with pytest.raises(DBAPIError, match="non-finite float"):
                session.commit()
            session.rollback()

    with Session(engine) as session:
        stored = session.get_one(TypeMatrix, stored_id)
        assert stored.small_value == values["small_value"]
        assert stored.int_value == values["int_value"]
        assert stored.big_value == values["big_value"]
        assert stored.huge_value == values["huge_value"]
        assert stored.float_value == pytest.approx(values["float_value"])
        assert stored.double_value == pytest.approx(values["double_value"])
        assert stored.numeric_value == values["numeric_value"]
        assert stored.string_value == ""
        assert stored.text_value == values["text_value"]
        assert stored.datetime_value == timestamp
        assert stored.json_value == values["json_value"]
        assert stored.second_interval_value == values["second_interval_value"]
        assert stored.month_interval_value == 14

        nulls = session.get_one(TypeMatrix, null_id)
        assert all(getattr(nulls, attribute) is None for attribute in values)
        arrow = fetch_arrow_table(
            session.connection(),
            select(TypeMatrix.__table__).where(TypeMatrix.id == stored_id),
        )
        assert arrow.num_rows == 1
        assert arrow.column("huge_value").to_pylist() == [values["huge_value"]]
        assert arrow.column("string_value").to_pylist() == [""]
        assert arrow.column("text_value").to_pylist() == [values["text_value"]]
        assert arrow.column("binary_value").to_pylist() == [values["binary_value"]]
        assert arrow.column("json_value").to_pylist() == ['{"nested":{"items":[1,"two",null]}}']
        assert arrow.column("second_interval_value").to_pylist() == [values["second_interval_value"]]
        assert arrow.column("month_interval_value").to_pylist() == [14]


def test_orm_crud_workflows_cross_parameter_batches(engine: Engine) -> None:
    sensors = [Sensor(id=10_000 + index, name=f"sensor-{index:04d}", active=index % 2 == 0) for index in range(2_500)]
    with Session(engine) as session:
        session.add_all(sensors)
        session.commit()

    ts = datetime.datetime(2026, 7, 28, 12, 0)
    with Session(engine) as session:
        sensor = session.get_one(Sensor, 10_000)
        sensor.readings = [
            Reading(ts=ts, value=1.5, quality=10, note="first"),
            Reading(ts=ts + datetime.timedelta(minutes=1), value=2.5, quality=20, note="second"),
        ]
        session.commit()

    with Session(engine) as session:
        joined = session.execute(
            select(Sensor.name, Reading.value).join(Reading).where(Reading.quality >= 10).order_by(Reading.ts)
        ).all()
        assert joined == [("sensor-0000", 1.5), ("sensor-0000", 2.5)]
        sensor = session.get_one(Sensor, 10_000)
        assert [reading.note for reading in sensor.readings] == ["first", "second"]
        assert session.scalar(select(func.count()).select_from(Sensor)) == 2_500
        sensor.name = "sensor-updated"
        session.commit()

    with engine.begin() as connection:
        connection.execute(update(Sensor).where(Sensor.id == 10_001).values(active=True))
        assert connection.scalar(select(Sensor.active).where(Sensor.id == 10_001)) is True

    with Session(engine) as session:
        merged = session.merge(Sensor(id=10_002, name="sensor-merged", active=True))
        session.commit()
        assert merged.name == "sensor-merged"

    with Session(engine) as session:
        session.execute(delete(Sensor).where(Sensor.id == 10_000))
        session.commit()
        assert session.scalar(select(func.count()).select_from(Reading)) == 0


def test_orm_constraint_errors_recover_after_rollback(engine: Engine) -> None:
    ts = datetime.datetime(2026, 7, 28, 12, 0)
    with Session(engine) as session:
        session.add_all(
            [
                Sensor(id=1, name="one", active=True),
                Sensor(id=2, name="two", active=True),
            ]
        )
        session.commit()

        rejected: list[Any] = [
            Sensor(id=1, name="duplicate-pk", active=True),
            Sensor(id=3, name="one", active=True),
            Sensor(id=4, name=cast(Any, None), active=True),
            Reading(sensor_id=999, ts=ts, quality=1),
            Reading(sensor_id=1, ts=ts, quality=101),
        ]
        for value in rejected:
            session.add(value)
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()
            assert session.scalar(select(func.count()).select_from(Sensor)) == 2


def test_orm_transactions_savepoints_and_ingest_poison(engine: Engine) -> None:
    with Session(engine) as session:
        session.add(Sensor(id=1, name="rolled-back", active=True))
        session.rollback()
    with Session(engine) as session:
        assert session.get(Sensor, 1) is None
        session.add(Sensor(id=2, name="committed", active=True))
        session.commit()

    with Session(engine) as session:
        session.add(Sensor(id=3, name="outer", active=True))
        nested = session.begin_nested()
        session.add(Sensor(id=4, name="inner", active=True))
        session.flush()
        nested.rollback()
        session.commit()
    with Session(engine) as session:
        assert session.get(Sensor, 3) is not None
        assert session.get(Sensor, 4) is None

    duplicate_ts = datetime.datetime(2026, 7, 28, 12, 0)
    with Session(engine) as session:
        data = _reading_table([(2, duplicate_ts), (2, duplicate_ts)])
        with pytest.raises(adbc_driver_manager.IntegrityError):
            ingest_arrow(
                session.connection(),
                cast(Table, Reading.__table__),
                data,
                statement_options={"adbc.monetdb.write_batch_rows": 1},
            )
        with pytest.raises(DBAPIError):
            session.commit()
        session.rollback()
        assert session.scalar(select(func.count()).select_from(Reading)) == 0
        assert session.scalar(select(func.count()).select_from(Sensor)) == 2


def test_arrow_ingest_and_identity_interoperate_with_orm(engine: Engine) -> None:
    with Session(engine) as session:
        first = Sensor(name="orm-first", active=True)
        session.add(first)
        session.commit()
        first_id = first.id

        ts = datetime.datetime(2026, 7, 28, 12, 0, 0, 123456)
        assert (
            ingest_arrow(
                session.connection(),
                cast(Table, Reading.__table__),
                _reading_table([(first_id, ts)]),
            )
            == 1
        )
        session.commit()
        reading = session.get_one(Reading, (first_id, ts))
        assert reading.raw == b"\x00raw"
        assert reading.delta == datetime.timedelta(milliseconds=1250)

        identity_schema = pa.schema(
            [
                pa.field("id", pa.int32(), nullable=False),
                pa.field("name", pa.large_string(), nullable=False),
            ]
        )
        bulk_identity = pa.Table.from_arrays(
            [
                pa.array([100], type=pa.int32()),
                pa.array(["bulk-100"], type=pa.large_string()),
            ],
            schema=identity_schema,
        )
        assert ingest_arrow(session.connection(), cast(Table, IngestIdentity.__table__), bulk_identity) == 1
        session.commit()

        after_bulk = IngestIdentity(name="orm-after-bulk")
        session.add(after_bulk)
        session.commit()
        assert after_bulk.id == 1
        assert session.get_one(IngestIdentity, 100).name == "bulk-100"


def test_pydantic_json_workflows_and_mutation_tracking(engine: Engine) -> None:
    with Session(engine) as session:
        session.add_all(
            [
                Sensor(id=1, name="model", tags=_tags(), active=True),
                Sensor(id=2, name="null", tags=None, active=True),
                *[
                    Sensor(id=100 + index, name=f"many-{index}", tags=_tags(str(index)), active=True)
                    for index in range(100)
                ],
            ]
        )
        session.commit()

    with Session(engine) as session:
        model = session.get_one(Sensor, 1)
        assert isinstance(model.tags, SensorTags)
        assert model.tags == _tags()
        assert session.get_one(Sensor, 2).tags is None

        model.tags.labels.append("not-tracked")
        session.commit()
        session.expire(model)
        assert model.tags is not None
        assert model.tags.labels == ["primary"]

        model.tags = model.tags.model_copy(update={"labels": ["primary", "assigned"]})
        session.commit()
        session.expire(model)
        assert cast(SensorTags, model.tags).labels == ["primary", "assigned"]

        arrow = fetch_arrow_table(session.connection(), select(Sensor.id, Sensor.tags).where(Sensor.id == 1))
        assert isinstance(arrow.column("tags").to_pylist()[0], str)

    observed = "2026-07-28T10:00:00.123456"
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO orm_sensor (id, name, tags, active) VALUES (5000, 'evolved', :tags, TRUE)"),
            {
                "tags": (
                    f'{{"labels":["new"],"details":{{"owner":"ops","calibrated":true}},'
                    f'"observed_at":"{observed}","extra":"ignored"}}'
                )
            },
        )
        connection.execute(
            text("INSERT INTO orm_sensor (id, name, tags, active) VALUES (5001, 'invalid', :tags, TRUE)"),
            {"tags": '{"details":{"owner":"ops","calibrated":true},"observed_at":"2026-07-28T10:00:00"}'},
        )

    with Session(engine) as session:
        evolved = session.get_one(Sensor, 5000)
        assert evolved.tags is not None
        assert evolved.tags.labels == ["new"]
        assert evolved.tags.note is None
        with pytest.raises(ValidationError):
            session.get_one(Sensor, 5001)

    reflected = {column["name"]: column["type"] for column in inspect(engine).get_columns("orm_sensor")}
    assert isinstance(reflected["tags"], JSON)

    class MutableBase(DeclarativeBase):
        pass

    class MutableDocument(MutableBase):
        __tablename__ = "orm_mutable_document"

        id: Mapped[int] = mapped_column(Integer, primary_key=True)
        document: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON))

    MutableBase.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            session.add(MutableDocument(id=1, document={"labels": ["one"]}))
            session.commit()
        with Session(engine) as session:
            document = session.get_one(MutableDocument, 1)
            document.document["state"] = "changed"
            session.commit()
        with Session(engine) as session:
            assert session.get_one(MutableDocument, 1).document["state"] == "changed"
    finally:
        MutableBase.metadata.drop_all(engine)


@pytest.mark.disconnect
def test_pool_pre_ping_recycles_after_server_restart(monetdb_uri: str) -> None:
    if os.environ.get("MONETDB_RUN_DISCONNECT_TEST") != "1":
        pytest.skip("set MONETDB_RUN_DISCONNECT_TEST=1 to restart the test container")

    container = os.environ.get("MONETDB_TEST_CONTAINER", "sqlalchemy-monetdb-adbc-monetdb-1")
    engine = create_engine(monetdb_uri, pool_pre_ping=True)
    connection = engine.connect()
    connection.exec_driver_sql("SELECT 1")
    subprocess.run(["docker", "restart", container], check=True, capture_output=True)
    with pytest.raises(DBAPIError):
        connection.close()

    deadline = time.monotonic() + 30
    while True:
        health = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
                container,
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if health == "healthy":
            break
        if time.monotonic() >= deadline:
            raise TimeoutError(f"MonetDB did not become healthy within 30 seconds (status: {health})")
        time.sleep(0.25)

    try:
        with engine.connect() as recycled:
            assert recycled.exec_driver_sql("SELECT 1").scalar() == 1
    finally:
        engine.dispose()
