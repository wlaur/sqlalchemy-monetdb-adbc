from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from numbers import Integral, Real
from typing import Any, Protocol, cast

import pyarrow as pa
from pydantic import BaseModel
from sqlalchemy import types as sqltypes
from sqlalchemy.engine.interfaces import Dialect


class BindProcessor(Protocol):
    def __call__(self, value: Any) -> Any: ...


class LiteralProcessor(Protocol):
    def __call__(self, value: Any) -> str: ...


class ResultProcessor(Protocol):
    def __call__(self, value: Any) -> Any: ...


def _json_path(value: Any) -> str:
    """Render a SQLAlchemy JSON index or path as a MonetDB JSONPath string.

    MonetDB's JSONPath takes bare keys: quoting one as ``$."key"`` silently
    matches nothing rather than erroring. An array subscript is a separate step,
    so it needs its own separator: ``$.arr.[0]``, not ``$.arr[0]``, which also
    matches nothing.
    """
    elements = cast(Sequence[Any], value) if isinstance(value, (list, tuple)) else (value,)
    return "$" + "".join(f".[{element}]" if isinstance(element, int) else f".{element}" for element in elements)


class _MonetDBJSONPathBase:
    def _path_processor(self, super_proc: BindProcessor | None) -> BindProcessor:
        def process(value: Any) -> Any:
            rendered = value if isinstance(value, str) and value.startswith("$") else _json_path(value)
            if super_proc:
                return super_proc(rendered)
            return rendered

        return process


class MonetDBJSONPathType(_MonetDBJSONPathBase, sqltypes.JSON.JSONPathType):
    def bind_processor(self, dialect: Dialect) -> BindProcessor:
        return self._path_processor(self.string_bind_processor(dialect))

    def literal_processor(self, dialect: Dialect) -> LiteralProcessor:
        return self._path_processor(self.string_literal_processor(dialect))


class MonetDBJSONIndexType(_MonetDBJSONPathBase, sqltypes.JSON.JSONIndexType):
    def bind_processor(self, dialect: Dialect) -> BindProcessor:
        return self._path_processor(self.string_bind_processor(dialect))

    def literal_processor(self, dialect: Dialect) -> LiteralProcessor:
        return self._path_processor(self.string_literal_processor(dialect))


class MonetDBFloat(sqltypes.Float[Any]):
    """FLOAT/DOUBLE binding that coerces Decimal to float.

    PyArrow cannot build one floating-point parameter array from arbitrary
    mixtures of float and Decimal values. Coercing here makes ordinary mixed
    SQLAlchemy executemany inputs match the prepared Arrow schema.
    """

    def bind_processor(self, dialect: Dialect) -> BindProcessor:
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

    def bind_processor(self, dialect: Dialect) -> BindProcessor | None:
        if not self.timezone:
            return None

        def process(value: Any) -> Any:
            if value is None or value.tzinfo is None or value.utcoffset() is None:
                return value
            return datetime.combine(date(2000, 1, 1), value).astimezone(UTC).time()

        return process

    def result_processor(self, dialect: Dialect, coltype: Any) -> ResultProcessor | None:
        if not self.timezone:
            return None

        def process(value: Any) -> Any:
            if value is not None and value.tzinfo is None:
                return value.replace(tzinfo=UTC)
            return value

        return process


class MonetDBNumeric(sqltypes.Numeric[Any]):
    """NUMERIC that returns Decimal even when MonetDB computed a double.

    The driver returns a real Decimal for a DECIMAL column, so those pass
    through untouched. MonetDB widens an expression to double when it cannot
    infer a bound parameter's type, though, as in ``decimal_column + :value``;
    the precision is already gone server-side, but SQLAlchemy's contract still
    says a Numeric column yields Decimal.
    """

    def bind_processor(self, dialect: Dialect) -> BindProcessor:
        as_decimal = self.asdecimal

        def process(value: Any) -> Any:
            if value is None:
                return None
            if isinstance(value, bool):
                return value
            if as_decimal:
                if isinstance(value, Integral):
                    return Decimal(int(value))
                if not isinstance(value, Real):
                    return value
                # str() first: Decimal(15.7563) would carry the full binary
                # expansion of the float rather than the value that was written.
                return Decimal(str(value))
            return float(value) if isinstance(value, Decimal | Real) else value

        return process

    def result_processor(self, dialect: Dialect, coltype: Any) -> ResultProcessor | None:
        if not self.asdecimal:
            if cast(Any, pa.types).is_floating(coltype):
                return None

            def to_float(value: Any) -> Any:
                return float(value) if isinstance(value, Decimal) else value

            return to_float

        if cast(Any, pa.types).is_decimal(coltype):
            return None

        scale = self._effective_decimal_return_scale

        def process(value: Any) -> Any:
            if isinstance(value, float):
                return Decimal(f"%.{scale}f" % value)
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

    def column_expression(self, colexpr: Any) -> Any:
        return colexpr.cast(sqltypes.String(128))


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


class PydanticJSON[ModelT: BaseModel](sqltypes.TypeDecorator[ModelT]):
    """Store a Pydantic model in a MonetDB JSON column.

    The model is serialized straight to JSON text and parsed straight back with
    ``model_validate_json``, so no intermediate ``dict`` is built in either
    direction. That means overriding ``bind_processor``/``result_processor``
    rather than ``process_bind_param``/``process_result_value``: the latter run
    around the underlying :class:`~sqlalchemy.types.JSON` codecs instead of
    replacing them, which would build the dict this type exists to avoid.

    As for any JSON column, in-place mutation is not tracked; assign a new value
    or use :mod:`sqlalchemy.ext.mutable`.
    """

    impl = MonetDBJSON
    cache_ok = True

    def __init__(self, model: type[ModelT], *args: Any, **kw: Any) -> None:
        self.model = model
        super().__init__(*args, **kw)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.model.__name__})"

    def bind_processor(self, dialect: Dialect) -> BindProcessor:
        def process(value: ModelT | None) -> Any:
            return None if value is None else value.model_dump_json()

        return process

    def result_processor(self, dialect: Dialect, coltype: Any) -> ResultProcessor:
        model = self.model

        def process(value: Any) -> ModelT | None:
            return None if value is None else model.model_validate_json(value)

        return process
