"""The Arrow to Python fast paths must be indistinguishable from ``to_pylist``.

Every case asserts value, exact Python type, and (for temporal types) tzinfo,
because the fast paths fail by returning a *plausible* wrong object rather than
by raising. Two such cases are regression tests for bugs caught in review:
``timestamp[ns]`` came back as a raw ``int``, and ``date64`` widened
``datetime.date`` to ``datetime.datetime``.
"""

import datetime
import decimal
from typing import Any

import pyarrow as pa
import pytest

from sqlalchemy_monetdb_adbc._convert import batch_to_rows, column_to_pylist

NAIVE = datetime.datetime(2024, 3, 15, 12, 30, 45, 123456)
AWARE = NAIVE.replace(tzinfo=datetime.UTC)
ROWS = 64


# pyarrow ships no type information, so Arrow objects are annotated Any, as
# the dialect does in arrow.py and _convert.py.
def _repeat(value: object, data_type: Any) -> Any:
    """Build a single-value pyarrow.Array of the given type."""
    return pa.array([value] * ROWS, data_type)


CASES: list[tuple[str, Any]] = [
    # Timestamps, the load-bearing case: naive and aware must both survive.
    ("timestamp[s]", _repeat(NAIVE.replace(microsecond=0), pa.timestamp("s"))),
    ("timestamp[ms]", _repeat(NAIVE.replace(microsecond=123000), pa.timestamp("ms"))),
    ("timestamp[us]", _repeat(NAIVE, pa.timestamp("us"))),
    ("timestamp[ns]", _repeat(NAIVE, pa.timestamp("ns"))),
    ("timestamp[us] UTC", _repeat(AWARE, pa.timestamp("us", tz="UTC"))),
    ("timestamp[us] offset", _repeat(AWARE, pa.timestamp("us", tz="+02:00"))),
    ("timestamp[us] zone", _repeat(AWARE, pa.timestamp("us", tz="Europe/Helsinki"))),
    ("timestamp[ns] UTC", _repeat(AWARE, pa.timestamp("ns", tz="UTC"))),
    ("timestamp[us] null", pa.array([NAIVE, None] * (ROWS // 2), pa.timestamp("us"))),
    ("timestamp[us] UTC null", pa.array([AWARE, None] * (ROWS // 2), pa.timestamp("us", tz="UTC"))),
    # Other temporal.
    ("date32", _repeat(datetime.date(2024, 3, 15), pa.date32())),
    ("date64", _repeat(datetime.date(2024, 3, 15), pa.date64())),
    ("time32[s]", _repeat(datetime.time(12, 30, 45), pa.time32("s"))),
    ("time64[us]", _repeat(datetime.time(12, 30, 45, 123456), pa.time64("us"))),
    ("time64[ns]", _repeat(datetime.time(12, 30, 45, 123456), pa.time64("ns"))),
    ("duration[us]", _repeat(datetime.timedelta(seconds=90), pa.duration("us"))),
    # Numerics, including the signed/unsigned and width edges.
    ("int8", _repeat(-128, pa.int8())),
    ("int16", _repeat(-32768, pa.int16())),
    ("int32", _repeat(-(2**31), pa.int32())),
    ("int64", _repeat(-(2**63), pa.int64())),
    ("uint8", _repeat(255, pa.uint8())),
    ("uint16", _repeat(65535, pa.uint16())),
    ("uint32", _repeat(2**32 - 1, pa.uint32())),
    ("uint64", _repeat(2**64 - 1, pa.uint64())),
    ("float32", _repeat(1.5, pa.float32())),
    ("float64", _repeat(1.5, pa.float64())),
    ("float64 inf", pa.array([float("inf"), float("-inf"), 0.0] * (ROWS // 3), pa.float64())),
    ("bool", pa.array([True, False] * (ROWS // 2), pa.bool_())),
    # Text and binary, including non-ASCII and the empty string.
    ("string", _repeat("plain", pa.string())),
    ("string unicode", _repeat("ünïcodé \U0001f600", pa.string())),
    ("string empty", _repeat("", pa.string())),
    ("large_string", _repeat("large", pa.large_string())),
    ("binary", _repeat(b"\x00\xff\xfe", pa.binary())),
    ("large_binary", _repeat(b"\x00\xff", pa.large_binary())),
    ("decimal128", _repeat(decimal.Decimal("-1.2345"), pa.decimal128(18, 4))),
    ("decimal256", _repeat(decimal.Decimal("1.2345"), pa.decimal256(40, 8))),
    # Integer/float nulls require fallback; other scalar types remain exact.
    ("null type", _repeat(None, pa.null())),
    ("int32 null", pa.array([1, None] * (ROWS // 2), pa.int32())),
    ("float64 null", pa.array([1.5, None] * (ROWS // 2), pa.float64())),
    ("string null", pa.array(["a", None] * (ROWS // 2), pa.string())),
    ("bool null", pa.array([True, None] * (ROWS // 2), pa.bool_())),
    ("decimal null", pa.array([decimal.Decimal("1.1"), None] * (ROWS // 2), pa.decimal128(18, 4))),
    # Nested types have no fast path and must fall back.
    ("list", _repeat([1, 2, 3], pa.list_(pa.int32()))),
    ("struct", _repeat({"a": 1}, pa.struct([("a", pa.int32())]))),
    # A sliced array carries a non-zero offset the fast path must honour.
    ("sliced int32", pa.array(list(range(ROWS)), pa.int32()).slice(7, 20)),
    ("sliced float64", pa.array([float(i) for i in range(ROWS)], pa.float64()).slice(3, 11)),
    ("sliced string", pa.array([f"s{i}" for i in range(ROWS)], pa.string()).slice(7, 20)),
    ("empty int32", pa.array([], pa.int32())),
    ("empty string", pa.array([], pa.string())),
]


@pytest.mark.parametrize("column", [case for _, case in CASES], ids=[name for name, _ in CASES])
def test_matches_to_pylist_exactly(column: Any) -> None:
    expected: list[Any] = column.to_pylist()
    actual = column_to_pylist(column)

    assert actual == expected
    assert [type(value) for value in actual] == [type(value) for value in expected]
    # Equality between datetimes can hold across different tzinfo, so compare it
    # separately: a naive result must never pass for an aware one.
    assert [getattr(value, "tzinfo", None) for value in actual] == [
        getattr(value, "tzinfo", None) for value in expected
    ]


def test_sliced_offset_is_not_ignored() -> None:
    """A conversion that ignored ``offset`` would silently return the wrong rows."""
    column = pa.array(list(range(100)), pa.int32()).slice(10, 5)

    assert column_to_pylist(column) == [10, 11, 12, 13, 14]


def test_single_scalar_batch_skips_numpy_setup() -> None:
    class SingleValueColumn:
        type = pa.int64()
        null_count = 0

        def __len__(self) -> int:
            return 1

        def to_pylist(self) -> list[int]:
            return [7]

        def to_numpy(self, *, zero_copy_only: bool) -> Any:
            raise AssertionError("single scalar batches should not use numpy")

    assert column_to_pylist(SingleValueColumn()) == [7]


def test_batch_to_rows_transposes_columns() -> None:
    batch = pa.RecordBatch.from_arrays(
        [pa.array([1, 2], pa.int32()), pa.array(["a", "b"]), pa.array([None, 1.5], pa.float64())],
        names=["i", "s", "f"],
    )

    assert batch_to_rows(batch) == [(1, "a", None), (2, "b", 1.5)]
