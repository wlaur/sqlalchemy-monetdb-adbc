import re

import pytest
from sqlalchemy.dialects import registry

registry.register("monetdb", "sqlalchemy_monetdb_adbc.dialect", "MonetDBADBCDialect")
registry.register("monetdb.adbc", "sqlalchemy_monetdb_adbc.dialect", "MonetDBADBCDialect")
registry.register("monetdbs", "sqlalchemy_monetdb_adbc.dialect", "MonetDBADBCDialect")
registry.register("monetdbs.adbc", "sqlalchemy_monetdb_adbc.dialect", "MonetDBADBCDialect")

from known_failures import KNOWN_FAILURES  # noqa: E402
from sqlalchemy.testing.plugin.pytestplugin import *  # noqa: E402, F403

import sqlalchemy_monetdb_adbc.provision  # noqa: E402, F401

# The plugin appends the dialect and server version to each class name, e.g.
# "IntegerTest_monetdb+adbc_11_55_7". Strip it so the entries stay readable and
# survive a server upgrade.
_CLASS_SUFFIX = re.compile(r"_monetdb\+adbc_[\d_]+$")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark the tests MonetDB cannot pass, so a clean run reports no failures.

    xfail rather than skip: the test still runs, and reports XPASS if MonetDB or
    the driver ever gains the behaviour, which is the signal to delete the entry.
    """
    for item in items:
        class_name = _CLASS_SUFFIX.sub("", item.cls.__name__) if item.cls else ""
        qualified = f"{class_name}::{item.name}"
        # Match the exact parametrisation first, then the whole test.
        reason = KNOWN_FAILURES.get(qualified) or KNOWN_FAILURES.get(qualified.split("[")[0])
        if reason is not None:
            item.add_marker(pytest.mark.xfail(reason=reason, strict=False))
