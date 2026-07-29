import time
import urllib.parse
import uuid
from typing import cast

import pytest
from adbc_driver_monetdb import dbapi
from sqlalchemy import create_engine, exc, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool, QueuePool, StaticPool

from sqlalchemy_monetdb_adbc import fetch_arrow_table, raw_adbc_connection

pytestmark = pytest.mark.integration


def _tagged_uri(uri: str, application: str) -> str:
    separator = "&" if "?" in uri else "?"
    return f"{uri}{separator}client_application={urllib.parse.quote(application)}"


def _session_ids(observer: dbapi.Connection, application: str) -> set[int]:
    cursor = observer.execute(
        "SELECT sessionid FROM sys.sessions WHERE application = ? ORDER BY sessionid",
        [application],
    )
    return {cast(int, row[0]) for row in cursor.fetchall()}


def _wait_for_sessions(
    observer: dbapi.Connection,
    application: str,
    expected: int,
) -> set[int]:
    deadline = time.monotonic() + 3
    while True:
        sessions = _session_ids(observer, application)
        if len(sessions) == expected:
            return sessions
        if time.monotonic() >= deadline:
            raise AssertionError(f"expected {expected} {application!r} sessions, found {sorted(sessions)}")
        time.sleep(0.01)


def test_null_pool_closes_sqlalchemy_arrow_raw_and_error_sessions(
    monetdb_uri: str,
) -> None:
    application = f"dialect-null-{uuid.uuid4().hex}"
    engine = create_engine(_tagged_uri(monetdb_uri, application), poolclass=NullPool)
    with dbapi.connect(monetdb_uri, autocommit=True) as observer:
        try:
            with engine.connect() as connection:
                assert connection.exec_driver_sql("SELECT 1").scalar_one() == 1
                assert fetch_arrow_table(connection, "SELECT 2 AS value").column("value").to_pylist() == [2]
                assert raw_adbc_connection(connection).execute("SELECT 3").fetchone() == (3,)
            _wait_for_sessions(observer, application, 0)

            with Session(engine) as session:
                assert session.scalar(text("SELECT 4")) == 4
            _wait_for_sessions(observer, application, 0)

            with engine.connect() as connection, pytest.raises(exc.ProgrammingError):
                connection.exec_driver_sql("SELECT * FROM dialect_missing_relation")
            _wait_for_sessions(observer, application, 0)
        finally:
            engine.dispose()
        _wait_for_sessions(observer, application, 0)


def test_queue_pool_reuses_healthy_sessions_and_honors_bounds(
    monetdb_uri: str,
) -> None:
    application = f"dialect-queue-{uuid.uuid4().hex}"
    engine = create_engine(
        _tagged_uri(monetdb_uri, application),
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=1,
        pool_timeout=0.01,
    )
    with dbapi.connect(monetdb_uri, autocommit=True) as observer:
        try:
            first = engine.connect()
            second = engine.connect()
            active = _wait_for_sessions(observer, application, 2)
            with pytest.raises(exc.TimeoutError):
                engine.connect()
            first.close()
            second.close()

            idle = _wait_for_sessions(observer, application, 1)
            assert idle <= active
            with engine.connect() as reused:
                assert reused.exec_driver_sql("SELECT 1").scalar_one() == 1
                assert _wait_for_sessions(observer, application, 1) == idle
        finally:
            engine.dispose()
        _wait_for_sessions(observer, application, 0)


def test_static_pool_reuses_one_session_and_dispose_closes_it(
    monetdb_uri: str,
) -> None:
    application = f"dialect-static-{uuid.uuid4().hex}"
    engine = create_engine(_tagged_uri(monetdb_uri, application), poolclass=StaticPool)
    with dbapi.connect(monetdb_uri, autocommit=True) as observer:
        try:
            with engine.connect() as first:
                assert first.exec_driver_sql("SELECT 1").scalar_one() == 1
                original = _wait_for_sessions(observer, application, 1)
            with engine.connect() as second:
                assert second.exec_driver_sql("SELECT 2").scalar_one() == 2
                assert _wait_for_sessions(observer, application, 1) == original
        finally:
            engine.dispose()
        _wait_for_sessions(observer, application, 0)
