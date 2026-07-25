from sqlalchemy.dialects import registry

registry.register("monetdb", "sqlalchemy_monetdb_adbc.dialect", "MonetDBADBCDialect")
registry.register("monetdb.adbc", "sqlalchemy_monetdb_adbc.dialect", "MonetDBADBCDialect")
registry.register("monetdbs", "sqlalchemy_monetdb_adbc.dialect", "MonetDBADBCDialect")
registry.register("monetdbs.adbc", "sqlalchemy_monetdb_adbc.dialect", "MonetDBADBCDialect")

from sqlalchemy.testing.plugin.pytestplugin import *  # noqa: E402, F403

import sqlalchemy_monetdb_adbc.provision  # noqa: E402, F401
