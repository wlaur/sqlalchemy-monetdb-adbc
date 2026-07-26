"""Fast Arrow column to Python list conversion.

``pyarrow.Array.to_pylist`` boxes every element through a generic scalar
conversion. For bulk reads that cost dominates everything else: converting a
50k-row, 4-column result took about ten times as long as pulling it off the
wire. ``to_numpy(zero_copy_only=False).tolist()`` does the same work in a single
C pass, roughly 13x faster on numerics, strings, and binary, and up to 36x on
temporal types. End to end that took a 50k-row read from 39.8ms to 12.3ms.

This is why numpy is a dependency rather than an optional accelerator: without
it the fastest available path is reading the Arrow values buffer through
``memoryview.cast``, which matches numpy on fixed-width numerics but does
nothing for the string and temporal columns where most of the cost actually is.

The fast path is guarded, and anything not provably identical to ``to_pylist``
falls back to it. Each exclusion is a case where numpy returns a different
Python type or value, and every one is asserted in ``tests/test_convert.py``:

* Integer and floating-point columns containing nulls. numpy has no integer
  null and represents both cases with ``nan``.
* Timezone-aware timestamps, which come back naive with ``tzinfo`` dropped.
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

from typing import Any, Final, cast

import pyarrow as pa

# Temporal units numpy converts to the same Python object ``to_pylist`` returns.
_SAFE_TEMPORAL_UNITS: Final = frozenset({"s", "ms", "us"})


def _numpy_safe(data_type: Any) -> bool:
    """Whether ``to_numpy().tolist()`` round-trips this ``pyarrow.DataType`` exactly.

    A whitelist rather than a blacklist: an unrecognized type falls back to
    ``to_pylist`` instead of silently converting through an unverified path.
    """
    if pa.types.is_timestamp(data_type):
        return data_type.tz is None and data_type.unit in _SAFE_TEMPORAL_UNITS

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


def column_to_pylist(column: Any) -> list[Any]:
    """Convert one :class:`pyarrow.Array` to Python objects.

    Always equal to ``column.to_pylist()``, down to the exact Python type of
    every element.
    """
    nulls_change_values = column.null_count and (pa.types.is_integer(column.type) or pa.types.is_floating(column.type))
    tiny_scalar_batch = len(column) == 1 and not pa.types.is_temporal(column.type)
    if tiny_scalar_batch or nulls_change_values or not _numpy_safe(column.type):
        return cast(list[Any], column.to_pylist())

    return cast(list[Any], column.to_numpy(zero_copy_only=False).tolist())


def batch_to_rows(batch: Any) -> list[tuple[Any, ...]]:
    """Convert a :class:`pyarrow.RecordBatch` to row tuples, one column at a time."""
    return list(zip(*(column_to_pylist(column) for column in batch.columns), strict=True))
