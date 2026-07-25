from adbc_driver_manager.dbapi import Connection as ADBCConnection
from sqlalchemy.engine import Connection


def raw_adbc_connection(connection: Connection) -> ADBCConnection:
    """Return the ADBC DB-API connection backing a SQLAlchemy connection.

    The returned connection is the exact session SQLAlchemy uses for this
    :class:`~sqlalchemy.engine.Connection`, so Arrow-native reads and
    ``adbc_ingest`` writes join the surrounding transaction. Do not close it or
    change its transaction state; use the SQLAlchemy connection for that.
    """
    driver_connection = connection.connection.driver_connection

    if not isinstance(driver_connection, ADBCConnection):
        raise TypeError(f"expected an ADBC DB-API connection, got {type(driver_connection).__name__}")

    return driver_connection
