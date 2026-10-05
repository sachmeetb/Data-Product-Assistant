"""Pure-function guard for the column-name standardizer's domain prefix.

Regression guard for the bug where a multi-word `Project.domain` (e.g.
"retail banking") leaked its internal space into `:Column.recommendedName`
("retail banking_loan_id"). That space then propagated verbatim into
`:DProdColumn.name`/`.uri` and the `:REFERENCES` edge, breaking view-DDL JOINs
(space-form join keys vs `_safe_name`'d SELECT aliases) and the marketplace
Preview column matcher.

The fix tokenises the domain before prefixing so a multi-word domain
snake_cases cleanly. This imports the skill script directly since it lives
outside the `workbench.backend` package.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from workbench.backend.config import SKILLS_DIR


def _load_standardizer():
    path = Path(SKILLS_DIR) / "column-name-standardizer" / "scripts" / "standardize_names.py"
    spec = importlib.util.spec_from_file_location("stdz", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_multi_word_domain_snake_cases_cleanly():
    m = _load_standardizer()
    # The bug: "retail banking" leaked a space -> "retail banking_loan_id".
    assert m.standardize("loan_id", "retail banking") == "retail_banking_loan_id"


def test_abbreviation_expansion_on_column_tokens_still_runs():
    m = _load_standardizer()
    # cust -> customer on the column side; domain tokens pass through untouched.
    assert m.standardize("cust_id", "retail banking") == "retail_banking_customer_id"


def test_single_word_domain_unchanged():
    m = _load_standardizer()
    assert m.standardize("loan_id", "customer") == "customer_loan_id"


def test_idempotent_when_column_already_leads_with_domain_tokens():
    m = _load_standardizer()
    # Re-run on an already-standardized name must not double-prefix.
    assert m.standardize("retail_banking_loan_id", "retail banking") == "retail_banking_loan_id"


def test_empty_domain_adds_no_prefix():
    m = _load_standardizer()
    assert m.standardize("loan_id", None) == "loan_id"
