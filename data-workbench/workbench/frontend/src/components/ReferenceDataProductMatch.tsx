import { useMemo, useState } from "react";
import type { ClusterProposal, EstateObject, ReportObject, ReferenceModel } from "../types";
import { columnsOf } from "../types";

/**
 * ReferenceDataProductMatch — the report-selection analysis, NOT a panel.
 *
 * Rendered in place of ModernizePanel when the selection is report nodes. It
 * exists because reports carry ATTRIBUTES (`sample_columns`) rather than the
 * `columnsOf` columns ModernizePanel's schema-DNA comparison consumes.
 *
 * NOTE: the reference-data-product ranking + propose below duplicates
 * ModernizePanel's own left rail, using exact attribute-name overlap instead of
 * the backend schema-DNA scorer. Folding this into ModernizePanel as a report
 * mode would remove the duplication and give reports the better scorer.
 *
 * Driven by the current report selection:
 *  1. Report detail cards (like a consumer registry) — each selected report's
 *     type / access method / owner, and the columns it reads grouped by the
 *     source table that provides them.
 *  2. Reference-model matches — the report attributes (unioned across the
 *     selection) scored against each canonical reference data product model by
 *     shared attribute name; ranked, best first.
 *  3. Propose — pick the winning reference model and propose a data product
 *     from it: the source objects whose columns fit the model are auto-selected
 *     (no manual view/table picking) and handed to the existing propose flow.
 */

const norm = (s: string) => s.trim().toLowerCase();
const CARD = { border: "1px solid #e2e8f0", borderRadius: 10, background: "#fff", padding: "12px 14px" };

interface Props {
  selectedIds: string[];
  objById: Record<string, EstateObject>;
  edges: [string, string][];
  referenceModels: ReferenceModel[];
  onProposeProduct?: (ids: string[]) => Promise<ClusterProposal>;
  onCreateProduct?: (ids: string[], proposal?: ClusterProposal) => void | Promise<void>;
}

export default function ReferenceDataProductMatch({ selectedIds, objById, edges, referenceModels, onProposeProduct, onCreateProduct }: Props) {
  const [chosen, setChosen] = useState<string | null>(null);
  const [proposal, setProposal] = useState<ClusterProposal | null>(null);
  const [busy, setBusy] = useState(false);

  const model = useMemo(() => {
    const bwd: Record<string, string[]> = {};
    edges.forEach(([a, b]) => { (bwd[b] = bwd[b] || []).push(a); });
    const real = (id: string) => !!objById[id] && !id.startsWith("db_");
    const isPass = (id: string) => objById[id]?.type === "query";
    // Nearest data-bearing source tables/views upstream of a report (hop over
    // query/job nodes).
    const upstreamSources = (start: string) => {
      const out = new Set<string>(); const seen = new Set<string>();
      const stack = [...(bwd[start] || [])];
      while (stack.length) {
        const n = stack.pop()!;
        if (seen.has(n)) continue; seen.add(n);
        if (!real(n)) continue;
        if (isPass(n)) { (bwd[n] || []).forEach((m) => stack.push(m)); continue; }
        out.add(n);
      }
      return [...out];
    };

    const reports = selectedIds.map((id) => objById[id]).filter((o): o is ReportObject => !!o && o.type === "report");

    // Per-report card: group the report's columns by the source table providing
    // them (name-match against each upstream source's columns).
    const cards = reports.map((r) => {
      const cols = r.sample_columns || [];
      const srcs = upstreamSources(r.id).map((id) => objById[id]).filter(Boolean);
      const groups: { table: string; cols: { name: string; type: string }[] }[] = [];
      const claimed = new Set<string>();
      srcs.forEach((s) => {
        const sc = new Set(columnsOf(s).map((c) => norm(c.name)));
        const g = cols.filter((c) => sc.has(norm(c.name)));
        if (g.length) { groups.push({ table: s.name || s.object_name, cols: g }); g.forEach((c) => claimed.add(norm(c.name))); }
      });
      const rest = cols.filter((c) => !claimed.has(norm(c.name)));
      if (rest.length) groups.push({ table: r.name || r.object_name, cols: rest });
      return { report: r, groups, tableCount: groups.length, colCount: cols.length };
    });

    // Union of report attributes across the selection.
    const attrs = new Map<string, string>();
    reports.forEach((r) => (r.sample_columns || []).forEach((c) => { if (!attrs.has(norm(c.name))) attrs.set(norm(c.name), c.name); }));

    // Score each reference model by shared attribute name.
    const matches = referenceModels.map((m) => {
      const matched = m.attributes.filter((a) => attrs.has(norm(a.name)));
      return { model: m, matched, coverage: m.attributes.length ? matched.length / m.attributes.length : 0 };
    }).filter((x) => x.matched.length > 0)
      .sort((a, b) => b.matched.length - a.matched.length || b.coverage - a.coverage);

    return { reports, cards, matches, attrCount: attrs.size, upstreamSources };
  }, [selectedIds, objById, edges, referenceModels]);

  const propose = async (modelId: string) => {
    const match = model.matches.find((x) => x.model.id === modelId);
    if (!match || !onProposeProduct) return;
    // Auto-select the source objects whose columns feed this reference model:
    // union the selected reports' upstream source tables, keep those that
    // contribute at least one of the model's attributes.
    const modelAttrs = new Set(match.model.attributes.map((a) => norm(a.name)));
    const srcIds = new Set<string>();
    model.reports.forEach((r) => model.upstreamSources(r.id).forEach((sid) => {
      const s = objById[sid];
      const contributes = !!s && columnsOf(s).some((c) => modelAttrs.has(norm(c.name)));
      if (contributes) srcIds.add(sid);
    }));
    const ids = [...srcIds];
    if (!ids.length) return;
    setChosen(modelId); setBusy(true); setProposal(null);
    try { setProposal(await onProposeProduct(ids)); } finally { setBusy(false); }
  };

  const openWizard = () => {
    if (!chosen || !proposal) return;
    const match = model.matches.find((x) => x.model.id === chosen);
    const modelAttrs = new Set((match?.model.attributes || []).map((a) => norm(a.name)));
    const srcIds = new Set<string>();
    model.reports.forEach((r) => model.upstreamSources(r.id).forEach((sid) => {
      const s = objById[sid];
      if (s && columnsOf(s).some((c) => modelAttrs.has(norm(c.name)))) srcIds.add(sid);
    }));
    onCreateProduct?.([...srcIds], proposal);
  };

  if (!model.reports.length) {
    return (
      <div style={{ ...CARD, color: "#94a3b8", fontSize: 13, textAlign: "center", padding: "28px 14px" }}>
        Select one or more <b style={{ color: "#64748b" }}>reports</b> in the DAG above to inspect what they consume and match them to a reference data product model.
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      {/* Report detail cards */}
      {model.cards.map(({ report, groups, tableCount, colCount }) => (
        <div key={report.id} style={CARD}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
            <div style={{ width: 34, height: 34, borderRadius: 8, background: "#fce7f3", color: "#be185d", display: "flex", alignItems: "center", justifyContent: "center", fontWeight: 800 }}>
              {(report.name || "?")[0]}
            </div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontWeight: 700, fontSize: 15, color: "#1e293b" }}>{report.name || report.object_name}</div>
              <div style={{ fontSize: 12, color: "#94a3b8" }}>UserID: {report.user_id || "—"}</div>
            </div>
            {report.consumer_kind && <Chip text={report.consumer_kind} tone="violet" />}
            {report.access_method && <Chip text={report.access_method} tone="red" />}
            <span style={{ fontSize: 12, color: "#64748b" }}>{tableCount} table{tableCount !== 1 ? "s" : ""} · {colCount} cols</span>
          </div>
          <div style={{ marginTop: 10, display: "flex", flexDirection: "column", gap: 8 }}>
            {groups.map((g) => (
              <div key={g.table}>
                <div style={{ fontSize: 12, fontWeight: 700, color: "#4338ca", fontFamily: "ui-monospace, monospace", marginBottom: 5 }}>{g.table}</div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                  {g.cols.map((c) => (
                    <span key={c.name} title={`${c.name} ${c.type}`} style={{ fontSize: 11.5, fontFamily: "ui-monospace, monospace", padding: "3px 9px", borderRadius: 999, background: M.bg, border: `1px solid ${M.pill}`, color: M.deep }}>
                      {c.name}
                    </span>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      ))}

      {/* Reference-model matches */}
      <div style={CARD}>
        <div style={{ fontSize: 12, fontWeight: 700, color: "#334155", textTransform: "uppercase", letterSpacing: 0.4, marginBottom: 8 }}>
          Related reference data products
        </div>
        {model.matches.length === 0 && <div style={{ fontSize: 12.5, color: "#94a3b8" }}>No reference model shares attributes with this selection.</div>}
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {model.matches.map(({ model: m, matched, coverage }, i) => {
            const best = i === 0;
            const active = chosen === m.id;
            return (
              <div key={m.id} style={{ border: `1px solid ${active ? P.fg : best ? P.border : "#e2e8f0"}`, borderRadius: 8, padding: "10px 12px", background: best ? P.wash : "#fff" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                  <span style={{ fontWeight: 700, fontSize: 13.5, color: "#1e293b" }}>{m.name}</span>
                  <span style={{ fontSize: 11, color: "#64748b" }}>{m.domain} · {m.kind}</span>
                  {best && <Chip text="Best fit" tone="violet" />}
                  <span style={{ marginLeft: "auto", fontSize: 11.5, fontWeight: 700, color: P.fg }}>
                    {matched.length}/{m.attributes.length} attrs · {Math.round(coverage * 100)}%
                  </span>
                </div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 5, marginTop: 7 }}>
                  {m.attributes.map((a) => {
                    const hit = matched.some((x) => x.name === a.name);
                    return (
                      <span key={a.name} title={`${a.name} ${a.type}`} style={{ fontSize: 11, fontFamily: "ui-monospace, monospace", padding: "2px 8px", borderRadius: 999, border: `1px solid ${hit ? P.border : "#e2e8f0"}`, background: hit ? P.bg : "#f8fafc", color: hit ? P.deep : "#cbd5e1", fontWeight: hit ? 700 : 400 }}>
                        {a.name}
                      </span>
                    );
                  })}
                </div>
                <div style={{ marginTop: 9, display: "flex", alignItems: "center", gap: 10 }}>
                  <button onClick={() => propose(m.id)} disabled={busy || !onProposeProduct}
                    style={{ fontSize: 12, fontWeight: 700, color: "#fff", background: P.fg, border: "none", borderRadius: 7, padding: "6px 12px", cursor: busy ? "default" : "pointer", opacity: busy && active ? 0.7 : 1 }}>
                    {busy && active ? "Composing…" : `✦ Propose data product from this model`}
                  </button>
                  {active && proposal && (
                    <span style={{ fontSize: 12, color: "#475569" }}>
                      Proposed: <b>{proposal.name}</b>
                    </span>
                  )}
                </div>
                {active && proposal && (
                  <div style={{ marginTop: 8, background: P.wash, border: `1px solid ${P.border}`, borderRadius: 7, padding: "9px 11px" }}>
                    <div style={{ fontSize: 12, color: "#4c1d95", marginBottom: 4 }}>{proposal.idea || proposal.purpose}</div>
                    <div style={{ fontSize: 11, color: P.fg }}>{(proposal.columns || []).length} columns · sources auto-selected from the estate that fit this model</div>
                    <button onClick={openWizard} style={{ marginTop: 8, fontSize: 12, fontWeight: 700, color: P.fg, background: "#fff", border: `1px solid ${P.border}`, borderRadius: 7, padding: "6px 12px", cursor: "pointer" }}>
                      Open in wizard →
                    </button>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function Chip({ text, tone }: { text: string; tone: "violet" | "red" }) {
  const c = tone === "violet" ? { bg: P.bg, fg: P.fg, bd: P.border } : { bg: R.bg, fg: R.fg, bd: R.border };
  return (
    <span style={{ fontSize: 11, fontWeight: 700, padding: "3px 10px", borderRadius: 999, background: c.bg, color: c.fg, border: `1px solid ${c.bd}` }}>{text}</span>
  );
}
