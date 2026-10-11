from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import adbc_driver_manager
import pytest
from sqlalchemy import create_engine, exc

from sqlalchemy_monetdb_adbc import raw_adbc_connection

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("finish", ["commit", "rollback", "close"])
def test_raw_begin_under_autocommit_obeys_transaction_and_pool_reset(monetdb_uri: str, finish: str) -> None:
    table = f"raw_begin_{uuid4().hex}"
    engine = create_engine(monetdb_uri, pool_size=1, max_overflow=0)
    try:
        with engine.begin() as setup:
            setup.exec_driver_sql(f"CREATE TABLE {table}(value INT)")
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            session_id = connection.exec_driver_sql("SELECT current_sessionid()").scalar_one()
            connection.commit()
            connection.exec_driver_sql("START TRANSACTION")
            assert connection.get_isolation_level() == "SERIALIZABLE"
            connection.exec_driver_sql(f"INSERT INTO {table} VALUES(7)")
            if finish == "commit":
                connection.commit()
            elif finish == "rollback":
                connection.rollback()
            if finish != "close":
                assert connection.get_isolation_level() == "AUTOCOMMIT"
        with engine.connect() as reused:
            assert reused.exec_driver_sql("SELECT current_sessionid()").scalar_one() == session_id
            assert reused.exec_driver_sql(f"SELECT value FROM {table}").all() == ([(7,)] if finish == "commit" else [])
            assert reused.get_isolation_level() == "SERIALIZABLE"
    finally:
        with engine.begin() as cleanup:
            cleanup.exec_driver_sql(f"DROP TABLE IF EXISTS {table}")
        engine.dispose()


def test_concurrent_commit_conflict_rolls_back_loser_and_reuses_sessions(monetdb_uri: str) -> None:
    table = f"commit_conflict_{uuid4().hex}"
    engine = create_engine(monetdb_uri, pool_size=2, max_overflow=0)
    barrier = Barrier(2, timeout=10)
    try:
        with engine.begin() as setup:
            setup.exec_driver_sql(f"CREATE TABLE {table}(id INT PRIMARY KEY, value INT)")

        def worker(value: int) -> tuple[int, bool, int]:
            with engine.connect() as connection:
                session_id = connection.exec_driver_sql("SELECT current_sessionid()").scalar_one()
                connection.exec_driver_sql(f"INSERT INTO {table} VALUES(1,{value})")
                connection.exec_driver_sql(f"INSERT INTO {table} VALUES({value},{value})")
                barrier.wait()
                try:
                    connection.commit()
                    committed = True
                except exc.OperationalError as conflict:
                    assert isinstance(conflict.orig, adbc_driver_manager.OperationalError)
                    assert conflict.orig.sqlstate == "40001"
                    assert not conflict.connection_invalidated
                    connection.rollback()
                    committed = False
                assert connection.exec_driver_sql("SELECT 42").scalar_one() == 42
                assert (
                    raw_adbc_connection(connection).adbc_connection.get_option("adbc.connection.autocommit") == "false"
                )
                return value, committed, session_id

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(worker, [10, 20]))
        winners = [value for value, committed, _ in results if committed]
        assert len(winners) == 1
        with engine.connect() as first, engine.connect() as second:
            reused = {
                first.exec_driver_sql("SELECT current_sessionid()").scalar_one(),
                second.exec_driver_sql("SELECT current_sessionid()").scalar_one(),
            }
            assert reused == {session_id for _, _, session_id in results}
            assert first.exec_driver_sql(f"SELECT * FROM {table} ORDER BY id").all() == [
                (1, winners[0]),
                (winners[0], winners[0]),
            ]
    finally:
        with engine.begin() as cleanup:
            cleanup.exec_driver_sql(f"DROP TABLE IF EXISTS {table}")
        engine.dispose()
