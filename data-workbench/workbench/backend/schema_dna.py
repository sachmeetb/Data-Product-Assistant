"""Schema-DNA attribute comparison.

A compact, dependency-light in-repo port of the *Semantic Data Alignment*
(SDA, a.k.a. "schema_dna") scorer. It compares one set of column-like
attributes against another and produces, per source attribute, a best match
with a 5-axis breakdown shaped exactly like the frontend ``ProductSimilarity``
radar:

    character    (S1)  surface/character stats of the name
    data_type    (S2)  datatype family + label/description signal
    semantic     (S3)  text-embedding cosine (BGE-small via ``embeddings.py``)
    statistical  (S4)  numeric instance stats  — needs profiling data
    distribution (S5)  text/value instance stats — needs profiling data

Design notes
------------
* This is a *compact* reimplementation — we do NOT import schema_dna's large
  modules (``semantic_data_alignment.py`` ~461 KB, ``ontology_feature_extractor.py``
  ~83 KB). The S1/S2 stats are inlined; S3 reuses the BGE-small model already
  shipped in :mod:`embeddings`; S4/S5 formulas mirror
  ``extract_statistical_features_from_values`` but consume Data-Workbench's own
  profiling output.
* **Only** the ``feature_based`` variant fills all five axes — it is the one
  that powers the spider. ``fuzzy_semantic`` (schema_dna's difflib + embedding
  blend) and ``llm_assisted`` produce an overall score with approximated axes;
  the instance axes come back ``None`` ("unknown") rather than faked.
* Everything degrades gracefully: when the embedding model is unavailable the
  semantic axis falls back to token-Jaccard (mirrors the pattern in
  ``embeddings.py`` / ``business_concepts``); when a product has not been
  profiled S4/S5 return ``None``.
"""

from __future__ import annotations

import math
import re
import string
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Optional

from . import embeddings
from .platform.type_system import CastCost, cast_cost

# --------------------------------------------------------------------------
# Shared name normalisation / tokenisation (mirrors frontend similarity.ts so
# the two surfaces agree on what "same name" and "same family" mean).
# --------------------------------------------------------------------------

_CAMEL_RE = re.compile(r"([a-z0-9])([A-Z])")
_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def norm(s: str) -> str:
    return (s or "").strip().lower()


def tokenize(s: str) -> set[str]:
    """camelCase/snake_case → lowercase word tokens (len > 1)."""
    spaced = _CAMEL_RE.sub(r"\1 \2", s or "")
    return {t for t in _SPLIT_RE.split(spaced.lower()) if len(t) > 1}


def token_jaccard(a: str, b: str) -> float:
    A, B = tokenize(a), tokenize(b)
    if not A and not B:
        return 1.0
    inter = len(A & B)
    union = len(A | B)
    return inter / union if union else 0.0


def char_jaccard(a: str, b: str) -> float:
    A = set(re.sub(r"[^a-z0-9]", "", (a or "").lower()))
    B = set(re.sub(r"[^a-z0-9]", "", (b or "").lower()))
    if not A and not B:
        return 1.0
    union = len(A | B)
    return len(A & B) / union if union else 0.0


def type_family(t: str) -> str:
    s = norm(t)
    if re.search(r"int|long|number|numeric|decimal|float|double|real|money", s):
        return "numeric"
    if re.search(r"char|text|string|clob", s):
        return "text"
    if re.search(r"date|time|stamp", s):
        return "temporal"
    if re.search(r"bool|bit", s):
        return "boolean"
    return s or "unknown"


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    # Raw cosine, clamped to [0, 1]. (Do NOT remap (cos+1)/2 — BGE similarities
    # are already ~[0,1] for these short texts, so the remap floored unrelated
    # pairs at ~0.5 and inflated every semantic score.)
    return _clamp01(dot / (na * nb))


# --------------------------------------------------------------------------
# Column feature model
# --------------------------------------------------------------------------


@dataclass
class ColumnFeature:
    """A column-like attribute to be compared.

    ``profile`` is optional per-column profiling output. When present the S4/S5
    axes activate; keys consumed (all optional): ``numeric`` bool, ``min``,
    ``max``, ``mean``, ``stddev``, ``distinct_ratio``, ``null_ratio``,
    ``avg_length``, ``entropy``.
    """

    name: str
    type: str = ""
    description: str = ""
    concept: str = ""
    profile: Optional[dict[str, Any]] = None
    extra: dict[str, Any] = field(default_factory=dict)

    def semantic_text(self) -> str:
        parts = [self.name or "", self.concept or "", self.description or ""]
        return ". ".join(p for p in parts if p)


def estate_column_embed_text(name: str, description: str = "") -> str:
    """Canonical text embedded for an ``:EstateColumn``.

    **Load-bearing invariant:** this MUST equal
    ``ColumnFeature(name=name, concept="", description=description).semantic_text()``
    so a vector persisted at enrichment time (keyed by this text's hash) hits the
    scorer's embed cache at feasibility time. That is why ``feasibility._raw_evidence``
    builds its estate ``ColumnFeature`` with ``description=<col description>`` and
    ``concept=""`` — anything else and every stored vector would miss the cache.
    Name only when there's no description.
    """
    parts = [name or "", description or ""]
    return ". ".join(p for p in parts if p)


def column_from_dict(d: dict[str, Any]) -> ColumnFeature:
    return ColumnFeature(
        name=str(d.get("name") or d.get("column_name") or ""),
        type=str(d.get("type") or d.get("data_type") or ""),
        description=str(d.get("description") or ""),
        concept=str(d.get("concept") or ""),
        profile=d.get("profile") if isinstance(d.get("profile"), dict) else None,
        extra={k: v for k, v in d.items() if k not in {"name", "type", "description", "concept", "profile"}},
    )


# --------------------------------------------------------------------------
# S1 — character statistics  (compact form of extract_character_features)
# --------------------------------------------------------------------------


def _char_vector(text: str) -> list[float]:
    """Compact character-feature vector: printable-char counts + basic stats.

    schema_dna's full block is 768-dim; this compact form keeps the printable
    histogram (the dominant signal) plus the 7 basic stats, which is enough for
    a stable pairwise cosine on short identifiers.
    """
    text = text or ""
    vec = [float(text.count(c)) for c in string.printable]
    vec.extend([
        float(len(text)),
        float(sum(c.isalpha() for c in text)),
        float(sum(c.isdigit() for c in text)),
        float(sum(c.isspace() for c in text)),
        float(sum(c.isupper() for c in text)),
        float(sum(c.islower() for c in text)),
        float(sum(c in string.punctuation for c in text)),
    ])
    return vec


def _s1_character(a: ColumnFeature, b: ColumnFeature) -> float:
    """Character-axis similarity in [0, 1].

    Alphabet-set Jaccard of the two names. We deliberately do NOT blend in the
    printable-histogram cosine (`_char_vector`) — any two lowercase identifiers
    have near-identical character-frequency distributions, so that cosine reads
    ~0.6 for totally unrelated names and floods the character axis with noise
    (e.g. zzz_widget_blob vs product_code scoring 63). Jaccard is discriminating.
    Token-overlap adds a word-level signal on top of the raw letter set.
    """
    return _clamp01(0.6 * char_jaccard(a.name, b.name) + 0.4 * token_jaccard(a.name, b.name))


# --------------------------------------------------------------------------
# S2 — metadata (datatype family + label/description presence)
# --------------------------------------------------------------------------


def _s2_data_type(a: ColumnFeature, b: ColumnFeature) -> float:
    if a.type and b.type and norm(a.type) == norm(b.type):
        base = 1.0
    elif type_family(a.type) == type_family(b.type) and type_family(a.type) != "unknown":
        base = 0.75
    else:
        # Cross-family: a routine cross-representation cast (a numeric stored as text,
        # a boolean as 0/1) shouldn't read as hard-incompatible — bump its floor so an
        # easily-castable mismatch doesn't drag the ranking or read as "type
        # incompatible". Genuinely structural mismatches (json/binary ↔ scalar) stay at
        # 0.3. The data_type axis is only 0.15 of the blend, so this softens the RANKING
        # without rescuing a wrong-entity match (which fails on the semantic axis).
        base = 0.55 if cast_cost(b.type, a.type) == CastCost.cross_family_castable else 0.3
    # A shared documented concept/description nudges the metadata axis up.
    if a.concept and b.concept and norm(a.concept) == norm(b.concept):
        base = min(1.0, base + 0.1)
    return base


# --------------------------------------------------------------------------
# S3 — semantic embedding cosine (BGE-small, token-Jaccard fallback)
# --------------------------------------------------------------------------


def _embed_texts(texts: list[str]) -> Optional[list[list[float]]]:
    if not embeddings.available():
        return None
    vecs = embeddings.embed_documents(texts)
    return vecs or None


def _s3_semantic_pair(a: ColumnFeature, b: ColumnFeature,
                      cache: dict[str, list[float]]) -> float:
    ta, tb = a.semantic_text(), b.semantic_text()
    if ta in cache and tb in cache:
        full = _cosine(cache[ta], cache[tb])
    else:
        # No embedding cache hit → token-Jaccard fallback (still a real signal).
        full = token_jaccard(ta, tb)
    # Name-level floor — applied ONLY when the two attribute names are TRULY equal.
    # semantic_text folds in concept + description, so a bare source column
    # ("channel") embedded against a governed attribute ("channel. Channel. <desc>")
    # scores < 1 purely from the extra target text; floor an identical-name pair at
    # 1.0 so it isn't penalised for that. For DIFFERENT names we deliberately do NOT
    # floor on token_jaccard(name_a, name_b): the tokenizer drops 1-char tokens, so
    # `n_name` and `c_name` both collapse to {name} → a false 1.0 that would discard
    # the cosine distinguishing a nation name from a customer name (the entity-blind
    # false-positive feasibility bug). Different names keep the real semantic signal
    # so entity meaning governs the match.
    na, nb = (a.name or ""), (b.name or "")
    if na and norm(na) == norm(nb):
        return 1.0
    return full


# --------------------------------------------------------------------------
# S4 / S5 — instance statistics from profiling (None = "unknown", not faked)
# --------------------------------------------------------------------------


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _rel_close(x: Optional[float], y: Optional[float]) -> Optional[float]:
    """1 when equal, decaying with relative difference. None if either missing."""
    if x is None or y is None:
        return None
    denom = max(abs(x), abs(y), 1e-9)
    return _clamp01(1.0 - abs(x - y) / denom)


def _s4_statistical(a: ColumnFeature, b: ColumnFeature) -> Optional[float]:
    """Numeric instance-stat similarity. None unless both are profiled numerics."""
    pa, pb = a.profile, b.profile
    if not pa or not pb:
        return None
    if not (pa.get("numeric") and pb.get("numeric")):
        return None
    comps = [
        _rel_close(_num(pa.get("min")), _num(pb.get("min"))),
        _rel_close(_num(pa.get("max")), _num(pb.get("max"))),
        _rel_close(_num(pa.get("mean")), _num(pb.get("mean"))),
        _rel_close(_num(pa.get("stddev")), _num(pb.get("stddev"))),
    ]
    vals = [c for c in comps if c is not None]
    return sum(vals) / len(vals) if vals else None


def _s5_distribution(a: ColumnFeature, b: ColumnFeature) -> Optional[float]:
    """Value-distribution similarity (length / cardinality / nullability). None
    unless both are profiled."""
    pa, pb = a.profile, b.profile
    if not pa or not pb:
        return None
    comps = [
        _rel_close(_num(pa.get("avg_length")), _num(pb.get("avg_length"))),
        _rel_close(_num(pa.get("distinct_ratio")), _num(pb.get("distinct_ratio"))),
        _rel_close(_num(pa.get("null_ratio")), _num(pb.get("null_ratio"))),
        _rel_close(_num(pa.get("entropy")), _num(pb.get("entropy"))),
    ]
    vals = [c for c in comps if c is not None]
    return sum(vals) / len(vals) if vals else None


# --------------------------------------------------------------------------
# Dimension assembly
# --------------------------------------------------------------------------

_AXIS_META = [
    ("data_type", "Data Type"),
    ("character", "Character"),
    ("semantic", "Semantic"),
    ("distribution", "Distribution"),
    ("statistical", "Statistical"),
]


def _pct(x: Optional[float]) -> Optional[int]:
    return None if x is None else round(100 * _clamp01(x))


def _instance_reason(a: ColumnFeature, b: ColumnFeature, *, numeric: bool) -> str:
    """Explain WHY an instance axis (S4 numeric / S5 text) has no value for a
    pair — profiling absent on one/both sides, or present but wrong kind."""
    pa, pb = a.profile, b.profile
    missing = [name for name, p in (("source", pa), ("target", pb)) if not p]
    if missing:
        which = " and ".join(missing)
        verb = "columns have" if len(missing) > 1 else "column has"
        return f"Not profiled — {which} {verb} no profiling data ({'S4 numeric' if numeric else 'S5 distribution'} stats need it)."
    if numeric:
        return "Both columns are non-numeric — S4 numeric instance stats don't apply."
    return "No comparable value-distribution stats in the profiling for this pair (S5)."


def _dimensions(scores: dict[str, Optional[float]], a: ColumnFeature, b: ColumnFeature) -> list[dict[str, Any]]:
    hints = {
        "data_type": f"{a.type or '?'} vs {b.type or '?'}",
        "character": "Character-level name similarity (S1).",
        "semantic": "Embedding cosine of name + concept (S3)." if embeddings.available()
                    else "Token overlap of name + concept (S3, embedding model unavailable — install fastembed for the real signal).",
        "distribution": "Value-distribution overlap from profiling (S5).",
        "statistical": "Numeric instance-stat similarity from profiling (S4).",
    }
    dims: list[dict[str, Any]] = []
    for key, label in _AXIS_META:
        v = _pct(scores.get(key))
        d: dict[str, Any] = {"key": key, "label": label, "value": v, "hint": hints[key]}
        if v is None:
            # Surface exactly why this axis wasn't measured.
            if key == "statistical":
                d["reason"] = _instance_reason(a, b, numeric=True)
            elif key == "distribution":
                d["reason"] = _instance_reason(a, b, numeric=False)
            else:
                d["reason"] = "Not measured for this pair."
        dims.append(d)
    return dims


# Overall match is a WEIGHTED blend, not a flat mean. What decides whether two
# columns represent the same thing is their meaning (semantic) and name
# (character); datatype only CONFIRMS (two unrelated varchars share a type but
# aren't a match), and instance stats refine when profiled. A flat mean let
# data_type=100 inflate non-matches — this is the fix for that.
_OVERALL_WEIGHTS = {
    "semantic": 0.42,
    "character": 0.28,
    "data_type": 0.15,
    "statistical": 0.075,
    "distribution": 0.075,
}


def _overall(scores: dict[str, Optional[float]]) -> int:
    num = 0.0
    den = 0.0
    for key, w in _OVERALL_WEIGHTS.items():
        v = scores.get(key)
        if v is None:
            continue
        num += w * v
        den += w
    return round(100 * (num / den)) if den else 0


# --------------------------------------------------------------------------
# Variants
# --------------------------------------------------------------------------


def _pair_scores_feature_based(a: ColumnFeature, b: ColumnFeature,
                               embed_cache: dict[str, list[float]]) -> dict[str, Optional[float]]:
    return {
        "character": _s1_character(a, b),
        "data_type": _s2_data_type(a, b),
        "semantic": _s3_semantic_pair(a, b, embed_cache),
        "statistical": _s4_statistical(a, b),
        "distribution": _s5_distribution(a, b),
    }


def _pair_scores_fuzzy_semantic(a: ColumnFeature, b: ColumnFeature,
                                embed_cache: dict[str, list[float]]) -> dict[str, Optional[float]]:
    """schema_dna's Fuzzy/Semantic blend: 0.5 * fuzzy-string + 0.5 * embedding.

    Overall lives on character (fuzzy) + semantic (vector); type is still real,
    but the instance axes stay None (this variant never touches S4/S5).
    """
    schema = SequenceMatcher(None, norm(a.name), norm(b.name)).ratio()
    vector = _s3_semantic_pair(a, b, embed_cache)
    return {
        "character": schema,
        "data_type": _s2_data_type(a, b),
        "semantic": vector,
        "statistical": None,
        "distribution": None,
    }


def _overall_fuzzy(scores: dict[str, Optional[float]]) -> int:
    # schema_dna weights the fuzzy + vector halves equally.
    schema = scores.get("character") or 0.0
    vector = scores.get("semantic") or 0.0
    return round(100 * (0.5 * schema + 0.5 * vector))


_VARIANTS = {
    "feature_based": (_pair_scores_feature_based, _overall),
    "fuzzy_semantic": (_pair_scores_fuzzy_semantic, _overall_fuzzy),
    # llm_assisted has no bundled skill wiring here; it falls back to
    # feature_based so the endpoint never hard-fails. Wire the SDK skill in the
    # router (see ingest_products._run_classifier_skill) to override.
    "llm_assisted": (_pair_scores_feature_based, _overall),
}

VARIANT_NAMES = list(_VARIANTS.keys())
DEFAULT_VARIANT = "feature_based"

# Below this overall score a source column has no credible counterpart — it's
# reported as unmatched (a gap) rather than force-bound to a best-of-bad target.
# 70 sits above BGE's short-string noise floor (unrelated column names on the
# same datatype tend to land in the 55–68 band) while still admitting genuine
# abbreviation matches like acct_no → account_number (~77).
MATCH_THRESHOLD = 70


def available_embeddings() -> bool:
    """True iff the BGE-small embedding model is loadable (semantic axis is
    real rather than the token-Jaccard fallback)."""
    return embeddings.available()


def text_similarity(a: str, b: str,
                    cache: Optional[dict[str, list[float]]] = None) -> int:
    """Coarse 0–100 similarity between two free-text blobs.

    Embedding cosine × 100 when the BGE-small model is available, else
    token-Jaccard × 100 — mirroring :func:`_s3_semantic_pair`'s embed-then-
    jaccard-fallback. The 0–100 scale matches :data:`MATCH_THRESHOLD` so callers
    can compare against the same levers. ``cache`` (text → vector) lets a caller
    precompute embeddings once per run and reuse them across many pairs; a missing
    entry is embedded on the fly.
    """
    a = a or ""
    b = b or ""
    if not a.strip() or not b.strip():
        return 0
    if available_embeddings():
        va = (cache or {}).get(a)
        vb = (cache or {}).get(b)
        if va is None or vb is None:
            vecs = embeddings.embed_documents([a, b])
            if len(vecs) == 2:
                va, vb = vecs[0], vecs[1]
        if va is not None and vb is not None:
            return round(100 * _cosine(va, vb))
    return round(100 * token_jaccard(a, b))


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def _build_embed_cache(cols: list[ColumnFeature],
                       seed: Optional[dict[str, list[float]]] = None) -> dict[str, list[float]]:
    """Build the text→vector cache for a set of columns.

    ``seed`` pre-populates the cache with vectors a caller already has (e.g. the
    estate column vectors persisted on ``:EstateColumn`` and read back by
    feasibility) — those texts are NOT re-embedded; only the genuinely-missing
    texts (typically the small spec-attribute set) hit the model. Seeded entries
    survive even when the model is unavailable at call time, so a fully-seeded run
    needs no live model on the estate side.
    """
    cache: dict[str, list[float]] = dict(seed or {})
    texts = sorted({c.semantic_text() for c in cols if c.semantic_text()})
    missing = [t for t in texts if t not in cache]
    if missing:
        vecs = _embed_texts(missing)
        if vecs and len(vecs) == len(missing):
            cache.update({t: v for t, v in zip(missing, vecs)})
    return cache


def compare_columns(source: list[ColumnFeature], target: list[ColumnFeature],
                    variant: str = DEFAULT_VARIANT) -> dict[str, Any]:
    """Match each source column to its best target attribute.

    Returns ``{variant, embeddings_available, matches: [...], aggregate: {...}}``
    where each match and the aggregate carry the ``ProductSimilarity`` shape
    (``title``, ``match``, ``dimensions[]``).
    """
    variant = variant if variant in _VARIANTS else DEFAULT_VARIANT
    pair_fn, overall_fn = _VARIANTS[variant]
    embed_cache = _build_embed_cache(list(source) + list(target))

    matches: list[dict[str, Any]] = []
    axis_acc: dict[str, list[float]] = {k: [] for k, _ in _AXIS_META}
    overall_acc: list[int] = []

    for s in source:
        best: Optional[dict[str, Any]] = None
        best_overall = -1
        for t in target:
            scores = pair_fn(s, t, embed_cache)
            ov = overall_fn(scores)
            if ov > best_overall:
                best_overall = ov
                best = {
                    "source": {"name": s.name, "type": s.type},
                    "target": {"name": t.name, "type": t.type, "concept": t.concept},
                    "title": f"{s.name} → {t.name}",
                    "match": ov,
                    "dimensions": _dimensions(scores, s, t),
                    "_scores": scores,
                }
        if best is None:
            # No target at all → an explicit gap, scored 0.
            matches.append({
                "source": {"name": s.name, "type": s.type},
                "target": None, "title": f"{s.name} → (no candidate)",
                "match": 0, "matched": False, "dimensions": [],
                "unmatched_reason": "No target attribute to compare against.",
            })
            continue
        best["matched"] = best["match"] >= MATCH_THRESHOLD
        if not best["matched"]:
            # Best candidate is below the match bar → report as a gap. Its real
            # per-axis scores stay on the row so the UI can explain WHY (e.g.
            # same datatype but low semantic overlap), but it does NOT feed the
            # aggregate — that would inflate a "match" from a non-match.
            best["unmatched_reason"] = (
                f"Best candidate '{best['target']['name']}' scores {best['match']}/100 — "
                f"below the {MATCH_THRESHOLD} match threshold."
            )
        else:
            for d in best["dimensions"]:
                if d["value"] is not None:
                    axis_acc[d["key"]].append(d["value"])
            overall_acc.append(best["match"])
        best.pop("_scores", None)
        matches.append(best)

    matched_count = sum(1 for m in matches if m.get("matched"))

    # Reasons for aggregate axes that never got a value, grounded in the data.
    any_profiled = any(c.profile for c in source) and any(c.profile for c in target)
    agg_reason = {
        "statistical": (
            "No matched column pairs are profiled numeric columns — S4 needs numeric instance stats."
            if any_profiled else
            "None of the compared columns carry profiling data — S4 numeric instance stats unavailable."
        ),
        "distribution": (
            "No matched column pairs have value-distribution profiling — S5 needs instance stats."
            if any_profiled else
            "None of the compared columns carry profiling data — S5 distribution stats unavailable."
        ),
    }
    # Coverage-weight every measurable axis the SAME way the headline is weighted:
    # divide the matched-column axis sum by the TOTAL column count so unmatched
    # columns count as 0. Otherwise the radar shape reads full (Data Type/Character/
    # Semantic = 100 on the 2 that matched) while the headline says 25 — a self-
    # contradiction. Now the radar shape and the Match score agree.
    total = len(matches)
    agg_dims = []
    for key, label in _AXIS_META:
        vals = axis_acc[key]
        d: dict[str, Any] = {
            "key": key,
            "label": label,
            "value": round(sum(vals) / total) if (vals and total) else None,
            "hint": f"Average {label.lower()} across all {total} attribute(s) — unmatched score 0.",
        }
        if not vals:
            d["reason"] = agg_reason.get(key, "Not measured across the matched attributes.")
        agg_dims.append(d)
    # Coverage-weighted overall match: the headline score must reflect HOW MUCH of
    # the schema lines up, not just how well the few that matched scored. Dividing
    # the matched-score sum by TOTAL columns (not just the matched ones) makes the
    # unmatched columns count as 0 — so "2 of 8 matched" reads ~25, not 100. Without
    # this, a selection where only 2 of 8 columns align perfectly showed Match: 100.
    aggregate = {
        "title": f"{matched_count} of {len(matches)} attribute(s) matched",
        "match": round(sum(overall_acc) / len(matches)) if matches else 0,
        "dimensions": agg_dims,
        "matched_count": matched_count,
        "total": len(matches),
    }

    return {
        "variant": variant,
        "embeddings_available": embeddings.available(),
        "match_threshold": MATCH_THRESHOLD,
        "matched_count": matched_count,
        "unmatched_count": len(matches) - matched_count,
        "matches": matches,
        "aggregate": aggregate,
    }


def best_match(source: ColumnFeature, target: list[ColumnFeature],
               variant: str = DEFAULT_VARIANT) -> Optional[dict[str, Any]]:
    """Convenience: best single match for one source column (marketplace use)."""
    res = compare_columns([source], target, variant)
    return res["matches"][0] if res["matches"] else None


def score_matrix(source: list[ColumnFeature], target: list[ColumnFeature],
                 variant: str = DEFAULT_VARIANT,
                 embed_cache: Optional[dict[str, list[float]]] = None) -> dict[str, Any]:
    """Full N×M pairwise overall-score matrix + per-pair axes.

    Unlike :func:`compare_columns` (which returns the single best target per
    source and so lets ONE target satisfy several sources — the double-counting
    the feasibility evaluator must avoid), this returns EVERY pair so a caller can
    run a one-to-one (bipartite) assignment of spec attributes → candidate columns.

    Returns ``{embeddings_available, match_threshold, cells}`` where
    ``cells[i][j] = {"overall": int, "axes": {axis: pct|None}}`` for
    ``source[i]`` × ``target[j]``. The embedding cache is built once over the union;
    pass ``embed_cache`` (text→vector) to seed it with vectors already on hand —
    typically the estate column vectors persisted at enrichment time — so only the
    missing (spec-side) texts are embedded during a run.
    """
    variant = variant if variant in _VARIANTS else DEFAULT_VARIANT
    pair_fn, overall_fn = _VARIANTS[variant]
    embed_cache = _build_embed_cache(list(source) + list(target), seed=embed_cache)
    cells: list[list[dict[str, Any]]] = []
    for s in source:
        row: list[dict[str, Any]] = []
        for t in target:
            scores = pair_fn(s, t, embed_cache)
            row.append({
                "overall": overall_fn(scores),
                "axes": {k: _pct(scores.get(k)) for k, _ in _AXIS_META},
            })
        cells.append(row)
    return {
        "embeddings_available": embeddings.available(),
        "match_threshold": MATCH_THRESHOLD,
        "cells": cells,
    }


# --------------------------------------------------------------------------
# Scorecard — lightweight port of sda_robustness_test + comparison_report
# --------------------------------------------------------------------------


def _abbreviate(name: str) -> str:
    return re.sub(r"[aeiou]", "", name) if len(name) > 4 else name


_MUTATIONS: dict[str, Any] = {
    "baseline": lambda n: n,
    "abbreviated": _abbreviate,
    "camelCase": lambda n: re.sub(r"_([a-z])", lambda m: m.group(1).upper(), n),
    "snake_case": lambda n: _CAMEL_RE.sub(r"\1_\2", n).lower(),
    "uppercase": lambda n: n.upper(),
}


def _predict(source: list[ColumnFeature], target: list[ColumnFeature],
             variant: str, threshold: int) -> dict[str, Optional[str]]:
    res = compare_columns(source, target, variant)
    out: dict[str, Optional[str]] = {}
    for m in res["matches"]:
        src = m["source"]["name"]
        out[src] = m["target"]["name"] if m["match"] >= threshold else None
    return out


def _prf(predicted: dict[str, Optional[str]], truth: dict[str, Optional[str]]) -> dict[str, float]:
    tp = fp = fn = 0
    for src, gold in truth.items():
        pred = predicted.get(src)
        if gold is None:
            if pred is not None:
                fp += 1
        else:
            if pred == gold:
                tp += 1
            elif pred is None:
                fn += 1
            else:
                fp += 1
                fn += 1
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"precision": round(precision, 3), "recall": round(recall, 3),
            "f1": round(f1, 3), "tp": tp, "fp": fp, "fn": fn}


def scorecard(source: list[ColumnFeature], target: list[ColumnFeature],
              ground_truth: dict[str, Optional[str]],
              variants: Optional[list[str]] = None,
              threshold: int = 60) -> dict[str, Any]:
    """Run each variant over the (possibly mutated) source columns and score
    against ground truth. Returns per-variant baseline F1 + per-mutation F1 so a
    caller can see which variant holds up best. Lightweight JSON analogue of
    schema_dna's robustness comparison report."""
    variants = variants or VARIANT_NAMES
    by_name = {c.name: c for c in source}
    rows: list[dict[str, Any]] = []
    for variant in variants:
        per_mutation: dict[str, dict[str, float]] = {}
        for mut_name, fn in _MUTATIONS.items():
            mutated = [
                ColumnFeature(name=fn(c.name), type=c.type, description=c.description,
                              concept=c.concept, profile=c.profile)
                for c in source
            ]
            # Ground truth is keyed by ORIGINAL name; remap to mutated names.
            name_map = {orig: fn(orig) for orig in by_name}
            mutated_truth = {name_map[orig]: gold for orig, gold in ground_truth.items()
                             if orig in name_map}
            predicted = _predict(mutated, target, variant, threshold)
            per_mutation[mut_name] = _prf(predicted, mutated_truth)
        baseline_f1 = per_mutation.get("baseline", {}).get("f1", 0.0)
        avg_f1 = round(sum(m["f1"] for m in per_mutation.values()) / len(per_mutation), 3)
        rows.append({
            "variant": variant,
            "baseline_f1": baseline_f1,
            "avg_f1": avg_f1,
            "per_mutation": per_mutation,
        })
    rows.sort(key=lambda r: (r["avg_f1"], r["baseline_f1"]), reverse=True)
    return {
        "embeddings_available": embeddings.available(),
        "threshold": threshold,
        "mutations": list(_MUTATIONS.keys()),
        "best_variant": rows[0]["variant"] if rows else None,
        "results": rows,
    }
