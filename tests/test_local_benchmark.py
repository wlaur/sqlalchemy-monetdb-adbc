import datetime
import gc
import os
import statistics
import time
from collections.abc import Callable
from typing import cast

import pymonetdb
import pytest
from adbc_driver_monetdb import dbapi

from sqlalchemy_monetdb_adbc.base import MonetDBCursor


def _positive_environment_integer(name: str, default: int) -> int:
    value = int(os.environ.get(name, default))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


@pytest.mark.integration
def test_local_temporal_materialization_against_pymonetdb(monetdb_uri: str) -> None:
    if os.environ.get("MONETDB_RUN_TEMPORAL_BENCHMARK") != "1":
        pytest.skip("set MONETDB_RUN_TEMPORAL_BENCHMARK=1 to run the temporal benchmark")

    rounds = _positive_environment_integer("MONETDB_BENCH_ROUNDS", 3)
    rows = _positive_environment_integer("MONETDB_BENCH_ROWS", 1_000_000)
    queries: dict[str, tuple[str, type[object]]] = {
        "timestamp": (
            (
                "SELECT TIMESTAMP '2024-01-01 00:00:00' + value * INTERVAL '0.001' SECOND "
                f"FROM sys.generate_series(0, {rows})"
            ),
            datetime.datetime,
        ),
        "timestamptz": (
            (
                "SELECT TIMESTAMP WITH TIME ZONE '2024-01-01 00:00:00+00:00' "
                "+ value * INTERVAL '0.001' SECOND "
                f"FROM sys.generate_series(0, {rows})"
            ),
            datetime.datetime,
        ),
        "time": (
            (f"SELECT TIME '00:00:00' + MOD(value, 86400) * INTERVAL '1' SECOND FROM sys.generate_series(0, {rows})"),
            datetime.time,
        ),
    }

    with (
        dbapi.connect(monetdb_uri, autocommit=True) as adbc_connection,
        pymonetdb.connect(monetdb_uri, autocommit=True) as pymonetdb_connection,
        adbc_connection.cursor() as adbc_cursor,
    ):
        dialect_cursor = MonetDBCursor(adbc_cursor)
        pymonetdb_cursor = pymonetdb_connection.cursor()

        def dialect_fetch(query: str) -> list[tuple[object, ...]]:
            dialect_cursor.execute(query)
            return dialect_cursor.fetchall()

        def pymonetdb_fetch(query: str) -> list[tuple[object, ...]]:
            pymonetdb_cursor.execute(query)
            return cast(list[tuple[object, ...]], pymonetdb_cursor.fetchall())

        fetchers: dict[str, Callable[[str], list[tuple[object, ...]]]] = {
            "dialect": dialect_fetch,
            "pymonetdb": pymonetdb_fetch,
        }
        measurements: dict[str, dict[str, list[float]]] = {
            query_name: {client: [] for client in fetchers} for query_name in queries
        }

        for query_index, (query_name, (query, expected_type)) in enumerate(queries.items()):
            for fetch in fetchers.values():
                warmup = fetch(query.replace(f"generate_series(0, {rows})", "generate_series(0, 1000)"))
                assert len(warmup) == 1_000
                assert isinstance(warmup[0][0], expected_type)

            for round_index in range(rounds):
                order = ("dialect", "pymonetdb") if (query_index + round_index) % 2 == 0 else ("pymonetdb", "dialect")
                for client in order:
                    gc.collect()
                    gc.disable()
                    try:
                        started = time.perf_counter()
                        result = fetchers[client](query)
                        elapsed = time.perf_counter() - started
                    finally:
                        gc.enable()
                    assert len(result) == rows
                    assert isinstance(result[0][0], expected_type)
                    assert isinstance(result[-1][0], expected_type)
                    measurements[query_name][client].append(elapsed)
                    del result

    rendered = [f"temporal_rows={rows}"]
    for query_name, clients in measurements.items():
        dialect_seconds = statistics.median(clients["dialect"])
        pymonetdb_seconds = statistics.median(clients["pymonetdb"])
        rendered.append(
            f"{query_name}_dialect={dialect_seconds:.3f}s "
            f"{query_name}_pymonetdb={pymonetdb_seconds:.3f}s "
            f"{query_name}_ratio={dialect_seconds / pymonetdb_seconds:.2f}x"
        )
    print("\n" + " ".join(rendered))
