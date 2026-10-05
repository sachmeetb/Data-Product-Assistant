"""Dialect-aware SQL rendering for the semantic-layer query path.

The NL→SQL skills (marketplace Semantic Q&A + the QA question executor) emit
**standard / ANSI** SQL — dialect-neutral, not Postgres-flavoured. This module
parses that standard text with ``sqlglot`` into a **dialect-neutral AST** and
re-renders it deterministically to the *target* engine before execution. No
engine is privileged: the emitted text is ephemeral parser input, the AST is the
canonical representation, and the output is always native to the target
platform. Adding a new platform is one entry in ``PLATFORM_TO_SQLGLOT`` plus a
driver — no per-dialect LLM prompt-engineering.

Peer of ``sql_ident.py`` (which owns *identifier quoting* for the pre-built
sampler SQL). This module owns *whole-statement* rendering for LLM-generated
queries.

**Invariant — render only at the LLM-generation sites.** Do NOT call
``render_for_platform`` inside ``sql_executor.execute_select``: the sampler
``SELECT * FROM quote_relation(...)`` (marketplace_chat / qa_execute prep) is
*already* target-dialect via ``sql_ident.quote_relation`` — re-parsing a
backtick-quoted relation through the neutral grammar would misread it. Render the
LLM query, then pass the rendered text to ``execute_select`` untouched.

This dialect binding is currently duplicated in a few places that own different
concerns — ``sql_ident._BACKTICK_PLATFORMS`` (identifier quoting),
``platform/sql_renderer`` (transform-DSL rendering), ``generate_view_ddl._DIALECTS``
(view-DDL emission), and ``stage_execution._PLATFORM_DIALECT_MAP`` (serving-stage
dialect flag). Full consolidation onto a single table is a follow-up; this module
owns the sqlglot binding for the query path only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# Platform id (as stored on a SourceBinding / connection) → sqlglot dialect name.
# NOTE: this maps to sqlglot's OWN dialect vocabulary, which is distinct from the
# view-DDL generator's ``_PLATFORM_DIALECT_MAP`` (that one uses "ansi" for MySQL
# because its emitter has no MySQL subclass). sqlglot has a first-class ``mysql``
# dialect, so we use it here to get correct backtick quoting + function mapping.
PLATFORM_TO_SQLGLOT: dict[str, str] = {
    "postgres": "postgres",
    "postgresql": "postgres",
    "mysql": "mysql",
    "snowflake": "snowflake",
    "databricks": "databricks",
    "bigquery": "bigquery",
    "duckdb": "duckdb",
}


@dataclass
class RenderResult:
    """Outcome of a render. ``ok`` is False on a parse/render failure, in which
    case ``sql`` is the ORIGINAL (unmodified) input and ``error`` explains why —
    the caller executes the original text (never a regression vs. today) and may
    surface a soft note."""
    sql: str
    ok: bool
    error: Optional[str] = None


def sqlglot_dialect_for(target_platform: str) -> str:
    """Resolve a platform id to its sqlglot dialect name. Fail-closed on an
    unknown platform (ADR-9 pattern, matches ``platform/sql_renderer.get_renderer``
    and ``platform/type_system.get_profile``) — never silently fall back to
    Postgres."""
    key = (target_platform or "").lower()
    try:
        return PLATFORM_TO_SQLGLOT[key]
    except KeyError:
        raise KeyError(
            f"No sqlglot dialect binding for platform {target_platform!r}. "
            f"Known platforms: {sorted(PLATFORM_TO_SQLGLOT)}."
        )


def render_for_platform(sql: str, target_platform: str, read: Optional[str] = None) -> RenderResult:
    """Parse ``sql`` and re-render it to ``target_platform``'s native dialect.

    ``read`` selects the source grammar:
      - ``None`` (default) — sqlglot's dialect-neutral grammar. This honours the
        "no engine is privileged" decision for the semantic-layer NL→SQL path,
        which emits standard/ANSI SQL. A target that maps to the same neutral
        form (Postgres) is effectively a no-op re-print.
      - a **platform id** (e.g. ``"postgres"``) — parse with that engine's
        grammar. The transform compiler passes the mapping's declared
        ``expressionDialect`` here, because a legacy raw expression is Postgres
        SQL, not neutral SQL, and parsing it as neutral risks a semantic misread.

    **Fail-open:** on any parse/render error return the ORIGINAL ``sql`` with
    ``ok=False`` + the error message. This never regresses relative to today
    (where the raw text was executed directly); the caller logs/surfaces a soft
    note and executes the original.

    Fail-**closed** only on an unknown ``target_platform`` / ``read`` platform
    (``KeyError``) — that's a programming/config error, not a query the user can fix.
    """
    dialect = sqlglot_dialect_for(target_platform)  # KeyError on unknown platform
    read_dialect = sqlglot_dialect_for(read) if read else None  # KeyError on unknown read
    if not sql or not sql.strip():
        return RenderResult(sql=sql, ok=True)
    try:
        import sqlglot
        # Parse → (fold identifiers on upper-folding targets) → generate, instead
        # of a bare ``sqlglot.transpile``. sqlglot PRESERVES a quoted identifier's
        # case across dialects (``normalize=True`` does not help), so an LLM's
        # standard ``"party"`` transpiled to Snowflake stays lowercase-exact and
        # misses the UPPER object dlt/dbt created unquoted. On a target whose
        # ``NORMALIZATION_STRATEGY`` is UPPERCASE (Snowflake), fold every
        # ``exp.Identifier`` to UPPER so it addresses the real physical object.
        expression = sqlglot.parse_one(sql, read=read_dialect)
        if expression is None:
            return RenderResult(sql=sql, ok=False, error="sqlglot produced no output")
        _fold_identifiers_for_target(expression, dialect)
        return RenderResult(sql=expression.sql(dialect=dialect), ok=True)
    except Exception as e:  # ParseError, UnsupportedError, anything — fail open
        return RenderResult(sql=sql, ok=False, error=f"{type(e).__name__}: {e}")


def _fold_identifiers_for_target(expression, dialect: str) -> None:
    """In-place UPPER-fold every identifier in ``expression`` when ``dialect``'s
    normalization strategy is UPPERCASE (Snowflake). No-op for LOWERCASE /
    CASE_SENSITIVE / CASE_INSENSITIVE targets, so byte output is unchanged there.

    Only ``exp.Identifier`` nodes are touched — column/table/alias names — never
    string literals (``exp.Literal``), so ``WHERE status = 'active'`` keeps its
    value verbatim. Fail-open: any inability to resolve the strategy leaves the
    identifiers untouched (the caller renders as before)."""
    from sqlglot import exp
    from sqlglot.dialects.dialect import Dialect, NormalizationStrategy
    try:
        strategy = Dialect.get_or_raise(dialect).NORMALIZATION_STRATEGY
    except Exception:
        return
    if strategy != NormalizationStrategy.UPPERCASE:
        return
    for node in expression.walk():
        if isinstance(node, exp.Identifier) and isinstance(node.this, str):
            node.set("this", node.this.upper())


# ─────────────────────────────────────────────────────────────────────────────
# AST-level capability validation (the authority is the artifact, NOT transpile)
# ─────────────────────────────────────────────────────────────────────────────
# sqlglot.transpile re-emits AGE(...) for Databricks and SPLIT_PART(...) for
# BigQuery/MySQL UNCHANGED — it accepts syntax without proving support. So we
# parse to an AST and check every function against
# platform/transform_capabilities: a function that is `unsupported`, or unknown
# (neither portable nor in the platform's map), fails closed. We never equate a
# clean transpile with capability.
#
# CompileResult is the single result shape every build/deploy path surfaces: no
# caller may hand out `sql` while `errors` is non-empty. Threading it out of the
# per-dataset compiler (generate_view_ddl.py) is done incrementally; this module
# owns the type + the per-expression validator that populates it.


@dataclass
class TransformDiagnostic:
    """One capability finding against a transform expression.

    ``product_col`` / ``mapping_uri`` are optional context the per-dataset
    compiler attaches so a preflight report can point the engineer at the exact
    mapping to re-author."""
    severity: str            # "error" | "warning"
    code: str                # machine code, e.g. "unsupported_function"
    message: str             # human-readable
    function: Optional[str] = None
    remediation: Optional[str] = None
    citation: Optional[str] = None
    product_col: Optional[str] = None
    mapping_uri: Optional[str] = None

    def to_dict(self) -> dict:
        d = {"severity": self.severity, "code": self.code, "message": self.message}
        if self.function:
            d["function"] = self.function
        if self.remediation:
            d["remediation"] = self.remediation
        if self.citation:
            d["citation"] = self.citation
        if self.product_col:
            d["product_col"] = self.product_col
        if self.mapping_uri:
            d["mapping_uri"] = self.mapping_uri
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "TransformDiagnostic":
        return cls(
            severity=d.get("severity", "error"),
            code=d.get("code", "unknown"),
            message=d.get("message", ""),
            function=d.get("function"),
            remediation=d.get("remediation"),
            citation=d.get("citation"),
            product_col=d.get("product_col"),
            mapping_uri=d.get("mapping_uri"),
        )


@dataclass
class CompileResult:
    """Result of compiling one transform surface for a target platform.

    ``sql`` is populated ONLY when ``errors`` is empty — no caller receives
    deployable SQL while any error is present. ``used_capabilities`` records the
    ``FUNCTION@platform:capability`` claims relied on (audit + conformance).
    """
    sql: Optional[str]
    catalog_version: Optional[str]
    warnings: list = field(default_factory=list)   # list[TransformDiagnostic]
    errors: list = field(default_factory=list)     # list[TransformDiagnostic]
    used_capabilities: list = field(default_factory=list)  # list[str]
    # False when capability validation was SKIPPED (non-served dialect, or the
    # backend validator wasn't reachable) — NOT the same as "clean". Callers use
    # this to distinguish "validated & clean" from "not validated".
    validated: bool = True

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "validated": self.validated,
            "sql": self.sql,
            "catalog_version": self.catalog_version,
            "warnings": [w.to_dict() for w in self.warnings],
            "errors": [e.to_dict() for e in self.errors],
            "used_capabilities": list(self.used_capabilities),
        }


def _func_name(node) -> Optional[str]:
    """Canonical UPPERCASE function name for an AST node, or None if not a call.

    ``exp.Anonymous`` (functions sqlglot doesn't model, e.g. AGE) carries the raw
    name in ``.name`` and reports ``sql_name() == 'ANONYMOUS'`` — handle it
    explicitly. Modeled functions (SplitPart, DateDiff, ...) report their real
    ``sql_name()``.
    """
    from sqlglot import exp
    if isinstance(node, exp.Anonymous):
        return (node.name or "").upper() or None
    if isinstance(node, exp.Func):
        try:
            sn = node.sql_name()
        except Exception:
            sn = None
        if sn and sn.upper() != "ANONYMOUS":
            return sn.upper()
        n = getattr(node, "name", None)
        return (n or node.__class__.__name__).upper()
    return None


def validate_expression(
    sql: str,
    target_platform: str,
    capabilities=None,
    *,
    read: Optional[str] = None,
    allow_subqueries: bool = False,
) -> tuple[list[TransformDiagnostic], list[str]]:
    """Parse ``sql`` and validate every function/node against the capability
    artifact for ``target_platform``.

    Returns ``(diagnostics, used_capabilities)``. Diagnostics carry their own
    severity; the caller (``compile_expression``) splits errors vs warnings.

    Fail-closed conditions (error):
      - the source dialect (``read``) is declared but unknown → ``unknown_source_dialect``
      - the target platform is not covered by the artifact → ``unknown_target_platform``
      - the expression is multiple statements → ``multi_statement``
      - a subquery is present and ``allow_subqueries`` is False → ``subquery``
      - a function is ``unsupported`` on the target → ``unsupported_function``
      - a function is unknown (not portable, absent from the map) → ``unknown_function``
      - the expression fails to parse → ``parse_error``
    """
    from sqlglot import exp
    import sqlglot

    if capabilities is None:
        from .platform.transform_capabilities import get_capabilities
        capabilities = get_capabilities()

    diags: list[TransformDiagnostic] = []
    used: list[str] = []

    if not sql or not sql.strip():
        return diags, used

    platform = (target_platform or "").lower()
    if platform not in {p.lower() for p in capabilities.platforms}:
        diags.append(TransformDiagnostic(
            "error", "unknown_target_platform",
            f"Target platform {target_platform!r} is not covered by the transform "
            f"capability artifact (known: {sorted(capabilities.platforms)}).",
        ))
        return diags, used

    # Resolve the source grammar. An unknown declared dialect fails closed rather
    # than risking a neutral-parse semantic misread of legacy Postgres SQL.
    read_dialect = None
    if read:
        try:
            read_dialect = sqlglot_dialect_for(read)
        except KeyError:
            diags.append(TransformDiagnostic(
                "error", "unknown_source_dialect",
                f"Expression declares source dialect {read!r}, which has no sqlglot "
                f"binding. Route to review before compiling.",
            ))
            return diags, used

    try:
        statements = sqlglot.parse(sql, read=read_dialect)
    except Exception as e:  # ParseError etc.
        diags.append(TransformDiagnostic(
            "error", "parse_error", f"Could not parse expression: {type(e).__name__}: {e}",
        ))
        return diags, used

    statements = [s for s in statements if s is not None]
    if len(statements) > 1:
        diags.append(TransformDiagnostic(
            "error", "multi_statement",
            "Transform expression must be a single expression, not multiple statements.",
        ))
        return diags, used
    if not statements:
        return diags, used

    root = statements[0]
    seen_funcs: set[str] = set()
    subquery_flagged = False
    for node in root.walk():
        # Disallow subqueries in a scalar transform expression. A nested query
        # shows up as both an exp.Subquery wrapper and an exp.Select — flag once.
        if not allow_subqueries and isinstance(node, (exp.Select, exp.Subquery)):
            if not subquery_flagged:
                subquery_flagged = True
                diags.append(TransformDiagnostic(
                    "error", "subquery",
                    "Subqueries are not allowed in a transform expression.",
                ))
            continue
        name = _func_name(node)
        if not name or name in seen_funcs:
            continue
        seen_funcs.add(name)
        support = capabilities.function_support(name, platform)
        if support in ("native", "emulated"):
            used.append(f"{name}@{platform}:{support}")
            continue
        entry = capabilities.function_entry(name, platform)
        if support == "unsupported":
            diags.append(TransformDiagnostic(
                "error", "unsupported_function",
                f"Function {name}() is unsupported on {platform}.",
                function=name,
                remediation=(entry or {}).get("remediation"),
                citation=(entry or {}).get("citation"),
            ))
        else:  # None → unknown
            diags.append(TransformDiagnostic(
                "error", "unknown_function",
                f"Function {name}() is not in the transform capability catalog for "
                f"{platform}; cannot prove the engine supports it (fail-closed).",
                function=name,
            ))
    return diags, used


def compile_expression(
    sql: str,
    target_platform: str,
    *,
    read: Optional[str] = None,
    capabilities=None,
    allow_subqueries: bool = False,
) -> CompileResult:
    """Full per-expression pipeline: validate source AST → render → re-validate
    rendered AST → CompileResult. ``sql`` is populated only when clean.

    This is the shared choke point design pillar #3/#4: parse(read) → validate →
    render(target) → re-validate. sqlglot is the parse/render engine; the
    artifact is the authority.
    """
    if capabilities is None:
        from .platform.transform_capabilities import get_capabilities
        capabilities = get_capabilities()

    errors: list[TransformDiagnostic] = []
    warnings: list[TransformDiagnostic] = []
    used: list[str] = []

    def _absorb(diags: list[TransformDiagnostic]):
        for d in diags:
            (errors if d.severity == "error" else warnings).append(d)

    # 1. validate the source expression against the target's capabilities.
    src_diags, src_used = validate_expression(
        sql, target_platform, capabilities, read=read, allow_subqueries=allow_subqueries,
    )
    _absorb(src_diags)
    used.extend(src_used)

    rendered_sql: Optional[str] = None
    if not errors:
        # 2. render to the target dialect.
        rr = render_for_platform(sql, target_platform, read=read)
        if not rr.ok:
            errors.append(TransformDiagnostic(
                "error", "render_error",
                f"Could not render for {target_platform}: {rr.error}",
            ))
        else:
            rendered_sql = rr.sql
            # 3. re-validate the RENDERED SQL (now in the target dialect) — catches
            #    any residual unsupported function the render didn't translate away.
            rnd_diags, rnd_used = validate_expression(
                rendered_sql, target_platform, capabilities,
                read=target_platform, allow_subqueries=allow_subqueries,
            )
            _absorb(rnd_diags)
            used.extend(rnd_used)

    return CompileResult(
        sql=rendered_sql if not errors else None,
        catalog_version=capabilities.schema_version,
        warnings=warnings,
        errors=errors,
        used_capabilities=sorted(set(used)),
    )
