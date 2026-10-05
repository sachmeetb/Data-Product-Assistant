import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import api from "../../api/client";
import { useCurrentUserEmail, useCurrentUserName } from "../../AuthContext";
import { productTheme } from "../../theme";
import { titleCaseDomain } from "../../lib/domainLabel";

type Step = "describe" | "confirm" | "success";

interface DomainOption {
  domain: string;
  label?: string;
  description?: string;
  column_count?: number;
}

interface SettingsResponse {
  neo4j_host: string;
  neo4j_port: number;
  neo4j_user: string;
  neo4j_password: string;
  neo4j_database: string;
}

interface CreateProjectResponse {
  id: number;
  project_code: string;
  name: string;
}

interface SubmitResponse {
  id: number;
  contract_id?: string;
}

/**
 * 3-step lightweight wizard for source-aligned products.
 *
 *   1. Describe — idea (free-form prose), domain, name, optional connection.
 *   2. Confirm — read-only summary; "this is what the engineer will see".
 *   3. Success — request id + project code + a banner explaining the next step.
 *
 * Submit path: POST /api/projects (archetype='dpe-sa') →
 *              POST /api/projects/{id}/product-requests/submit (kind='new').
 *
 * Unlike the consumer-aligned wizard, we don't write an ODCS spec or
 * generate a dprod here — there's no contract until the engineer's
 * synthesize_odcs_from_graph stage runs against approved graph state.
 */
export default function NewSourceProductWizard() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const CURRENT_USER_EMAIL = useCurrentUserEmail();
  const CURRENT_USER_NAME = useCurrentUserName();
  const theme = productTheme;
  const [step, setStep] = useState<Step>("describe");

  // When launched from a consumer-aligned ingest's "Create now" gap button,
  // the IngestExistingProductPage passes these query params so we can prefill
  // the wizard with the gap suggestion and link the new request back to the
  // parent ingest draft via parent_ingest_draft_id.
  const fromIngestParam = searchParams.get("from_ingest");
  const fromIngestId = fromIngestParam ? Number(fromIngestParam) : null;
  const slotId = searchParams.get("slot_id");
  const prefillIdea = searchParams.get("prefill_idea") || "";
  const prefillDomain = searchParams.get("prefill_domain") || "";
  const prefillName = searchParams.get("prefill_name") || "";

  const [productIdea, setProductIdea] = useState(prefillIdea);
  const [domain, setDomain] = useState(prefillDomain);
  const [name, setName] = useState(prefillName);
  const [sourcePlatform, setSourcePlatform] = useState("");
  const [targetPlatform, setTargetPlatform] = useState("");
  const [offline, setOffline] = useState(false);

  const [domains, setDomains] = useState<DomainOption[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submittedProject, setSubmittedProject] = useState<CreateProjectResponse | null>(null);
  const [submittedRequestId, setSubmittedRequestId] = useState<number | null>(null);

  useEffect(() => {
    api.get<{ catalogs: DomainOption[] }>("/api/domain-catalogs")
      .then((r) => setDomains(r.data?.catalogs || []))
      .catch(() => setDomains([]));
  }, []);

  const onConfirmNext = () => {
    setError(null);
    if (!productIdea.trim()) { setError("Describe what this product mirrors before continuing."); return; }
    if (!domain) { setError("Pick a domain so column names get a consistent prefix."); return; }
    if (!name.trim()) { setError("Give the product a working name."); return; }
    setStep("confirm");
  };

  const onSubmit = async () => {
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const settings = (await api.get<SettingsResponse>("/api/settings")).data;
      const createRes = await api.post<CreateProjectResponse>("/api/projects", {
        name: name.trim(),
        archetype: "dpe-sa",
        domain,
        product_idea: productIdea.trim(),
        // Offline: the engineer seeds the catalog from an uploaded manifest
        // (data_discovery_offline stage) instead of live discovery + profiling.
        data_connectivity_mode: offline ? "offline" : "live",
        // Flows into the synthesized ODCS spec's owners[] so the product
        // surfaces in the PO's My Products list (filtered by owner email).
        owner_email: CURRENT_USER_EMAIL,
        owner_name: CURRENT_USER_NAME,
        neo4j_host: settings.neo4j_host,
        neo4j_port: settings.neo4j_port,
        neo4j_user: settings.neo4j_user,
        neo4j_password: settings.neo4j_password,
        neo4j_database: settings.neo4j_database,
      });
      setSubmittedProject(createRes.data);

      // The idea is persisted on Project.product_idea (see /api/projects).
      // Notes here is just a human-readable summary so the engineer's
      // Incoming queue card has a useful preview before they accept.
      const submitRes = await api.post<SubmitResponse>(
        `/api/projects/${createRes.data.id}/product-requests/submit`,
        {
          kind: "new",
          submitted_by: CURRENT_USER_EMAIL,
          notes:
            `Source-aligned product. Domain '${domain}'.` +
            (offline ? " OFFLINE discovery — engineer imports an uploaded metadata manifest." : "") +
            (sourcePlatform ? ` Source platform: ${sourcePlatform}.` : "") +
            (targetPlatform && targetPlatform !== "same" ? ` Target platform: ${targetPlatform}.` : "") +
            (fromIngestId ? ` Spawned from ingest draft #${fromIngestId}.` : ""),
          parent_ingest_draft_id: fromIngestId ?? undefined,
        }
      );
      setSubmittedRequestId(submitRes.data.id);

      // Cross-link: stamp the parent ingest draft's slot with the new
      // request + project ids so the resume page can render "1 of 2 source
      // products ready" and auto-bind once this product is published.
      if (fromIngestId && slotId) {
        try {
          await api.patch(`/api/ingest-products/drafts/${fromIngestId}`, {
            slot_update: {
              slot_id: slotId,
              spawned_request_id: submitRes.data.id,
              spawned_project_id: createRes.data.id,
            },
          });
        } catch {
          // Non-fatal; the resume page can re-match against the marketplace.
        }
      }

      setStep("success");
    } catch (e: unknown) {
      const msg = e && typeof e === "object" && "response" in e
        ? (e as { response?: { data?: { detail?: unknown } } }).response?.data?.detail ?? String(e)
        : String(e);
      setError(typeof msg === "string" ? msg : JSON.stringify(msg));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div style={{ maxWidth: 720, margin: "0 auto" }}>
      <h1 style={{ fontSize: 22, fontWeight: 700, color: "#0f172a", marginBottom: 4 }}>
        Request a Source-aligned Product
      </h1>
      <p style={{ fontSize: 13, color: "#475569", marginBottom: 20 }}>
        A source-aligned product mirrors an operational source — a customer master, an
        orders database — close to 1:1. The engineer connects to the source, profiles
        it, and proposes a clean schema; you validate names/descriptions/rules before
        publish.
      </p>

      <Stepper step={step} />

      {step === "describe" && (
        <Card
          title="Describe the source"
          description="Free-form prose is fine — the engineer interprets your intent."
          theme={theme}
        >
          <Field label="What does this product mirror?" required>
            <textarea
              value={productIdea}
              onChange={(e) => setProductIdea(e.target.value)}
              placeholder="e.g. Mirror our customer master from the CRM into a clean source product so other teams can build on it."
              rows={4}
              style={textareaStyle}
            />
          </Field>

          <Field label="Domain" required>
            <select
              value={domain}
              onChange={(e) => setDomain(e.target.value)}
              style={selectStyle}
            >
              <option value="">Pick a domain…</option>
              {domains.map((d) => (
                <option key={d.domain} value={d.domain}>{d.label || titleCaseDomain(d.domain)}</option>
              ))}
            </select>
          </Field>

          <Field label="Working name" required>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Customer Master"
              style={inputStyle}
            />
          </Field>

          <Field
            label="Source platform (optional)"
            help="Where does the source data currently live? The engineer will confirm the exact connection."
          >
            <select
              value={sourcePlatform}
              onChange={(e) => setSourcePlatform(e.target.value)}
              style={selectStyle}
            >
              <option value="">Not sure / engineer will decide</option>
              <option value="postgres">PostgreSQL</option>
              <option value="mysql">MySQL</option>
              <option value="snowflake">Snowflake</option>
              <option value="databricks">Databricks</option>
            </select>
          </Field>

          <Field
            label="Target platform (optional)"
            help="Where will consumers query the product from? Defaults to same as source."
          >
            <select
              value={targetPlatform}
              onChange={(e) => setTargetPlatform(e.target.value)}
              style={selectStyle}
            >
              <option value="same">Same as source</option>
              <option value="postgres">PostgreSQL</option>
              <option value="mysql">MySQL</option>
              <option value="snowflake">Snowflake</option>
              <option value="databricks">Databricks</option>
              <option value="lakehouse">Lakehouse (Parquet + DuckDB)</option>
            </select>
          </Field>

          <label style={{ display: "flex", gap: 8, alignItems: "flex-start", marginTop: 14,
                          padding: "10px 12px", border: `1px solid ${offline ? "#0891b2" : "#e2e8f0"}`,
                          borderRadius: 8, background: offline ? "#ecfeff" : "#fff", cursor: "pointer" }}>
            <input type="checkbox" checked={offline} onChange={(e) => setOffline(e.target.checked)}
              style={{ marginTop: 2 }} />
            <span style={{ fontSize: 13, color: "#334155" }}>
              <b>I can't grant a live connection — I'll upload metadata instead.</b><br />
              <span style={{ fontSize: 12, color: "#64748b" }}>
                The engineer downloads a small extraction kit, you run it in your environment,
                review the produced manifest, and upload it. Discovery + profiling seed
                deterministically from that file — no live source.
              </span>
            </span>
          </label>

          {error && <div style={errorStyle}>{error}</div>}

          <div style={{ display: "flex", justifyContent: "space-between", marginTop: 18 }}>
            <button onClick={() => navigate("/product/my-products")} style={secondaryBtn}>Cancel</button>
            <button onClick={onConfirmNext} style={primaryBtn(theme.accent)}>
              Next — Review
            </button>
          </div>
        </Card>
      )}

      {step === "confirm" && (
        <Card
          title="Review and submit"
          description="The engineer will pick this up from the Incoming queue and run discovery."
          theme={theme}
        >
          <SummaryRow label="Product name" value={name} />
          <SummaryRow label="Domain" value={titleCaseDomain(domain)} />
          <SummaryRow label="Idea" value={productIdea} multiline />
          <SummaryRow label="Source platform" value={sourcePlatform || "(engineer will decide)"} />
          <SummaryRow label="Target platform" value={targetPlatform === "same" || !targetPlatform ? "(same as source)" : targetPlatform} />
          <SummaryRow label="Discovery mode" value={offline ? "Offline — upload metadata manifest (no live connection)" : "Live connection"} />

          <div style={infoBoxStyle}>
            <div style={{ fontWeight: 600, marginBottom: 4 }}>What happens next</div>
            <ol style={{ margin: 0, paddingLeft: 18, fontSize: 13, color: "#334155" }}>
              <li>Engineer accepts from Incoming and runs discovery + profiling.</li>
              <li>Auto-generated column names (snake_case + domain prefix), descriptions, and Tier 1 DQ rules.</li>
              <li>You'll see "Ready to validate" on My Products — review names, descriptions, and rules in one panel.</li>
              <li>Engineer materializes the product (auto 1:1 mapping) and publishes to the marketplace.</li>
            </ol>
          </div>

          {error && <div style={errorStyle}>{error}</div>}

          <div style={{ display: "flex", justifyContent: "space-between", marginTop: 18 }}>
            <button onClick={() => setStep("describe")} style={secondaryBtn} disabled={submitting}>Back</button>
            <button onClick={onSubmit} style={primaryBtn(theme.accent)} disabled={submitting}>
              {submitting ? "Submitting…" : "Submit Request"}
            </button>
          </div>
        </Card>
      )}

      {step === "success" && submittedProject && (
        <Card
          title="Request submitted"
          description="Engineering will pick this up shortly."
          theme={theme}
        >
          <SummaryRow label="Project code" value={submittedProject.project_code} />
          <SummaryRow label="Request id" value={String(submittedRequestId ?? "—")} />
          <div style={{ ...infoBoxStyle, backgroundColor: "#ecfdf5", borderColor: "#86efac" }}>
            We'll surface this on My Products as <strong>"In discovery"</strong> until
            the engineer marks discovery complete. Then it flips to{" "}
            <strong>"Ready to validate"</strong> and you'll be asked to review.
            {fromIngestId && (
              <>
                {" "}This source product was spawned from ingest draft{" "}
                <strong>#{fromIngestId}</strong>; that ingest will auto-bind this slot
                once the new product is published.
              </>
            )}
          </div>
          <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 18, gap: 8 }}>
            <button onClick={() => navigate("/product/my-products")} style={secondaryBtn}>
              Back to My Products
            </button>
            {fromIngestId ? (
              <button
                onClick={() => navigate(`/product/ingest?draft=${fromIngestId}`)}
                style={primaryBtn(theme.accent)}
              >
                Return to ingest →
              </button>
            ) : (
              <button onClick={() => navigate("/product/marketplace")} style={primaryBtn(theme.accent)}>
                Browse Marketplace
              </button>
            )}
          </div>
        </Card>
      )}
    </div>
  );
}

// ── Helper components and styles ──────────────────────────────────────────

function Stepper({ step }: { step: Step }) {
  const items: { key: Step; label: string }[] = [
    { key: "describe", label: "1. Describe" },
    { key: "confirm",  label: "2. Review" },
    { key: "success",  label: "3. Submitted" },
  ];
  const idxByKey: Record<Step, number> = { describe: 0, confirm: 1, success: 2 };
  const currentIdx = idxByKey[step];
  return (
    <div style={{ display: "flex", gap: 8, marginBottom: 16 }}>
      {items.map((item, i) => {
        const active = i === currentIdx;
        const done = i < currentIdx;
        return (
          <div
            key={item.key}
            style={{
              flex: 1,
              padding: "8px 12px",
              borderRadius: 6,
              fontSize: 13,
              fontWeight: active ? 700 : 500,
              backgroundColor: done ? "#ecfdf5" : active ? productTheme.accentSoft : "#f8fafc",
              color: done ? "#065f46" : active ? productTheme.badgeFg : "#64748b",
              border: `1px solid ${done ? "#86efac" : active ? productTheme.accent : "#e2e8f0"}`,
            }}
          >
            {item.label}
          </div>
        );
      })}
    </div>
  );
}

function Card({
  title, description, children,
}: {
  title: string; description?: string; children: React.ReactNode; theme?: typeof productTheme;
}) {
  return (
    <div style={{
      padding: 20, backgroundColor: "#fff", borderRadius: 10,
      border: "1px solid #e2e8f0", boxShadow: "0 1px 2px rgba(15,23,42,0.04)",
    }}>
      <div style={{ fontSize: 16, fontWeight: 600, color: "#0f172a", marginBottom: 4 }}>{title}</div>
      {description && (
        <div style={{ fontSize: 13, color: "#64748b", marginBottom: 16 }}>{description}</div>
      )}
      {children}
    </div>
  );
}

function Field({
  label, required, help, children,
}: {
  label: string; required?: boolean; help?: string; children: React.ReactNode;
}) {
  return (
    <div style={{ marginBottom: 14 }}>
      <label style={{ display: "block", fontSize: 13, fontWeight: 600, color: "#334155", marginBottom: 4 }}>
        {label}{required && <span style={{ color: "#dc2626", marginLeft: 4 }}>*</span>}
      </label>
      {children}
      {help && <div style={{ fontSize: 12, color: "#64748b", marginTop: 4 }}>{help}</div>}
    </div>
  );
}

function SummaryRow({ label, value, multiline }: { label: string; value: string; multiline?: boolean }) {
  return (
    <div style={{ display: "flex", flexDirection: multiline ? "column" : "row", gap: multiline ? 4 : 8, marginBottom: 10 }}>
      <div style={{ fontSize: 12, fontWeight: 600, color: "#64748b", minWidth: 110 }}>{label}</div>
      <div style={{ fontSize: 13, color: "#0f172a", whiteSpace: multiline ? "pre-wrap" : "normal" }}>
        {value || <span style={{ color: "#94a3b8" }}>(none)</span>}
      </div>
    </div>
  );
}

const inputStyle: React.CSSProperties = {
  width: "100%", padding: "8px 10px", fontSize: 13,
  border: "1px solid #e2e8f0", borderRadius: 6, fontFamily: "inherit",
};

const textareaStyle: React.CSSProperties = {
  ...inputStyle, resize: "vertical", lineHeight: 1.5,
};

const selectStyle: React.CSSProperties = {
  ...inputStyle, padding: "7px 10px",
};

const errorStyle: React.CSSProperties = {
  marginTop: 12, padding: "10px 12px", borderRadius: 6,
  backgroundColor: "#fef2f2", color: "#991b1b", fontSize: 13,
  border: "1px solid #fecaca",
};

const infoBoxStyle: React.CSSProperties = {
  marginTop: 16, padding: "12px 14px", borderRadius: 6,
  backgroundColor: "#f1f5f9", color: "#0f172a", fontSize: 13,
  border: "1px solid #e2e8f0",
};

const primaryBtn = (color: string): React.CSSProperties => ({
  padding: "8px 16px", fontSize: 13, fontWeight: 600, color: "#fff",
  backgroundColor: color, borderRadius: 6, border: "none", cursor: "pointer",
});

const secondaryBtn: React.CSSProperties = {
  padding: "8px 16px", fontSize: 13, fontWeight: 500, color: "#475569",
  backgroundColor: "#fff", borderRadius: 6, border: "1px solid #cbd5e1",
  cursor: "pointer",
};
