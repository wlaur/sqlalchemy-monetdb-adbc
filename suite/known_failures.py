"""Compliance-suite tests MonetDB cannot pass, with the reason for each.

These are marked xfail rather than skipped so that the suite still runs them: if
MonetDB or the driver gains the missing behaviour, the test reports XPASS and the
entry here is removed. Nothing in this file is a defect in the dialect. Anything
that turned out to be one was fixed instead.

Each key is matched against ``<ClassName>::<test name>``, with the dialect and
server-version suffix the SQLAlchemy plugin appends to class names removed. A key
without a parameter bracket matches every parametrisation of that test.
"""

from typing import Final

KNOWN_FAILURES: Final[dict[str, str]] = {
    # MonetDB cannot infer a type for an expression built only from parameters.
    # "SELECT ? = ?" is rejected outright with "Cannot have a parameter (?) on
    # both sides of an expression".
    "IntegerTest::test_huge_int_auto_accommodation": ("MonetDB rejects an expression with a parameter on both sides"),
    # SQLAlchemy renders true division as "? / CAST(? AS DECIMAL(18, 3))",
    # casting one side to promote the result. MonetDB guesses tinyint for the
    # untyped left parameter and then reads the division as interval
    # arithmetic: "types tinyint(4,0) and sec_interval(13,0) are not equal".
    "TrueDivTest::test_truediv_integer_bound": (
        "MonetDB mistypes an untyped parameter divided by a decimal as an interval"
    ),
    # MonetDB has no lastrowid; the dialect uses INSERT ... RETURNING instead,
    # which is why every other insert test passes.
    "LastrowidTest::test_last_inserted_id": "MonetDB has no lastrowid",
    # MonetDB rejects '%' in an identifier: "Invalid identifier '%percent'".
    "DifficultParametersTest::test_round_trip_same_named_column[%percent]": ("MonetDB rejects '%' in an identifier"),
    # MonetDB parses and re-serialises JSON on the way in, so it stores
    # '{"key1":"data1"}' for the '{"key1": "data1"}' that was sent. The test
    # asserts the custom deserializer receives the original text byte for byte.
    "JSONTest::test_round_trip_custom_json": "MonetDB normalises JSON text on storage",
    # Dropping a table normally drops the sequence backing its AUTO_INCREMENT
    # column, and get_sequence_names already excludes sequences a column still
    # references. Some suite tables nonetheless leave a "seq_<id>" behind that
    # MonetDB then refuses to drop ("unable to drop sequence"), with a dangling
    # sys.dependencies row and no owning column. Reporting them is correct: they
    # really are sequences with no owner.
    "HasSequenceTestEmpty::test_get_sequence_names_no_sequence": (
        "MonetDB leaves undroppable orphan sequences behind in the catalog"
    ),
}
