from importlib.metadata import PackageNotFoundError, version

from sqlalchemy_monetdb_adbc.arrow import (
    DEFAULT_PARQUET_RECLAIM_BYTES,
    ParquetArrowStream,
    ParquetEpochUnit,
    fetch_arrow_table,
    ingest_arrow,
    open_arrow_batch_reader,
)
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
    "DEFAULT_PARQUET_RECLAIM_BYTES",
    "DOUBLE_PRECISION",
    "HUGEINT",
    "INET",
    "MONTH_INTERVAL",
    "SECOND_INTERVAL",
    "TINYINT",
    "URL",
    "MonetDBADBCDialect",
    "ParquetArrowStream",
    "ParquetEpochUnit",
    "PydanticJSON",
    "__version__",
    "fetch_arrow_table",
    "ingest_arrow",
    "open_arrow_batch_reader",
    "raw_adbc_connection",
]
