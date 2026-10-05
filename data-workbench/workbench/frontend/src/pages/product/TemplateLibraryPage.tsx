import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import api from "../../api/client";
import ProductKindChip from "../../components/ProductKindChip";
import { usePrompt, useNotify } from "../../components/dialogContext";

/**
 * Blueprint Library — browse / filter / search the project-independent
 * data-product spec templates. The single source of truth every template
 * consumer (feasibility scan, wizard clone, Pulse) reads from. Never "catalog".
 */

interface TemplateSummary {
  id: string;
  name: string;
  domain: string;
  description: string;
  status: "draft" | "published";
  origin: "seed" | "clone" | "import" | "authored";
  owner_email: string;
  product_kind: string;
  cloned_from: string | null;
  version: number;
  tags: string[];
  sub_domain: string;
  source_spec_id: string;
  score?: number;
}

const ORIGIN_BADGE: Record<string, { label: string; bg: string; fg: string }> = {
  seed: { label: "Seed", bg: "#f1f5f9", fg: "#475569" },
  clone: { label: "Clone", bg: "#ede9fe", fg: "#6d28d9" },
  import: { label: "Imported", bg: "#e0f2fe", fg: "#0369a1" },
  authored: { label: "Authored", bg: "#dcfce7", fg: "#15803d" },
};

type StatusFilter = "published" | "draft";

export default function TemplateLibraryPage() {
  const navigate = useNavigate();
  const prompt = usePrompt();
  const { showError } = useNotify();

  const [rows, setRows] = useState<TemplateSummary[]>([]);
  const [domains, setDomains] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  const [domain, setDomain] = useState<string>("");
  const [status, setStatus] = useState<StatusFilter>("published");
  const [origin, setOrigin] = useState<string>("");
  const [productKind, setProductKind] = useState<string>("");
  const [subdomain, setSubdomain] = useState<string>("");
  const [query, setQuery] = useState<string>("");
  const [searchMode, setSearchMode] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const q = query.trim();
      if (q) {
        const r = await api.get("/api/templates/search", { params: { q, limit: 50 } });
        setRows(r.data.templates || []);
        setSearchMode(r.data.mode || null);
      } else {
        const r = await api.get("/api/templates", {
          params: {
            domain: domain || undefined,
            status,
            origin: origin || undefined,
            product_kind: productKind || undefined,
            subdomain: subdomain || undefined,
          },
        });
        setRows(r.data.templates || []);
        setSearchMode(null);
      }
    } catch (e) {
      showError(e, { title: "Failed to load templates" });
    }
    setLoading(false);
  }, [query, domain, status, origin, productKind, subdomain, showError]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    api.get("/api/templates/domains").then((r) => setDomains(r.data.domains || [])).catch(() => {});
  }, []);

  // Search results carry their own visibility; the chip filters only apply to
  // the browse path, so post-filter search rows client-side for consistency.
  const visible = useMemo(() => {
    if (!query.trim()) return rows;
    return rows.filter((r) =>
      (!domain || r.domain === domain) &&
      (!origin || r.origin === origin) &&
      (!productKind || r.product_kind === productKind) &&
      (status === "published" ? r.status === "published" : true)
    );
  }, [rows, query, domain, origin, productKind, status]);

  const createBlank = async () => {
    const name = await prompt({
      title: "New template",
      label: "Template name",
      placeholder: "e.g. Customer 360",
      confirmLabel: "Create draft",
    });
    if (name === null) return;
    const dom = await prompt({
      title: "New template",
      label: "Domain",
      placeholder: "e.g. Retail Banking",
      confirmLabel: "Create draft",
    });
    if (dom === null) return;
    setBusy(true);
    try {
      const r = await api.post("/api/templates", {
        spec: {
          apiVersion: "v3.1.0", kind: "DataContract",
          name: name || "Untitled template", domain: dom || "general",
          description: "", productKind: "consumer",
          schema: [{ name: name || "dataset", physicalName: "dataset", physicalType: "table", properties: [] }],
        },
      });
      navigate(`/product/templates/${encodeURIComponent(r.data.contract_id)}`);
    } catch (e) {
      showError(e, { title: "Create failed" });
    }
    setBusy(false);
  };

  const importOdcs = async () => {
    const content = await prompt({
      title: "Import ODCS",
      label: "Paste an ODCS spec (YAML or JSON)",
      placeholder: "apiVersion: v3.1.0\nkind: DataContract\n...",
      multiline: true,
      confirmLabel: "Import as draft",
    });
    if (!content || !content.trim()) return;
    setBusy(true);
    try {
      const r = await api.post("/api/templates/import", { content });
      navigate(`/product/templates/${encodeURIComponent(r.data.contract_id)}`);
    } catch (e) {
      showError(e, { title: "Import failed" });
    }
    setBusy(false);
  };

  const chip = (active: boolean): React.CSSProperties => ({
    padding: "5px 12px", borderRadius: 999, fontSize: 12, fontWeight: 600,
    cursor: "pointer", border: active ? "1px solid #7c3aed" : "1px solid #e2e8f0",
    backgroundColor: active ? "#7c3aed" : "#fff", color: active ? "#fff" : "#475569",
  });

  return (
    <div style={{ maxWidth: 1200, margin: "0 auto", padding: 24, fontFamily: "system-ui" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
        <h1 style={{ fontSize: 24, fontWeight: 800, color: "#0f172a", margin: 0 }}>Data Product Templates</h1>
        <div style={{ display: "flex", gap: 8 }}>
          <button onClick={importOdcs} disabled={busy} style={styles.secondaryBtn}>Import ODCS</button>
          <button onClick={createBlank} disabled={busy} style={styles.primaryBtn}>+ New template</button>
        </div>
      </div>
      <p style={{ color: "#64748b", fontSize: 14, marginTop: 4, marginBottom: 20 }}>
        The Blueprint Library — reusable data-product specs. Published templates feed
        the feasibility scan, the consumer wizard, and Pulse discovery. Clone a seed
        to make it your own.
      </p>

      {/* Search */}
      <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search templates by name, description, domain…"
          style={{ flex: 1, padding: "9px 12px", borderRadius: 8, border: "1px solid #e2e8f0", fontSize: 14 }}
        />
        {searchMode && (
          <span style={{ alignSelf: "center", fontSize: 11, color: "#94a3b8" }}>
            {searchMode === "semantic" ? "semantic" : "keyword"} search
          </span>
        )}
      </div>

      {/* Status + origin + kind filters */}
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 10 }}>
        <span style={styles.filterLabel}>Status</span>
        <span style={chip(status === "published")} onClick={() => setStatus("published")}>Published</span>
        <span style={chip(status === "draft")} onClick={() => setStatus("draft")}>My Drafts</span>
        <span style={{ width: 12 }} />
        <span style={styles.filterLabel}>Origin</span>
        {["", "seed", "clone", "import", "authored"].map((o) => (
          <span key={o || "all"} style={chip(origin === o)} onClick={() => setOrigin(o)}>
            {o ? ORIGIN_BADGE[o].label : "All"}
          </span>
        ))}
      </div>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 10 }}>
        <span style={styles.filterLabel}>Kind</span>
        {["", "source", "aggregate", "consumer"].map((k) => (
          <span key={k || "all"} style={chip(productKind === k)} onClick={() => setProductKind(k)}>
            {k ? k[0].toUpperCase() + k.slice(1) : "All"}
          </span>
        ))}
      </div>

      {/* Domain chips */}
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 8 }}>
        <span style={styles.filterLabel}>Domain</span>
        <span style={chip(domain === "")} onClick={() => setDomain("")}>All</span>
        {domains.map((d) => (
          <span key={d} style={chip(domain === d)} onClick={() => setDomain(d)}>{d}</span>
        ))}
      </div>
      <div style={{ marginBottom: 16 }}>
        <input
          value={subdomain}
          onChange={(e) => setSubdomain(e.target.value)}
          placeholder="Filter by sub-domain / tag…"
          style={{ padding: "6px 10px", borderRadius: 8, border: "1px solid #e2e8f0", fontSize: 12, width: 260 }}
        />
      </div>

      {loading ? (
        <div style={{ color: "#94a3b8", padding: 40 }}>Loading…</div>
      ) : visible.length === 0 ? (
        <div style={styles.empty}>No templates match these filters.</div>
      ) : (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))", gap: 16 }}>
          {visible.map((t) => (
            <div
              key={t.id}
              onClick={() => navigate(`/product/templates/${encodeURIComponent(t.id)}`)}
              style={styles.card}
            >
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8, gap: 8 }}>
                <span style={{ fontWeight: 700, fontSize: 15, color: "#0f172a" }}>
                  {t.origin === "seed" && <span title="Read-only seed" style={{ marginRight: 6 }}>🔒</span>}
                  {t.name}
                </span>
                <ProductKindChip kind={t.product_kind} compact />
              </div>
              <div style={{ fontSize: 13, color: "#475569", lineHeight: 1.4, marginBottom: 10, minHeight: 36 }}>
                {(t.description || "").slice(0, 130)}{(t.description || "").length > 130 ? "…" : ""}
              </div>
              {t.cloned_from && (
                <div style={{ fontSize: 11, color: "#7c3aed", marginBottom: 6 }}>
                  cloned from <code style={{ fontSize: 10 }}>{t.cloned_from.split(":").slice(-1)[0]}</code>
                </div>
              )}
              <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                <span style={{ ...styles.badge, backgroundColor: ORIGIN_BADGE[t.origin]?.bg, color: ORIGIN_BADGE[t.origin]?.fg }}>
                  {ORIGIN_BADGE[t.origin]?.label || t.origin}
                </span>
                <span style={{ ...styles.badge, backgroundColor: t.status === "published" ? "#dcfce7" : "#fef9c3", color: t.status === "published" ? "#15803d" : "#a16207" }}>
                  {t.status}
                </span>
                <span style={{ fontSize: 11, color: "#94a3b8" }}>{t.domain}</span>
                {typeof t.score === "number" && (
                  <span style={{ fontSize: 10, color: "#cbd5e1", marginLeft: "auto" }}>{t.score.toFixed(2)}</span>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  primaryBtn: { padding: "8px 16px", borderRadius: 8, border: "none", backgroundColor: "#7c3aed", color: "#fff", fontWeight: 600, fontSize: 13, cursor: "pointer" },
  secondaryBtn: { padding: "8px 16px", borderRadius: 8, border: "1px solid #e2e8f0", backgroundColor: "#fff", color: "#475569", fontWeight: 600, fontSize: 13, cursor: "pointer" },
  filterLabel: { alignSelf: "center", fontSize: 11, fontWeight: 700, color: "#94a3b8", textTransform: "uppercase", letterSpacing: 0.4 },
  card: { padding: 16, borderRadius: 10, border: "2px solid #e2e8f0", cursor: "pointer", backgroundColor: "#fff", transition: "all 0.15s" },
  badge: { padding: "2px 8px", borderRadius: 999, fontSize: 11, fontWeight: 600 },
  empty: { textAlign: "center", padding: "60px 24px", backgroundColor: "#fff", borderRadius: 12, border: "1px solid #e2e8f0", color: "#94a3b8" },
};
