// Chrome for the Estate Graph: top bar, legend, LOD ladder, detail rail.
// Ported from merge-chrome.jsx; selects are driven by the derived model
// (systems / types / domains) rather than a synthetic global ESTATE.
import { useEffect, useState } from "react";
import { Glyph } from "./EstateNode";
import ExtLinkIcon from "../ExtLinkIcon";
import type { EGNode, SystemDef, TypeDef, DomainDef, Tier } from "./estateModel";

interface TopBarProps {
  stats: [number | string, string][];
  mode: "overview" | "focused";
  q: string;
  setQ: (v: string) => void;
  fSys: string;
  setFSys: (v: string) => void;
  fType: string;
  setFType: (v: string) => void;
  fDom: string;
  setFDom: (v: string) => void;
  root: EGNode | null;
  onBack: () => void;
  onFit: () => void;
  onReset: () => void;
  /** Clear the four filter fields ONLY — camera and drill state stay put. */
  onClearFilters: () => void;
  /** Overview layout grouping — "none" = pure force layout. */
  clusterBy: "none" | "domain" | "system" | "type";
  onPickCluster: (k: "none" | "domain" | "system" | "type") => void;
  showAll: boolean;
  setShowAll: (v: boolean) => void;
  systems: Record<string, SystemDef>;
  types: Record<string, TypeDef>;
  domains: DomainDef[];
}

export function TopBar({
  stats, mode, q, setQ, fSys, setFSys, fType, setFType, fDom, setFDom,
  root, onBack, onFit, onReset, onClearFilters, clusterBy, onPickCluster, showAll, setShowAll, systems, types, domains,
}: TopBarProps) {
  const filtering = !!q || fSys !== "all" || fType !== "all" || fDom !== "all";
  return (
    <header className="topbar">
      <div className="tb-row">
        <div className="stats">
          {stats.map(([v, l]) => (
            <span className="stat" key={l}><b>{v}</b><i>{l}</i></span>
          ))}
        </div>
        <div className="tb-hint">
          {mode === "overview" ? (
            <span>Click to select · <b>click more to multi-select</b> (click again to deselect) · double-click to drill in</span>
          ) : (
            <span>Focused subgraph · <b>Esc</b> to return to the estate</span>
          )}
        </div>
        {root ? <span className="crumb">Focused: <b>{root.name}</b></span> : null}
      </div>
      <div className="tb-row tb-controls">
        <input className="search" placeholder="Search by name…" value={q} onChange={(e) => setQ(e.target.value)} />
        <select value={fSys} onChange={(e) => setFSys(e.target.value)}>
          <option value="all">All platforms</option>
          {Object.keys(systems).map((k) => <option key={k} value={k}>{systems[k].label}</option>)}
        </select>
        <select value={fType} onChange={(e) => setFType(e.target.value)}>
          <option value="all">All types</option>
          {Object.keys(types).map((k) => <option key={k} value={k}>{types[k].label}</option>)}
        </select>
        <select value={fDom} onChange={(e) => setFDom(e.target.value)}>
          <option value="all">All domains</option>
          {domains.map((d) => <option key={d.key} value={d.key}>{d.label}</option>)}
        </select>
        <label className="tb-toggle" title="On: show the whole estate with filter matches highlighted. Off: show only the objects matching the filters.">
          <input type="checkbox" checked={showAll} onChange={(e) => setShowAll(e.target.checked)} />
          Show filtered objects
        </label>
        {mode === "overview" ? (
          <select value={clusterBy} onChange={(e) => onPickCluster(e.target.value as "none" | "domain" | "system" | "type")}
            title="Group the overview layout by an attribute — None keeps a pure lineage-driven force layout">
            <option value="none">No clustering</option>
            <option value="domain">Cluster by domain</option>
            <option value="system">Cluster by platform</option>
            <option value="type">Cluster by type</option>
          </select>
        ) : null}
        {/* View-action buttons pushed to the right; filters stay left. */}
        <span style={{ marginLeft: "auto", display: "inline-flex", alignItems: "center", gap: 8 }}>
          {filtering ? (
            <button className="btn" onClick={onClearFilters} title="Clear the search + platform / type / domain filters without moving the view">
              Clear filters
            </button>
          ) : null}
          <button className="btn" onClick={onReset}>Reset view</button>
          {mode === "focused" ? (
            <>
              <button className="btn" onClick={onFit}>Fit subgraph</button>
              <button className="btn primary eg-btn-icon" onClick={onBack}>
                <svg width={13} height={13} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ flex: "none" }}>
                  <path d="M23 4v6h-6" />
                  <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
                </svg>
                Full estate
              </button>
            </>
          ) : null}
        </span>
      </div>
    </header>
  );
}

export function Legend({ collapsed, preview, systems, types }: { collapsed: boolean; preview: boolean; systems: Record<string, SystemDef>; types: Record<string, TypeDef> }) {
  const [open, setOpen] = useState(true);
  useEffect(() => { setOpen(!collapsed); }, [collapsed]);
  return (
    <div className="legend">
      <button className="legend-toggle" onClick={() => setOpen((o) => !o)}>
        Encoding<span>{open ? "−" : "+"}</span>
      </button>
      {open ? (
        <>
          {preview ? (
            <>
              <div className="legend-h">Proposed change</div>
              <div className="legend-list">
                <span className="lg-item"><i className="lg-line l-retire"></i>Retiring</span>
                <span className="lg-item"><i className="lg-line l-add"></i>Proposed</span>
                <span className="lg-item"><i className="lg-line l-none"></i>Unchanged</span>
              </div>
            </>
          ) : null}
          <div className="legend-h">Platform color</div>
          <div className="legend-list">
            {Object.keys(systems).map((k) => (
              <span className="lg-item" key={k}>
                <i className="lg-swatch" style={{ background: systems[k].color }}></i>
                {systems[k].label}
              </span>
            ))}
          </div>
          <div className="legend-h">Object shape</div>
          <div className="legend-list">
            {Object.keys(types).map((k) => (
              <span className="lg-item" key={k}>
                <Glyph shape={types[k].shape} color="var(--eg-muted)" size={13} />
                {types[k].label}
              </span>
            ))}
          </div>
        </>
      ) : null}
    </div>
  );
}

// Level-of-detail picker. Auto = zoom-driven (the default); clicking Glyph /
// Chip / Card pins that tier regardless of zoom. The active button is the tier
// currently on stage; Auto is also lit while in auto mode.
export function ZoomLadder({ tier, override, onPick }: { tier: Tier; override: Tier | null; onPick: (t: Tier | null) => void }) {
  const tiers: [Tier, string][] = [
    ["glyph", "Glyph"],
    ["chip", "Chip"],
    ["card", "Card"],
  ];
  const auto = override === null;
  return (
    <div className="ladder">
      {/* Auto is an on/off TOGGLE, not a dead label. ON = level of detail follows
          the zoom. Clicking it while ON freezes the CURRENT tier (pins whatever's
          on stage) so zooming no longer changes the node shape; clicking it while
          OFF (a tier pinned) resumes zoom-driven auto. When ON, the tier it resolves
          to shows a subtle "auto-here" marker instead of the solid pinned look. */}
      <button
        className={"ladder-step ladder-auto" + (auto ? " on" : "")}
        onClick={() => onPick(auto ? tier : null)}
        title={auto ? "Auto (on) — level of detail follows the zoom. Click to lock the current level." : "Auto (off) — level of detail is locked. Click to resume zoom-driven auto."}
      >
        <span className="ls-label">Auto</span>
      </button>
      <span className="ladder-sep" aria-hidden="true" />
      {tiers.map(([k, label]) => {
        const pinned = override === k;
        const autoHere = auto && tier === k;
        return (
          <button
            className={"ladder-step" + (pinned ? " on" : autoHere ? " auto-here" : "")}
            onClick={() => onPick(k)}
            key={k}
            title={auto ? `Pin the view to ${label.toLowerCase()}s (currently auto)` : `Always show ${label.toLowerCase()}s`}
          >
            <span className="ls-label">{label}</span>
          </button>
        );
      })}
    </div>
  );
}

// Solid button colour per recommendation (disposition) — matches the card
// disposition pills. Recommendation planning tints to the shared recommendation.
const DISP_BTN: Record<string, string> = {
  migrate: "#2563eb",   // blue
  modernize: "#7c3aed", // violet
  retire: "#dc2626",    // red
  remain: "#64748b",    // slate
};
function dispBtnStyle(disp: string | null): React.CSSProperties {
  const c = disp ? DISP_BTN[disp] : undefined;
  return c ? { background: c, borderColor: c } : {};
}

interface DetailProps {
  node: EGNode;
  up: number;
  down: number;
  mode: "overview" | "focused";
  onFocus: () => void;
  onClose: () => void;
  onOpenRow?: () => void;
  onPlan?: () => void;
  planCount?: number;
  planSameRec?: boolean;
  planDisp?: string | null;
  systems: Record<string, SystemDef>;
  types: Record<string, TypeDef>;
}

export function Detail({ node, up, down, mode, onFocus, onClose, onOpenRow, onPlan, planCount = 0, planSameRec = true, planDisp = null, systems, types }: DetailProps) {
  const sys = systems[node.system] || { label: node.system, color: "var(--eg-border-strong)" };
  const typeDef = types[node.type] || { label: node.type, shape: "square" };
  return (
    <aside className="detail">
      <button className="detail-close" onClick={onClose} aria-label="Close">×</button>
      <div className="detail-kicker">
        <span className="d-chip" style={{ "--sys": sys.color } as React.CSSProperties}>
          <Glyph shape={typeDef.shape} color={sys.color} size={12} />
          {typeDef.label}
        </span>
        <span className={"d-status s-" + node.status}>
          {node.status === "fresh" ? "Healthy" : node.status === "warn" ? "At risk" : "Stale"}
        </span>
      </div>
      <h2 className="detail-title">{node.name}</h2>
      <div className="detail-sub">{node.domainLabel} · {sys.label}</div>
      <div className="detail-lin">
        <span><b>{up}</b> upstream</span>
        <span><b>{down}</b> downstream</span>
      </div>
      {planCount > 1 && !planSameRec ? (
        <div className="detail-planhint">{planCount} objects selected with different recommendations · select objects that share one recommendation to plan them together</div>
      ) : planSameRec && planDisp === "remain" ? (
        <div className="detail-planhint">Out of scope — no recommendation planning.</div>
      ) : planCount > 1 && planDisp && planDisp !== "remain" ? (
        <div className="detail-planhint">{planCount} objects selected (same recommendation) · analyzed together</div>
      ) : null}
      {onPlan && planSameRec && planDisp && planDisp !== "remain" ? (
        <button className="btn primary wide eg-btn-icon" style={dispBtnStyle(planDisp)} onClick={onPlan}>
          Recommendation planning{planCount > 1 ? ` (${planCount})` : ""} <ExtLinkIcon />
        </button>
      ) : null}
      {mode === "overview" ? (
        <button className={"btn wide" + (onPlan ? "" : " primary")} onClick={onFocus}>Drill into lineage</button>
      ) : (
        <button className="btn wide" onClick={onFocus}>Recenter</button>
      )}
      {onOpenRow ? (
        <button className="btn wide eg-btn-icon" onClick={onOpenRow}>
          View in disposition table <ExtLinkIcon />
        </button>
      ) : null}
      <div className="detail-rows">
        {node.panel.rows.map(([k, v], i) => (
          <div className="drow" key={k + i}><span className="dk">{k}</span><span className="dv">{v}</span></div>
        ))}
      </div>
      <div className="detail-metric">{node.metric}</div>
    </aside>
  );
}
