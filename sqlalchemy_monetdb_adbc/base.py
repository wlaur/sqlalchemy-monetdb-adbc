from collections.abc import Callable
from collections.abc import Sequence as AbstractSequence
from typing import Any, cast

from sqlalchemy.engine import default
from sqlalchemy.engine.interfaces import DBAPICursor
from sqlalchemy.sql import compiler
from sqlalchemy.sql.schema import Sequence
from sqlalchemy.sql.type_api import TypeEngine

RESERVED_WORDS = frozenset(
    {
        "action",
        "add",
        "admin",
        "after",
        "aggregate",
        "all",
        "alter",
        "always",
        "and",
        "any",
        "as",
        "asc",
        "asymmetric",
        "atomic",
        "authorization",
        "auto_increment",
        "autoincrement",
        "before",
        "begin",
        "between",
        "bigint",
        "bigserial",
        "binary",
        "blob",
        "boolean",
        "by",
        "cache",
        "call",
        "cascade",
        "case",
        "cast",
        "char",
        "character",
        "check",
        "clob",
        "cluster",
        "clustered",
        "column",
        "comment",
        "commit",
        "committed",
        "comparison",
        "constraint",
        "copy",
        "create",
        "cross",
        "current_date",
        "current_role",
        "current_schema",
        "current_time",
        "current_timestamp",
        "current_user",
        "cycle",
        "data",
        "date",
        "day",
        "decimal",
        "declare",
        "default",
        "delete",
        "delimiters",
        "desc",
        "distinct",
        "do",
        "double",
        "drop",
        "each",
        "else",
        "elseif",
        "encrypted",
        "end",
        "escape",
        "every",
        "except",
        "exclude",
        "execute",
        "exists",
        "external",
        "extract",
        "false",
        "fetch",
        "filter",
        "first",
        "float",
        "following",
        "for",
        "foreign",
        "from",
        "full",
        "function",
        "generated",
        "global",
        "grant",
        "group",
        "grouping",
        "having",
        "hour",
        "huge",
        "hugeint",
        "identity",
        "if",
        "ilike",
        "in",
        "increment",
        "index",
        "inner",
        "insert",
        "int",
        "integer",
        "intersect",
        "interval",
        "into",
        "is",
        "isolation",
        "join",
        "json",
        "key",
        "language",
        "last",
        "lateral",
        "left",
        "level",
        "like",
        "limit",
        "local",
        "localtime",
        "localtimestamp",
        "lockedcopy",
        "match",
        "maxvalue",
        "median",
        "merge",
        "minute",
        "minvalue",
        "month",
        "name",
        "natural",
        "new",
        "next",
        "no",
        "nomaxvalue",
        "nominvalue",
        "not",
        "null",
        "nulls",
        "numeric",
        "of",
        "offset",
        "old",
        "on",
        "only",
        "option",
        "options",
        "or",
        "order",
        "others",
        "outer",
        "over",
        "overlaps",
        "partial",
        "partition",
        "password",
        "path",
        "position",
        "preceding",
        "precision",
        "preferences",
        "preserve",
        "primary",
        "privileges",
        "procedure",
        "public",
        "quantile",
        "range",
        "read",
        "real",
        "records",
        "references",
        "referencing",
        "release",
        "remote",
        "rename",
        "repeatable",
        "replace",
        "restart",
        "restrict",
        "return",
        "returns",
        "revoke",
        "right",
        "role",
        "rollback",
        "rollup",
        "row",
        "rows",
        "savepoint",
        "schema",
        "second",
        "select",
        "sequence",
        "serial",
        "serializable",
        "session",
        "session_user",
        "set",
        "similar",
        "simple",
        "smallint",
        "some",
        "split_part",
        "start",
        "statement",
        "stdin",
        "stdout",
        "stream",
        "string",
        "sublist",
        "substring",
        "symmetric",
        "table",
        "temporary",
        "text",
        "then",
        "ties",
        "time",
        "timestamp",
        "tinyint",
        "to",
        "trace",
        "transaction",
        "trigger",
        "true",
        "truncate",
        "type",
        "unbounded",
        "uncommitted",
        "unencrypted",
        "union",
        "unique",
        "unknown",
        "update",
        "user",
        "using",
        "uuid",
        "values",
        "varchar",
        "varying",
        "view",
        "when",
        "where",
        "while",
        "window",
        "with",
        "within",
        "without",
        "work",
        "write",
        "year",
        "zone",
    }
)


class MonetDBIdentifierPreparer(compiler.IdentifierPreparer):
    reserved_words = RESERVED_WORDS
    illegal_initial_characters = {str(digit) for digit in range(10)} | {"_", "$"}


class MonetDBCursor:
    """DB-API cursor wrapper that reports "no result set" as ``None``.

    ADBC always hands back an Arrow stream, so ``adbc_driver_manager`` derives
    an empty ``description`` list for DDL and for DML without RETURNING. PEP 249
    requires ``None`` there, and SQLAlchemy relies on that to decide whether a
    statement returned rows: an empty list is falsy but not ``None``, so it
    would build a zero-column result instead of a no-rows result.
    """

    __slots__ = (
        "_cursor",
        "close",
        "execute",
        "executemany",
        "fetchall",
        "fetchmany",
        "fetchone",
    )

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor
        self.execute: Callable[..., Any] = cursor.execute
        self.executemany: Callable[..., Any] = cursor.executemany
        self.fetchone: Callable[[], Any] = cursor.fetchone
        self.fetchmany: Callable[..., Any] = cursor.fetchmany
        self.fetchall: Callable[[], Any] = cursor.fetchall
        self.close: Callable[[], None] = cursor.close

    @property
    def description(self) -> AbstractSequence[Any] | None:
        return self._cursor.description or None

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def adbc_cursor(self) -> Any:
        return self._cursor

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class MonetDBExecutionContext(default.DefaultExecutionContext):
    def create_cursor(self) -> DBAPICursor:
        return cast(DBAPICursor, MonetDBCursor(self._dbapi_connection.cursor()))

    def fire_sequence(self, seq: Sequence, type_: TypeEngine[Any]) -> int:
        name = self.identifier_preparer.format_sequence(seq)
        return self._execute_scalar(f"SELECT NEXT VALUE FOR {name}", type_)
