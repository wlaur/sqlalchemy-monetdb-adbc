from typing import cast

from sqlalchemy.engine.default import DefaultDialect
from sqlalchemy.engine.interfaces import DBAPIModule
from sqlalchemy.engine.url import URL


class MonetDBADBCDialect(DefaultDialect):
    name = "monetdb"
    driver = "adbc"
    supports_statement_cache = False
    supports_sane_rowcount = False
    supports_sane_multi_rowcount = False
    default_paramstyle = "qmark"

    @classmethod
    def import_dbapi(cls) -> DBAPIModule:
        from adbc_driver_monetdb import dbapi

        return cast(DBAPIModule, dbapi)

    def create_connect_args(self, url: URL) -> tuple[tuple[str], dict[str, object]]:
        driver_url = url.set(drivername="monetdb")
        return (driver_url.render_as_string(hide_password=False),), {}
