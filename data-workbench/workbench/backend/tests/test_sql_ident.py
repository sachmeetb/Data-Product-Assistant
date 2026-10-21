"""Tests for the sql_ident identifier helpers, focused on the Snowflake
UPPER-fold read path (``quote_created_relation`` / ``fold_created_ident`` /
``upper_folds``).

The motivating bug: a source-aligned product served to Snowflake via
``transfer_then_transform`` (dlt loads rows with UNQUOTED DDL → objects fold to
UPPER) was read back with a lowercase-double-quoted relation (``"party"``), a
case-sensitive-exact miss against the physical ``PARTY`` → opaque ``sql_error``.
``quote_created_relation`` folds each segment to the platform's natural unquoted
case before quoting so the read addresses the real object.
"""
from __future__ import annotations

import pytest

from workbench.backend.sql_ident import (
    quote_relation, quote_created_relation, fold_created_ident, upper_folds,
)

# Byte-identity is the load-bearing safety property for non-upper-folding
# platforms: quote_created_relation must equal quote_relation there so Postgres/
# MySQL/Databricks reads are unchanged.
_NON_FOLD = ["postgres", "postgresql", "mysql", "databricks", "bigquery", "duckdb", "ansi", ""]


class TestUpperFolds:
    def test_only_snowflake_upper_folds(self):
        assert upper_folds("snowflake") is True
        assert upper_folds("Snowflake") is True  # case-insensitive on the platform id
        for p in _NON_FOLD:
            assert upper_folds(p) is False


class TestFoldCreatedIdent:
    def test_snowflake_uppercases(self):
        assert fold_created_ident("party", "snowflake") == "PARTY"
        assert fold_created_ident("PARTY", "snowflake") == "PARTY"  # idempotent

    def test_identity_elsewhere(self):
        for p in _NON_FOLD:
            assert fold_created_ident("party", p) == "party"


class TestQuoteCreatedRelation:
    def test_snowflake_folds_and_double_quotes(self):
        # The exact failing shape from the bug: DB + schema resolve (already
        # UPPER) but the table was lowercase-quoted. Folding fixes the table.
        assert quote_created_relation("DWB_SERVING_DB.PUBLIC", "party", "snowflake") == \
            '"DWB_SERVING_DB"."PUBLIC"."PARTY"'
        assert quote_created_relation("public", "party", "snowflake") == '"PUBLIC"."PARTY"'
        assert quote_created_relation(None, "party", "snowflake") == '"PARTY"'

    @pytest.mark.parametrize("platform", _NON_FOLD)
    def test_byte_identical_to_quote_relation_on_non_folding(self, platform):
        for schema, name in [("public", "party"), ("cat.sch", "Mixed_Case"),
                             (None, "solo"), ("workspace.default", "vw_x")]:
            assert quote_created_relation(schema, name, platform) == \
                quote_relation(schema, name, platform)

    def test_reserved_name_stays_quoted_in_upper(self):
        # Quoting-in-UPPER (not bare SQL) keeps a sanitized-but-reserved name safe.
        assert quote_created_relation("public", "order", "snowflake") == '"PUBLIC"."ORDER"'
