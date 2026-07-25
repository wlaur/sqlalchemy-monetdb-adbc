import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine

TEST_URI_VAR = "MONETDB_TEST_URI"


@pytest.fixture(scope="session")
def monetdb_uri() -> str:
    uri = os.environ.get(TEST_URI_VAR)
    if not uri:
        pytest.skip(f"{TEST_URI_VAR} is not set")
    return uri


@pytest.fixture(scope="session")
def engine(monetdb_uri: str) -> Iterator[Engine]:
    engine = create_engine(monetdb_uri)
    with engine.connect() as connection:
        connection.exec_driver_sql("SELECT 1")
    yield engine
    engine.dispose()
