from importlib.metadata import PackageNotFoundError, version

from sqlalchemy_monetdb_adbc.dialect import MonetDBADBCDialect

try:
    __version__ = version("sqlalchemy-monetdb-adbc")
except PackageNotFoundError:
    __version__ = "0.0.0"

__all__ = ["MonetDBADBCDialect", "__version__"]
