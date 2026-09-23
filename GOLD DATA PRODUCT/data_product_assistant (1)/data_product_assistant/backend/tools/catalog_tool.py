"""
catalog_tool.py — Data catalog access for the Discovery Agent.

utility_catalog.json contains only the data_catalog section:
  - Mock BigQuery-style metadata across gold / silver / bronze Medallion layers
  - Foundry IQ semantic index
  - Scoring reference
  - Test scenarios with expected output and confidence scores

Discovery tools called directly by discovery.py:
  match_requirements_to_catalog
  extract_search_targets
  search_catalog_layer
  search_semantic_layer
  describe_table
  score_candidate
  assign_status
  compute_missing_information
  check_close_calls
  detect_conflicts
  build_architecture_diagram_spec
  get_closest_matches
  get_discovery_schema
  search_catalog
"""

import json
import random
import re
from pathlib import Path
from typing import Annotated

try:
    from semantic_kernel.functions import kernel_function
except ImportError:
    def kernel_function(**kwargs):
        return lambda f: f


_CATALOG_PATH = Path(__file__).parent.parent / "data" / "utility_catalog.json"
_DISCOVERY_SKILL_PATH = (
    Path(__file__).parent.parent / "prompts" / "DPI" / "discovery" / "SKILL.md"
)


def _load_sample_catalogue() -> dict:
    """
    Parse the ```sample_data ... ``` fenced JSON block out of the Discovery
    Agent SKILL.md. This is the single source of truth for the sample values
    the agent injects into its discovery report.

    Shape returned:
      {
        "<full_table_name>": {
          "_count_range": [min, max],    # optional
          "<column>": [<values>, ...],
          ...
        },
        ...
      }

    Returns an empty dict if SKILL.md is missing or the block can't be parsed
    so the rest of the pipeline still works (just without sample values).
    """
    try:
        text = _DISCOVERY_SKILL_PATH.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return {}

    m = re.search(r"```sample_data\s*\n(.*?)\n```", text, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}


_SAMPLE_CATALOGUE = _load_sample_catalogue()

_STOP_WORDS = {
    "total", "number", "of", "by", "per", "average", "avg", "count",
    "sum", "rate", "the", "a", "an", "and", "or", "for", "to", "in",
    "is", "are", "with", "from", "as", "at", "on",
}

_FRESHNESS_RANK = {
    "real-time": 0,
    "hourly": 0,
    "daily": 1,
    "weekly": 2,
    "monthly": 3,
    "quarterly": 4,
    "yearly": 5,
}

_TECHNICAL_NAME_PATTERNS = (
    "_ingestion",
    "etl_load",
    "_source_file",
    "_load_timestamp",
    "_loaded_at",
    "_extracted_at",
)

_GENERIC_TOKENS = {
    "id", "code", "key", "num", "number", "date", "dt", "ts",
    "amount", "amt", "value", "val", "name", "type", "status", "flag",
    "ind", "indicator", "field", "column", "row", "record",
}

_DATE_GRAIN_TERMS = {
    "daily", "day", "date"
}

_DATE_GRAIN_COLUMN_TOKENS = {
    "timestamp", "date", "time", "created", "event"
}

_COUNT_SOURCE_ALIASES = {
    "impression": {"impression_id"},
    "impressions": {"impression_id"},
    "click": {"click_id"},
    "clicks": {"click_id"},
    "view": {"view_id"},
    "views": {"view_id"},
    "visit": {"visit_id"},
    "visits": {"visit_id"},
}

_COUNT_KPI_TOKENS = {
    "impression", "impressions", "click", "clicks",
    "view", "views", "visit", "visits",
    "order", "orders", "purchase", "purchases",
}

def _fallback_sample_for_column(column_name: str) -> str:
    col = (column_name or "").lower()

    if col == "impressions" or "impression_count" in col:
        return "12,450"
    if col == "clicks" or "click_count" in col:
        return "1,286"
    if col == "ctr" or "click_through" in col:
        return "10.3%"
    if "impression_id" in col:
        return "IMP-900001"
    if "click_id" in col:
        return "CLK-700001"
    if "campaign_id" in col:
        return "CMP-1001"
    if "campaign_name" in col:
        return "Spring Promo 2026"
    if "device_id" in col:
        return "DEV-1042"
    if "timestamp" in col:
        return "2026-05-18 09:30:00"
    if col == "date" or col.endswith("_date"):
        return "2026-05-18"

    return "sample_001"

def _pick_sample(table_full_name: str, column_name: str) -> str:
    """
    Draw one sample value from the SKILL.md sample catalogue.
    If no sample is catalogued, return a deterministic fallback sample.
    """
    tbl = _SAMPLE_CATALOGUE.get(table_full_name, {})
    samples = tbl.get(column_name) or []

    if samples:
        return str(random.choice(samples))

    return _fallback_sample_for_column(column_name)


def _pick_count(table_full_name: str) -> int:
    """Pick a synthetic count for count-source KPIs from this table's _count_range."""
    tbl = _SAMPLE_CATALOGUE.get(table_full_name, {})
    rng = tbl.get("_count_range") or [120, 5400]
    try:
        return random.randint(int(rng[0]), int(rng[1]))
    except (TypeError, ValueError, IndexError):
        return random.randint(120, 5400)


def _format_count_kpi_sample(kpi_name: str, table_full_name: str) -> str:
    return f"Daily {kpi_name.lower()} count, e.g. {_pick_count(table_full_name):,}"


def _tokenise(text: str) -> set[str]:
    """
    Split a name or phrase into lowercased tokens.
    Handles snake_case, SCREAMING_SNAKE, camelCase, and free text.
    """
    if not text:
        return set()
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(text))
    parts = re.split(r"[\W_]+", spaced)
    return {p.lower() for p in parts if p}


def _singularise(token: str) -> str:
    """Tiny plural normalizer for matching count-source metrics."""
    token = token.lower()
    if token.endswith("ies") and len(token) > 3:
        return token[:-3] + "y"
    if token.endswith("ses") and len(token) > 3:
        return token[:-2]
    if token.endswith("s") and len(token) > 3:
        return token[:-1]
    return token


def _is_technical_column(col_name: str, tags: dict) -> bool:
    """
    A column is technical if it carries a technical tag or matches known
    pipeline-metadata naming patterns.
    """
    if tags and str(tags.get("technical", "")).lower() in ("true", "1", "yes"):
        return True

    name_lower = (col_name or "").lower()
    if name_lower.startswith("_"):
        return True

    return any(pattern in name_lower for pattern in _TECHNICAL_NAME_PATTERNS)


def _column_matches_term(
    col_name: str,
    col_desc: str,
    col_tags: dict,
    term_tokens: set[str],
) -> bool:
    """
    Token-set candidacy check.

    A column matches a requirement term if:
      1. It is not technical metadata, and
      2. Its name shares at least one non-generic token with the term, or
      3. Its name is purely generic and its description shares a non-generic token.
    """
    if not term_tokens:
        return False

    if _is_technical_column(col_name, col_tags):
        return False

    name_tokens = _tokenise(col_name)

    if name_tokens & term_tokens:
        return True

    name_is_purely_generic = bool(name_tokens) and name_tokens.issubset(_GENERIC_TOKENS)
    if name_is_purely_generic and col_desc:
        desc_tokens = _tokenise(col_desc)
        if desc_tokens & term_tokens:
            return True

    return False


def _column_matches_date_grain(
    requested_dimension: str,
    col_name: str,
    col_desc: str,
    col_tags: dict,
    col_data_type: str = "",
) -> bool:
    """
    Match requested date grains such as daily/date/day to timestamp-like columns.

    Examples:
      daily -> timestamp
      daily -> event_timestamp
      date  -> event_date
      day   -> created_at
    """
    if _is_technical_column(col_name, col_tags):
        return False

    requested_tokens = _tokenise(requested_dimension)
    if not (requested_tokens & _DATE_GRAIN_TERMS):
        return False

    name_tokens = _tokenise(col_name)
    desc_tokens = _tokenise(col_desc)
    dtype = (col_data_type or "").upper()

    is_date_type = dtype == "DATE" or "TIMESTAMP" in dtype or dtype == "DATETIME"

    if is_date_type and (name_tokens & _DATE_GRAIN_COLUMN_TOKENS):
        return True

    if name_tokens & _DATE_GRAIN_COLUMN_TOKENS:
        return True

    if desc_tokens & {"timestamp", "date", "occurred", "event"}:
        return True

    return False


def _column_matches_count_source(
    kpi_name: str,
    col_name: str,
    col_desc: str,
    col_tags: dict,
) -> bool:
    """
    Match count-style KPI names only to event identifier columns.

    Examples:
      impressions -> impression_id
      clicks      -> click_id
    """
    if _is_technical_column(col_name, col_tags):
        return False

    kpi_tokens = _tokenise(kpi_name)
    normalized_kpi_tokens = set(kpi_tokens)
    normalized_kpi_tokens.update(_singularise(t) for t in kpi_tokens)

    col_name_lower = (col_name or "").lower()
    col_tokens = _tokenise(col_name)

    for token in normalized_kpi_tokens:
        alias_cols = _COUNT_SOURCE_ALIASES.get(token, set())

        if col_name_lower in alias_cols:
            return True

        if token in col_tokens and "id" in col_tokens:
            return True

    return False

def _is_dimension_like_term(term: str) -> bool:
    """
    Terms such as campaign_id, campaign_name, region, date, and daily are
    usually grouping dimensions, not competing KPI close-call targets.
    """
    term_lower = (term or "").lower().strip()
    tokens = _tokenise(term_lower)

    if not term_lower:
        return True

    if term_lower in {"daily", "date", "day", "month", "monthly", "week", "weekly"}:
        return True

    if term_lower.endswith("_id") or term_lower.endswith("_name"):
        return True

    if tokens & {"id", "name", "date", "day", "region", "country", "campaign"} and len(tokens) <= 2:
        return True

    return False

# ── Column-match candidacy helpers (added for matcher correctness fix) ────────
#
# The previous implementation matched requirement terms to columns using
# substring containment over keyword lists. That produced false positives
# like Invoice ID → user_id (both contain "id") and tenure → etl_load_timestamp
# (pipeline metadata leaking into business matches).
#
# These helpers replace substring matching with token-set candidacy plus
# explicit exclusion of pipeline/technical columns.

# Columns that are pipeline metadata, not business data. Never match these
# to a requirement term. The catalog also tags these with
# tags={"technical": "true"} — we honour that AND match by name pattern.
_TECHNICAL_NAME_PATTERNS = (
    "_ingestion",
    "etl_load",
    "_source_file",
    "_load_timestamp",
    "_loaded_at",
    "_extracted_at",
)

# Tokens that are too generic to anchor a match. A column is only a candidate
# if it shares at least one NON-generic token with the requirement term.
# `id` alone is the classic false-positive driver: invoice_id, vendor_id,
# user_id, customer_id all share `id` but mean different things.
_GENERIC_TOKENS = {
    "id", "code", "key", "num", "number", "date", "dt", "ts",
    "amount", "amt", "value", "val", "name", "type", "status", "flag",
    "ind", "indicator", "field", "column", "row", "record",
}


def _tokenise(text: str) -> set:
    """
    Split a name or phrase into lowercased tokens. Handles snake_case,
    SCREAMING_SNAKE, camelCase, and free text. Drops empties.
    """
    if not text:
        return set()
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    parts = re.split(r"[\W_]+", spaced)
    return {p.lower() for p in parts if p}


def _is_technical_column(col_name: str, tags: dict) -> bool:
    """
    A column is technical (and therefore never a business match) if either:
      - It carries the 'technical' tag set to truthy, OR
      - Its name matches a known pipeline-metadata pattern.
    """
    if tags and str(tags.get("technical", "")).lower() in ("true", "1", "yes"):
        return True
    name_lower = (col_name or "").lower()
    if name_lower.startswith("_"):
        return True
    return any(p in name_lower for p in _TECHNICAL_NAME_PATTERNS)


def _column_matches_term(
    col_name: str,
    col_desc: str,
    col_tags: dict,
    term_tokens: set,
) -> bool:
    """
    Token-set candidacy check. A column is a candidate for a requirement
    term iff:
      1. The column is not technical/pipeline metadata, AND
      2. EITHER the column name shares at least one NON-generic token with
         the term (the strong path),
         OR the column name is composed ENTIRELY of generic tokens (e.g.
         a column literally named `amount`, `value`, `id`) AND the column
         description shares a non-generic token with the term.

    The second branch handles legitimate aliases like `amount` mapping
    to `invoice_amount` when the column description says "Invoice line
    item amount...". It does NOT fire for specific column names like
    `user_id` or `tenure_months` — those carry their own meaning and
    should not borrow context from their descriptions.

    `term_tokens` must be pre-stripped of generic tokens by the caller.
    """
    if not term_tokens:
        return False
    if _is_technical_column(col_name, col_tags):
        return False

    name_tokens = _tokenise(col_name)

    # Strong path: name shares a non-generic token with the term.
    if name_tokens & term_tokens:
        return True

    # Description fallback — only when the column NAME is purely generic.
    # A column named `amount` with description "Invoice line item amount..."
    # legitimately matches an "invoice_amount" requirement. A column named
    # `user_id` with description "User identifier" does NOT match a
    # "Vendor ID" requirement just because both descriptions mention
    # "identifier" somewhere.
    name_is_purely_generic = bool(name_tokens) and name_tokens.issubset(_GENERIC_TOKENS)
    if name_is_purely_generic and col_desc:
        desc_tokens = _tokenise(col_desc)
        if desc_tokens & term_tokens:
            return True

    return False

# ── Column-match candidacy helpers (added for matcher correctness fix) ────────
#
# The previous implementation matched requirement terms to columns using
# substring containment over keyword lists. That produced false positives
# like Invoice ID → user_id (both contain "id") and tenure → etl_load_timestamp
# (pipeline metadata leaking into business matches).
#
# These helpers replace substring matching with token-set candidacy plus
# explicit exclusion of pipeline/technical columns.

# Columns that are pipeline metadata, not business data. Never match these
# to a requirement term. The catalog also tags these with
# tags={"technical": "true"} — we honour that AND match by name pattern.
_TECHNICAL_NAME_PATTERNS = (
    "_ingestion",
    "etl_load",
    "_source_file",
    "_load_timestamp",
    "_loaded_at",
    "_extracted_at",
)

# Tokens that are too generic to anchor a match. A column is only a candidate
# if it shares at least one NON-generic token with the requirement term.
# `id` alone is the classic false-positive driver: invoice_id, vendor_id,
# user_id, customer_id all share `id` but mean different things.
_GENERIC_TOKENS = {
    "id", "code", "key", "num", "number", "date", "dt", "ts",
    "amount", "amt", "value", "val", "name", "type", "status", "flag",
    "ind", "indicator", "field", "column", "row", "record",
}


def _tokenise(text: str) -> set:
    """
    Split a name or phrase into lowercased tokens. Handles snake_case,
    SCREAMING_SNAKE, camelCase, and free text. Drops empties.
    """
    if not text:
        return set()
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    parts = re.split(r"[\W_]+", spaced)
    return {p.lower() for p in parts if p}


def _is_technical_column(col_name: str, tags: dict) -> bool:
    """
    A column is technical (and therefore never a business match) if either:
      - It carries the 'technical' tag set to truthy, OR
      - Its name matches a known pipeline-metadata pattern.
    """
    if tags and str(tags.get("technical", "")).lower() in ("true", "1", "yes"):
        return True
    name_lower = (col_name or "").lower()
    if name_lower.startswith("_"):
        return True
    return any(p in name_lower for p in _TECHNICAL_NAME_PATTERNS)


def _column_matches_term(
    col_name: str,
    col_desc: str,
    col_tags: dict,
    term_tokens: set,
) -> bool:
    """
    Token-set candidacy check. A column is a candidate for a requirement
    term iff:
      1. The column is not technical/pipeline metadata, AND
      2. EITHER the column name shares at least one NON-generic token with
         the term (the strong path),
         OR the column name is composed ENTIRELY of generic tokens (e.g.
         a column literally named `amount`, `value`, `id`) AND the column
         description shares a non-generic token with the term.

    The second branch handles legitimate aliases like `amount` mapping
    to `invoice_amount` when the column description says "Invoice line
    item amount...". It does NOT fire for specific column names like
    `user_id` or `tenure_months` — those carry their own meaning and
    should not borrow context from their descriptions.

    `term_tokens` must be pre-stripped of generic tokens by the caller.
    """
    if not term_tokens:
        return False
    if _is_technical_column(col_name, col_tags):
        return False

    name_tokens = _tokenise(col_name)

    # Strong path: name shares a non-generic token with the term.
    if name_tokens & term_tokens:
        return True

    # Description fallback — only when the column NAME is purely generic.
    # A column named `amount` with description "Invoice line item amount..."
    # legitimately matches an "invoice_amount" requirement. A column named
    # `user_id` with description "User identifier" does NOT match a
    # "Vendor ID" requirement just because both descriptions mention
    # "identifier" somewhere.
    name_is_purely_generic = bool(name_tokens) and name_tokens.issubset(_GENERIC_TOKENS)
    if name_is_purely_generic and col_desc:
        desc_tokens = _tokenise(col_desc)
        if desc_tokens & term_tokens:
            return True

    return False


_DATE_GRAIN_TOKENS = {"date", "day", "daily", "week", "weekly", "month", "monthly", "year", "yearly", "hour", "time"}


def _compose_recommended_action(
    status: str,
    full_name: str,
    layer_name: str,
    kpi_matches: list,
    dim_matches: list,
) -> str:
    """
    Build a context-aware, human-readable rationale for a single catalog match,
    suitable for the discovery verdict banner. Deterministic — derived entirely
    from the match's own coverage signals (matched columns, partial KPIs,
    date-grain dimensions, layer, and information gaps).

    Returns a *standalone action phrase* with no leading table name, so callers
    can render it as `<table> — <action>` (verdict banner) or `` `<table>` —
    <action>`` (markdown) without duplicating the name. The cross-layer "build
    from Bronze source X" wording for build_new verdicts is composed later in
    discovery.py, which can see all three layers at once.
    """
    if status == "reuse":
        return "fully covers the required data points; no changes needed."

    if status == "build_new":
        return (
            f"no suitable existing {layer_name.capitalize()} asset matches the "
            f"requirement; build a new asset"
        )

    # ── extend ────────────────────────────────────────────────────────────────
    clauses: list[str] = []

    # Silver/Bronze assets that satisfy a Gold-style requirement must be promoted.
    if layer_name in ("silver", "bronze"):
        clauses.append(f"promote to Gold from {layer_name.capitalize()}")

    # Date/time grain dimensions that map to a timestamp column need aggregation.
    for d in dim_matches:
        if d.get("coverage") != "matched":
            continue
        col = (d.get("matched_column") or "").lower()
        dim_name = (d.get("dimension") or "").lower()
        is_date_grain = (
            any(t in dim_name for t in _DATE_GRAIN_TOKENS)
            or any(t in col for t in ("date", "time", "timestamp"))
        )
        if is_date_grain and ("time" in col or "timestamp" in col):
            grain = d.get("dimension") or "date"
            clauses.append(f"add {grain} aggregation to support {grain} grouping")
            break

    # KPIs present but only partially covered need their coverage completed.
    partial_kpis = [k.get("kpi") for k in kpi_matches if k.get("coverage") == "partial"]
    if partial_kpis:
        kpi_list = ", ".join(f"`{k}`" for k in partial_kpis if k)
        if kpi_list:
            clauses.append(f"complete coverage for {kpi_list}")

    # KPIs the table lacks entirely → propose adding them.
    missing_kpis = [
        k.get("kpi") for k in kpi_matches
        if k.get("coverage") in ("none", "description_only")
    ]
    if missing_kpis:
        kpi_list = ", ".join(f"`{k}`" for k in missing_kpis if k)
        if kpi_list:
            clauses.append(f"add {kpi_list}")

    if not clauses:
        clauses.append("add additional columns or transforms to fully satisfy the requirement")

    return "; ".join(clauses) + "."


class CatalogPlugin:
    """
    Provides structured access to the data catalog.
    Loaded once at instantiation.
    """

    def __init__(self):
        with open(_CATALOG_PATH, encoding="utf-8") as f:
            self._catalog: dict = json.load(f)

    @kernel_function(
        name="search_catalog",
        description=(
            "Full-text search across all tables and columns in the data catalog."
        ),
    )
    def search_catalog(
        self,
        query: Annotated[str, "Search term"],
        layer_filter: Annotated[str, "Optional layer: gold, silver, or bronze"] = "",
    ) -> str:
        query_lower = query.lower().strip()
        tables = self._get_catalog_tables(layer_filter if layer_filter else None)

        results = []
        for tbl in tables:
            col_names = [c["name"] for c in tbl.get("columns", [])]
            col_descs = [c.get("description", "") for c in tbl.get("columns", [])]
            text = " ".join([
                tbl.get("full_name", ""),
                tbl.get("description", ""),
                " ".join(col_names),
                " ".join(col_descs),
            ]).lower()

            if query_lower in text:
                matching_cols = [
                    c["name"]
                    for c in tbl.get("columns", [])
                    if query_lower in (c["name"] + " " + c.get("description", "")).lower()
                ]
                results.append({
                    "table": tbl["full_name"],
                    "layer": tbl.get("layer", ""),
                    "description": tbl.get("description", ""),
                    "matching_columns": matching_cols,
                })

        return json.dumps(results, indent=2, ensure_ascii=False)

    @kernel_function(
        name="get_discovery_schema",
        description="Returns the Data Discovery Agent scoring thresholds and formula.",
    )
    def get_discovery_schema(
        self,
        use_case_type: Annotated[str, "Use case type"] = "analytics",
    ) -> str:
        dc = self._catalog.get("data_catalog", {})
        scoring_ref = dc.get("scoring_reference", {})

        return json.dumps({
            "use_case_type": use_case_type,
            "scoring_formula": scoring_ref,
            "layer_search_priority": "gold -> silver -> bronze",
            "catalogs_searched": ["bq_project.gold", "bq_project.silver", "bq_project.bronze"],
            "total_tables_in_catalog": sum(len(v) for v in dc.get("layers", {}).values()),
        }, indent=2, ensure_ascii=False)

    @kernel_function(
        name="get_closest_matches",
        description="Fallback top-N candidate search by keyword overlap.",
    )
    def get_closest_matches(
        self,
        search_terms_json: Annotated[str, "JSON array of search terms"],
        top_n: Annotated[int, "Maximum number of candidates per layer"] = 3,
    ) -> str:
        try:
            terms = json.loads(search_terms_json)
        except Exception:
            terms = [search_terms_json]

        terms_lower = [str(t).lower() for t in terms]
        candidates = self._get_catalog_tables()
        scored = []

        for tbl in candidates:
            col_names = [c["name"] for c in tbl.get("columns", [])]
            hit_terms = []
            text = " ".join([
                tbl.get("full_name", ""),
                tbl.get("description", ""),
                " ".join(col_names),
            ]).lower()

            for term in terms_lower:
                term_words = re.split(r"\W+", term)
                if any(w and w in text for w in term_words):
                    hit_terms.append(term)

            if hit_terms:
                layer = tbl.get("layer", "bronze")
                scored.append({
                    "name": tbl["full_name"],
                    "description": tbl.get("description", ""),
                    "layer": layer,
                    "matched_fields": col_names,
                    "confidence": tbl.get("expected_confidence", {}).get("overall_confidence", 0.5),
                    "similarity_score": round(len(hit_terms) / max(len(terms_lower), 1), 2),
                    "matched_terms": hit_terms,
                    "status": "extend",
                    "note": "Closest match from data catalog.",
                })

        scored.sort(key=lambda x: x["similarity_score"], reverse=True)

        by_layer: dict = {}
        for item in scored:
            layer = item.get("layer", "unknown")
            by_layer.setdefault(layer, [])
            if len(by_layer[layer]) < top_n:
                by_layer[layer].append(item)

        return json.dumps(by_layer, indent=2, ensure_ascii=False)

    @kernel_function(
        name="extract_search_targets",
        description="Parse requirements into KPI, dimension, and data-source targets.",
    )
    def extract_search_targets(
        self,
        requirements_json: Annotated[str, "JSON string of the requirements block"],
    ) -> str:
        try:
            reqs = json.loads(requirements_json)
        except Exception:
            return json.dumps({"error": "Invalid JSON", "targets": [], "keywords": {}})

        targets = []
        keywords: dict = {}

        for kpi in reqs.get("kpis", reqs.get("final_kpi_list", [])):
            name = kpi.get("kpi_name") or kpi.get("kpi") or kpi.get("name", "")
            if not name:
                continue

            kws = self._extract_keywords(name)
            if kpi.get("description"):
                kws += self._extract_keywords(kpi["description"])

            targets.append({
                "name": name,
                "type": "kpi",
                "description": kpi.get("description", ""),
            })
            keywords[name] = list(dict.fromkeys(kws))

        for dim in reqs.get("granularity", reqs.get("granularity_level_required", [])):
            d = dim.get("dimension", "")
            if not d:
                continue

            targets.append({"name": d, "type": "dimension"})
            keywords[d] = self._extract_keywords(d)

        for src in reqs.get("data_sources", reqs.get("data_types", [])):
            s = src.get("data_source") or src.get("data_type") or src.get("source_name", "")
            if not s:
                continue

            targets.append({
                "name": s,
                "type": "data_source",
                "notes": src.get("notes", ""),
            })
            keywords[s] = self._extract_keywords(s)

        return json.dumps({"targets": targets, "keywords": keywords}, indent=2, ensure_ascii=False)

    @kernel_function(
        name="search_catalog_layer",
        description="Search structured catalog metadata for one Medallion layer.",
    )
    def search_catalog_layer(
        self,
        layer: Annotated[str, "Medallion layer: gold, silver, or bronze"],
        keywords_json: Annotated[str, "JSON dict mapping target names to keyword lists"],
        active_fields_json: Annotated[str, "JSON array of active field names"],
    ) -> str:
        try:
            keywords: dict = json.loads(keywords_json)
        except Exception:
            keywords = {}

        try:
            active_fields: list = json.loads(active_fields_json)
        except Exception:
            active_fields = []

        tables = self._get_catalog_tables(layer.lower())
        all_kws = [kw.lower() for kws in keywords.values() for kw in kws]

        results = []
        for tbl in tables:
            col_names = [c["name"] for c in tbl.get("columns", [])]
            col_descs = " ".join(c.get("description", "") for c in tbl.get("columns", []))
            text = " ".join([
                tbl.get("full_name", ""),
                tbl.get("description", ""),
                " ".join(col_names),
                col_descs,
            ]).lower()

            hit_count = sum(1 for kw in all_kws if kw and kw in text)
            if hit_count == 0:
                continue

            matched_active = [
                af
                for af in active_fields
                if any(kw in text for kw in self._extract_keywords(af))
            ]

            results.append({
                "full_name": tbl["full_name"],
                "description": tbl.get("description", ""),
                "matched_fields": col_names,
                "active_fields_matched": matched_active,
                "tags": tbl.get("tags", {"layer": layer.lower()}),
                "refresh_cadence": tbl.get("refresh_cadence", "weekly"),
                "confidence": tbl.get("expected_confidence", {}).get("overall_confidence", 0.5),
                "hit_score": round(hit_count / max(len(all_kws), 1), 3),
                "layer": layer.lower(),
            })

        results.sort(key=lambda x: x["hit_score"], reverse=True)
        return json.dumps(results[:6], indent=2, ensure_ascii=False)

    @kernel_function(
        name="search_semantic_layer",
        description="Search Foundry IQ / semantic proxy for one Medallion layer.",
    )
    def search_semantic_layer(
        self,
        layer: Annotated[str, "Medallion layer: gold, silver, or bronze"],
        keywords_json: Annotated[str, "JSON dict mapping target names to keyword lists"],
        active_fields_json: Annotated[str, "JSON array of active field names"],
    ) -> str:
        try:
            keywords: dict = json.loads(keywords_json)
        except Exception:
            keywords = {}

        try:
            active_fields: list = json.loads(active_fields_json)
        except Exception:
            active_fields = []

        tables = self._get_catalog_tables(layer.lower())
        all_kws = [kw.lower() for kws in keywords.values() for kw in kws]

        fiq_index = self._catalog.get("data_catalog", {}).get("foundry_iq_index", [])

        fiq_scores: dict = {}
        fiq_aliases: dict = {}

        for entry in fiq_index:
            tbl = entry.get("candidate_table", "")
            if entry.get("included") and tbl:
                fiq_scores[tbl] = max(fiq_scores.get(tbl, 0.0), entry.get("embedding_similarity", 0.0))
            if tbl and entry.get("alias_mappings"):
                fiq_aliases.setdefault(tbl, []).extend(entry["alias_mappings"])

        results = []
        for tbl in tables:
            col_names = [c["name"] for c in tbl.get("columns", [])]
            col_descs = " ".join(c.get("description", "") for c in tbl.get("columns", []))
            full_name = tbl["full_name"]

            text_tokens = re.split(r"\W+", " ".join([
                full_name,
                tbl.get("description", ""),
                " ".join(col_names),
                col_descs,
            ]).lower())
            text_tokens = [t for t in text_tokens if t]

            hit_kws = [
                kw
                for kw in all_kws
                if kw and any(kw in tok or tok in kw for tok in text_tokens)
            ]

            if not hit_kws:
                continue

            if full_name in fiq_scores:
                semantic_score = round(fiq_scores[full_name], 3)
            else:
                semantic_score = round(min(len(hit_kws) / max(len(all_kws), 1) * 1.15, 1.0), 3)

            col_names_lo = [c.lower() for c in col_names]
            alias_mappings = {}

            for af in active_fields:
                af_kws = self._extract_keywords(af)
                for fn in col_names_lo:
                    if any(kw in fn or fn in kw for kw in af_kws if kw):
                        alias_mappings[af] = fn
                        break

            if full_name in fiq_aliases:
                alias_mappings["_foundry_iq"] = fiq_aliases[full_name]

            results.append({
                "full_name": full_name,
                "description": tbl.get("description", ""),
                "matched_fields": col_names,
                "semantic_score": semantic_score,
                "alias_mappings": alias_mappings,
                "layer": layer.lower(),
            })

        results.sort(key=lambda x: x["semantic_score"], reverse=True)
        return json.dumps(results[:6], indent=2, ensure_ascii=False)

    @kernel_function(
        name="describe_table",
        description="Retrieve full metadata for a specific table.",
    )
    def describe_table(
        self,
        full_table_name: Annotated[str, "Fully qualified table name"],
    ) -> str:
        name_lower = full_table_name.lower().strip()
        all_tables = self._get_catalog_tables()

        for tbl in all_tables:
            if tbl.get("full_name", "").lower() == name_lower:
                columns = [
                    {
                        "column_name": c["name"],
                        "data_type": c.get("data_type", "STRING"),
                        "nullable": c.get("nullable", True),
                        "description": c.get("description", ""),
                        "is_primary_key": c.get("is_pk", False),
                        "fk_ref": c.get("fk_ref"),
                        "tags": c.get("tags", {}),
                    }
                    for c in tbl.get("columns", [])
                ]

                return json.dumps({
                    "full_name": tbl["full_name"],
                    "description": tbl.get("description", ""),
                    "layer": tbl.get("layer", ""),
                    "format": tbl.get("format", "DELTA"),
                    "columns": columns,
                    "tags": tbl.get("tags", {}),
                    "owner": tbl.get("owner", ""),
                    "refresh_cadence": tbl.get("refresh_cadence", ""),
                    "last_updated": tbl.get("last_updated", ""),
                    "lineage": {
                        "upstream": tbl.get("upstream_lineage", []),
                        "downstream": [],
                    },
                    "expected_confidence": tbl.get("expected_confidence", {}),
                }, indent=2, ensure_ascii=False)

        return json.dumps({
            "full_name": full_table_name,
            "error": "Table not found in data catalog.",
            "available_tables": [t["full_name"] for t in all_tables],
        }, indent=2, ensure_ascii=False)

    @kernel_function(
        name="score_candidate",
        description="Compute the six-dimension composite score for a candidate table.",
    )
    def score_candidate(
        self,
        candidate_json: Annotated[str, "JSON candidate table"],
        requirements_json: Annotated[str, "JSON requirements block"],
        active_fields_json: Annotated[str, "JSON array of active field names"],
    ) -> str:
        try:
            candidate = json.loads(candidate_json)
            requirements = json.loads(requirements_json)
            active_fields = json.loads(active_fields_json)
        except Exception as e:
            return json.dumps({"error": f"Invalid JSON input: {e}"})

        cand_fields_raw = candidate.get("matched_fields", [])
        cand_field_names = [
            (f.get("field", "") if isinstance(f, dict) else str(f)).lower()
            for f in cand_fields_raw
        ]

        cand_text = " ".join([
            candidate.get("full_name", candidate.get("name", "")),
            candidate.get("description", ""),
            " ".join(cand_field_names),
        ]).lower()

        if active_fields:
            per_field = []
            for af in active_fields:
                af_lower = af.lower()
                af_kws = self._extract_keywords(af)

                if af_lower in cand_field_names:
                    per_field.append(1.0)
                elif any(kw and (kw in cf or cf in kw) for kw in af_kws for cf in cand_field_names):
                    per_field.append(0.7)
                elif any(kw and kw in cand_text for kw in af_kws):
                    per_field.append(0.4)
                else:
                    per_field.append(0.0)

            field_overlap = sum(per_field) / len(per_field)
        else:
            field_overlap = candidate.get("confidence", 0.5)

        req_sources = []
        for src in requirements.get("data_sources", requirements.get("data_types", [])):
            s = src.get("data_source") or src.get("data_type") or src.get("source_name") or ""
            if s:
                req_sources.append(s.lower())

        source_compat = 0.5
        if req_sources:
            name_lower = candidate.get("full_name", candidate.get("name", "")).lower()
            exact_hits = sum(
                1
                for src in req_sources
                if any(kw in name_lower for kw in self._extract_keywords(src))
            )

            if exact_hits == len(req_sources):
                source_compat = 1.0
            elif exact_hits > 0:
                source_compat = 0.7
            else:
                source_compat = 0.3

        req_dims = [
            dim.get("dimension", "").lower()
            for dim in requirements.get("granularity", [])
            if dim.get("dimension")
        ]

        if req_dims:
            dim_hits = sum(
                1
                for d in req_dims
                if any(
                    kw and (kw in cf or cf in kw)
                    for kw in self._extract_keywords(d)
                    for cf in cand_field_names
                )
            )
            granularity_alignment = dim_hits / len(req_dims)
        else:
            granularity_alignment = 0.7

        semantic_similarity = float(
            candidate.get(
                "semantic_score",
                candidate.get("confidence", candidate.get("hit_score", 0.6)),
            )
        )
        semantic_similarity = min(max(semantic_similarity, 0.0), 1.0)

        grain_compat = 0.7

        req_freshness = str(requirements.get("data_freshness", "weekly")).lower()
        cand_cadence = str(candidate.get("refresh_cadence", "weekly")).lower()
        req_rank = _FRESHNESS_RANK.get(req_freshness, 2)
        cand_rank = _FRESHNESS_RANK.get(cand_cadence, 2)

        if cand_rank <= req_rank:
            freshness_alignment = 1.0
        elif cand_rank == req_rank + 1:
            freshness_alignment = 0.5
        else:
            freshness_alignment = 0.0

        composite = (
            field_overlap * 0.30 +
            source_compat * 0.15 +
            granularity_alignment * 0.15 +
            semantic_similarity * 0.15 +
            grain_compat * 0.15 +
            freshness_alignment * 0.10
        )

        return json.dumps({
            "table": candidate.get("full_name", candidate.get("name", "")),
            "scores": {
                "field_overlap": round(field_overlap, 3),
                "source_compat": round(source_compat, 3),
                "granularity_alignment": round(granularity_alignment, 3),
                "semantic_similarity": round(semantic_similarity, 3),
                "grain_compatibility": round(grain_compat, 3),
                "freshness_alignment": round(freshness_alignment, 3),
            },
            "composite_score": round(composite, 3),
        }, indent=2, ensure_ascii=False)

    @kernel_function(
        name="assign_status",
        description="Map composite score to reuse, extend, or build_new.",
    )
    def assign_status(
        self,
        composite_score: Annotated[float, "Composite score from score_candidate"],
        threshold_config_json: Annotated[str, "JSON threshold config"] = "{}",
    ) -> str:
        try:
            thresholds = json.loads(threshold_config_json)
        except Exception:
            thresholds = {}

        reuse_min = float(thresholds.get("reuse_minimum", 0.80))
        extend_min = float(thresholds.get("extend_minimum", 0.50))
        score = float(composite_score)

        if score >= reuse_min:
            return json.dumps({"status": "reuse", "color": "green"})
        if score >= extend_min:
            return json.dumps({"status": "extend", "color": "amber"})

        return json.dumps({"status": "build_new", "color": "blue"})

    @kernel_function(
        name="compute_missing_information",
        description="Compute natural-language gaps and suggested column names.",
    )
    def compute_missing_information(
        self,
        candidate_json: Annotated[str, "JSON candidate table"],
        active_fields_json: Annotated[str, "JSON array of active field names"],
        requirements_json: Annotated[str, "JSON requirements block"],
    ) -> str:
        try:
            candidate = json.loads(candidate_json)
            active_fields = json.loads(active_fields_json)
            requirements = json.loads(requirements_json)
        except Exception as e:
            return json.dumps({
                "error": f"Invalid JSON: {e}",
                "missing_information": [],
                "suggested_names": [],
            })

        cand_fields_raw = candidate.get("matched_fields", [])

        # Build a lookup from target name → KPI/dimension/source details
        target_details: dict = {}

        for kpi in requirements.get("kpis", requirements.get("final_kpi_list", [])):
            name = kpi.get("kpi_name") or kpi.get("kpi") or kpi.get("name", "")
            if name:
                target_details[name.lower()] = {
                    "type": "kpi",
                    "description": kpi.get("description", ""),
                }

        for dim in requirements.get("granularity", []):
            d = dim.get("dimension", "")
            if d:
                target_details[d.lower()] = {"type": "dimension"}

        for src in requirements.get("data_sources", requirements.get("data_types", [])):
            s = src.get("data_source") or src.get("data_type") or src.get("source_name") or ""
            if s:
                target_details[s.lower()] = {
                    "type": "data_source",
                    "notes": src.get("notes", ""),
                }

        missing_info = []
        suggested_names = []

        for af in active_fields:
            af_lower = af.lower()
            af_kws   = self._extract_keywords(af)
            af_term_tokens = {kw for kw in af_kws if kw not in _GENERIC_TOKENS}

            # Use the same candidacy check as the matcher: a field is covered
            # iff some column on the candidate qualifies as a token-set match.
            is_covered = False
            for c in cand_fields_raw:
                if isinstance(c, dict):
                    col_name = c.get("field", "") or c.get("name", "")
                    col_desc = c.get("description", "")
                    col_tags = c.get("tags", {})
                else:
                    col_name = str(c)
                    col_desc = ""
                    col_tags = {}
                if _column_matches_term(col_name, col_desc, col_tags, af_term_tokens):
                    is_covered = True
                    break
            # Fallback exact name match — keeps behaviour for callers that
            # pass a `matched_fields` list of plain column-name strings with
            # no tokens that overlap a term but where the field name is
            # literally the term itself.
            if not is_covered and af_lower in [
                (c.get("field", "") if isinstance(c, dict) else str(c)).lower()
                for c in cand_fields_raw
            ]:
                is_covered = True

            if is_covered:
                continue

            detail = target_details.get(af_lower, {})
            ftype = detail.get("type", "field")

            if ftype == "kpi":
                desc = detail.get("description", "")
                if desc:
                    missing_info.append(f"Whether {desc.rstrip('.').lower()}")
                else:
                    missing_info.append(f"The {af} metric is not captured in this table")
            elif ftype == "dimension":
                missing_info.append(f"Dimensional breakdown by {af} is not available in this table")
            elif ftype == "data_source":
                notes = detail.get("notes", "")
                if notes:
                    missing_info.append(f"Data from {af} ({notes}) is not present in this table")
                else:
                    missing_info.append(f"Data from {af} is not present in this table")
            else:
                missing_info.append(f"The {af} field or concept is not captured in this table")

            suggested = re.sub(r"\W+", "_", af.lower()).strip("_")
            suggested_names.append(suggested)

        return json.dumps({
            "missing_information": missing_info,
            "suggested_names": suggested_names,
        }, indent=2, ensure_ascii=False)

    @kernel_function(
        name="check_close_calls",
        description="Flag same-layer close-call candidates.",
    )
    def check_close_calls(
        self,
        scored_matches_json: Annotated[str, "JSON array of match dicts"],
    ) -> str:
        try:
            matches: list = json.loads(scored_matches_json)
        except Exception:
            return scored_matches_json

        if not isinstance(matches, list):
            return scored_matches_json

        def _get_score(m: dict) -> float:
            score = m.get("composite_score")
            if score is None:
                conf = m.get("match_confidence", {})
                score = conf.get("overall_confidence", 0.0) if isinstance(conf, dict) else conf
            return float(score or 0.0)

        def _matched_metric_terms(m: dict) -> set[str]:
            terms = set()
            for item in m.get("kpi_matches", []):
                term = item.get("kpi", "")
                coverage = item.get("coverage", "none")
                if coverage in {"full", "partial"} and not _is_dimension_like_term(term):
                    terms.add(term.lower())
            return terms

        for m in matches:
            m["close_call"] = False

        for i in range(len(matches)):
            for j in range(i + 1, len(matches)):
                if abs(_get_score(matches[i]) - _get_score(matches[j])) > 0.05:
                    continue

                left_terms = _matched_metric_terms(matches[i])
                right_terms = _matched_metric_terms(matches[j])

                # Only flag close calls when two candidates are competing for
                # the same business metric. Complementary tables such as
                # impressions vs clicks should not be marked as close calls.
                if left_terms and right_terms and (left_terms & right_terms):
                    matches[i]["close_call"] = True
                    matches[j]["close_call"] = True

        return json.dumps(matches, indent=2, ensure_ascii=False)

    @kernel_function(
        name="detect_conflicts",
        description="Detect genuine multi-source field conflicts.",
    )
    def detect_conflicts(
        self,
        all_matches_json: Annotated[str, "JSON dict with gold, silver, bronze arrays"],
    ) -> str:
        try:
            all_matches: dict = json.loads(all_matches_json)
        except Exception:
            return json.dumps([])

        field_index: dict = {}

        for layer_key in ("gold", "silver", "bronze"):
            for m in all_matches.get(layer_key, []):
                table_name = m.get("name", m.get("full_name", ""))
                for f in m.get("matched_fields", []):
                    field_name = (f.get("field", "") if isinstance(f, dict) else str(f)).lower()
                    if not field_name:
                        continue

                    field_index.setdefault(field_name, []).append({
                        "layer": layer_key,
                        "table": table_name,
                        "description": m.get("description", ""),
                        "status": m.get("status", ""),
                    })

        conflicts = []

        for field_name, sources in field_index.items():
            layers_seen = {s["layer"] for s in sources}
            if len(layers_seen) < 2:
                continue

            bronze_sources = [s for s in sources if s["layer"] == "bronze"]
            if len(bronze_sources) >= 2:
                catalogs = {
                    s["table"].split(".")[1] if "." in s["table"] else ""
                    for s in bronze_sources
                }
                if len(catalogs) >= 2:
                    conflicts.append({
                        "requested_item": field_name,
                        "conflicting_sources": sources,
                        "reason": (
                            f"Field '{field_name}' matched from "
                            f"{', '.join(sorted(catalogs))} source systems with no confirmed "
                            "entity-resolution mapping. Human review required to determine golden source."
                        ),
                    })

        return json.dumps(conflicts, indent=2, ensure_ascii=False)

    @kernel_function(
        name="build_architecture_diagram_spec",
        description="Generate a cross-layer medallion architecture diagram spec.",
    )
    def build_architecture_diagram_spec(
        self,
        discovery_result_json: Annotated[str, "Complete DiscoveryResult JSON"],
    ) -> str:
        try:
            result: dict = json.loads(discovery_result_json)
        except Exception:
            return json.dumps({"error": "Invalid DiscoveryResult JSON"})

        status_colors = {
            "reuse": "green",
            "extend": "amber",
            "build_new": "blue",
        }

        lanes = []

        for layer_key, lane_label in [
            ("gold_matches", "Gold - Consumption Layer"),
            ("silver_matches", "Silver - Aggregated Layer"),
            ("bronze_matches", "Bronze - Source Layer"),
        ]:
            cards = []

            for m in result.get(layer_key, []):
                conf = m.get("match_confidence", {})
                score = conf.get("overall_confidence", conf) if isinstance(conf, dict) else conf

                cards.append({
                    "id": m.get("name", ""),
                    "label": m.get("name", "").split(".")[-1],
                    "full_name": m.get("name", ""),
                    "status": m.get("status", "build_new"),
                    "color": status_colors.get(m.get("status", ""), "blue"),
                    "confidence": round(float(score or 0.0), 2),
                    "close_call": m.get("close_call", False),
                    "missing_count": len(m.get("missing_information", [])),
                    "style": "dashed" if m.get("status") == "build_new" else "solid",
                })

            lanes.append({
                "layer": layer_key.replace("_matches", ""),
                "label": lane_label,
                "cards": cards,
            })

        lineage_arrows = []

        bronze_names = [m.get("name", "") for m in result.get("bronze_matches", [])]
        silver_names = [m.get("name", "") for m in result.get("silver_matches", [])]
        gold_names = [m.get("name", "") for m in result.get("gold_matches", [])]

        for b in bronze_names:
            b_tokens = set(re.split(r"\W+", b.lower()))

            for s in silver_names:
                s_tokens = set(re.split(r"\W+", s.lower()))
                if len(b_tokens & s_tokens) >= 2:
                    lineage_arrows.append({"from": b, "to": s, "direction": "bronze->silver"})

            for g in gold_names:
                g_tokens = set(re.split(r"\W+", g.lower()))
                if len(b_tokens & g_tokens) >= 2:
                    lineage_arrows.append({"from": b, "to": g, "direction": "bronze->gold"})

        for s in silver_names:
            s_tokens = set(re.split(r"\W+", s.lower()))

            for g in gold_names:
                g_tokens = set(re.split(r"\W+", g.lower()))
                if len(s_tokens & g_tokens) >= 2:
                    lineage_arrows.append({"from": s, "to": g, "direction": "silver->gold"})

        conflict_markers = [
            {
                "field": c.get("requested_item", ""),
                "layers": [s["layer"] for s in c.get("conflicting_sources", [])],
            }
            for c in result.get("conflicts", [])
        ]

        summary = result.get("summary", {})

        return json.dumps({
            "diagram_type": "cross_layer_medallion",
            "lanes": lanes,
            "lineage_arrows": lineage_arrows,
            "conflict_markers": conflict_markers,
            "legend": {
                "green": "reuse - use existing asset as-is",
                "amber": "extend - existing asset needs additional columns or transforms",
                "blue": "build_new - no suitable existing asset; must be built from scratch",
                "dashed": "build_new outline",
            },
            "summary": {
                "total_matches": summary.get("total_matches", 0),
                "reuse_count": summary.get("reuse_count", 0),
                "extend_count": summary.get("extend_count", 0),
                "build_new_count": summary.get("build_new_count", 0),
                "close_calls_flagged": summary.get("close_calls_flagged", 0),
                "conflicts_detected": len(conflict_markers),
            },
        }, indent=2, ensure_ascii=False)

    @kernel_function(
        name="match_requirements_to_catalog",
        description="Primary discovery tool returning gold/silver/bronze matches.",
    )
    def match_requirements_to_catalog(
        self,
        combined_json: Annotated[str, "Combined JSON from requirement and classification outputs"],
    ) -> str:
        try:
            combined: dict = json.loads(combined_json)
        except Exception:
            return json.dumps({"error": "Invalid JSON", "gold": [], "silver": [], "bronze": []})

        reqs = combined.get("requirements", combined)

        kpis = []

        # Prefer legacy kpis if the server created them.
        for kpi in reqs.get("kpis", reqs.get("final_kpi_list", [])):
            name = kpi.get("kpi_name") or kpi.get("kpi") or kpi.get("name", "")
            desc = kpi.get("description", "")
            if name:
                kpis.append({"name": name, "description": desc})

        # Defense in depth: if no legacy kpis exist but data_points does,
        # only take data_points with kind == "kpi".
        if not kpis and reqs.get("data_points"):
            for dp in reqs.get("data_points", []):
                if not isinstance(dp, dict):
                    continue
                if dp.get("kind") != "kpi":
                    continue
                name = dp.get("name", "")
                if name:
                    kpis.append({
                        "name": name,
                        "description": dp.get("description", ""),
                    })

        dims = [
            dim.get("dimension", "")
            for dim in reqs.get("granularity", reqs.get("granularity_level_required", []))
            if dim.get("dimension")
        ]

        sources = []
        for src in reqs.get("data_sources", reqs.get("data_types", [])):
            s = src.get("data_source") or src.get("data_type") or src.get("source_name") or ""
            if s:
                sources.append({"name": s, "notes": src.get("notes", "")})

        threshold_config = combined.get("threshold_config", {})
        reuse_min = float(threshold_config.get("reuse_minimum", 0.80))
        extend_min = float(threshold_config.get("extend_minimum", 0.50))

        dc = self._catalog.get("data_catalog", {})
        fiq_index = dc.get("foundry_iq_index", [])

        fiq_scores: dict = {}
        for fiq in fiq_index:
            tbl_name = fiq.get("candidate_table", "")
            if fiq.get("included") and tbl_name:
                fiq_scores[tbl_name] = max(
                    fiq_scores.get(tbl_name, 0.0),
                    fiq.get("embedding_similarity", 0.0),
                )

        results: dict = {
            "gold": [],
            "silver": [],
            "bronze": [],
        }

        for layer_name in ("gold", "silver", "bronze"):
            for tbl in dc.get("layers", {}).get(layer_name, []):
                full_name = tbl["full_name"]
                description = tbl.get("description", "")
                columns_raw = tbl.get("columns", [])
                col_names = [c["name"] for c in columns_raw]
                col_descs = {c["name"]: c.get("description", "") for c in columns_raw}

                table_text = " ".join([
                    full_name.lower(),
                    description.lower(),
                    " ".join(c.lower() for c in col_names),
                    " ".join(d.lower() for d in col_descs.values()),
                ])

                # Token-set candidacy with explicit exclusion of pipeline/
                # technical columns (etl_load_timestamp, _ingestion_*, etc.).
                # See _column_matches_term for the rules.
                #
                # IMPORTANT: candidacy uses tokens from the KPI NAME only,
                # not the KPI description. The description enriches the
                # column-side context (table description, column description),
                # but using description tokens on the requirement side leaks
                # generic concepts ('user', 'identifier', 'months') into the
                # match space and produces false positives. The KPI name is
                # the contract; the description is exposition.
                kpi_matches = []
                # Reuse-friendly index of columns by name so we can look up data_type/tags.
                cols_by_name = {c["name"]: c for c in columns_raw}


                for kpi in kpis:
                    kpi_kws = self._extract_keywords(kpi["name"])
                    kpi_term_tokens = {kw for kw in kpi_kws if kw not in _GENERIC_TOKENS}

                    related: list = []
                    match_methods: dict = {}
                    for c in columns_raw:
                        col_name = c["name"]
                        col_desc = c.get("description", "")
                        col_tags = c.get("tags", {})

                        if _column_matches_term(col_name, col_desc, col_tags, kpi_term_tokens):
                            related.append(col_name)
                            # Label how it matched, for downstream display
                            if col_name.lower() == kpi["name"].lower():
                                match_methods[col_name] = "exact"
                            elif _tokenise(col_name) & kpi_term_tokens:
                                match_methods[col_name] = "token_overlap"
                            else:
                                match_methods[col_name] = "description_alias"
                        elif _column_matches_count_source(kpi["name"], col_name, col_desc, col_tags):
                            related.append(col_name)
                            match_methods[col_name] = "count_source_alias"



                    # Description hint: the table's own description references
                    # the KPI but no individual column does. Useful signal for
                    # "this table is in the right ballpark but doesn't have
                    # the field as a column."
                    desc_hint = bool(kpi_term_tokens & _tokenise(description))

                    if not related:
                        coverage = "description_only" if desc_hint else "none"
                        if desc_hint:
                            gap = (
                                f"'{kpi['name']}' is referenced in the table "
                                f"purpose but has no direct column"
                            )
                        else:
                            gap = (
                                f"No column or description reference found for "
                                f"'{kpi['name']}' in this table"
                            )
                    elif len(related) >= 2:
                        coverage = "full"
                        gap = None
                    else:
                        coverage = "partial"
                        gap = None

                    # Dynamic sample data drawn from the SKILL.md catalogue —
                    # one fresh value per column per run. For count-source
                    # KPIs the headline value is a synthesised daily count
                    # ("Daily impressions count, e.g. 1,240"); the per-column
                    # logic + example pair feeds the per-table card.
                    kpi_is_count_source = any(
                        t in _COUNT_KPI_TOKENS for t in _tokenise(kpi["name"])
                    ) and any(
                        match_methods.get(col) == "count_source_alias"
                        or ("id" in _tokenise(col) and (_tokenise(col) & {t for t in _tokenise(kpi["name"]) if t in _COUNT_KPI_TOKENS}))
                        for col in related
                    )

                    sample_logic_entries = []
                    for col in related:
                        sample = _pick_sample(full_name, col)
                        col_tokens = _tokenise(col)
                        is_count_col = (
                            kpi_is_count_source
                            and "id" in col_tokens
                            and (match_methods.get(col) == "count_source_alias"
                                 or col_tokens & {t for t in _tokenise(kpi["name"]) if t in _COUNT_KPI_TOKENS})
                        )
                        sample_logic_entries.append({
                            "column":       col,
                            "logic":        f"COUNT({col})" if is_count_col else col,
                            "sample_value": sample,
                            "example":      f"{col} = {sample}" if sample else col,
                        })

                    if kpi_is_count_source:
                        sample_data_point = _format_count_kpi_sample(kpi["name"], full_name)
                    elif sample_logic_entries:
                        sample_data_point = next(
                            (e["sample_value"] for e in sample_logic_entries if e["sample_value"]),
                            "",
                        )
                    else:
                        sample_data_point = ""

                    kpi_matches.append({
                        "kpi": kpi["name"],
                        "matched_columns": related,
                        "coverage": coverage,
                        "gap": gap,
                        "match_methods": match_methods,
                        "sample_data_point": sample_data_point,
                        "sample_logic": sample_logic_entries,
                    })

                dim_matches = []

                for dim in dims:
                    dim_kws = self._extract_keywords(dim)
                    dim_term_tokens = {kw for kw in dim_kws if kw not in _GENERIC_TOKENS}
                    # Full token set including generic tokens — used to pick the
                    # closer column when several token-match (e.g. dim
                    # "campaign name" picks `campaign_name` over `campaign_id`
                    # because both share 'campaign' but only `campaign_name`
                    # also shares 'name').
                    dim_full_tokens = set(dim_kws)

                    candidates = []
                    for idx, c in enumerate(columns_raw):
                        col_name = c["name"]
                        col_desc = c.get("description", "")
                        col_tags = c.get("tags", {})
                        is_date_grain = _column_matches_date_grain(
                            dim, col_name, col_desc, col_tags, c.get("data_type", ""),
                        )
                        if not _column_matches_term(col_name, col_desc, col_tags, dim_term_tokens) \
                                and not is_date_grain:
                            continue
                        col_tokens = _tokenise(col_name)
                        affinity = len(col_tokens & dim_full_tokens)
                        candidates.append((affinity, idx, c))

                    matched_col = None
                    matched_col_obj = None
                    if candidates:
                        candidates.sort(key=lambda t: (-t[0], t[1]))
                        matched_col_obj = candidates[0][2]
                        matched_col = matched_col_obj["name"]

                    sample = _pick_sample(full_name, matched_col) if matched_col else ""
                    dim_matches.append({
                        "dimension":      dim,
                        "matched_column": matched_col,
                        "coverage":       "matched" if matched_col else "none",
                        "sample_value":   sample,
                        "sample_example": (f"{matched_col} = {sample}" if matched_col and sample else ""),
                    })

                # ── Source relevance ──────────────────────────────────────────
                source_matches = []

                for src in sources:
                    src_kws = self._extract_keywords(src["name"])
                    relevant = any(
                        kw and kw in table_text
                        for kw in src_kws
                        if len(kw) > 2
                    )

                    source_matches.append({
                        "source": src["name"],
                        "relevant": relevant,
                        "notes": src.get("notes", ""),
                    })

                ec = tbl.get("expected_confidence", {})

                kpi_has_match = any(k["coverage"] != "none" for k in kpi_matches)
                dim_has_match = any(d["coverage"] == "matched" for d in dim_matches)

                if ec and ec.get("overall_confidence") and (kpi_has_match or dim_has_match):
                    adjusted = float(ec["overall_confidence"])
                    structural_score = float(ec.get("structural_score", adjusted))
                    semantic_score = float(ec.get(
                        "semantic_score",
                        fiq_scores.get(full_name, adjusted),
                    ))
                else:
                    kpi_covered = sum(
                        1
                        for k in kpi_matches
                        if k["coverage"] in {"full", "partial", "description_only"}
                    )
                    dim_covered = sum(
                        1
                        for d in dim_matches
                        if d["coverage"] == "matched"
                    )
                    src_covered = sum(
                        1
                        for s in source_matches
                        if s["relevant"]
                    )

                    kpi_ratio = kpi_covered / max(len(kpis), 1)
                    dim_ratio = dim_covered / max(len(dims), 1)
                    src_ratio = src_covered / max(len(sources), 1) if sources else 0.5

                    field_overlap = kpi_ratio * 0.6 + dim_ratio * 0.4
                    source_compat = 1.0 if src_ratio >= 0.8 else (0.7 if src_ratio > 0.3 else 0.3)
                    semantic_score = fiq_scores.get(full_name, 0.5)

                    adjusted = round(
                        field_overlap * 0.30 +
                        source_compat * 0.15 +
                        dim_ratio * 0.15 +
                        semantic_score * 0.15 +
                        0.7 * 0.15 +
                        1.0 * 0.10,
                        3,
                    )
                    structural_score = round(field_overlap, 3)

                if adjusted >= reuse_min:
                    status = "reuse"
                elif adjusted >= extend_min:
                    status = "extend"
                else:
                    status = "build_new"

                matched_fields = []
                seen_cols: set = set()

                for km in kpi_matches:
                    for col in km["matched_columns"]:
                        if col in seen_cols:
                            continue

                        method = km.get("match_methods", {}).get(col)
                        if not method:
                            method = "exact" if col.lower() == km["kpi"].lower() else "semantic_alias"

                        col_obj = cols_by_name.get(col, {})
                        matched_fields.append({
                            "field": col,
                            "match_method": method,
                            "requirement_term": km["kpi"],
                            "sample_value": _pick_sample(full_name, col),
                            "data_type": col_obj.get("data_type", ""),
                        })
                        seen_cols.add(col)

                for dm in dim_matches:
                    matched_col = dm.get("matched_column")
                    if matched_col and matched_col not in seen_cols:
                        col_obj = cols_by_name.get(matched_col, {})
                        matched_fields.append({
                            "field": matched_col,
                            "match_method": dm.get("match_method") or "exact",
                            "requirement_term": dm["dimension"],
                            "sample_value": dm.get("sample_value") or _pick_sample(full_name, matched_col),
                            "data_type": col_obj.get("data_type", ""),
                        })
                        seen_cols.add(matched_col)

                if ec and ec.get("matched_fields"):
                    for mf_str in ec["matched_fields"]:
                        parts = str(mf_str).split("|")
                        raw_field = parts[0].strip()

                        if "(" in raw_field:
                            raw_field = raw_field.split("(")[0].strip()

                        if "->" in raw_field:
                            raw_field = raw_field.split("->")[-1].strip()

                        if "→" in raw_field:
                            raw_field = raw_field.split("→")[-1].strip()

                        method = parts[1].strip() if len(parts) > 1 else "exact"
                        req_term = ""
                        if len(parts) > 2:
                            req_term = parts[2].replace("requirement_term:", "").strip()

                        if raw_field and raw_field not in seen_cols:
                            col_obj = cols_by_name.get(raw_field, {})
                            matched_fields.append({
                                "field": raw_field,
                                "match_method": method,
                                "requirement_term": req_term,
                                "sample_value": _pick_sample(full_name, raw_field),
                                "data_type": col_obj.get("data_type", ""),
                            })
                            seen_cols.add(raw_field)

                missing_information = [k["gap"] for k in kpi_matches if k["gap"]]

                suggested_names = [
                    re.sub(r"\W+", "_", k["kpi"].lower()).strip("_")
                    for k in kpi_matches
                    if k["coverage"] == "none"
                ]

                results[layer_name].append({
                    "name": full_name,
                    "description": description,
                    "layer": layer_name,
                    "columns": col_names,
                    "kpi_matches": kpi_matches,
                    "dimension_matches": dim_matches,
                    "source_matches": source_matches,
                    "match_confidence": {
                        "structural_score": round(structural_score, 3),
                        "semantic_score": round(semantic_score, 3),
                        "overall_confidence": round(adjusted, 3),
                    },
                    "status": status,
                    "matched_fields": matched_fields,
                    "missing_information": missing_information,
                    "suggested_names": suggested_names,
                    "recommended_action": _compose_recommended_action(
                        status, full_name, layer_name,
                        kpi_matches, dim_matches,
                    ),
                    "close_call": False,
                })

        return json.dumps(results, indent=2, ensure_ascii=False)

    _LAYER_NORM = {
        "gold": "gold",
        "consumption": "gold",
        "silver": "silver",
        "aggregated": "silver",
        "bronze": "bronze",
        "source": "bronze",
    }

    def _get_catalog_tables(self, layer: str = None) -> list:
        """Return tables from the real data_catalog section of utility_catalog.json."""
        dc = self._catalog.get("data_catalog", {})
        layers = dc.get("layers", {})

        if layer:
            norm = self._LAYER_NORM.get(layer.lower(), layer.lower())
            return layers.get(norm, [])

        result = []
        for tbl_list in layers.values():
            result.extend(tbl_list)
        return result

    def _extract_keywords(self, text: str) -> list:
        """
        Extract meaningful keywords from a text string, stripping stop words.

        NOTE 1: Previously this also returned 3-character abbreviations of every
        word ≥6 chars (e.g. 'customer' → 'cus', 'vendor' → 'ven'). Combined
        with the substring matching downstream, those abbreviations produced
        a flood of false positives. Removed.

        NOTE 2: Previously the split regex was ``r"\\W+"`` which does NOT split
        on underscore — so 'invoice_amount' came out as a single token and
        never matched anything via token-set logic. Now we split on word
        boundaries AND underscore AND camelCase boundaries.
        """
        spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
        words = re.split(r"[\W_]+", spaced.lower())
        return [w for w in words if w and w not in _STOP_WORDS and len(w) > 2]