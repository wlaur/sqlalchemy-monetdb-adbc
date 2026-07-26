"""Fast Arrow column to Python list conversion.

``pyarrow.Array.to_pylist`` boxes every element through a generic scalar
conversion. For bulk reads that cost can dominate pulling the result off the
wire. ``to_numpy(zero_copy_only=False).tolist()`` does the same work one column
at a time in compiled code.

This is why numpy is a dependency rather than an optional accelerator: without
it the fastest available path is reading the Arrow values buffer through
``memoryview.cast``, which matches numpy on fixed-width numerics but does
nothing for the string and temporal columns where most of the cost actually is.

The fast path is guarded, and anything not provably identical to ``to_pylist``
falls back to it. Each exclusion is a case where numpy returns a different
Python type or value, and every one is asserted in ``tests/test_convert.py``:

* Integer and floating-point columns containing nulls. numpy has no integer
  null and represents both cases with ``nan``.
* Nanosecond timestamps and times, which come back as a raw ``int`` count
  because numpy cannot represent a nanosecond instant as a ``datetime``.
* ``date64``, which widens ``datetime.date`` to ``datetime.datetime``.
* Nested and extension types, which are simply not whitelisted.

For a single non-temporal value, the generic scalar conversion is faster than
setting up numpy, so point-query batches take that path directly.
"""


# pyarrow ships no type information, so its data types, arrays, and predicates
# all read as unknown under strict mode. As in ``arrow.py``, Arrow objects are
# annotated ``Any`` and their concrete types given per function.
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false

import datetime
from typing import Any, Final, cast

import pyarrow as pa

# Temporal units numpy converts to the same Python object ``to_pylist`` returns.
_SAFE_TEMPORAL_UNITS: Final = frozenset({"s", "ms", "us"})
_ARROW_EXTENSION_NAME: Final = b"ARROW:extension:name"
_MONETDB_HUGEINT_EXTENSION: Final = b"monetdb.hugeint"


def _numpy_safe(data_type: Any) -> bool:
    """Whether ``to_numpy().tolist()`` round-trips this ``pyarrow.DataType`` exactly.

    A whitelist rather than a blacklist: an unrecognized type falls back to
    ``to_pylist`` instead of silently converting through an unverified path.
    """
    if pa.types.is_timestamp(data_type):
        return data_type.unit in _SAFE_TEMPORAL_UNITS

    if pa.types.is_time(data_type):
        return data_type.unit in _SAFE_TEMPORAL_UNITS

    if pa.types.is_date(data_type):
        return bool(pa.types.is_date32(data_type))

    return bool(
        pa.types.is_integer(data_type)
        or pa.types.is_floating(data_type)
        or pa.types.is_boolean(data_type)
        or pa.types.is_string(data_type)
        or pa.types.is_large_string(data_type)
        or pa.types.is_binary(data_type)
        or pa.types.is_large_binary(data_type)
        or pa.types.is_decimal(data_type)
    )


def _timestamp_to_pylist(column: Any) -> list[datetime.datetime]:
    values = cast(list[datetime.datetime], column.to_numpy(zero_copy_only=False).tolist())
    timezone = cast(datetime.tzinfo, cast(Any, pa).lib.string_to_tzinfo(column.type.tz))
    if timezone.utcoffset(None) == datetime.timedelta(0):
        return [value.replace(tzinfo=timezone) for value in values]

    return [value.replace(tzinfo=datetime.UTC).astimezone(timezone) for value in values]


def column_to_pylist(column: Any) -> list[Any]:
    """Convert one :class:`pyarrow.Array` to Python objects.

    Always equal to ``column.to_pylist()``, down to the exact Python type of
    every element.
    """
    nulls_change_values = column.null_count and (pa.types.is_integer(column.type) or pa.types.is_floating(column.type))
    tiny_scalar_batch = len(column) == 1 and not pa.types.is_temporal(column.type)
    timestamp_with_timezone = bool(pa.types.is_timestamp(column.type) and column.type.tz is not None)
    if timestamp_with_timezone and not column.null_count and _numpy_safe(column.type):
        return cast(list[Any], _timestamp_to_pylist(column))

    if tiny_scalar_batch or nulls_change_values or timestamp_with_timezone or not _numpy_safe(column.type):
        return cast(list[Any], column.to_pylist())

    return cast(list[Any], column.to_numpy(zero_copy_only=False).tolist())


def batch_to_rows(batch: Any) -> list[tuple[Any, ...]]:
    """Convert a :class:`pyarrow.RecordBatch` to row tuples, one column at a time."""
    columns: list[list[Any]] = []
    for field, column in zip(batch.schema, batch.columns, strict=True):
        values = column_to_pylist(column)
        if field.metadata and field.metadata.get(_ARROW_EXTENSION_NAME) == _MONETDB_HUGEINT_EXTENSION:
            values = [None if value is None else int(value) for value in values]
        columns.append(values)
    return list(zip(*columns, strict=True))
