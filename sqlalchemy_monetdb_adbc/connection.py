from typing import Any

from adbc_driver_manager.dbapi import Connection as ADBCConnection
from adbc_driver_manager.dbapi import Cursor as ADBCCursor
from sqlalchemy.engine import Connection as SQLAlchemyConnection


class MonetDBConnection:
    __slots__ = ("_autocommit", "_closed", "raw_connection")

    def __init__(self, connection: ADBCConnection) -> None:
        self.raw_connection = connection
        self._autocommit = connection.adbc_connection.get_option("adbc.connection.autocommit") == "true"
        self._closed = False

    def cursor(self) -> ADBCCursor:
        return self.raw_connection.cursor()

    def commit(self) -> None:
        if not self._autocommit:
            self.raw_connection.commit()

    def rollback(self) -> None:
        if not self._autocommit:
            self.raw_connection.rollback()

    def close(self) -> None:
        self.raw_connection.close()
        self._closed = True

    @property
    def autocommit(self) -> bool:
        return self._autocommit

    @property
    def closed(self) -> bool:
        return self._closed

    def set_autocommit(self, value: bool) -> None:
        if self._autocommit == value:
            return
        self.raw_connection.adbc_connection.set_autocommit(value)
        self._autocommit = value

    def __getattr__(self, name: str) -> Any:
        return getattr(self.raw_connection, name)


def raw_adbc_connection(connection: SQLAlchemyConnection) -> ADBCConnection:
    """Return the ADBC DB-API connection backing a SQLAlchemy connection.

    The returned connection is the exact session SQLAlchemy uses for this
    :class:`~sqlalchemy.engine.Connection`, so Arrow-native reads and
    ``adbc_ingest`` writes join the surrounding transaction. Accessing it starts
    SQLAlchemy's transaction if necessary. Do not close it or change its
    transaction state; use the SQLAlchemy connection for that.
    """
    if not connection.in_transaction():
        connection.begin()

    driver_connection = connection.connection.driver_connection

    if not isinstance(driver_connection, MonetDBConnection):
        raise TypeError(f"expected an ADBC DB-API connection, got {type(driver_connection).__name__}")

    return driver_connection.raw_connection
