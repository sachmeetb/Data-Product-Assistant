"""Anti-regression guard for the three-valued productKind vocabulary.

Adding `aggregate` as a third `productKind` re-terms the whole product model.
History shows a core-model change without a disciplined guard leaves the UI /
agents half-legacy. These CONCRETE (non-fuzzy) checks fail if a future edit
silently reintroduces the two-value world — a two-value `KindFilter`, a
`ProductKindChip` that can't render aggregate, an upstream-selection surface that
implies source-only, or a backend allow-list that drops aggregate.

Deliberately structural (exact literals / enum membership), not prose-scanning,
so it never false-fails on a legitimate `source-aligned` LEAF reference.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
FRONTEND = REPO / "workbench" / "frontend" / "src"


# ── Backend: the canonical allow-set + resolver ─────────────────────────────


def test_allowed_upstream_kinds_is_three_valued():
    from workbench.backend._contract_versioning import ALLOWED_UPSTREAM_PRODUCT_KINDS
    assert set(ALLOWED_UPSTREAM_PRODUCT_KINDS) == {"source", "aggregate", "consumer"}


def test_resolve_product_kind_accepts_aggregate():
    from workbench.backend.routers.odcs import _resolve_product_kind
    assert _resolve_product_kind({"productKind": "aggregate"}, "dpe-cf") == "aggregate"


def test_match_inputs_docstring_mentions_all_three_kinds():
    # The upstream-selection entry point must not imply source-only.
    from workbench.backend.routers.ingest_products import match_inputs
    doc = (match_inputs.__doc__ or "").lower()
    assert "aggregate" in doc and "consumer" in doc, (
        "match_inputs docstring still implies source-only upstream selection"
    )


def test_aggregate_recommends_materialized_default():
    from workbench.backend.routers.serving_strategy import _heuristic_serving_strategy
    rec = _heuristic_serving_strategy(
        "dpe-cf", [{"name": "d", "scd_policy": "", "grouping": False}],
        cross_platform=False, product_kind="aggregate",
    )
    assert rec["recommended_mode"] == "materialized"


# ── Frontend: the enumerating surfaces ──────────────────────────────────────


def test_product_kind_chip_renders_all_three():
    # The kind → style map is the single source of truth for chip color/label.
    # It lives in its own module (productKindStyles.ts) so non-chip surfaces
    # (e.g. the product-lineage graph nodes) can import it without tripping
    # react-refresh; ProductKindChip.tsx re-exports/imports it.
    styles = (FRONTEND / "components" / "productKindStyles.ts").read_text()
    for kind in ("source", "aggregate", "consumer"):
        assert f'"{kind}"' in styles or f"{kind}:" in styles, (
            f"productKindStyles.ts no longer handles kind '{kind}'"
        )


def test_marketplace_kind_filter_includes_aggregate():
    page = (FRONTEND / "pages" / "MarketplacePage.tsx").read_text()
    # The KindFilter union + the useState initializer must both carry aggregate.
    assert 'type KindFilter = "all" | "source" | "aggregate" | "consumer"' in page, (
        "MarketplacePage KindFilter union regressed to two values"
    )


def test_source_input_candidate_carries_product_kind():
    # The wizard picker candidate must thread product_kind so the chip renders.
    wiz = (FRONTEND / "pages" / "product" / "NewProductWizard.tsx").read_text()
    assert "product_kind" in wiz, "NewProductWizard dropped product_kind threading"
