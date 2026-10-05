import type { ProductSimilarity, SampleColumn } from "../types";
import api from "../api/client";

/**
 * Shared basic similarity heuristics for the Pipeline DAG V2 radar surfaces.
 *
 * Three radars, one shape (ProductSimilarity):
 *  • node-level    — 2+ selected data-bearing nodes compared to each other
 *                    (LineageExplorer right panel)
 *  • product-level — the selection vs the reference data product it would
 *                    consolidate into (DAG what-if preview popup)
 *  • attribute-level — one source column vs the product attribute it matched
 *                    (ModernizePanel hover tooltip)
 *
 * PREFER the backend schema-DNA scorer (`fetchColumnSimilarity`) — it returns
 * embedding-backed semantic scores plus real instance-stat axes when profiling
 * is present. The sync heuristics below remain only as an OFFLINE FALLBACK when
 * the endpoint is unavailable; they emit `null` (not a faked value) for the
 * instance axes (statistical/distribution) so nothing is fabricated.
 */

export type Variant = "feature_based" | "fuzzy_semantic" | "llm_assisted";

interface CompareCol { name: string; type?: string; description?: string; concept?: string; profile?: Record<string, number | boolean> }

/**
 * Per-source-column result from the schema-DNA scorer: the best target it found
 * (or null), the match score, whether it cleared the match threshold, and — when
 * it didn't — the reason. Carries the ProductSimilarity dimensions so a row can be
 * explained axis-by-axis. This is what lets a caller show WHICH columns matched
 * and WHY the rest didn't, instead of only the aggregate.
 */
export interface AttrMatch extends ProductSimilarity {
  source: { name: string; type: string };
  target: { name: string; type: string; concept?: string } | null;
  matched: boolean;
  unmatched_reason?: string;
}

/**
 * Backend schema-DNA comparison — the real scorer. Returns the aggregate
 * ProductSimilarity plus per-attribute matches. Throws on network error so the
 * caller can fall back to the sync heuristics.
 */
export async function fetchColumnSimilarity(
  projectId: number | string,
  sourceColumns: CompareCol[],
  target: { modelId?: string; columns?: CompareCol[] },
  variant: Variant = "feature_based",
): Promise<{ aggregate: ProductSimilarity; matches: AttrMatch[]; embeddings_available: boolean }> {
  const { data } = await api.post(`/api/projects/${projectId}/discovery/compare`, {
    source_columns: sourceColumns,
    target_model_id: target.modelId ?? null,
    target_columns: target.columns ?? null,
    variant,
  });
  return {
    aggregate: { ...data.aggregate, variant: data.variant, embeddings_available: data.embeddings_available },
    matches: data.matches,
    embeddings_available: data.embeddings_available,
  };
}

export interface RankRow {
  model_id: string;
  model_name: string;
  score: number;
  matched_count: number;
  product_coverage: number;
  aggregate: ProductSimilarity;
}

/**
 * Backend schema-DNA candidate RANKING — scores the source columns against
 * every reference model with the chosen scorer variant. This is what makes the
 * candidate ranking (not just the selected model's detail) re-rank when the
 * scorer changes. Throws on network error so the caller can fall back offline.
 */
export async function fetchRank(
  projectId: number | string,
  sourceColumns: CompareCol[],
  variant: Variant = "feature_based",
): Promise<{ ranked: RankRow[]; embeddings_available: boolean }> {
  const { data } = await api.post(`/api/projects/${projectId}/discovery/compare/rank`, {
    source_columns: sourceColumns,
    variant,
  });
  return { ranked: data.ranked || [], embeddings_available: data.embeddings_available };
}

export const norm = (s: string) => s.trim().toLowerCase();

// Alphabet-set (Jaccard) overlap of two identifiers — a cheap "character"
// similarity that ignores order/length, useful for text-ish column names.
export function charJaccard(a: string, b: string) {
  const A = new Set(a.toLowerCase().replace(/[^a-z0-9]/g, ""));
  const B = new Set(b.toLowerCase().replace(/[^a-z0-9]/g, ""));
  if (!A.size && !B.size) return 1;
  let inter = 0; A.forEach((c) => { if (B.has(c)) inter++; });
  return inter / (new Set([...A, ...B]).size || 1);
}

// Word-token overlap of snake/camel-case identifiers — the "semantic" signal
// (shared vocabulary like customer/account/id) without a real embedding.
export function tokenJaccard(a: string, b: string) {
  const toks = (s: string) => new Set(
    s.replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase().split(/[^a-z0-9]+/).filter((t) => t.length > 1),
  );
  const A = toks(a), B = toks(b);
  if (!A.size && !B.size) return 1;
  let inter = 0; A.forEach((t) => { if (B.has(t)) inter++; });
  return inter / (new Set([...A, ...B]).size || 1);
}

// Coarse type family so int/bigint/decimal read as "same kind of data".
export function typeFamily(t: string): string {
  const s = norm(t);
  if (/int|long|number|numeric|decimal|float|double|real|money/.test(s)) return "numeric";
  if (/char|text|string|clob/.test(s)) return "text";
  if (/date|time|stamp/.test(s)) return "temporal";
  if (/bool|bit/.test(s)) return "boolean";
  return s || "unknown";
}

const pct = (x: number) => Math.round(100 * Math.max(0, Math.min(1, x)));

export interface ColumnLike { name: string; type: string }

/**
 * Attribute-level: one source column vs the product attribute it matched.
 * Explains a single match score (the hover tooltip).
 */
export function columnPairSimilarity(
  src: ColumnLike, attr: ColumnLike, concept: string | undefined, match: number,
): ProductSimilarity {
  // Real, deterministic type agreement: identical = 100, same family = 75,
  // otherwise 40. (Was a pseudo-random hashBand — that's what made an identical
  // type read as e.g. 94 instead of 100.)
  const dataType = norm(src.type) === norm(attr.type) ? 100
    : typeFamily(src.type) === typeFamily(attr.type) ? 75
    : 40;
  const character = pct(charJaccard(src.name, attr.name));
  const semantic = pct(tokenJaccard(src.name, attr.name));
  // Statistical/Distribution need value-level profiling we don't have in the
  // offline path — report them as unknown (null) rather than faking a number.
  return {
    title: `${src.name} vs ${attr.name}`,
    match,
    dimensions: [
      { key: "data_type", label: "Data Type", value: dataType, hint: `${src.type} vs ${attr.type}` },
      { key: "character", label: "Character", value: character, hint: "Alphabet-set overlap of the two names." },
      { key: "semantic", label: "Semantic", value: semantic, hint: concept ? `Shared concept [${concept}].` : "Word-token overlap of the two names." },
      { key: "distribution", label: "Distribution", value: null, hint: "Not profiled — value distribution unavailable offline." },
      { key: "statistical", label: "Statistical", value: null, hint: "Not profiled — value stats unavailable offline." },
    ],
  };
}

/**
 * Node-level: how alike 2+ selected data-bearing nodes are (column overlap,
 * type consistency, naming). Pairwise for two nodes; for more, every node is
 * compared against the union of the others and the dimensions are averaged.
 */
// Minimal structural shape the node-level heuristic needs — any data-bearing
// estate object (EstateObject variant or the graph's EGNode) satisfies it, so
// this stays decoupled from the strict EstateObject union.
export interface NodeLike { name?: string; object_name?: string; sample_columns?: SampleColumn[] }

export function nodesSimilarity(nodes: NodeLike[]): ProductSimilarity | null {
  const withCols = nodes.filter((o) => (o.sample_columns?.length ?? 0) > 0);
  if (withCols.length < 2) return null;

  // Every column is paired with its BEST counterpart in the other nodes (by
  // name-token overlap), whatever that best is — so two unrelated tables still
  // produce a radar, just a low-scoring one. "Overlapping" (>= .34 token
  // similarity or an exact name hit) drives the type/coverage dimensions.
  const OVERLAP = 0.34;
  let total = 0, overlapping = 0;
  let dataTypeAcc = 0, charAcc = 0, semAcc = 0;
  withCols.forEach((o, oi) => {
    const others = withCols.filter((_, i) => i !== oi).flatMap((x) => x.sample_columns || []);
    const byName = new Map(others.map((c) => [norm(c.name), c]));
    (o.sample_columns || []).forEach((c) => {
      total++;
      let mate = byName.get(norm(c.name)) || null;
      let sim = mate ? 1 : 0;
      if (!mate) {
        others.forEach((x) => { const t = tokenJaccard(c.name, x.name); if (t > sim) { sim = t; mate = x; } });
      }
      if (!mate) return;
      charAcc += charJaccard(c.name, mate.name);
      semAcc += tokenJaccard(c.name, mate.name);
      if (sim >= OVERLAP) {
        overlapping++;
        dataTypeAcc += norm(c.type) === norm(mate.type) ? 1 : typeFamily(c.type) === typeFamily(mate.type) ? 0.7 : 0.2;
      }
    });
  });
  if (!total) return null;

  const allNames = new Set(withCols.flatMap((o) => (o.sample_columns || []).map((c) => norm(c.name))));
  const overlapCols = Math.round(overlapping / 2); // each match counted from both sides
  const dims = [
    { key: "data_type", label: "Data Type", value: overlapping ? pct(dataTypeAcc / overlapping) : 0, hint: overlapping ? "Type agreement across the overlapping columns." : "No overlapping columns to compare types on." },
    { key: "character", label: "Character", value: pct(charAcc / total), hint: "Alphabet-set similarity of best-matched column names." },
    { key: "semantic", label: "Semantic", value: pct(semAcc / total), hint: "Shared name vocabulary across the columns." },
    { key: "distribution", label: "Distribution", value: pct(overlapCols / (allNames.size || 1)), hint: "Column-set overlap — how much of the combined schema is shared." },
    { key: "statistical", label: "Statistical", value: null, hint: "Not profiled — value similarity unavailable offline." },
  ];
  const known = dims.map((d) => d.value).filter((v): v is number => v !== null);
  const match = known.length ? Math.round(known.reduce((s, v) => s + v, 0) / known.length) : 0;
  const names = withCols.map((o) => o.name || o.object_name);
  return {
    title: withCols.length === 2 ? `${names[0]} vs ${names[1]}` : `${withCols.length} selected sources`,
    subtitle: `${overlapCols} overlapping column${overlapCols !== 1 ? "s" : ""} · ${allNames.size} distinct`,
    match,
    dimensions: dims,
  };
}
