from types import ModuleType

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import registry
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SAWarning
from sqlalchemy.sql import sqltypes

from sqlalchemy_monetdb_adbc import MonetDBADBCDialect
from sqlalchemy_monetdb_adbc._alembic import MonetDBImpl
from sqlalchemy_monetdb_adbc.constants import DIALECT_NAMES
from sqlalchemy_monetdb_adbc.reflection import resolve_type


def test_entry_point_loads_dialect() -> None:
    for dialect_name in DIALECT_NAMES:
        assert registry.load(dialect_name) is MonetDBADBCDialect


def test_create_engine_loads_dialect_without_connecting() -> None:
    urls = (
        "monetdb://monetdb:secret@localhost:50000/demo",
        "monetdb+adbc://monetdb:secret@localhost:50000/demo",
        "monetdbs://monetdb:secret@localhost:50000/demo",
        "monetdbs+adbc://monetdb:secret@localhost:50000/demo",
    )

    for url in urls:
        engine = create_engine(url)
        assert isinstance(engine.dialect, MonetDBADBCDialect)
        engine.dispose()


def test_import_dbapi_loads_monetdb_adbc_driver() -> None:
    dbapi = MonetDBADBCDialect.import_dbapi()

    assert isinstance(dbapi, ModuleType)
    assert dbapi.__name__ == "adbc_driver_monetdb.dbapi"
    assert dbapi.paramstyle == "qmark"


def test_create_connect_args_translates_sqlalchemy_scheme() -> None:
    url = make_url(
        "monetdb+adbc://monetdb:secret@localhost:50000/demo?client_application=sqlalchemy-monetdb-adbc&read_timeout=30"
    )

    args, kwargs = MonetDBADBCDialect().create_connect_args(url)

    assert args == (
        "monetdb://monetdb:secret@localhost:50000/demo?client_application=sqlalchemy-monetdb-adbc&read_timeout=30",
    )
    assert kwargs == {}


def test_create_connect_args_preserves_tls_scheme() -> None:
    url = make_url("monetdbs+adbc://monetdb:secret@localhost:50000/demo")

    args, kwargs = MonetDBADBCDialect().create_connect_args(url)

    assert args == ("monetdbs://monetdb:secret@localhost:50000/demo",)
    assert kwargs == {}


def test_unknown_reflected_type_warns_and_uses_nulltype() -> None:
    with pytest.warns(SAWarning, match="Did not recognize MonetDB type 'future_type'"):
        data_type = resolve_type("future_type", 0, 0)

    assert data_type is sqltypes.NULLTYPE


def test_alembic_unquotes_exactly_one_sql_string_layer() -> None:
    implementation = MonetDBImpl.__new__(MonetDBImpl)
    assert not implementation.compare_server_default(None, None, "'x'", "'x'")
    assert implementation.compare_server_default(None, None, "'x'", "'''x'''")
    assert not implementation.compare_server_default(
        None,
        None,
        "CURRENT_TIMESTAMP",
        "CURRENT_TIMESTAMP",
    )
