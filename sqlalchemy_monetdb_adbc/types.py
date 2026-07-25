from collections.abc import Sequence
from datetime import UTC
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import types as sqltypes
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.sql.type_api import (
    _BindProcessorType,  # pyright: ignore[reportPrivateUsage]
    _LiteralProcessorType,  # pyright: ignore[reportPrivateUsage]
)


def _json_path(value: Any) -> str:
    """Render a SQLAlchemy JSON index or path as a MonetDB JSONPath string."""
    if isinstance(value, str):
        return f'$."{value}"'
    if isinstance(value, int):
        return f"$[{value}]"
    if isinstance(value, Sequence):
        elements = cast(Sequence[Any], value)
        parts = "".join(f"[{element}]" if isinstance(element, int) else f'."{element}"' for element in elements)
        return f"${parts}"
    return "$"


class _MonetDBJSONPathBase:
    def _path_processor(self, super_proc: _BindProcessorType[str] | None) -> _BindProcessorType[Any]:
        def process(value: Any) -> Any:
            rendered = value if isinstance(value, str) and value.startswith("$") else _json_path(value)
            if super_proc:
                return super_proc(rendered)
            return rendered

        return process


class MonetDBJSONPathType(_MonetDBJSONPathBase, sqltypes.JSON.JSONPathType):
    def bind_processor(self, dialect: Dialect) -> _BindProcessorType[Any]:
        return self._path_processor(self.string_bind_processor(dialect))

    def literal_processor(self, dialect: Dialect) -> _LiteralProcessorType[Any]:
        return self._path_processor(self.string_literal_processor(dialect))


class MonetDBJSONIndexType(_MonetDBJSONPathBase, sqltypes.JSON.JSONIndexType):
    def bind_processor(self, dialect: Dialect) -> _BindProcessorType[Any]:
        return self._path_processor(self.string_bind_processor(dialect))

    def literal_processor(self, dialect: Dialect) -> _LiteralProcessorType[Any]:
        return self._path_processor(self.string_literal_processor(dialect))


class MonetDBFloat(sqltypes.Float[Any]):
    """FLOAT/DOUBLE binding that coerces Decimal to float.

    ADBC infers the Arrow type of a bound column from its first value, so a
    column mixing float and Decimal (which SQLAlchemy permits for a Float
    column) fails inference. Coercing here keeps the column uniformly double.
    """

    def bind_processor(self, dialect: Dialect) -> _BindProcessorType[Any]:
        def process(value: Any) -> Any:
            if isinstance(value, Decimal):
                return float(value)
            return value

        return process


class MonetDBTime(sqltypes.Time):
    """TIME WITH TIME ZONE result handling.

    MonetDB normalizes timetz to UTC and Arrow's time64 carries no zone, so the
    driver returns a naive time. Reattach UTC for a timezone-aware column.
    """

    def result_processor(self, dialect: Dialect, coltype: Any) -> Any:
        if not self.timezone:
            return None

        def process(value: Any) -> Any:
            if value is not None and value.tzinfo is None:
                return value.replace(tzinfo=UTC)
            return value

        return process


class MonetDBJSON(sqltypes.JSON):
    """MonetDB JSON.

    MonetDB validates and normalizes JSON on input, so round-tripped documents
    keep their values but not their original whitespace or key order.
    """


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
