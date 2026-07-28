from collections.abc import Callable
from collections.abc import Sequence as AbstractSequence
from typing import Any, cast

import pyarrow as pa
from sqlalchemy.engine import default
from sqlalchemy.engine.interfaces import DBAPICursor
from sqlalchemy.sql import compiler
from sqlalchemy.sql.schema import Sequence
from sqlalchemy.sql.type_api import TypeEngine

from ._convert import batch_to_rows

record_batch = cast(Callable[..., Any], cast(Any, pa).record_batch)
arrow = cast(Any, pa)
ArrowInvalidError = cast(type[Exception], cast(Any, pa).ArrowInvalid)
ArrowTypeError = cast(type[Exception], cast(Any, pa).ArrowTypeError)
ArrowExtensionName = b"ARROW:extension:name"
MonetDBHugeIntExtension = b"monetdb.hugeint"
MIN_INT64 = -(2**63)
MAX_INT64 = 2**63 - 1

RESERVED_WORDS = frozenset(
    {
        "add",
        "admin",
        "after",
        "aggregate",
        "all",
        "alter",
        "always",
        "analyze",
        "and",
        "any",
        "asc",
        "asymmetric",
        "at",
        "atomic",
        "authorization",
        "auto_increment",
        "before",
        "begin",
        "best",
        "between",
        "big",
        "bigint",
        "bigserial",
        "binary",
        "blob",
        "by",
        "cache",
        "call",
        "cascade",
        "case",
        "cast",
        "century",
        "chain",
        "char",
        "character",
        "check",
        "client",
        "clob",
        "coalesce",
        "column",
        "comment",
        "commit",
        "committed",
        "constraint",
        "continue",
        "convert",
        "copy",
        "corresponding",
        "create",
        "cross",
        "cube",
        "current",
        "current_date",
        "current_role",
        "current_schema",
        "current_time",
        "current_timestamp",
        "current_timezone",
        "current_user",
        "cycle",
        "data",
        "date",
        "day",
        "deallocate",
        "debug",
        "dec",
        "decade",
        "decimal",
        "declare",
        "default",
        "delete",
        "delimiters",
        "desc",
        "diagnostics",
        "distinct",
        "do",
        "double",
        "dow",
        "doy",
        "drop",
        "each",
        "effort",
        "else",
        "elseif",
        "encrypted",
        "end",
        "endian",
        "epoch",
        "escape",
        "every",
        "except",
        "exclude",
        "exec",
        "execute",
        "exists",
        "explain",
        "external",
        "extract",
        "false",
        "first",
        "float",
        "following",
        "for",
        "foreign",
        "from",
        "full",
        "function",
        "fwf",
        "generated",
        "global",
        "grant",
        "group",
        "grouping",
        "groups",
        "having",
        "hour",
        "hugeint",
        "identity",
        "if",
        "ilike",
        "imprints",
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
        "key",
        "language",
        "large",
        "last",
        "lateral",
        "left",
        "level",
        "like",
        "limit",
        "little",
        "loader",
        "local",
        "localtime",
        "localtimestamp",
        "match",
        "matched",
        "maxvalue",
        "mediumint",
        "merge",
        "minute",
        "minvalue",
        "month",
        "name",
        "native",
        "natural",
        "new",
        "next",
        "no",
        "not",
        "now",
        "null",
        "nullif",
        "nulls",
        "numeric",
        "object",
        "of",
        "offset",
        "old",
        "on",
        "only",
        "option",
        "options",
        "or",
        "order",
        "ordered",
        "others",
        "outer",
        "over",
        "partial",
        "partition",
        "password",
        "path",
        "position",
        "preceding",
        "precision",
        "prep",
        "prepare",
        "preserve",
        "primary",
        "privileges",
        "procedure",
        "public",
        "quarter",
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
        "replica",
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
        "sample",
        "savepoint",
        "schema",
        "second",
        "seed",
        "select",
        "sequence",
        "serial",
        "serializable",
        "server",
        "session",
        "session_user",
        "set",
        "sets",
        "simple",
        "size",
        "smallint",
        "some",
        "split_part",
        "start",
        "statement",
        "stdin",
        "stdout",
        "storage",
        "string",
        "substring",
        "symmetric",
        "table",
        "temp",
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
        "update",
        "user",
        "using",
        "value",
        "values",
        "varchar",
        "varying",
        "view",
        "week",
        "when",
        "where",
        "while",
        "window",
        "with",
        "work",
        "write",
        "xmlagg",
        "xmlattributes",
        "xmlcomment",
        "xmlconcat",
        "xmldocument",
        "xmlelement",
        "xmlforest",
        "xmlnamespaces",
        "xmlparse",
        "xmlpi",
        "xmlquery",
        "xmlschema",
        "xmltext",
        "xmlvalidate",
        "year",
        "zone",
    }
) | frozenset(
    {
        "as",
        "both",
        "details",
        "fetch",
        "geometrycollection",
        "geometrycollectionm",
        "geometrycollectionz",
        "geometrycollectionzm",
        "leading",
        "linestring",
        "linestringm",
        "linestringz",
        "linestringzm",
        "logical",
        "multilinestring",
        "multilinestringm",
        "multilinestringz",
        "multilinestringzm",
        "multipoint",
        "multipointm",
        "multipointz",
        "multipointzm",
        "multipolygon",
        "multipolygonm",
        "multipolygonz",
        "multipolygonzm",
        "physical",
        "point",
        "pointm",
        "pointz",
        "pointzm",
        "polygon",
        "polygonm",
        "polygonz",
        "polygonzm",
        "qualify",
        "recursive",
        "returning",
        "rewrite",
        "show",
        "snapshot",
        "sql_bigint",
        "sql_binary",
        "sql_bit",
        "sql_char",
        "sql_date",
        "sql_decimal",
        "sql_double",
        "sql_float",
        "sql_guid",
        "sql_hugeint",
        "sql_integer",
        "sql_interval_day",
        "sql_interval_day_to_hour",
        "sql_interval_day_to_minute",
        "sql_interval_day_to_second",
        "sql_interval_hour",
        "sql_interval_hour_to_minute",
        "sql_interval_hour_to_second",
        "sql_interval_minute",
        "sql_interval_minute_to_second",
        "sql_interval_month",
        "sql_interval_second",
        "sql_interval_year",
        "sql_interval_year_to_month",
        "sql_longvarbinary",
        "sql_longvarchar",
        "sql_numeric",
        "sql_real",
        "sql_smallint",
        "sql_time",
        "sql_timestamp",
        "sql_tinyint",
        "sql_tsi_day",
        "sql_tsi_frac_second",
        "sql_tsi_hour",
        "sql_tsi_minute",
        "sql_tsi_month",
        "sql_tsi_quarter",
        "sql_tsi_second",
        "sql_tsi_week",
        "sql_tsi_year",
        "sql_varbinary",
        "sql_varchar",
        "sql_wchar",
        "sql_wlongvarchar",
        "sql_wvarchar",
        "trailing",
        "unnest",
        "within",
    }
)


class MonetDBIdentifierPreparer(compiler.IdentifierPreparer):
    reserved_words = RESERVED_WORDS
    illegal_initial_characters = {str(digit) for digit in range(10)} | {"_", "$"}


class MonetDBCursor:
    """DB-API cursor wrapper with column-wise row conversion.

    Two departures from ``adbc_driver_manager``'s cursor:

    1. ``description`` reports "no result set" as ``None``.
       DRIVER-WORKAROUND(adbc-driver-manager #3, upstream): ADBC always hands
       back an Arrow stream, so the manager derives an empty ``description``
       list for DDL and for DML without RETURNING. PEP 249 requires ``None``
       there, and SQLAlchemy uses it to decide whether a statement returned
       rows: an empty list is falsy but not ``None``, so it would build a
       zero-column result instead of a no-rows result.

    2. Rows are converted one column at a time rather than one cell at a time.
       The manager builds each row as
       ``tuple(arr[i].as_py() for arr in batch.columns)``, which pays a pyarrow
       scalar boxing cost per cell. :mod:`._convert` converts whole columns and
       zips them, which is several times faster for wide or long results.
       Batches are still consumed lazily, so ``stream_results`` and
       ``yield_per`` keep working.
    """

    __slots__ = ("_batch", "_cursor", "_index", "_reader", "close")

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor
        self._reader: Any = None
        self._batch: list[tuple[Any, ...]] = []
        self._index = 0
        self.close: Callable[[], None] = cursor.close

    def _reset(self) -> None:
        self._reader = None
        self._batch = []
        self._index = 0

    def execute(self, operation: str, parameters: Any = None) -> Any:
        self._reset()
        return self._cursor.execute(operation, parameters)

    def executemany(self, operation: str, seq_of_parameters: Any) -> Any:
        self._reset()
        return self._cursor.executemany(operation, seq_of_parameters)

    def execute_with_parameter_schema(
        self,
        operation: str,
        parameters: Any,
        schema: Any,
    ) -> tuple[Any, Any | None]:
        self._reset()
        batch = parameter_record_batch([parameters], schema)
        if batch is None:
            return self._cursor.execute(operation, parameters), None
        if schema is None:
            prepared_schema = self._cursor.adbc_prepare(operation)
            if prepared_schema is not None:
                batch = parameter_record_batch([parameters], prepared_schema)
                assert batch is not None
        return self._cursor.execute(operation, batch), batch.schema

    def executemany_with_parameter_schema(
        self,
        operation: str,
        seq_of_parameters: Any,
        schema: Any,
    ) -> tuple[Any, Any | None]:
        self._reset()
        if not isinstance(seq_of_parameters, AbstractSequence):
            return self._cursor.executemany(operation, seq_of_parameters), None
        batch = parameter_record_batch(
            cast(AbstractSequence[Any], seq_of_parameters),
            schema,
        )
        if batch is None:
            return self._cursor.executemany(operation, seq_of_parameters), None
        if schema is None:
            prepared_schema = self._cursor.adbc_prepare(operation)
            if prepared_schema is not None:
                batch = parameter_record_batch(
                    cast(AbstractSequence[Any], seq_of_parameters),
                    prepared_schema,
                )
                assert batch is not None
        return self._cursor.executemany(operation, batch), batch.schema

    def _next_batch(self) -> bool:
        """Buffer the next record batch as tuples. False when exhausted."""
        if self._reader is None:
            if self._cursor.description is None:
                return False
            self._reader = self._cursor.fetch_record_batch()

        while True:
            try:
                batch = self._reader.read_next_batch()
            except StopIteration:
                return False
            if batch.num_rows:
                self._batch = batch_to_rows(batch)
                self._index = 0
                return True

    def fetchone(self) -> tuple[Any, ...] | None:
        while self._index >= len(self._batch):
            if not self._next_batch():
                return None
        row = self._batch[self._index]
        self._index += 1
        return row

    def fetchmany(self, size: int | None = None) -> list[tuple[Any, ...]]:
        count = int(self._cursor.arraysize) if size is None else size
        rows: list[tuple[Any, ...]] = []
        while len(rows) < count:
            if self._index >= len(self._batch) and not self._next_batch():
                break
            take = min(count - len(rows), len(self._batch) - self._index)
            rows.extend(self._batch[self._index : self._index + take])
            self._index += take
        return rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        rows = self._batch[self._index :]
        self._index = len(self._batch)
        while self._next_batch():
            rows.extend(self._batch)
            self._index = len(self._batch)
        return rows

    @property
    def description(self) -> AbstractSequence[Any] | None:
        return self._cursor.description or None

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def arraysize(self) -> int:
        return int(self._cursor.arraysize)

    @arraysize.setter
    def arraysize(self, value: int) -> None:
        self._cursor.arraysize = value

    @property
    def adbc_cursor(self) -> Any:
        return self._cursor

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


def parameter_record_batch(rows: AbstractSequence[Any], schema: Any) -> Any | None:
    if not rows or not all(
        isinstance(row, AbstractSequence) and not isinstance(row, (str, bytes, bytearray)) for row in rows
    ):
        return None
    width = len(rows[0])
    if width == 0:
        return None
    if any(len(row) != width for row in rows):
        return None
    columns = [[row[index] for row in rows] for index in range(width)]
    wide_integer_columns = {
        index
        for index, values in enumerate(columns)
        if any(
            isinstance(value, int) and not isinstance(value, bool) and not MIN_INT64 <= value <= MAX_INT64
            for value in values
        )
    }
    if wide_integer_columns:
        arrays: list[Any] = []
        fields: list[Any] = []
        for index, values in enumerate(columns):
            if index in wide_integer_columns:
                data_type: Any = arrow.decimal128(38, 0)
                arrays.append(arrow.array(values, type=data_type))
                fields.append(
                    arrow.field(
                        str(index),
                        data_type,
                        metadata={ArrowExtensionName: MonetDBHugeIntExtension},
                    )
                )
                continue

            field = schema.field(index) if schema is not None and index < len(schema) else None
            array: Any = arrow.array(values, type=field.type if field is not None else None)
            arrays.append(array)
            fields.append(arrow.field(str(index), array.type, metadata=field.metadata if field is not None else None))

        return arrow.RecordBatch.from_arrays(arrays, schema=arrow.schema(fields))
    if schema is None:
        return record_batch(columns, names=[str(index) for index in range(width)])
    try:
        return record_batch(columns, schema=schema)
    except (ArrowInvalidError, ArrowTypeError, OverflowError):
        return record_batch(columns, names=[str(index) for index in range(width)])


class MonetDBExecutionContext(default.DefaultExecutionContext):
    def create_cursor(self) -> DBAPICursor:
        return cast(DBAPICursor, MonetDBCursor(self._dbapi_connection.cursor()))

    def fire_sequence(self, seq: Sequence, type_: TypeEngine[Any]) -> int:
        name = self.identifier_preparer.format_sequence(seq)
        return self._execute_scalar(f"SELECT NEXT VALUE FOR {name}", type_)
