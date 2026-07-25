from typing import Any

from sqlalchemy.testing import exclusions
from sqlalchemy.testing.requirements import SuiteRequirements


class Requirements(SuiteRequirements):
    """Capability declarations for SQLAlchemy's dialect compliance suite."""

    @property
    def returning(self) -> Any:
        return exclusions.open()

    @property
    def insert_returning(self) -> Any:
        return exclusions.open()

    @property
    def update_returning(self) -> Any:
        return exclusions.open()

    @property
    def delete_returning(self) -> Any:
        return exclusions.open()

    @property
    def insert_executemany_returning(self) -> Any:
        # Served by insertmanyvalues, which rewrites the INSERT into multiple
        # VALUES clauses rather than using executemany.
        return exclusions.open()

    @property
    def sequences(self) -> Any:
        return exclusions.open()

    @property
    def sequences_optional(self) -> Any:
        return exclusions.open()

    @property
    def autoincrement_insert(self) -> Any:
        return exclusions.open()

    @property
    def temp_table_reflection(self) -> Any:
        return exclusions.open()

    @property
    def temp_table_names(self) -> Any:
        return exclusions.open()

    @property
    def temporary_views(self) -> Any:
        # MonetDB has no temporary views.
        return exclusions.closed()

    @property
    def temporary_tables(self) -> Any:
        return exclusions.open()

    @property
    def views(self) -> Any:
        return exclusions.open()

    @property
    def schemas(self) -> Any:
        return exclusions.open()

    @property
    def comment_reflection(self) -> Any:
        return exclusions.open()

    @property
    def index_reflection(self) -> Any:
        return exclusions.open()

    @property
    def reflects_pk_names(self) -> Any:
        return exclusions.open()

    @property
    def unique_index_reflect_as_unique_constraints(self) -> Any:
        # MonetDB has no CREATE UNIQUE INDEX, so a unique Index is compiled to a
        # UNIQUE constraint and is then reported as one.
        return exclusions.open()

    @property
    def unique_constraints_reflect_as_index(self) -> Any:
        # MonetDB backs a UNIQUE constraint with an index of the same name, so
        # get_indexes() reports it as well.
        return exclusions.open()

    @property
    def foreign_keys_reflect_as_index(self) -> Any:
        # MonetDB also indexes foreign keys, but get_indexes() filters those out
        # because they are reported through get_foreign_keys().
        return exclusions.closed()

    @property
    def reflect_indexes_with_ascdesc_as_expression(self) -> Any:
        # MonetDB indexes carry no ordering, so none is reflected back.
        return exclusions.closed()

    @property
    def unique_constraint_reflection(self) -> Any:
        return exclusions.open()

    @property
    def check_constraint_reflection(self) -> Any:
        return exclusions.open()

    @property
    def primary_key_constraint_reflection(self) -> Any:
        return exclusions.open()

    @property
    def foreign_key_constraint_reflection(self) -> Any:
        return exclusions.open()

    @property
    def datetime_timezone(self) -> Any:
        return exclusions.open()

    @property
    def time_timezone(self) -> Any:
        return exclusions.open()

    @property
    def datetime_microseconds(self) -> Any:
        return exclusions.open()

    @property
    def time_microseconds(self) -> Any:
        return exclusions.open()

    @property
    def precision_numerics_enotation_large(self) -> Any:
        return exclusions.open()

    @property
    def precision_numerics_many_significant_digits(self) -> Any:
        return exclusions.open()

    @property
    def infinity_floats(self) -> Any:
        # MonetDB rejects infinity in DOUBLE columns.
        return exclusions.closed()

    @property
    def json_type(self) -> Any:
        return exclusions.open()

    @property
    def uuid_data_type(self) -> Any:
        return exclusions.open()

    @property
    def binary_literals(self) -> Any:
        return exclusions.open()

    @property
    def regexp_match(self) -> Any:
        # MonetDB has no regular-expression match operator. Its "~" is
        # mbr_contains, a geometry operator, so emitting it would be wrong
        # rather than merely unsupported.
        return exclusions.closed()

    @property
    def regexp_replace(self) -> Any:
        return exclusions.open()

    @property
    def is_distinct_from(self) -> Any:
        return exclusions.open()

    @property
    def ctes(self) -> Any:
        return exclusions.open()

    @property
    def ctes_with_update_delete(self) -> Any:
        return exclusions.open()

    @property
    def ctes_with_values(self) -> Any:
        return exclusions.open()

    @property
    def ctes_on_dml(self) -> Any:
        # MonetDB's WITH accepts only SELECT or VALUES, not INSERT/UPDATE/DELETE.
        return exclusions.closed()

    @property
    def update_from(self) -> Any:
        return exclusions.open()

    @property
    def window_functions(self) -> Any:
        return exclusions.open()

    @property
    def sane_rowcount(self) -> Any:
        return exclusions.open()

    @property
    def sane_multi_rowcount(self) -> Any:
        return exclusions.open()

    @property
    def implicit_default_schema(self) -> Any:
        return exclusions.open()

    @property
    def default_values(self) -> Any:
        # MonetDB has no INSERT ... DEFAULT VALUES.
        return exclusions.closed()

    @property
    def empty_inserts(self) -> Any:
        return exclusions.closed()
