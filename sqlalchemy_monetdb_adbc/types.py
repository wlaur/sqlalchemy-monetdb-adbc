from typing import Any

from sqlalchemy import types as sqltypes


class TINYINT(sqltypes.Integer):
    __visit_name__ = "TINYINT"


class HUGEINT(sqltypes.Integer):
    __visit_name__ = "HUGEINT"


class DOUBLE_PRECISION(sqltypes.Double[float]):  # noqa: N801
    __visit_name__ = "DOUBLE_PRECISION"


class INET(sqltypes.TypeEngine[str]):
    __visit_name__ = "INET"


class URL(sqltypes.TypeEngine[str]):
    __visit_name__ = "URL"


class MONTH_INTERVAL(sqltypes.TypeEngine[Any]):  # noqa: N801
    __visit_name__ = "MONTH_INTERVAL"


class SECOND_INTERVAL(sqltypes.TypeEngine[Any]):  # noqa: N801
    __visit_name__ = "SECOND_INTERVAL"


MONETDB_TYPE_MAP: dict[str, type[sqltypes.TypeEngine[Any]]] = {
    "bigint": sqltypes.BIGINT,
    "blob": sqltypes.BLOB,
    "boolean": sqltypes.BOOLEAN,
    "char": sqltypes.CHAR,
    "clob": sqltypes.TEXT,
    "date": sqltypes.DATE,
    "day_interval": SECOND_INTERVAL,
    "decimal": sqltypes.DECIMAL,
    "double": DOUBLE_PRECISION,
    "hugeint": HUGEINT,
    "inet": INET,
    "int": sqltypes.INTEGER,
    "json": sqltypes.JSON,
    "month_interval": MONTH_INTERVAL,
    "oid": sqltypes.BIGINT,
    "real": sqltypes.REAL,
    "sec_interval": SECOND_INTERVAL,
    "smallint": sqltypes.SMALLINT,
    "time": sqltypes.TIME,
    "timestamp": sqltypes.TIMESTAMP,
    "timestamptz": sqltypes.TIMESTAMP,
    "timetz": sqltypes.TIME,
    "tinyint": TINYINT,
    "url": URL,
    "uuid": sqltypes.UUID,
    "varchar": sqltypes.VARCHAR,
}
