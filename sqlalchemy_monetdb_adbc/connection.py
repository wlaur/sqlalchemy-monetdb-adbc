from typing import Any

import adbc_driver_manager
from adbc_driver_manager.dbapi import Connection as ADBCConnection
from adbc_driver_manager.dbapi import Cursor as ADBCCursor
from sqlalchemy.engine import Connection as SQLAlchemyConnection


def is_connection_closed_error(error: BaseException) -> bool:
    return (
        isinstance(error, adbc_driver_manager.Error)
        and error.status_code == adbc_driver_manager.AdbcStatusCode.INVALID_STATE
        and "connection has been closed" in str(error).lower()
    )


class MonetDBConnection:
    __slots__ = ("_autocommit", "_closed", "_defunct", "raw_connection")

    def __init__(self, connection: ADBCConnection) -> None:
        self.raw_connection = connection
        self._autocommit = connection.adbc_connection.get_option("adbc.connection.autocommit") == "true"
        self._closed = False
        self._defunct = False

    def cursor(self) -> ADBCCursor:
        return self.raw_connection.cursor()

    def commit(self) -> None:
        if not self._autocommit:
            try:
                self.raw_connection.commit()
            except adbc_driver_manager.ProgrammingError as error:
                if (
                    error.status_code == adbc_driver_manager.AdbcStatusCode.INVALID_STATE
                    and error.sqlstate is not None
                    and error.sqlstate.startswith("25")
                ):
                    try:
                        self.raw_connection.rollback()
                    except adbc_driver_manager.Error as rollback_error:
                        error.add_note(f"automatic rollback also failed: {rollback_error}")
                        self.close()
                raise

    def rollback(self) -> None:
        if self._autocommit or self._closed or self._defunct:
            return
        try:
            self.raw_connection.rollback()
        except adbc_driver_manager.ProgrammingError as error:
            if not is_connection_closed_error(error):
                raise
            self._defunct = True

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.raw_connection.close()
        except adbc_driver_manager.Error as error:
            if not is_connection_closed_error(error):
                raise
        finally:
            self._closed = True
            self._defunct = True

    @property
    def autocommit(self) -> bool:
        return self._autocommit

    @property
    def closed(self) -> bool:
        return self._closed or self._defunct

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
