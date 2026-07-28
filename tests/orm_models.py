from __future__ import annotations

import datetime
import decimal
import uuid
from typing import Any

from pydantic import BaseModel
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Text,
    Time,
    Uuid,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from sqlalchemy_monetdb_adbc import (
    DOUBLE_PRECISION,
    HUGEINT,
    INET,
    MONTH_INTERVAL,
    SECOND_INTERVAL,
    TINYINT,
    URL,
    PydanticJSON,
)


class TagDetails(BaseModel):
    owner: str
    calibrated: bool


class SensorTags(BaseModel):
    labels: list[str]
    details: TagDetails
    observed_at: datetime.datetime
    note: str | None = None


class ORMBase(DeclarativeBase):
    pass


class IngestIdentity(ORMBase):
    __tablename__ = "orm_ingest_identity"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)


class Sensor(ORMBase):
    __tablename__ = "orm_sensor"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    tags: Mapped[SensorTags | None] = mapped_column(PydanticJSON(SensorTags))
    url: Mapped[str | None] = mapped_column(URL)
    addr: Mapped[str | None] = mapped_column(INET)
    created: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    readings: Mapped[list[Reading]] = relationship(back_populates="sensor", passive_deletes=True)


class Reading(ORMBase):
    __tablename__ = "orm_reading"
    __table_args__ = (
        CheckConstraint("quality BETWEEN 0 AND 100", name="ck_orm_reading_quality"),
        Index("ix_orm_reading_sensor_quality", "sensor_id", "quality"),
    )

    sensor_id: Mapped[int] = mapped_column(
        ForeignKey("orm_sensor.id", ondelete="CASCADE"),
        primary_key=True,
    )
    ts: Mapped[datetime.datetime] = mapped_column(DateTime, primary_key=True)
    value: Mapped[float | None] = mapped_column(DOUBLE_PRECISION)
    raw: Mapped[bytes | None] = mapped_column(LargeBinary)
    quality: Mapped[int | None] = mapped_column(TINYINT)
    delta: Mapped[datetime.timedelta | None] = mapped_column(SECOND_INTERVAL)
    age: Mapped[int | None] = mapped_column(MONTH_INTERVAL)
    note: Mapped[str | None] = mapped_column(Text)
    sensor: Mapped[Sensor] = relationship(back_populates="readings")


class TypeMatrix(ORMBase):
    __tablename__ = "orm_type_matrix"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    small_value: Mapped[int | None] = mapped_column(SmallInteger)
    int_value: Mapped[int | None] = mapped_column(Integer)
    big_value: Mapped[int | None] = mapped_column(BigInteger)
    huge_value: Mapped[int | None] = mapped_column(HUGEINT)
    float_value: Mapped[float | None] = mapped_column(Float)
    double_value: Mapped[float | None] = mapped_column(DOUBLE_PRECISION)
    numeric_value: Mapped[decimal.Decimal | None] = mapped_column(Numeric(18, 6))
    string_value: Mapped[str | None] = mapped_column(String(32))
    text_value: Mapped[str | None] = mapped_column(Text)
    bool_value: Mapped[bool | None] = mapped_column(Boolean)
    date_value: Mapped[datetime.date | None] = mapped_column(Date)
    time_value: Mapped[datetime.time | None] = mapped_column(Time)
    datetime_value: Mapped[datetime.datetime | None] = mapped_column(DateTime)
    binary_value: Mapped[bytes | None] = mapped_column(LargeBinary)
    uuid_value: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    json_value: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    second_interval_value: Mapped[datetime.timedelta | None] = mapped_column(SECOND_INTERVAL)
    month_interval_value: Mapped[int | None] = mapped_column(MONTH_INTERVAL)
    inet_value: Mapped[str | None] = mapped_column(INET)
    url_value: Mapped[str | None] = mapped_column(URL)
