import { useEffect, useMemo, useState } from "react";
import api from "../api/client";
import { useNotify } from "./dialogContext";
import { useCurrentUserEmail } from "../AuthContext";
import SensitivityChip from "./SensitivityChip";
import RelationshipKindChip, {
  RELATIONSHIP_KIND_LABELS as RK_LABELS_SHARED,
} from "./RelationshipKindChip";

interface Props {
  projectId: number;
  onReviewComplete: () => void;
  /** When true, fetch with ?include_approved=true and surface a "Re-edit"
   *  action on approved items. Used when the PO opens a published SA
   *  product to make changes — the panel becomes the editing surface,
   *  not just the validation gate. */
  editMode?: boolean;
}

interface NameItem {
  schema: string;
  table_name: string;
  col_uri: string;
  current_name: string;
  recommended_name: string;
  data_type: string;
  ordinal: number;
  // Only present when GET fetched with include_approved=true.
  status?: string;
  /** Phase 1 sensitivity enum — surfaced as a chip alongside the name. */
  sensitivity?: string | null;
}

interface DescriptionItem {
  schema: string;
  table_name: string;
  col_uri: string;
  col_name: string;
  data_type: string;
  ordinal: number;
  desc_uri: string;
  description_text: string;
  status?: string;
}

interface RuleItem {
  schema: string;
  table_name: string;
  col_uri: string;
  col_name: string;
  data_type: string;
  rule_uri: string;
  rule_type: string;
  severity: string;
  description: string;
  confidence: number | null;
  status?: string;
}

interface TableDescriptionItem {
  schema: string;
  table_name: string;
  dataset_uri: string;
  desc_uri: string;
  description_text: string;
  relationship_kind: string;  // general_membership | specialization | fact | lookup_dimension | audit_log | configuration | unknown
  status?: string;
}

interface RelationshipDescriptionItem {
  from_schema: string;
  from_table: string;
  from_dataset_uri: string;
  to_schema: string;
  to_table: string;
  to_dataset_uri: string;
  desc_uri: string;
  description_text: string;
  relationship_nature: string;  // belongs_to | categorises | audit_log_for | references
  status?: string;
}

interface PayloadShape {
  column_names: NameItem[];
  descriptions: DescriptionItem[];
  rules: RuleItem[];
  table_descriptions: TableDescriptionItem[];
  relationship_descriptions: RelationshipDescriptionItem[];
  count: number;
}

type Tab = "names" | "descriptions" | "rules" | "tables" | "relationships";

// Minimal v1 enum — mirrors the metadata-enrichment skill's
// write_relationship_descriptions.py. Long-form labels for the PO's
// edit dropdown; the chip below renders a terser version.
const RELATIONSHIP_NATURE_LABELS: Record<string, string> = {
  belongs_to: "Belongs to (child → parent)",
  categorises: "Categorises (dimensional lookup)",
  audit_log_for: "Audit log for (history table)",
  references: "References (generic)",
};
const RELATIONSHIP_NATURE_VALUES = Object.keys(RELATIONSHIP_NATURE_LABELS);
const RELATIONSHIP_NATURE_PALETTE: Record<string, { bg: string; fg: string }> = {
  belongs_to: { bg: "#dbeafe", fg: "#1e40af" },
  categorises: { bg: "#fef3c7", fg: "#92400e" },
  audit_log_for: { bg: "#e0e7ff", fg: "#4338ca" },
  references: { bg: "#f1f5f9", fg: "#475569" },
};

// Long-form labels (panel uses them as dropdown option text). Keys
// intentionally match the shared chip — the chip exposes the short label
// `RK_LABELS_SHARED` for terse renders; here we use a more descriptive
// version since the PO is choosing the value.
const RELATIONSHIP_KIND_LABELS: Record<string, string> = {
  fact: "Fact (primary entity)",
  lookup_dimension: "Lookup / dimension",
  general_membership: "General membership (M:N junction)",
  specialization: "Specialization (role-specific)",
  audit_log: "Audit / event log",
  configuration: "Configuration",
  unknown: "Unknown",
};
const RELATIONSHIP_KIND_VALUES = Object.keys(RELATIONSHIP_KIND_LABELS);
// Reference the shared labels so the import isn't dead — useful as a
// fallback if the long-form map ever drifts.
void RK_LABELS_SHARED;

/**
 * dpe-sa combined PO validation gate. Three tabs (Names / Descriptions /
 * Rules), each surfaces pending items with inline approve / edit / reject
 * controls. The panel hides items as they're acted on; when every tab is
 * empty the parent ReviewPanel calls onReviewComplete and the stage flips
 * to ``complete``.
 */
export default function SourceProductValidationPanel({ projectId, onReviewComplete, editMode = false }: Props) {
  const { showError } = useNotify();
  const [tab, setTab] = useState<Tab>("names");
  const [data, setData] = useState<PayloadShape | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const reviewer = useCurrentUserEmail();

  const load = async () => {
    setLoading(true);
    try {
      const r = await api.get<PayloadShape>(
        `/api/projects/${projectId}/reviews/source_product_validation`,
        { params: { include_approved: editMode ? true : undefined } },
      );
      setData(r.data);
    } catch {
      setData({ column_names: [], descriptions: [], rules: [], table_descriptions: [], relationship_descriptions: [], count: 0 });
    }
    setLoading(false);
  };

  useEffect(() => { load(); }, [projectId, editMode]);

  const totals = useMemo(() => ({
    names: data?.column_names.length ?? 0,
    descriptions: data?.descriptions.length ?? 0,
    rules: data?.rules.length ?? 0,
    tables: data?.table_descriptions.length ?? 0,
    relationships: data?.relationship_descriptions?.length ?? 0,
  }), [data]);

  const post = async (body: Record<string, unknown>) => {
    setBusy(body.action as string);
    try {
      await api.post(`/api/projects/${projectId}/reviews/source_product_validation`, body);
      await load();
      // ReviewPanel re-pulls counts when this fires.
      if ((data?.count ?? 0) <= 1) onReviewComplete();
    } catch (err) {
      console.error(err);
    }
    setBusy(null);
  };

  const allTotal = totals.names + totals.descriptions + totals.rules + totals.tables + totals.relationships;

  // Bulk-validate modal: the PO toggles which surfaces to batch-approve, then
  // executes. The backend loops the per-item approve so provenance is identical
  // to clicking each; you can still reject individually first.
  const BULK_TABS: { key: Tab; label: string }[] = [
    { key: "names", label: "Names" },
    { key: "descriptions", label: "Descriptions" },
    { key: "tables", label: "Tables" },
    { key: "relationships", label: "Relationships" },
    { key: "rules", label: "Rules" },
  ];
  const [bulkOpen, setBulkOpen] = useState(false);
  const [bulkSel, setBulkSel] = useState<Record<Tab, boolean>>({
    names: true, descriptions: true, tables: true, relationships: true, rules: true,
  });

  const openBulk = () => {
    // Default: every surface that still has pending items.
    setBulkSel({
      names: totals.names > 0, descriptions: totals.descriptions > 0,
      tables: totals.tables > 0, relationships: totals.relationships > 0,
      rules: totals.rules > 0,
    });
    setBulkOpen(true);
  };

  const bulkSelectedCount = BULK_TABS
    .filter((t) => bulkSel[t.key] && totals[t.key] > 0)
    .reduce((s, t) => s + totals[t.key], 0);

  const executeBulk = async () => {
    const tabs = BULK_TABS.filter((t) => bulkSel[t.key] && totals[t.key] > 0).map((t) => t.key);
    if (tabs.length === 0) return;
    setBusy("approve-all");
    try {
      await api.post(`/api/projects/${projectId}/reviews/source_product_validation/approve-all`,
        { tabs, quality: 2, reviewer: reviewer || "Data Product Owner" });
      await load();
      onReviewComplete();
      setBulkOpen(false);
    } catch (err) {
      console.error(err);
      showError(err, { title: "Bulk validation failed" });
    }
    setBusy(null);
  };

  if (loading) return <div style={{ color: "#64748b", fontSize: 14, padding: 12 }}>Loading…</div>;

  return (
    <div style={panelStyle}>
      <div style={{ marginBottom: 10 }}>
        <div style={{ fontSize: 16, fontWeight: 700, color: "#0f172a" }}>Validate Source Product</div>
        <div style={{ fontSize: 13, color: "#64748b" }}>
          Review every surface (names, descriptions, tables, relationships, rules)
          before the engineer can publish. The stage advances when every list is empty.
        </div>
      </div>

      <div style={{ display: "flex", gap: 4, borderBottom: "1px solid #e2e8f0", marginBottom: 12 }}>
        <TabBtn active={tab === "names"} onClick={() => setTab("names")} label="Names" count={totals.names} />
        <TabBtn active={tab === "descriptions"} onClick={() => setTab("descriptions")} label="Descriptions" count={totals.descriptions} />
        <TabBtn active={tab === "tables"} onClick={() => setTab("tables")} label="Tables" count={totals.tables} />
        <TabBtn active={tab === "relationships"} onClick={() => setTab("relationships")} label="Relationships" count={totals.relationships} />
        <TabBtn active={tab === "rules"} onClick={() => setTab("rules")} label="Rules" count={totals.rules} />
      </div>

      {allTotal > 0 && (
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginBottom: 8 }}>
          <button
            onClick={openBulk}
            disabled={!!busy}
            style={{
              padding: "6px 14px", borderRadius: 6, border: "1px solid #16a34a",
              background: busy ? "#f1f5f9" : "#16a34a", color: busy ? "#94a3b8" : "#fff",
              fontSize: 12.5, fontWeight: 700, cursor: busy ? "default" : "pointer",
            }}
            title="Batch-approve selected surfaces"
          >
            {busy === "approve-all" ? "Validating…" : `✓ Bulk validate (${allTotal} pending)`}
          </button>
        </div>
      )}

      {bulkOpen && (
        <div
          onClick={() => !busy && setBulkOpen(false)}
          style={{ position: "fixed", inset: 0, background: "rgba(15,23,42,0.5)", zIndex: 200,
            display: "flex", alignItems: "center", justifyContent: "center" }}
        >
          <div onClick={(e) => e.stopPropagation()} style={{ width: 460, maxWidth: "calc(100vw - 32px)",
            background: "#fff", borderRadius: 12, padding: 22, boxShadow: "0 20px 40px rgba(15,23,42,0.25)",
            display: "flex", flexDirection: "column", gap: 14 }}>
            <div>
              <div style={{ fontSize: 16, fontWeight: 700, color: "#0f172a" }}>Bulk validate</div>
              <div style={{ fontSize: 12.5, color: "#64748b", marginTop: 4, lineHeight: 1.5 }}>
                Choose which surfaces to approve as-is. Each item is recorded with your sign-off;
                you can still reject items individually first. Surfaces with nothing pending are disabled.
              </div>
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              {BULK_TABS.map((t) => {
                const n = totals[t.key];
                const disabled = n === 0;
                return (
                  <label key={t.key} style={{ display: "flex", alignItems: "center", gap: 10,
                    padding: "8px 10px", borderRadius: 8, cursor: disabled ? "default" : "pointer",
                    background: disabled ? "#f8fafc" : bulkSel[t.key] ? "#f0fdf4" : "#fff",
                    border: `1px solid ${!disabled && bulkSel[t.key] ? "#86efac" : "#e2e8f0"}`,
                    opacity: disabled ? 0.55 : 1 }}>
                    <input type="checkbox" disabled={disabled}
                      checked={!disabled && bulkSel[t.key]}
                      onChange={() => setBulkSel((s) => ({ ...s, [t.key]: !s[t.key] }))} />
                    <span style={{ fontSize: 13, fontWeight: 600, color: "#334155", flex: 1 }}>{t.label}</span>
                    <span style={{ fontSize: 12, fontWeight: 700,
                      color: n > 0 ? "#16a34a" : "#94a3b8" }}>{n} pending</span>
                  </label>
                );
              })}
            </div>
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 10, marginTop: 4 }}>
              <button type="button" onClick={() => setBulkOpen(false)} disabled={!!busy}
                style={{ padding: "8px 16px", borderRadius: 6, background: "#fff", color: "#334155",
                  border: "1px solid #cbd5e1", fontSize: 13, fontWeight: 600,
                  cursor: busy ? "not-allowed" : "pointer" }}>
                Cancel
              </button>
              <button type="button" onClick={() => void executeBulk()}
                disabled={!!busy || bulkSelectedCount === 0}
                style={{ padding: "8px 16px", borderRadius: 6,
                  background: (busy || bulkSelectedCount === 0) ? "#cbd5e1" : "#16a34a",
                  color: "#fff", border: "none", fontSize: 13, fontWeight: 700,
                  cursor: (busy || bulkSelectedCount === 0) ? "not-allowed" : "pointer" }}>
                {busy === "approve-all" ? "Validating…" : `Approve ${bulkSelectedCount} item${bulkSelectedCount === 1 ? "" : "s"}`}
              </button>
            </div>
          </div>
        </div>
      )}

      {tab === "names" && <NamesList items={data?.column_names ?? []} busy={busy} onAction={post} />}
      {tab === "descriptions" && <DescriptionsList items={data?.descriptions ?? []} busy={busy} onAction={post} />}
      {tab === "tables" && <TablesList items={data?.table_descriptions ?? []} busy={busy} onAction={post} />}
      {tab === "relationships" && <RelationshipsList items={data?.relationship_descriptions ?? []} busy={busy} onAction={post} />}
      {tab === "rules" && <RulesList items={data?.rules ?? []} busy={busy} onAction={post} />}
    </div>
  );
}

// ── Tabs ──────────────────────────────────────────────────────────────────

function TabBtn({ active, onClick, label, count }: { active: boolean; onClick: () => void; label: string; count: number }) {
  return (
    <button
      onClick={onClick}
      style={{
        padding: "8px 14px",
        background: "none",
        border: "none",
        borderBottom: active ? "2px solid #7c3aed" : "2px solid transparent",
        cursor: "pointer",
        fontSize: 13,
        fontWeight: active ? 700 : 500,
        color: active ? "#5b21b6" : "#475569",
      }}
    >
      {label}
      <span style={{
        marginLeft: 6,
        padding: "1px 6px",
        borderRadius: 9,
        fontSize: 11,
        fontWeight: 600,
        backgroundColor: count > 0 ? (active ? "#ede9fe" : "#f1f5f9") : "#e2e8f0",
        color: count > 0 ? (active ? "#5b21b6" : "#475569") : "#94a3b8",
      }}>{count}</span>
    </button>
  );
}

// ── Names tab ─────────────────────────────────────────────────────────────

function NamesList({
  items, busy, onAction,
}: {
  items: NameItem[];
  busy: string | null;
  onAction: (body: Record<string, unknown>) => Promise<void>;
}) {
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");

  if (items.length === 0) {
    return <EmptyState message="No name recommendations pending review." />;
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {items.map((it) => {
        const isEditing = editing === it.col_uri;
        // In edit mode the GET includes already-approved/rejected items
        // so the PO can change their mind on a published SA product. Show
        // a status badge + a Re-edit affordance instead of the approve/
        // reject/edit triplet for those rows.
        const isLocked = !isEditing && (it.status === "approved" || it.status === "rejected");
        return (
          <div key={it.col_uri} style={rowStyle}>
            <div style={{ flex: 1 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <span style={{ fontSize: 12, color: "#64748b" }}>{it.schema}.{it.table_name} · {it.data_type}</span>
                <SensitivityChip sensitivity={it.sensitivity} compact />
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 4 }}>
                <code style={{ fontSize: 13, color: "#475569" }}>{it.current_name}</code>
                <span style={{ color: "#94a3b8" }}>→</span>
                {isEditing ? (
                  <input
                    type="text"
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                    autoFocus
                    style={{ ...inputStyle, width: 240 }}
                  />
                ) : (
                  <code style={{ fontSize: 13, fontWeight: 600, color: "#0f172a" }}>{it.recommended_name}</code>
                )}
                {isLocked && it.status && <StatusBadge status={it.status} />}
              </div>
            </div>
            <div style={{ display: "flex", gap: 6 }}>
              {isEditing ? (
                <>
                  <button
                    onClick={async () => {
                      await onAction({ action: "edit_name", col_uri: it.col_uri, new_name: draft.trim() });
                      setEditing(null);
                    }}
                    disabled={busy !== null || !draft.trim()}
                    style={primaryBtn}
                  >Save</button>
                  <button onClick={() => setEditing(null)} style={tertiaryBtn}>Cancel</button>
                </>
              ) : isLocked ? (
                <button
                  onClick={() => onAction({ action: "reopen_name", col_uri: it.col_uri })}
                  disabled={busy !== null}
                  style={editBtn}
                  title="Reopen this name for editing — flips status back to pending review"
                >Re-edit</button>
              ) : (
                <>
                  <button
                    onClick={() => onAction({ action: "approve_name", col_uri: it.col_uri })}
                    disabled={busy !== null}
                    style={approveBtn}
                  >Approve</button>
                  <button
                    onClick={() => { setEditing(it.col_uri); setDraft(it.recommended_name); }}
                    disabled={busy !== null}
                    style={editBtn}
                  >Edit</button>
                  <button
                    onClick={() => onAction({ action: "reject_name", col_uri: it.col_uri })}
                    disabled={busy !== null}
                    style={rejectBtn}
                  >Reject</button>
                </>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const palette = status === "approved"
    ? { bg: "#dcfce7", fg: "#15803d", border: "#86efac" }
    : { bg: "#fee2e2", fg: "#991b1b", border: "#fca5a5" };
  return (
    <span style={{
      padding: "1px 6px", borderRadius: 999, fontSize: 10, fontWeight: 700,
      backgroundColor: palette.bg, color: palette.fg, border: `1px solid ${palette.border}`,
      textTransform: "uppercase", letterSpacing: 0.4,
    }}>{status}</span>
  );
}

// ── Descriptions tab ──────────────────────────────────────────────────────

function DescriptionsList({
  items, busy, onAction,
}: {
  items: DescriptionItem[];
  busy: string | null;
  onAction: (body: Record<string, unknown>) => Promise<void>;
}) {
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");

  if (items.length === 0) {
    return <EmptyState message="No descriptions pending review." />;
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {items.map((it) => {
        const isEditing = editing === it.desc_uri;
        const isLocked = !isEditing && (it.status === "approved" || it.status === "rejected");
        return (
          <div key={it.desc_uri} style={{ ...rowStyle, alignItems: "stretch", flexDirection: "column" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 12, color: "#64748b", display: "flex", alignItems: "center", gap: 6 }}>
                  <span>{it.schema}.{it.table_name}.<strong>{it.col_name}</strong> · {it.data_type}</span>
                  {isLocked && it.status && <StatusBadge status={it.status} />}
                </div>
                {isEditing ? (
                  <textarea
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                    rows={3}
                    autoFocus
                    style={{ ...inputStyle, width: "100%", marginTop: 6, resize: "vertical" }}
                  />
                ) : (
                  <div style={{ fontSize: 13, color: "#0f172a", marginTop: 6, lineHeight: 1.4 }}>
                    {it.description_text || <span style={{ color: "#94a3b8" }}>(no description text)</span>}
                  </div>
                )}
              </div>
              <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
                {isEditing ? (
                  <>
                    <button
                      onClick={async () => {
                        await onAction({
                          action: "edit_description",
                          desc_uri: it.desc_uri,
                          col_uri: it.col_uri,
                          new_text: draft,
                        });
                        setEditing(null);
                      }}
                      disabled={busy !== null || !draft.trim()}
                      style={primaryBtn}
                    >Save</button>
                    <button onClick={() => setEditing(null)} style={tertiaryBtn}>Cancel</button>
                  </>
                ) : isLocked ? (
                  <button
                    onClick={() => onAction({ action: "reopen_description", desc_uri: it.desc_uri })}
                    disabled={busy !== null}
                    style={editBtn}
                    title="Reopen this description for editing"
                  >Re-edit</button>
                ) : (
                  <>
                    <button
                      onClick={() => onAction({ action: "approve_description", desc_uri: it.desc_uri })}
                      disabled={busy !== null}
                      style={approveBtn}
                    >Approve</button>
                    <button
                      onClick={() => { setEditing(it.desc_uri); setDraft(it.description_text); }}
                      disabled={busy !== null}
                      style={editBtn}
                    >Edit</button>
                    <button
                      onClick={() => onAction({
                        action: "reject_description",
                        desc_uri: it.desc_uri,
                        col_uri: it.col_uri,
                        corrected_text: it.description_text || "(rejected by PO)",
                        category: "incorrect_meaning",
                      })}
                      disabled={busy !== null}
                      style={rejectBtn}
                    >Reject</button>
                  </>
                )}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ── Tables tab ─────────────────────────────────────────────────────────────
// PO reviews per-:Dataset descriptions + relationship_kind classification
// produced by metadata-enrichment. The classification feeds the consumer-
// side view-DDL bridge ranker (general_membership beats specialization
// when picking among candidate junctions).

function TablesList({
  items, busy, onAction,
}: {
  items: TableDescriptionItem[];
  busy: string | null;
  onAction: (body: Record<string, unknown>) => Promise<void>;
}) {
  const [editing, setEditing] = useState<string | null>(null);
  const [draftText, setDraftText] = useState("");
  const [draftKind, setDraftKind] = useState("unknown");

  if (items.length === 0) {
    return <EmptyState message="No table descriptions pending review." />;
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {items.map((it) => {
        const isEditing = editing === it.desc_uri;
        const isLocked = !isEditing && (it.status === "approved" || it.status === "rejected");
        return (
          <div key={it.desc_uri} style={{ ...rowStyle, alignItems: "stretch", flexDirection: "column" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 12, color: "#64748b", display: "flex", alignItems: "center", gap: 6 }}>
                  <span>{it.schema}.<strong>{it.table_name}</strong></span>
                  <RelationshipKindChip kind={it.relationship_kind} />
                  {isLocked && it.status && <StatusBadge status={it.status} />}
                </div>
                {isEditing ? (
                  <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 6 }}>
                    <textarea
                      value={draftText}
                      onChange={(e) => setDraftText(e.target.value)}
                      rows={3}
                      autoFocus
                      style={{ ...inputStyle, width: "100%", resize: "vertical" }}
                    />
                    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                      <label style={{ fontSize: 11, color: "#64748b" }}>relationship_kind:</label>
                      <select
                        value={draftKind}
                        onChange={(e) => setDraftKind(e.target.value)}
                        style={{ ...inputStyle, width: 240 }}
                      >
                        {RELATIONSHIP_KIND_VALUES.map((k) => (
                          <option key={k} value={k}>{RELATIONSHIP_KIND_LABELS[k]}</option>
                        ))}
                      </select>
                    </div>
                  </div>
                ) : (
                  <div style={{ fontSize: 13, color: "#0f172a", marginTop: 6, lineHeight: 1.4 }}>
                    {it.description_text || <span style={{ color: "#94a3b8" }}>(no description text)</span>}
                  </div>
                )}
              </div>
              <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
                {isEditing ? (
                  <>
                    <button
                      onClick={async () => {
                        await onAction({
                          action: "edit_table",
                          desc_uri: it.desc_uri,
                          new_text: draftText,
                          relationship_kind: draftKind,
                        });
                        setEditing(null);
                      }}
                      disabled={busy !== null || !draftText.trim()}
                      style={primaryBtn}
                    >Save</button>
                    <button onClick={() => setEditing(null)} style={tertiaryBtn}>Cancel</button>
                  </>
                ) : isLocked ? (
                  <span style={{ fontSize: 11, color: "#94a3b8", alignSelf: "center" }}>—</span>
                ) : (
                  <>
                    <button
                      onClick={() => onAction({ action: "approve_table", desc_uri: it.desc_uri })}
                      disabled={busy !== null}
                      style={approveBtn}
                    >Approve</button>
                    <button
                      onClick={() => {
                        setEditing(it.desc_uri);
                        setDraftText(it.description_text);
                        setDraftKind(it.relationship_kind || "unknown");
                      }}
                      disabled={busy !== null}
                      style={editBtn}
                    >Edit</button>
                    <button
                      onClick={() => onAction({ action: "reject_table", desc_uri: it.desc_uri })}
                      disabled={busy !== null}
                      style={rejectBtn}
                    >Reject</button>
                  </>
                )}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

// (badge styling moved to ./RelationshipKindChip)

// ── Relationships tab ──────────────────────────────────────────────────────
// PO reviews the semantic description of each FK edge between :Datasets,
// authored by the metadata-enrichment skill's write_relationship_descriptions.py.
// The relationship_nature classification (belongs_to / categorises /
// audit_log_for / references) is consumed by the data-mapping skill,
// view-DDL bridge ranker, and question-analyzer downstream, so PO
// approval is load-bearing.

function RelationshipsList({
  items, busy, onAction,
}: {
  items: RelationshipDescriptionItem[];
  busy: string | null;
  onAction: (body: Record<string, unknown>) => Promise<void>;
}) {
  const [editing, setEditing] = useState<string | null>(null);
  const [draftText, setDraftText] = useState("");
  const [draftNature, setDraftNature] = useState("references");

  if (items.length === 0) {
    return <EmptyState message="No relationship descriptions pending review. Re-run metadata enrichment to generate them." />;
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {items.map((it) => {
        const isEditing = editing === it.desc_uri;
        const isLocked = !isEditing && (it.status === "approved" || it.status === "rejected");
        const palette = RELATIONSHIP_NATURE_PALETTE[it.relationship_nature]
          || RELATIONSHIP_NATURE_PALETTE.references;
        return (
          <div key={it.desc_uri} style={{ ...rowStyle, alignItems: "stretch", flexDirection: "column" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 12, color: "#64748b", display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
                  <span>
                    {it.from_schema}.<strong>{it.from_table}</strong>
                    {" "}<span style={{ color: "#94a3b8" }}>→</span>{" "}
                    {it.to_schema}.<strong>{it.to_table}</strong>
                  </span>
                  <span style={{
                    padding: "2px 8px",
                    borderRadius: 999,
                    fontSize: 11,
                    fontWeight: 600,
                    backgroundColor: palette.bg,
                    color: palette.fg,
                  }}>
                    {it.relationship_nature.replace(/_/g, " ")}
                  </span>
                  {isLocked && it.status && <StatusBadge status={it.status} />}
                </div>
                {isEditing ? (
                  <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 6 }}>
                    <textarea
                      value={draftText}
                      onChange={(e) => setDraftText(e.target.value)}
                      rows={3}
                      autoFocus
                      style={{ ...inputStyle, width: "100%", resize: "vertical" }}
                    />
                    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                      <label style={{ fontSize: 11, color: "#64748b" }}>relationship_nature:</label>
                      <select
                        value={draftNature}
                        onChange={(e) => setDraftNature(e.target.value)}
                        style={{ ...inputStyle, width: 280 }}
                      >
                        {RELATIONSHIP_NATURE_VALUES.map((k) => (
                          <option key={k} value={k}>{RELATIONSHIP_NATURE_LABELS[k]}</option>
                        ))}
                      </select>
                    </div>
                  </div>
                ) : (
                  <div style={{ fontSize: 13, color: "#0f172a", marginTop: 6, lineHeight: 1.4 }}>
                    {it.description_text || <span style={{ color: "#94a3b8" }}>(no description text)</span>}
                  </div>
                )}
              </div>
              <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
                {isEditing ? (
                  <>
                    <button
                      onClick={async () => {
                        await onAction({
                          action: "edit_relationship",
                          desc_uri: it.desc_uri,
                          new_text: draftText,
                          relationship_nature: draftNature,
                        });
                        setEditing(null);
                      }}
                      disabled={busy !== null || !draftText.trim()}
                      style={primaryBtn}
                    >Save</button>
                    <button onClick={() => setEditing(null)} style={tertiaryBtn}>Cancel</button>
                  </>
                ) : isLocked ? (
                  <span style={{ fontSize: 11, color: "#94a3b8", alignSelf: "center" }}>—</span>
                ) : (
                  <>
                    <button
                      onClick={() => onAction({ action: "approve_relationship", desc_uri: it.desc_uri })}
                      disabled={busy !== null}
                      style={approveBtn}
                    >Approve</button>
                    <button
                      onClick={() => {
                        setEditing(it.desc_uri);
                        setDraftText(it.description_text);
                        setDraftNature(it.relationship_nature || "references");
                      }}
                      disabled={busy !== null}
                      style={editBtn}
                    >Edit</button>
                    <button
                      onClick={() => onAction({ action: "reject_relationship", desc_uri: it.desc_uri })}
                      disabled={busy !== null}
                      style={rejectBtn}
                    >Reject</button>
                  </>
                )}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ── Rules tab ─────────────────────────────────────────────────────────────

function RulesList({
  items, busy, onAction,
}: {
  items: RuleItem[];
  busy: string | null;
  onAction: (body: Record<string, unknown>) => Promise<void>;
}) {
  const [editing, setEditing] = useState<string | null>(null);
  const [draftDesc, setDraftDesc] = useState("");
  const [draftSev, setDraftSev] = useState("sh:Warning");

  if (items.length === 0) {
    return <EmptyState message="No DQ rules pending review." />;
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {items.map((it) => {
        const isEditing = editing === it.rule_uri;
        const isLocked = !isEditing && (it.status === "approved" || it.status === "rejected");
        return (
          <div key={it.rule_uri} style={{ ...rowStyle, alignItems: "stretch", flexDirection: "column" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 12, color: "#64748b", display: "flex", alignItems: "center", gap: 6 }}>
                  <span>{it.schema}.{it.table_name}.<strong>{it.col_name}</strong> · {it.data_type}</span>
                  {isLocked && it.status && <StatusBadge status={it.status} />}
                </div>
                <div style={{ fontSize: 13, marginTop: 4 }}>
                  <code style={{ color: "#5b21b6", fontWeight: 600 }}>{it.rule_type}</code>
                  <span style={severityChip(it.severity)}>{shortSeverity(it.severity)}</span>
                  {it.confidence !== null && (
                    <span style={{ color: "#94a3b8", marginLeft: 6, fontSize: 12 }}>
                      conf {(it.confidence * 100).toFixed(0)}%
                    </span>
                  )}
                </div>
                {isEditing ? (
                  <div style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 6 }}>
                    <textarea
                      value={draftDesc}
                      onChange={(e) => setDraftDesc(e.target.value)}
                      rows={2}
                      style={{ ...inputStyle, width: "100%", resize: "vertical" }}
                      placeholder="Description"
                    />
                    <select value={draftSev} onChange={(e) => setDraftSev(e.target.value)} style={inputStyle}>
                      <option value="sh:Violation">Error</option>
                      <option value="sh:Warning">Warning</option>
                    </select>
                  </div>
                ) : (
                  <div style={{ fontSize: 13, color: "#0f172a", marginTop: 4, lineHeight: 1.4 }}>
                    {it.description || <span style={{ color: "#94a3b8" }}>(no description)</span>}
                  </div>
                )}
              </div>
              <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
                {isEditing ? (
                  <>
                    <button
                      onClick={async () => {
                        await onAction({
                          action: "edit_rule",
                          rule_uri: it.rule_uri,
                          description: draftDesc,
                          severity: draftSev,
                        });
                        setEditing(null);
                      }}
                      disabled={busy !== null}
                      style={primaryBtn}
                    >Save</button>
                    <button onClick={() => setEditing(null)} style={tertiaryBtn}>Cancel</button>
                  </>
                ) : isLocked ? (
                  <button
                    onClick={() => onAction({ action: "reopen_rule", rule_uri: it.rule_uri })}
                    disabled={busy !== null}
                    style={editBtn}
                    title="Reopen this rule for editing"
                  >Re-edit</button>
                ) : (
                  <>
                    <button
                      onClick={() => onAction({ action: "approve_rule", rule_uri: it.rule_uri })}
                      disabled={busy !== null}
                      style={approveBtn}
                    >Approve</button>
                    <button
                      onClick={() => {
                        setEditing(it.rule_uri);
                        setDraftDesc(it.description);
                        setDraftSev(it.severity || "sh:Warning");
                      }}
                      disabled={busy !== null}
                      style={editBtn}
                    >Edit</button>
                    <button
                      onClick={() => onAction({
                        action: "reject_rule",
                        rule_uri: it.rule_uri,
                        category: "irrelevant",
                      })}
                      disabled={busy !== null}
                      style={rejectBtn}
                    >Reject</button>
                  </>
                )}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ── shared ────────────────────────────────────────────────────────────────

function EmptyState({ message }: { message: string }) {
  return (
    <div style={{ padding: 14, color: "#22c55e", fontSize: 13, fontWeight: 600, backgroundColor: "#f0fdf4", borderRadius: 6, border: "1px solid #86efac" }}>
      ✓ {message}
    </div>
  );
}

function shortSeverity(raw: string): string {
  if (!raw) return "warning";
  const r = raw.toLowerCase();
  if (r === "sh:violation" || r === "violation" || r === "error") return "error";
  return "warning";
}

function severityChip(raw: string): React.CSSProperties {
  const sev = shortSeverity(raw);
  const isErr = sev === "error";
  return {
    marginLeft: 8,
    padding: "1px 6px",
    fontSize: 11,
    fontWeight: 600,
    borderRadius: 4,
    backgroundColor: isErr ? "#fef2f2" : "#fffbeb",
    color: isErr ? "#991b1b" : "#92400e",
    border: `1px solid ${isErr ? "#fecaca" : "#fde68a"}`,
  };
}

const panelStyle: React.CSSProperties = {
  padding: 16,
  backgroundColor: "#fff",
  borderRadius: 10,
  border: "1px solid #e2e8f0",
};

const rowStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 12,
  padding: "12px 14px",
  backgroundColor: "#f8fafc",
  borderRadius: 8,
  border: "1px solid #e2e8f0",
};

const inputStyle: React.CSSProperties = {
  padding: "6px 9px",
  fontSize: 13,
  borderRadius: 5,
  border: "1px solid #cbd5e1",
  fontFamily: "inherit",
};

const primaryBtn: React.CSSProperties = {
  padding: "6px 12px", fontSize: 13, fontWeight: 600, color: "#fff",
  backgroundColor: "#7c3aed", border: "none", borderRadius: 5, cursor: "pointer",
};

const approveBtn: React.CSSProperties = {
  padding: "6px 10px", fontSize: 12, fontWeight: 600, color: "#065f46",
  backgroundColor: "#ecfdf5", border: "1px solid #86efac",
  borderRadius: 5, cursor: "pointer",
};

const editBtn: React.CSSProperties = {
  padding: "6px 10px", fontSize: 12, fontWeight: 500, color: "#475569",
  backgroundColor: "#fff", border: "1px solid #cbd5e1",
  borderRadius: 5, cursor: "pointer",
};

const rejectBtn: React.CSSProperties = {
  padding: "6px 10px", fontSize: 12, fontWeight: 600, color: "#991b1b",
  backgroundColor: "#fef2f2", border: "1px solid #fecaca",
  borderRadius: 5, cursor: "pointer",
};

const tertiaryBtn: React.CSSProperties = {
  padding: "6px 10px", fontSize: 12, fontWeight: 500, color: "#64748b",
  backgroundColor: "transparent", border: "none",
  borderRadius: 5, cursor: "pointer",
};
