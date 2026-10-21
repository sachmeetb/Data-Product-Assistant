"""Ephemeral instance/value resolution for marketplace Semantic Q&A.

The semantic layer (:BusinessConcept entity→attribute→value) maps datasets and
columns but **never** individual rows. The `value` tier only covers low-cardinality
enumerations (status, country code). So when a question references a specific
RECORD by a high-cardinality value — *"where does John Doe live?"* — that literal
("John Doe") cannot be grounded against the ontology or graph at all.

This module resolves such mentions at QUERY TIME against the live deployed Postgres
views. For each value-mention the LLM extracted from the question we:

  1. rank candidate attributes/columns the mention could refer to (ontology +
     embeddings + dataType filter),
  2. fuzzy-probe the actual data in the top column(s) (CHESS-style value
     retrieval: ILIKE/pg_trgm prefilter + in-Python fuzzy ranking),
  3. either bind a VERIFIED exact literal (injected into the NL→SQL skill so it
     writes ``WHERE col = '<exact>'`` instead of guessing) or return a
     disambiguation payload — top candidate records, an attribute choice, or a
     graceful "not found".

Design constraints (load-bearing):
  * **Ephemeral.** Nothing is persisted to Neo4j except the standard ``:QueryRun``
    audit row written by ``sql_executor.execute_select``. No new graph nodes.
  * **Safe.** Every probe routes through ``sql_executor.execute_select`` (read-only
    txn, statement timeout, row cap, audit) and is scoped to the chat's
    deployed-view allow-list. Suppressed/PII columns are absent from deployed
    views, so they are never probed.
  * **Hybrid fuzzy engine.** ILIKE prefilter + ``rapidfuzz`` ranking by default
    (zero DB changes); opportunistically uses ``pg_trgm`` similarity() only when
    the extension is already enabled — never installs it.

This module is import-only-downward: ``marketplace_chat`` imports it, never the
reverse (the LLM extraction pass lives in ``marketplace_chat`` and feeds mentions
in as plain dicts), keeping the resolver pure and unit-testable without the SDK.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from . import embeddings
from . import sql_executor
from .models import AppSettings
from .neo4j_client import neo4j_session
from .sql_ident import fold_created_ident


# ── Fuzzy backend (rapidfuzz with stdlib fallback) ───────────────────────────

try:  # rapidfuzz is C-backed and handles token reordering/partial far better
    from rapidfuzz import fuzz as _rf_fuzz

    def _fuzz_score(a: str, b: str) -> float:
        return float(_rf_fuzz.WRatio(a or "", b or ""))
except Exception:  # pragma: no cover - exercised only when rapidfuzz absent
    import difflib

    def _fuzz_score(a: str, b: str) -> float:
        return difflib.SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio() * 100.0


# ── Thresholds (single source of truth) ──────────────────────────────────────

PROBE_MIN_FUZZY = 78.0          # keep probed values at/above this fuzzy score
AUTO_RESOLVE_FUZZY = 95.0       # a unique value at/above this auto-resolves (near-exact;
                                # a typo like "Sophie"→"Sophia" lands below → recommended)
RESOLVE_MARGIN = 8.0            # winner must beat the runner-up by this to auto-resolve
COLUMN_DOMINANCE_MARGIN = 6.0   # one column's best match must beat another's by this,
                                # else the value is genuinely ambiguous ACROSS columns
PROBE_COLUMN_CAP = 10           # max distinct columns probed per mention (cost bound)
PROBE_MAX_DISTINCT = 200        # cap rows pulled per probe
CANDIDATES_RETURNED = 5         # candidate records surfaced on record-ambiguity
MAX_MENTION_LEN = 128           # reject pathological mentions
MAX_TOKENS = 8                  # cap ILIKE token-AND clauses
MAX_CONTEXT_COLS = 2            # identifying columns pulled for candidate labels


# ── Dataclasses ──────────────────────────────────────────────────────────────


@dataclass
class ValueMention:
    text: str                                   # "John Doe"
    type_hint: str = ""                         # "person name" | "city" | ...
    span: Optional[tuple[int, int]] = None      # best-effort char offsets


@dataclass
class AttributeCandidate:
    attribute_name: str
    column_name: str
    column_uri: str
    view_name: str
    view_schema: str
    data_type: str
    score: float
    view: dict[str, Any] = field(default_factory=dict)  # source view (for context cols)


@dataclass
class ValueCandidate:
    value: str
    column_name: str
    view_name: str
    view_schema: str
    fuzzy_score: float
    pk_context: dict[str, Any] = field(default_factory=dict)
    field_label: str = ""   # human field name (e.g. "Full Name") for cross-column labels


@dataclass
class MentionResolution:
    mention: ValueMention
    status: str = "not_found"   # resolved | record_ambiguous | attribute_ambiguous | not_found
    column_name: str = ""
    view_name: str = ""
    view_schema: str = ""
    resolved_value: str = ""
    candidates: list[ValueCandidate] = field(default_factory=list)
    attribute_options: list[AttributeCandidate] = field(default_factory=list)


@dataclass
class ResolutionOutcome:
    grounded_values: list[dict[str, Any]] = field(default_factory=list)
    disambiguation: Optional[dict[str, Any]] = None
    trace: list[dict[str, Any]] = field(default_factory=list)
    skipped_reason: str = ""


# ── Pre-gate: does the question plausibly reference a record-level value? ─────

_ANALYTIC_RE = re.compile(
    r"\b(count|sum|avg|average|total|how many|number of|trend|over time|per\s|"
    r"by month|by year|by week|by day|distribution|breakdown|top\s+\d+|group by)\b",
    re.IGNORECASE,
)
# Trigger phrases that strongly imply a specific named record is being referenced.
_VALUE_TRIGGER_RE = re.compile(
    r"\b(named|called|name is|who is|whose|where is|where does|belongs to|"
    r"works (?:in|at|for)|lives? in|located in|assigned to)\b",
    re.IGNORECASE,
)
_QUOTED_RE = re.compile(r"""['"]([^'"]{2,})['"]""")
# Stopwords that are commonly capitalised but never a record value.
_CAP_STOPWORDS = {
    "i", "show", "list", "get", "give", "find", "what", "which", "who", "where",
    "when", "how", "the", "a", "an", "is", "are", "do", "does", "can", "please",
    "tell", "me", "all", "top", "count", "total", "and", "or", "of", "in", "for",
    "by", "to", "with", "from",
}


def _has_proper_noun(message: str) -> bool:
    """A capitalised, non-sentence-initial token that isn't a stopword — the
    classic proper-noun (record-value) signal."""
    tokens = re.findall(r"[A-Za-z][A-Za-z'.\-]*", message or "")
    for i, tok in enumerate(tokens):
        if i == 0:
            continue
        if len(tok) >= 2 and tok[0].isupper() and tok.lower() not in _CAP_STOPWORDS:
            return True
    return False


def should_resolve_values(user_message: str) -> bool:
    """Cheap, LLM-free pre-gate (used in `full` mode, where no decompose pass
    runs). Returns True when the message plausibly references a specific record
    by value — a quoted span, a proper noun, or a trigger phrase. Conservative
    toward False only for clearly analytic questions with no value signal, so
    aggregate questions ("orders by month") never pay for extraction/probing."""
    msg = user_message or ""
    if _QUOTED_RE.search(msg) or _VALUE_TRIGGER_RE.search(msg) or _has_proper_noun(msg):
        return True
    return False  # nothing that looks like a record value


# ── SQL literal / ILIKE pattern escaping (the #1 injection surface) ───────────


def _sanitize_mention(text: str) -> str:
    """Strip control chars (incl. NUL, which Postgres rejects), collapse
    whitespace, cap length. Run before any literal is built."""
    cleaned = "".join(ch for ch in (text or "") if ch == "\t" or ch >= " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:MAX_MENTION_LEN]


def _tokenize_mention(text: str) -> list[str]:
    """Whitespace tokens that contain at least one alphanumeric char, capped."""
    toks = [t for t in (text or "").split() if any(c.isalnum() for c in t)]
    return toks[:MAX_TOKENS]


def _pg_text_literal(s: str) -> str:
    """Return a safe single-quoted Postgres string literal. Assumes
    ``standard_conforming_strings = on`` (the modern default), where the ONLY
    metacharacter inside a regular '...' literal is the single quote — backslash
    is literal. So we escape ``'`` → ``''`` and rely on _sanitize_mention having
    already removed control chars."""
    return "'" + (s or "").replace("'", "''") + "'"


def _ilike_pattern_literal(token: str) -> str:
    """Build a ``%token%`` ILIKE/LIKE pattern literal with LIKE metacharacters
    (``!`` ``%`` ``_``) escaped, for use with ``ESCAPE '!'``.

    Uses ``!`` as the escape character because backslash is the string-escape
    character inside SQL literals on MySQL/Snowflake/Databricks — writing
    ``ESCAPE '\\\\'`` produces an unterminated-string error on those platforms.
    ``!`` is universally safe (not special in any SQL dialect's LIKE syntax)."""
    esc = token.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return _pg_text_literal(f"%{esc}%")


def _fuzzy_prefix(token: str) -> str:
    """A typo-tolerant prefix of a token: drop the last ~2 chars (capped to ≥3)
    so a trailing-character typo still prefilters in — "Sophie"/"Sophia" both
    share "Soph". The Python rapidfuzz pass restores precision afterward. (Internal
    typos like "Jon"→"John" need pg_trgm, used automatically when available.)"""
    t = token or ""
    if len(t) <= 3:
        return t
    return t[:max(3, len(t) - 2)]


def _quote_ident(name: str, platform: str = "") -> str:
    """Quote an identifier: backtick for MySQL/Databricks, double-quote otherwise.
    Identifiers come from information_schema (deployed views), not user input,
    but quote defensively.

    On an upper-folding platform (Snowflake) the identifier is folded to UPPER
    before quoting so it addresses the object created by unquoted DDL — the
    relation name / schema arrive logical-cased from the graph and would
    otherwise be a case-sensitive miss against the UPPER physical object. A
    no-op for column names the provider already returned UPPER, and for every
    non-upper-folding platform."""
    folded = fold_created_ident(name or "", platform)
    if (platform or "").lower() in ("mysql", "databricks"):
        return "`" + folded.replace("`", "``") + "`"
    return '"' + folded.replace('"', '""') + '"'


def _exec_platform(inputs: Any) -> str:
    """The served platform the value probes run against (default postgres)."""
    return (getattr(inputs, "platform_type", "postgres") or "postgres").lower()


def _exec_conn(inputs: Any) -> tuple[str, dict, str]:
    """Resolve the value-probe execution connection from the STRUCTURED
    ``inputs.connection_ref`` — returns ``(platform, connection_ref, source_dsn)``.

    ``source_dsn`` is the transient Postgres DSN derived only here (empty for
    non-Postgres, which ride ``connection_ref`` into the executor). Mirrors the
    single connection contract used by marketplace_chat / qa_execute."""
    platform = _exec_platform(inputs)
    connection_ref = getattr(inputs, "connection_ref", {}) or {}
    source_dsn = ""
    if platform in ("postgres", "postgresql"):
        from .routers.connections import build_connection_string
        source_dsn = build_connection_string("postgres", connection_ref)
    return platform, connection_ref, source_dsn


# ── pg_trgm detection (cached per connection) ────────────────────────────────

_pg_trgm_cache: dict[str, bool] = {}


def _has_pg_trgm(settings: AppSettings, inputs: Any) -> bool:
    """Best-effort: is the pg_trgm extension already installed on this DB? Cached
    per connection. Never installs it. Any failure → False (ILIKE fallback).

    Capability-gated: pg_trgm is a Postgres extension, so a non-Postgres served
    platform short-circuits to False (rapidfuzz-only) without a doomed probe —
    the natural cross-engine divergence the plan sanctions."""
    platform, connection_ref, source_dsn = _exec_conn(inputs)
    if platform not in ("postgres", "postgresql"):
        return False
    key = source_dsn or ""
    if key in _pg_trgm_cache:
        return _pg_trgm_cache[key]
    available = False
    try:
        with neo4j_session(
            settings.neo4j_host, settings.neo4j_port,
            settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
        ) as ns:
            res = sql_executor.execute_select(
                neo4j_session=ns, project_code=f"marketplace:{getattr(inputs, 'domain', '')}",
                pg_connection=source_dsn,
                sql="SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'",
                executed_by="value-probe:trgm-detect", max_rows=1,
                platform=platform, connection_ref=connection_ref or None,
            )
        available = res.status == "ok" and res.row_count > 0
    except Exception:
        available = False
    _pg_trgm_cache[key] = available
    return available


# ── Attribute/column ranking ─────────────────────────────────────────────────

_TEXT_TYPE_HINTS = ("char", "text", "citext", "name")
_NUM_TYPE_HINTS = ("int", "numeric", "decimal", "real", "double", "money", "float")


def _is_text_type(dt: str) -> bool:
    return any(h in (dt or "").lower() for h in _TEXT_TYPE_HINTS)


def _is_numeric_type(dt: str) -> bool:
    return any(h in (dt or "").lower() for h in _NUM_TYPE_HINTS)


def _mention_is_numeric(m: ValueMention) -> bool:
    if (m.type_hint or "").lower() in {"id", "number", "amount", "quantity", "code"}:
        return True
    txt = (m.text or "").replace(",", "").replace(".", "").replace("-", "").strip()
    return bool(txt) and txt.isdigit()


def _type_compatible(m: ValueMention, dt: str) -> bool:
    """Keep columns whose type matches the mention's nature. Unknown/empty type
    is allowed (don't over-filter)."""
    if not dt:
        return True
    if _mention_is_numeric(m):
        return _is_numeric_type(dt)
    # textual mention: keep text columns; reject pure numeric/date/bool
    return _is_text_type(dt)


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(t) > 1}


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _humanize(col: str) -> str:
    return (col or "").replace("_", " ").strip()


# Column-name tokens that mark a high-cardinality human-recognisable value — the
# kind a question references by literal ("John Doe", "Acme", "Seattle"). Used to
# bias which columns get probed when the type_hint is weak/absent (a name is
# rarely modelled as a :BusinessConcept, so semantic ranking alone misses it).
_IDENT_TOKENS = {
    "name", "title", "label", "email", "code", "city", "company", "customer",
    "employee", "manager", "vendor", "supplier", "region", "department", "dept",
    "country", "state", "street", "address", "product", "account", "org",
}


def _identifier_bonus(column_name: str) -> float:
    return 0.35 if (_tokens(column_name) & _IDENT_TOKENS) else 0.0


def _lexical_attr_score(type_hint: str, attr_name: str, col_human: str, defn: str) -> float:
    """Token overlap between the mention's TYPE (type_hint) and the column's
    semantic text (concept attr name + humanised column name + definition). We
    score the TYPE, not the literal value — "person name" should match a
    "Full Name"/"employee_name" column, never let the value 'Sophia' match
    'Gender'."""
    q = _tokens(type_hint)
    t = _tokens(f"{attr_name} {col_human} {defn}")
    if not q or not t:
        return 0.0
    return len(q & t) / len(q)


def _column_concept_index(concepts: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """``{column_name_lower: {attr_name, attr_defn, column_uri}}`` — the concept
    attribute (if any) a deployed column is bound to, for labels + ranking. A
    column need NOT be concept-bound to be a candidate."""
    out: dict[str, dict[str, str]] = {}
    for entity in concepts or []:
        for attr in entity.get("attributes") or []:
            for rep in attr.get("represented_by_columns") or []:
                cn = (rep.get("column_name") or "").lower()
                if cn and cn not in out:
                    out[cn] = {"attr_name": attr.get("name") or "",
                               "attr_defn": attr.get("definition") or "",
                               "column_uri": rep.get("column_uri") or ""}
    return out


def rank_attribute_candidates(
    mention: ValueMention, concepts: list[dict[str, Any]],
    deployed_views: list[dict[str, Any]], *, k: int = PROBE_COLUMN_CAP,
) -> list[AttributeCandidate]:
    """Choose which columns to PROBE for a value-mention (the data decides the
    answer, not this ranking — see ``_resolve_one``).

    Candidate universe is EVERY type-compatible deployed column — concept-bound or
    not, because a high-cardinality identifier (a person name) is rarely modelled
    as a :BusinessConcept. Each column is scored by the mention's TYPE against the
    column/attribute name (+ an identifier-shape bonus so name/email/title columns
    get probed even when the type_hint is weak). Deduped by column name; top-k by
    score (no hard floor — the probe eliminates non-matches)."""
    col_concept = _column_concept_index(concepts)
    type_hint = mention.type_hint or ""

    # All type-compatible deployed columns, deduped by column name (the same
    # physical column repeats across a product's views — probe it once, via the
    # richest view).
    by_col: dict[str, dict[str, Any]] = {}
    for v in deployed_views or []:
        vs, vn = v.get("view_schema", ""), v.get("view_name", "")
        ncols = len(v.get("columns") or [])
        for c in v.get("columns") or []:
            cn = c.get("name") or ""
            if not cn or not _type_compatible(mention, c.get("data_type", "")):
                continue
            key = cn.lower()
            prev = by_col.get(key)
            if prev is None or ncols > prev["ncols"]:  # prefer the richest view
                meta = col_concept.get(key, {})
                by_col[key] = {
                    "column_name": cn, "view_name": vn, "view_schema": vs,
                    "data_type": c.get("data_type", ""), "view": v, "ncols": ncols,
                    "attr_name": meta.get("attr_name", ""), "attr_defn": meta.get("attr_defn", ""),
                    "column_uri": meta.get("column_uri", ""),
                }
    pending = list(by_col.values())
    if not pending:
        return []

    # Embedding similarity of the mention TYPE vs each column's semantic text.
    # Best-effort and type_hint-only (never embed the raw value — 'Sophia' near
    # 'Gender' is exactly the trap we avoid). Lexical + identifier bonus carry
    # when embeddings/type_hint are unavailable.
    emb_scores: list[float] = [0.0] * len(pending)
    if embeddings.available() and type_hint.strip():
        try:
            qvec = embeddings.embed_query(type_hint)
            docs = [f"{p['attr_name']} {_humanize(p['column_name'])}. {p['attr_defn']}".strip(". ")
                    for p in pending]
            dvecs = embeddings.embed_documents(docs)
            if qvec and dvecs and len(dvecs) == len(pending):
                emb_scores = [_cosine(qvec, dv) for dv in dvecs]
        except Exception:
            pass

    cands: list[AttributeCandidate] = []
    for i, p in enumerate(pending):
        lex = _lexical_attr_score(type_hint, p["attr_name"], _humanize(p["column_name"]), p["attr_defn"])
        score = max(lex, emb_scores[i]) + _identifier_bonus(p["column_name"])
        cands.append(AttributeCandidate(
            attribute_name=p["attr_name"] or _humanize(p["column_name"]),
            column_name=p["column_name"], column_uri=p["column_uri"],
            view_name=p["view_name"], view_schema=p["view_schema"],
            data_type=p["data_type"], score=round(min(score, 1.0), 4), view=p["view"],
        ))
    ranked = sorted(cands, key=lambda x: x.score, reverse=True)
    return ranked[:k]


# ── Fuzzy data probe ─────────────────────────────────────────────────────────


def _context_columns(candidate: AttributeCandidate) -> list[str]:
    """Up to MAX_CONTEXT_COLS identifying columns from the candidate's view
    (other than the match column) for human-readable candidate labels. Prefer
    short text columns whose name looks identifying."""
    match_col = candidate.column_name.lower()
    cols = candidate.view.get("columns") or []
    scored: list[tuple[int, str]] = []
    for c in cols:
        cn = c.get("name") or ""
        if not cn or cn.lower() == match_col:
            continue
        nl = cn.lower()
        rank = 0
        if any(tok in nl for tok in ("id", "name", "code", "email", "dept", "department",
                                     "city", "title", "role", "team", "country", "region")):
            rank += 2
        if _is_text_type(c.get("data_type", "")):
            rank += 1
        if rank:
            scored.append((rank, cn))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [cn for _r, cn in scored[:MAX_CONTEXT_COLS]]


def probe_column_for_value(
    settings: AppSettings, inputs: Any, candidate: AttributeCandidate, mention_text: str, *,
    max_distinct: int = PROBE_MAX_DISTINCT, min_fuzzy: float = PROBE_MIN_FUZZY,
    _platform: str = "",
) -> list[ValueCandidate]:
    """Fuzzy-probe one candidate column for the mention against live data.

    Hybrid: pg_trgm similarity() when the extension is present, else a token-AND
    ILIKE prefilter (LIKE on MySQL, which is case-insensitive by default);
    both ranked in Python with rapidfuzz. Routes through
    ``sql_executor.execute_select`` (read-only, timeout, row cap, :QueryRun audit)
    and is scoped to the deployed-view allow-list."""
    # Allow-list guard FIRST — never probe a non-deployed pair.
    pair = (candidate.view_schema, candidate.view_name)
    if pair not in set(inputs.allowed_pairs or []):
        return []

    # Engine-correct identifier quoting: default the platform from the resolved
    # inputs so a MySQL/Snowflake/Databricks served view is backtick/ANSI-quoted
    # correctly even when the caller didn't pass _platform explicitly.
    if not _platform:
        _platform = _exec_platform(inputs)

    clean = _sanitize_mention(mention_text)
    tokens = _tokenize_mention(clean)
    if not clean or not tokens:
        return []

    col = _quote_ident(candidate.column_name, _platform)
    rel = f'{_quote_ident(candidate.view_schema, _platform)}.{_quote_ident(candidate.view_name, _platform)}'
    ctx_cols = _context_columns(candidate)
    select_ctx = "".join(f", {_quote_ident(c, _platform)}" for c in ctx_cols)

    use_trgm = _has_pg_trgm(settings, inputs)
    if use_trgm:
        lit = _pg_text_literal(clean)
        sql = (
            f'SELECT {col} AS _wb_match{select_ctx} FROM {rel} '
            f'WHERE {col} % {lit} ORDER BY similarity({col}, {lit}) DESC'
        )
    else:
        # Typo-tolerant prefix prefilter (token-AND): each token matches on a
        # shortened prefix so trailing-char typos still pass; rapidfuzz then ranks
        # for precision. Without this, "Sophie" can't find "Sophia".
        # MySQL uses LIKE (case-insensitive by default with utf8 collations);
        # Postgres/Snowflake/Databricks use ILIKE. ESCAPE '!' is universally safe
        # (backslash is a string-escape char on MySQL/Snowflake/Databricks).
        _like_op = "LIKE" if (_platform or "").lower() == "mysql" else "ILIKE"
        clauses = " AND ".join(
            f"{col} {_like_op} {_ilike_pattern_literal(_fuzzy_prefix(t))} ESCAPE '!'" for t in tokens
        )
        sql = f'SELECT {col} AS _wb_match{select_ctx} FROM {rel} WHERE {clauses}'

    _plat, _conn_ref, _src_dsn = _exec_conn(inputs)
    try:
        with neo4j_session(
            settings.neo4j_host, settings.neo4j_port,
            settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
        ) as ns:
            res = sql_executor.execute_select(
                neo4j_session=ns, project_code=f"marketplace:{getattr(inputs, 'domain', '')}",
                pg_connection=_src_dsn, sql=sql,
                executed_by=f"value-probe:{getattr(inputs, 'domain', '')}",
                max_rows=max_distinct, product_uri=getattr(inputs, "product_uri", None),
                view_schema=candidate.view_schema,
                platform=_plat, connection_ref=_conn_ref or None,
            )
    except Exception:
        return []
    if res.status != "ok" or not res.rows:
        return []

    col_names = [c["name"] for c in res.columns]
    try:
        match_idx = col_names.index("_wb_match")
    except ValueError:
        match_idx = 0

    # Dedupe by cell value, keep best fuzzy + first context row.
    by_value: dict[str, ValueCandidate] = {}
    for row in res.rows:
        raw = row[match_idx]
        if raw is None:
            continue
        value = str(raw)
        score = _fuzz_score(clean, value)
        if score < min_fuzzy:
            continue
        existing = by_value.get(value)
        if existing is not None and existing.fuzzy_score >= score:
            continue
        ctx = {
            col_names[i]: row[i]
            for i in range(len(col_names))
            if i != match_idx and row[i] is not None
        }
        by_value[value] = ValueCandidate(
            value=value, column_name=candidate.column_name,
            view_name=candidate.view_name, view_schema=candidate.view_schema,
            fuzzy_score=round(score, 2), pk_context=ctx,
            field_label=candidate.attribute_name or _humanize(candidate.column_name),
        )
    return sorted(by_value.values(), key=lambda c: c.fuzzy_score, reverse=True)[:CANDIDATES_RETURNED]


# ── Orchestrator ─────────────────────────────────────────────────────────────


def _candidate_label(c: ValueCandidate) -> str:
    label = c.value
    if c.pk_context:
        ctx = ", ".join(str(v) for v in c.pk_context.values())
        if ctx:
            label = f"{c.value} — {ctx}"
    if c.field_label:
        label = f"{label}  ·  {c.field_label}"
    return label


def _grounded_dict(column_name: str, view_name: str, view_schema: str,
                   value: str, mention_text: str) -> dict[str, Any]:
    return {
        "column_name": column_name, "view_name": view_name,
        "view_schema": view_schema, "value": value, "mention_text": mention_text,
    }


def _decide_from_candidates(
    mention: ValueMention, candidates: list[ValueCandidate], *,
    fallback: Optional[AttributeCandidate] = None,
) -> MentionResolution:
    """Auto-resolve a unique high-confidence match; else record-ambiguous; else
    not-found. Shared by the ranked path (_resolve_one) and the pinned path
    (resolve_pins)."""
    if not candidates:
        return MentionResolution(
            mention=mention, status="not_found",
            column_name=fallback.column_name if fallback else "",
            view_name=fallback.view_name if fallback else "",
            view_schema=fallback.view_schema if fallback else "",
        )
    candidates = sorted(candidates, key=lambda c: c.fuzzy_score, reverse=True)
    top = candidates[0]
    # Values within RESOLVE_MARGIN of the top, by distinct cell value.
    near_top = [c for c in candidates if (top.fuzzy_score - c.fuzzy_score) < RESOLVE_MARGIN]
    if top.fuzzy_score >= AUTO_RESOLVE_FUZZY and len({c.value for c in near_top}) == 1:
        return MentionResolution(
            mention=mention, status="resolved", column_name=top.column_name,
            view_name=top.view_name, view_schema=top.view_schema,
            resolved_value=top.value, candidates=candidates,
        )
    return MentionResolution(mention=mention, status="record_ambiguous",
                             candidates=candidates[:CANDIDATES_RETURNED])


def _resolve_one(
    settings: AppSettings, inputs: Any, mention: ValueMention,
) -> MentionResolution:
    """Rank candidate columns → PROBE them → let the DATA decide.

    Semantic ranking only picks which columns to probe; the answer comes from
    which column actually CONTAINS the value. A name doesn't live in a Gender
    column no matter how an embedding ranks it — so a column with no data match is
    eliminated, and we only declare attribute-ambiguity when two DIFFERENT columns
    each hold a strong match.

    Searches the FULL deployed-view set (``all_deployed_views``), not the
    concept-narrowed subset — the column holding the record (a person name) often
    lives in a view the concept retrieval narrowed away ("where does X live" →
    location views, but the name is on the employee view)."""
    views = getattr(inputs, "all_deployed_views", None) or inputs.deployed_views
    concepts = getattr(inputs, "all_concepts", None) or inputs.concepts
    attrs = rank_attribute_candidates(mention, concepts, views)
    if not attrs:
        return MentionResolution(mention=mention, status="not_found")

    # Probe every ranked candidate; keep only the columns the data supports.
    probed: list[tuple[AttributeCandidate, list[ValueCandidate]]] = []
    for a in attrs:
        cands = probe_column_for_value(settings, inputs, a, mention.text)
        if cands:
            probed.append((a, cands))
    if not probed:
        a0 = attrs[0]
        return MentionResolution(mention=mention, status="not_found",
                                 column_name=a0.column_name, view_name=a0.view_name,
                                 view_schema=a0.view_schema)

    # Flatten matches across every column the data supports, ranked by fuzzy. The
    # answer is whichever record the data holds — across columns — not an abstract
    # "which attribute" choice (a name in first_name vs full_name is the same
    # person; "Paris" the person vs the city are concrete, distinct values). We
    # present CONCRETE candidate values, never column names.
    flat: list[ValueCandidate] = [c for _a, cands in probed for c in cands]
    flat.sort(key=lambda c: c.fuzzy_score, reverse=True)
    top = flat[0]

    # Auto-resolve only a unique, near-exact winner.
    near = [c for c in flat if (top.fuzzy_score - c.fuzzy_score) < RESOLVE_MARGIN]
    if top.fuzzy_score >= AUTO_RESOLVE_FUZZY and len({(c.column_name, c.value) for c in near}) == 1:
        return MentionResolution(
            mention=mention, status="resolved", column_name=top.column_name,
            view_name=top.view_name, view_schema=top.view_schema,
            resolved_value=top.value, candidates=flat,
        )

    # Otherwise recommend concrete candidates (deduped by column+value), tagging
    # each with its field when the matches span more than one column.
    multi_field = len({c.column_name for c in flat}) > 1
    out: list[ValueCandidate] = []
    seen: set[tuple[str, str]] = set()
    for c in flat:
        key = (c.column_name, c.value)
        if key in seen:
            continue
        seen.add(key)
        if not multi_field:
            c.field_label = ""   # single field → don't clutter labels
        out.append(c)
        if len(out) >= CANDIDATES_RETURNED:
            break
    return MentionResolution(mention=mention, status="record_ambiguous", candidates=out)


def _candidate_for_pin(pin: dict[str, Any], deployed_views: list[dict[str, Any]],
                       ) -> Optional[AttributeCandidate]:
    """Reconstruct an AttributeCandidate from a user's attribute-choice pin
    (`{view_schema, view_name, column_name}`), resolving its dataType + view
    from the hydrated deployed views."""
    vs, vn, cn = pin.get("view_schema", ""), pin.get("view_name", ""), pin.get("column_name", "")
    for v in deployed_views or []:
        if v.get("view_schema") == vs and v.get("view_name") == vn:
            for c in v.get("columns") or []:
                if (c.get("name") or "") == cn:
                    return AttributeCandidate(
                        attribute_name=cn, column_name=cn, column_uri="",
                        view_name=vn, view_schema=vs, data_type=c.get("data_type", ""),
                        score=1.0, view=v,
                    )
    return None


def resolve_pins(
    settings: AppSettings, inputs: Any, pins: list[dict[str, Any]],
) -> ResolutionOutcome:
    """Re-probe explicit column pins from an attribute-choice disambiguation
    (`resolved_values` entries with empty ``value``). Each pin carries
    ``mention_text`` + the chosen column; we probe only that column and decide."""
    outcome = ResolutionOutcome()
    views = getattr(inputs, "all_deployed_views", None) or inputs.deployed_views
    for pin in pins or []:
        mention_text = _sanitize_mention(str(pin.get("mention_text") or ""))
        cand = _candidate_for_pin(pin, views)
        if not mention_text or cand is None:
            continue
        mention = ValueMention(text=mention_text)
        candidates = probe_column_for_value(settings, inputs, cand, mention_text)
        res = _decide_from_candidates(mention, candidates, fallback=cand)
        outcome.trace.append(_trace_entry(res))
        if res.status == "resolved":
            outcome.grounded_values.append(_grounded_dict(
                res.column_name, res.view_name, res.view_schema,
                res.resolved_value, mention_text,
            ))
        else:
            outcome.disambiguation = _build_disambiguation(
                res, list(outcome.grounded_values), getattr(inputs, "domain", ""))
            break
    return outcome


def _build_disambiguation(res: MentionResolution, already_grounded: list[dict[str, Any]],
                          domain: str) -> dict[str, Any]:
    mt = res.mention.text
    if res.status == "attribute_ambiguous":
        opts = [{
            "label": a.attribute_name or a.column_name,
            "column_name": a.column_name, "view_name": a.view_name,
            "view_schema": a.view_schema, "value": "",
        } for a in res.attribute_options]
        return {
            "kind": "attribute", "mention_text": mt,
            "prompt": (f"“{mt}” could refer to more than one thing. "
                       "Which did you mean?"),
            "attribute_options": opts, "candidates": [],
            "already_grounded": already_grounded,
        }
    if res.status == "record_ambiguous":
        cands = [{
            "label": _candidate_label(c), "value": c.value,
            "column_name": c.column_name, "view_name": c.view_name,
            "view_schema": c.view_schema,
        } for c in res.candidates]
        prompt = (f"Did you mean {cands[0]['label']}?" if len(cands) == 1
                  else f"I found a few possible matches for “{mt}”. Which one did you mean?")
        return {
            "kind": "record", "mention_text": mt,
            "prompt": prompt,
            "candidates": cands, "attribute_options": [],
            "already_grounded": already_grounded,
        }
    # not_found
    return {
        "kind": "not_found", "mention_text": mt,
        "prompt": (f"I couldn’t find “{mt}” in the {domain} data. "
                   "Check the spelling, or tell me which entity it belongs to."),
        "candidates": [], "attribute_options": [],
        "already_grounded": already_grounded,
    }


def _trace_entry(res: MentionResolution) -> dict[str, Any]:
    return {
        "mention_text": res.mention.text,
        "type_hint": res.mention.type_hint,
        "status": res.status,
        "column": res.column_name or (res.candidates[0].column_name if res.candidates else ""),
        "view": res.view_name or (res.candidates[0].view_name if res.candidates else ""),
        "candidate_count": len(res.candidates),
        "fuzzy_top": res.candidates[0].fuzzy_score if res.candidates else None,
        "resolved_value": res.resolved_value or None,
    }


def resolve_mentions(
    settings: AppSettings, inputs: Any, mentions: list[dict[str, Any]],
) -> ResolutionOutcome:
    """Resolve every extracted value-mention against live data.

    ``mentions`` is the LLM-extracted list of ``{text, type_hint}`` dicts. Returns
    a ResolutionOutcome carrying either verified ``grounded_values`` (all mentions
    resolved) or a ``disambiguation`` payload (first unresolved mention drives it;
    one is resolved at a time for UI simplicity). Always populates ``trace``."""
    clean_mentions: list[ValueMention] = []
    for m in mentions or []:
        text = _sanitize_mention(str((m or {}).get("text") or ""))
        if not text:
            continue
        clean_mentions.append(ValueMention(text=text, type_hint=str((m or {}).get("type_hint") or "")))

    if not clean_mentions:
        return ResolutionOutcome(skipped_reason="no value mentions")

    outcome = ResolutionOutcome()
    for mention in clean_mentions:
        res = _resolve_one(settings, inputs, mention)
        outcome.trace.append(_trace_entry(res))
        if res.status == "resolved":
            outcome.grounded_values.append(_grounded_dict(
                res.column_name, res.view_name, res.view_schema,
                res.resolved_value, mention.text,
            ))
            continue
        # First unresolved mention drives the disambiguation; stop probing further.
        outcome.disambiguation = _build_disambiguation(
            res, list(outcome.grounded_values), getattr(inputs, "domain", ""),
        )
        break
    return outcome
