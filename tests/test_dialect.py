from types import ModuleType

from sqlalchemy import create_engine
from sqlalchemy.dialects import registry
from sqlalchemy.engine import make_url

from sqlalchemy_monetdb_adbc import MonetDBADBCDialect


def test_entry_point_loads_dialect() -> None:
    assert registry.load("monetdb") is MonetDBADBCDialect
    assert registry.load("monetdb.adbc") is MonetDBADBCDialect
    assert registry.load("monetdbs") is MonetDBADBCDialect
    assert registry.load("monetdbs.adbc") is MonetDBADBCDialect


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
