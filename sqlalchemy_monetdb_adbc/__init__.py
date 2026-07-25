from importlib.metadata import PackageNotFoundError, version

from sqlalchemy_monetdb_adbc.connection import raw_adbc_connection
from sqlalchemy_monetdb_adbc.dialect import MonetDBADBCDialect
from sqlalchemy_monetdb_adbc.types import (
    DOUBLE_PRECISION,
    HUGEINT,
    INET,
    MONTH_INTERVAL,
    SECOND_INTERVAL,
    TINYINT,
    URL,
    PydanticJSON,
)

try:
    __version__ = version("sqlalchemy-monetdb-adbc")
except PackageNotFoundError:
    __version__ = "0.0.0"

__all__ = [
    "DOUBLE_PRECISION",
    "HUGEINT",
    "INET",
    "MONTH_INTERVAL",
    "SECOND_INTERVAL",
    "TINYINT",
    "URL",
    "MonetDBADBCDialect",
    "PydanticJSON",
    "__version__",
    "raw_adbc_connection",
]
