import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import api from "../../api/client";
import { useCurrentUserEmail } from "../../AuthContext";
import { productTheme } from "../../theme";
import type {
  IngestClassification,
  IngestDraftRow,
  ResolveSlot,
} from "../../types";
import ArchetypeClassificationBanner from "./ingest/ArchetypeClassificationBanner";
import ResolveAndBindSourcesStep from "./shared/ResolveAndBindSourcesStep";
import { allSlotsMatched, hydrateSlotFromMatch } from "./shared/resolveSlotHelpers";

type Step = "source" | "confirm" | "resolve" | "success";

interface ParsedProperty {
  name?: string;
  physicalName?: string;
  logicalType?: string;
  physicalType?: string;
  description?: string;
  primaryKey?: boolean;
}

interface ParsedSchema {
  name?: string;
  physicalName?: string;
  description?: string;
  properties?: ParsedProperty[];
}

interface ParsedOwner {
  username?: string;
  name?: string;
  role?: string;
  email?: string;
}

interface ParsedSpec {
  name?: string;
  domain?: string;
  description?: string;
  purpose?: string;
  owners?: ParsedOwner[];
  schema?: ParsedSchema[];
  inputs?: Array<{ dprod_uri?: string; name?: string }>;
  [key: string]: unknown;
}

interface ParseResponse {
  parsed: ParsedSpec;
  errors: string[];
  warnings: string[];
}

interface MatchInputsResponse {
  slots: Array<{
    slot_id: string;
    source: ResolveSlot["source"];
    declared: ResolveSlot["declared"];
    encompasses_hint?: string | null;
    candidates: ResolveSlot["candidates"];
    preselected_candidate_uri: string | null;
    gap_suggestion: ResolveSlot["gap_suggestion"];
  }>;
}

interface IngestResponse {
  project_id: number;
  project_code: string;
  product_request_id: number | null;
  archetype: string;
  status: string;
}

/**
 * 4-step ingest flow:
 *   1. Source — upload/paste an ODCS v3.1 YAML or JSON spec
 *   2. Confirm — read-only summary + archetype classification banner (with override)
 *   3. Resolve — (cf only) bind each declared/inferred source dependency to a
 *      marketplace product, or mark as gap + Create now via NewSourceProductWizard
 *   4. Success — landing page with project code + request id
 *
 * Draft state is auto-saved on every step transition and slot change so the
 * PO can leave (e.g. to author a missing source product) and resume via
 * `?draft=<id>` deep-link.
 */
export default function IngestExistingProductPage() {
  const navigate = useNavigate();
  const CURRENT_USER_EMAIL = useCurrentUserEmail();
  const [searchParams, setSearchParams] = useSearchParams();
  const draftIdFromUrl = searchParams.get("draft");

  const [step, setStep] = useState<Step>("source");

  // Source step
  const [content, setContent] = useState("");
  const [filename, setFilename] = useState("");
  const [parsing, setParsing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Confirm + Resolve step
  const [parsed, setParsed] = useState<ParsedSpec | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [expandedSchemas, setExpandedSchemas] = useState<Set<number>>(new Set());
  const [submitToEngineer, setSubmitToEngineer] = useState(true);

  // Classification state
  const [classification, setClassification] = useState<IngestClassification | null>(null);
  const [classifying, setClassifying] = useState(false);
  const [archetypeChoice, setArchetypeChoice] = useState<"source" | "consumer">("source");

  // Resolve step
  const [slots, setSlots] = useState<ResolveSlot[]>([]);
  const [matching, setMatching] = useState(false);

  // Draft state (persisted)
  const [draftId, setDraftId] = useState<number | null>(null);
  const [savingDraft, setSavingDraft] = useState(false);
  const draftLoadedRef = useRef(false);

  // Success step
  const [result, setResult] = useState<IngestResponse | null>(null);

  // ── Draft load (resume from ?draft=<id>) ─────────────────────────────
  useEffect(() => {
    if (draftLoadedRef.current) return;
    if (!draftIdFromUrl) {
      draftLoadedRef.current = true;
      return;
    }
    const id = Number(draftIdFromUrl);
    if (!Number.isFinite(id)) {
      draftLoadedRef.current = true;
      return;
    }
    (async () => {
      try {
        const res = await api.get<IngestDraftRow>(`/api/ingest-products/drafts/${id}`);
        const d = res.data;
        setDraftId(d.id);
        if (d.parsed_spec_json) {
          try {
            const spec = JSON.parse(d.parsed_spec_json);
            setParsed(spec);
          } catch {
            // fall through
          }
        }
        if (d.classification_json) {
          try {
            const c = JSON.parse(d.classification_json) as IngestClassification;
            setClassification(c);
          } catch {
            // ignore
          }
        }
        const choice = (d.archetype_choice || "").toLowerCase();
        if (choice === "dpe-cf") setArchetypeChoice("consumer");
        else if (choice === "dpe-sa") setArchetypeChoice("source");
        if (d.input_selections_json) {
          try {
            const arr = JSON.parse(d.input_selections_json);
            if (Array.isArray(arr)) {
              setSlots(arr as ResolveSlot[]);
            }
          } catch {
            // ignore
          }
        }
        if (d.source_filename) setFilename(d.source_filename);
        // Jump straight into the resolve step on resume — that's where the
        // PO left off (everything else is read-only context).
        if (d.archetype_choice === "dpe-cf") setStep("resolve");
        else setStep("confirm");
      } catch {
        setError("Could not load saved draft.");
      } finally {
        draftLoadedRef.current = true;
      }
    })();
  }, [draftIdFromUrl]);

  // ── Draft autosave on key state transitions ──────────────────────────
  const saveDraft = useCallback(
    async (overrides: { status?: string } = {}) => {
      if (!parsed) return null;
      setSavingDraft(true);
      try {
        const archetypeStr = archetypeChoice === "consumer" ? "dpe-cf" : "dpe-sa";
        const body = {
          id: draftId ?? undefined,
          owner_email: CURRENT_USER_EMAIL,
          source_filename: filename || undefined,
          parsed_spec_json: JSON.stringify(parsed),
          classification_json: classification ? JSON.stringify(classification) : undefined,
          archetype_choice: archetypeStr,
          input_selections_json: JSON.stringify(slots),
          status: overrides.status,
        };
        const res = await api.post<IngestDraftRow>("/api/ingest-products/drafts", body);
        setDraftId(res.data.id);
        if (!draftIdFromUrl) {
          // Bind the URL so the PO can refresh/bookmark and stay in context.
          setSearchParams((sp) => {
            const next = new URLSearchParams(sp);
            next.set("draft", String(res.data.id));
            return next;
          });
        }
        return res.data.id;
      } catch {
        // Best-effort autosave; don't let failures block the flow.
        return null;
      } finally {
        setSavingDraft(false);
      }
    },
    [parsed, archetypeChoice, slots, classification, filename, draftId, draftIdFromUrl, setSearchParams],
  );

  // ── Parse + Classify ─────────────────────────────────────────────────
  const detectSource = (text: string): "json" | "yaml" =>
    text.trim().startsWith("{") || text.trim().startsWith("[") ? "json" : "yaml";

  const runClassification = useCallback(async (spec: ParsedSpec) => {
    setClassifying(true);
    try {
      const res = await api.post<IngestClassification>(
        "/api/ingest-products/classify-archetype",
        { spec },
      );
      setClassification(res.data);
      setArchetypeChoice(res.data.kind);
    } catch {
      // Heuristic fallback rendered in the banner; nothing to do client-side.
      setClassification({
        kind: "source",
        confidence: 0,
        rationale: "Could not reach the classifier; defaulting to source-aligned.",
        signals: [],
        inferred_dependencies: [],
        _fallback: true,
      });
      setArchetypeChoice("source");
    } finally {
      setClassifying(false);
    }
  }, []);

  const onParse = async () => {
    if (!content.trim()) {
      setError("Paste or upload an ODCS spec first.");
      return;
    }
    setParsing(true);
    setError(null);
    try {
      const res = await api.post<ParseResponse>("/api/ingest-products/parse-odcs", {
        content,
        source: detectSource(content),
      });
      const data = res.data;
      if (data.errors.length > 0) {
        setError(data.errors.join(" · "));
        return;
      }
      setParsed(data.parsed);
      setWarnings(data.warnings || []);
      if ((data.parsed.schema || []).length === 1) {
        setExpandedSchemas(new Set([0]));
      } else {
        setExpandedSchemas(new Set());
      }
      // Hold the user on the Source step until classification is also done.
      // Otherwise the Confirm screen renders without a recommendation and
      // the classifier banner pops in mid-read, which feels jittery.
      await runClassification(data.parsed);
      setStep("confirm");
    } catch (e: unknown) {
      const msg = extractErrorMessage(e);
      setError(msg);
    } finally {
      setParsing(false);
    }
  };

  // ── Match (Confirm → Resolve transition for cf path) ─────────────────
  const fetchSlots = useCallback(async () => {
    if (!parsed) return;
    setMatching(true);
    try {
      const res = await api.post<MatchInputsResponse>(
        "/api/ingest-products/match-inputs",
        {
          spec: parsed,
          inferred_dependencies: classification?.inferred_dependencies || [],
        },
      );
      const hydrated = res.data.slots.map(hydrateSlotFromMatch);
      // Preserve any existing slot resolutions (e.g. spawned_request_id) when
      // the matcher re-runs on resume — merge by slot_id.
      setSlots((prev) => {
        const prevById = new Map(prev.map((s) => [s.slot_id, s]));
        return hydrated.map((h) => {
          const existing = prevById.get(h.slot_id);
          if (!existing) return h;
          // Auto-bind: if the existing slot was a gap waiting on a spawned
          // source product and the matcher now sees a high-confidence match,
          // flip to matched. Otherwise preserve the spawned_request_id link.
          if (existing.spawned_request_id && h.resolution === "matched") {
            return { ...h, spawned_request_id: existing.spawned_request_id, spawned_project_id: existing.spawned_project_id };
          }
          return {
            ...h,
            resolution: existing.resolution ?? h.resolution,
            selected_uri: existing.selected_uri ?? h.selected_uri,
            selected_contract_id: existing.selected_contract_id ?? h.selected_contract_id,
            selected_name: existing.selected_name ?? h.selected_name,
            spawned_request_id: existing.spawned_request_id ?? null,
            spawned_project_id: existing.spawned_project_id ?? null,
          };
        });
      });
    } catch {
      // Show empty slot list; PO can still submit if no deps.
      setSlots([]);
    } finally {
      setMatching(false);
    }
  }, [parsed, classification]);

  const goToResolve = useCallback(async () => {
    await saveDraft();
    // Wait for the matcher to fill in slot candidates before transitioning,
    // so the Resolve step lands fully populated. The spinner panel rendered
    // on Confirm while `matching === true` is the visual cue.
    await fetchSlots();
    setStep("resolve");
  }, [saveDraft, fetchSlots]);

  // ── Slot interactions ────────────────────────────────────────────────
  const onSlotChange = useCallback((slotId: string, update: Partial<ResolveSlot>) => {
    setSlots((prev) => prev.map((s) => (s.slot_id === slotId ? { ...s, ...update } : s)));
  }, []);

  // Autosave slots whenever they change.
  useEffect(() => {
    if (step !== "resolve") return;
    if (!parsed) return;
    const handle = setTimeout(() => {
      void saveDraft();
    }, 600);
    return () => clearTimeout(handle);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slots, step]);

  const onCreateGap = useCallback(
    async (slot: ResolveSlot) => {
      // Persist before navigating away — the source wizard PATCHes the draft
      // when it submits, so we need a draft id present.
      const id = await saveDraft();
      const prefill = {
        from_ingest: String(id ?? ""),
        slot_id: slot.slot_id,
        prefill_idea: slot.gap_suggestion?.encompasses || slot.encompasses_hint || "",
        prefill_domain: slot.gap_suggestion?.domain || (parsed?.domain || ""),
        prefill_name: slot.gap_suggestion?.name || slot.declared.name || "",
      };
      const qs = new URLSearchParams(prefill).toString();
      navigate(`/product/new/source?${qs}`);
    },
    [saveDraft, parsed, navigate],
  );

  // ── Submit ──────────────────────────────────────────────────────────
  const submitGated = archetypeChoice === "consumer" && !allSlotsMatched(slots);

  const onSubmit = async () => {
    if (!parsed) return;
    setSubmitting(true);
    setError(null);
    try {
      const inputSelections = slots.map((s) => ({
        slot_id: s.slot_id,
        resolution: s.resolution === "matched" ? "matched" : "gap",
        dprod_uri: s.selected_uri,
        contract_id: s.selected_contract_id,
        name: s.selected_name,
        spawned_request_id: s.spawned_request_id,
        spawned_project_id: s.spawned_project_id,
        gap_suggestion: s.gap_suggestion,
      }));
      const res = await api.post<IngestResponse>("/api/ingest-products/from-odcs", {
        spec: parsed,
        owner_email: CURRENT_USER_EMAIL,
        notes: filename ? `Imported from ${filename}` : "Imported from pasted ODCS spec",
        submit_to_engineer: submitToEngineer,
        archetype_override: archetypeChoice === "consumer" ? "dpe-cf" : "dpe-sa",
        input_selections: inputSelections,
        ingest_draft_id: draftId,
      });
      setResult(res.data);
      setStep("success");
    } catch (e: unknown) {
      const msg = extractErrorMessage(e);
      setError(msg);
    } finally {
      setSubmitting(false);
    }
  };

  const restart = () => {
    setStep("source");
    setContent("");
    setFilename("");
    setParsed(null);
    setWarnings([]);
    setError(null);
    setResult(null);
    setExpandedSchemas(new Set());
    setSubmitToEngineer(true);
    setClassification(null);
    setArchetypeChoice("source");
    setSlots([]);
    setDraftId(null);
    setSearchParams(new URLSearchParams());
    draftLoadedRef.current = false;
  };

  const toggleSchema = (idx: number) => {
    setExpandedSchemas((prev) => {
      const next = new Set(prev);
      if (next.has(idx)) next.delete(idx);
      else next.add(idx);
      return next;
    });
  };

  // ── Confirm step's "Next" routing ────────────────────────────────────
  const onConfirmAdvance = () => {
    if (archetypeChoice === "consumer") {
      void goToResolve();
    } else {
      void onSubmit();
    }
  };

  const confirmAdvanceLabel = useMemo(() => {
    if (submitting) return "Submitting…";
    if (matching) return "Matching sources…";
    if (archetypeChoice === "consumer") return "Continue: Resolve sources →";
    return submitToEngineer
      ? "Submit for Engineering Lineage Discovery →"
      : "Publish to Marketplace →";
  }, [archetypeChoice, submitting, submitToEngineer, matching]);

  return (
    <div style={{ maxWidth: 760, margin: "0 auto" }}>
      <h1 style={{ fontSize: 22, fontWeight: 700, color: "#0f172a", marginBottom: 4 }}>
        Ingest an Existing Product
      </h1>
      <p style={{ fontSize: 13, color: "#475569", marginBottom: 20 }}>
        Bring an existing data product into the workbench by importing its ODCS v3.1 spec.
        Source-aligned imports publish directly; consumer-aligned imports prompt you to bind
        upstream source products before submitting.
      </p>

      <Stepper step={step} archetype={archetypeChoice} />

      {step === "source" && (
        <Card title="Upload or paste your ODCS v3.1 spec" description="YAML or JSON. We'll parse it deterministically and show you what we found before anything is saved.">
          <Field label="Upload a file">
            <input
              type="file"
              accept=".yaml,.yml,.json,.txt"
              onChange={async (e) => {
                const f = e.target.files?.[0];
                if (f) {
                  setFilename(f.name);
                  setContent(await f.text());
                }
              }}
              style={{ fontSize: 13 }}
            />
            {filename && (
              <span style={{ fontSize: 12, color: "#64748b", marginTop: 4 }}>{filename}</span>
            )}
          </Field>
          <Field label="…or paste it below">
            <textarea
              value={content}
              onChange={(e) => setContent(e.target.value)}
              rows={14}
              spellCheck={false}
              placeholder={`apiVersion: v3.1.0\nkind: DataContract\nname: Employee 360\ndomain: hr\n…`}
              style={{
                width: "100%",
                padding: "8px 12px",
                borderRadius: 6,
                border: "1px solid #cbd5e1",
                fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
                fontSize: 12,
                resize: "vertical",
                boxSizing: "border-box",
              }}
            />
          </Field>
          {error && <div style={errorStyle}>{error}</div>}
          {(parsing || classifying) && (
            <AnalysisPanel
              title={
                classifying
                  ? "Analyzing your spec to classify it…"
                  : "Reading your spec…"
              }
              detail={
                classifying
                  ? "Determining whether this is source-aligned (mirror of a system of record) or consumer-aligned (composed from one or more source products). Usually takes 10–30 seconds."
                  : "Parsing ODCS YAML/JSON and canonicalising fields."
              }
            />
          )}
          <ActionRow>
            <PrimaryBtn
              onClick={onParse}
              disabled={parsing || classifying || !content.trim()}
            >
              {classifying ? "Analyzing…" : parsing ? "Parsing…" : "Parse"}
            </PrimaryBtn>
          </ActionRow>
        </Card>
      )}

      {step === "confirm" && parsed && (
        <Card
          title="Confirm what we extracted"
          description="The classifier reads your spec and recommends source-aligned vs consumer-aligned. You can override before continuing."
        >
          <ArchetypeClassificationBanner
            classification={classification}
            loading={classifying}
            archetypeChoice={archetypeChoice}
            onArchetypeChange={(next) => {
              setArchetypeChoice(next);
              // Reset slot state when the archetype changes; cf may need
              // matching, sa drops the resolve step.
              setSlots([]);
              void saveDraft();
            }}
          />

          {warnings.length > 0 && (
            <div
              style={{
                padding: 10,
                borderRadius: 8,
                backgroundColor: "#fef3c7",
                color: "#854d0e",
                fontSize: 12,
              }}
            >
              <div style={{ fontWeight: 700, marginBottom: 4 }}>Warnings</div>
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {warnings.map((w, i) => (
                  <li key={i}>{w}</li>
                ))}
              </ul>
            </div>
          )}

          <SummaryGrid spec={parsed} />

          <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 8 }}>
            <div style={{ fontSize: 13, fontWeight: 700, color: "#0f172a" }}>
              Schemas ({(parsed.schema || []).length})
            </div>
            {(parsed.schema || []).map((s, idx) => {
              const props = s.properties || [];
              const expanded = expandedSchemas.has(idx);
              return (
                <div key={idx} style={{ border: "1px solid #e2e8f0", borderRadius: 8 }}>
                  <button
                    type="button"
                    onClick={() => toggleSchema(idx)}
                    style={{
                      width: "100%",
                      textAlign: "left",
                      padding: "10px 12px",
                      border: "none",
                      backgroundColor: "transparent",
                      cursor: "pointer",
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center",
                    }}
                  >
                    <div>
                      <span style={{ fontWeight: 600, color: "#0f172a", fontSize: 13 }}>
                        {s.physicalName || s.name || `schema[${idx}]`}
                      </span>
                      <span style={{ marginLeft: 10, fontSize: 12, color: "#64748b" }}>
                        {props.length} column{props.length === 1 ? "" : "s"}
                      </span>
                    </div>
                    <span style={{ fontSize: 12, color: "#64748b" }}>{expanded ? "▾" : "▸"}</span>
                  </button>
                  {expanded && props.length > 0 && (
                    <div style={{ borderTop: "1px solid #e2e8f0", padding: 6 }}>
                      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
                        <thead>
                          <tr style={{ color: "#64748b", textAlign: "left" }}>
                            <th style={tableHeadStyle}>Name</th>
                            <th style={tableHeadStyle}>Logical</th>
                            <th style={tableHeadStyle}>Physical</th>
                            <th style={tableHeadStyle}>PK</th>
                            <th style={tableHeadStyle}>Description</th>
                          </tr>
                        </thead>
                        <tbody>
                          {props.map((p, pi) => (
                            <tr key={pi} style={{ borderTop: "1px solid #f1f5f9" }}>
                              <td style={tableCellStyle}>
                                <strong>{p.physicalName || p.name || ""}</strong>
                              </td>
                              <td style={tableCellStyle}>{p.logicalType || "—"}</td>
                              <td style={tableCellStyle}>
                                <code>{p.physicalType || "—"}</code>
                              </td>
                              <td style={tableCellStyle}>{p.primaryKey ? "PK" : ""}</td>
                              <td style={{ ...tableCellStyle, color: "#475569" }}>
                                {p.description || ""}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              );
            })}
          </div>

          {/* Submit-direct-or-resolve toggle only meaningful for sa; cf always
              has the explicit Resolve step ahead. */}
          {archetypeChoice === "source" && (
            <div
              style={{
                marginTop: 6,
                padding: 12,
                borderRadius: 8,
                backgroundColor: "#f8fafc",
                border: "1px solid #e2e8f0",
              }}
            >
              <label style={{ display: "flex", alignItems: "flex-start", gap: 10, cursor: "pointer" }}>
                <input
                  type="checkbox"
                  checked={submitToEngineer}
                  onChange={(e) => setSubmitToEngineer(e.target.checked)}
                  style={{ marginTop: 2 }}
                />
                <div>
                  <div style={{ fontSize: 13, fontWeight: 600, color: "#0f172a" }}>
                    Submit to engineer for lineage discovery
                  </div>
                  <div style={{ fontSize: 12, color: "#64748b", marginTop: 2 }}>
                    Uncheck if the imported spec is authoritative and you want to publish directly to the marketplace.
                  </div>
                </div>
              </label>
            </div>
          )}

          {error && <div style={errorStyle}>{error}</div>}
          {matching && (
            <AnalysisPanel
              title="Matching upstream source products…"
              detail="Searching the marketplace for source products that match this consumer's declared and inferred dependencies, scoring each candidate. Usually takes 5–20 seconds."
            />
          )}
          {savingDraft && <DraftIndicator />}
          <ActionRow>
            <SecondaryBtn onClick={() => setStep("source")} disabled={submitting || matching}>
              ← Back
            </SecondaryBtn>
            <PrimaryBtn onClick={onConfirmAdvance} disabled={submitting || classifying || matching}>
              {confirmAdvanceLabel}
            </PrimaryBtn>
          </ActionRow>
        </Card>
      )}

      {step === "resolve" && parsed && (
        <Card
          title="Resolve & bind source products"
          description="This consumer-aligned product depends on one or more published source products. Match each dependency to an existing source, or create the missing source product before continuing."
        >
          <ResolveAndBindSourcesStep
            slots={slots}
            onSlotChange={onSlotChange}
            onCreateGap={onCreateGap}
            mode="ingest"
            matching={matching}
          />
          {error && <div style={errorStyle}>{error}</div>}
          {savingDraft && <DraftIndicator />}
          <ActionRow>
            <SecondaryBtn onClick={() => setStep("confirm")} disabled={submitting}>
              ← Back
            </SecondaryBtn>
            <PrimaryBtn onClick={onSubmit} disabled={submitting || submitGated}>
              {submitting
                ? "Submitting…"
                : submitGated
                ? `Bind all sources to continue (${slots.filter((s) => s.resolution !== "matched" || !s.selected_uri).length} pending)`
                : "Submit for Engineering →"}
            </PrimaryBtn>
          </ActionRow>
        </Card>
      )}

      {step === "success" && result && (
        <Card
          title={result.product_request_id != null ? "Submitted for engineering" : "Published to the marketplace"}
          description={
            result.product_request_id != null
              ? result.archetype === "dpe-cf"
                ? "Engineering will pick up the request and run data_mapping + serving against the bound source products."
                : "Engineering will pick up the request from their Incoming queue and establish lineage between the source system and your product columns."
              : "The imported spec was published directly. You can run discovery later by adding workflows to the project."
          }
        >
          <div
            style={{
              padding: 16,
              borderRadius: 10,
              backgroundColor: "#dcfce7",
              border: "1px solid #86efac",
              color: "#065f46",
              fontSize: 13,
              lineHeight: 1.5,
            }}
          >
            Project <strong>{result.project_code}</strong> created ({result.archetype}).
            {result.product_request_id != null ? (
              <>
                {" "}
                Request <strong>#{result.product_request_id}</strong> sent to engineering.
              </>
            ) : (
              " Published to the marketplace."
            )}
          </div>
          <ActionRow>
            <SecondaryBtn onClick={restart}>Ingest another</SecondaryBtn>
            <PrimaryBtn onClick={() => navigate("/product/my-products")}>View My Products</PrimaryBtn>
          </ActionRow>
        </Card>
      )}
    </div>
  );
}

function Stepper({ step, archetype }: { step: Step; archetype: "source" | "consumer" }) {
  const baseSteps: Array<{ key: Step; label: string }> = [
    { key: "source", label: "1. Source" },
    { key: "confirm", label: "2. Confirm" },
  ];
  if (archetype === "consumer") {
    baseSteps.push({ key: "resolve", label: "3. Resolve sources" });
    baseSteps.push({ key: "success", label: "4. Submitted" });
  } else {
    baseSteps.push({ key: "success", label: "3. Submitted" });
  }
  const order: Record<Step, number> = { source: 0, confirm: 1, resolve: 2, success: 3 };
  return (
    <div style={{ display: "flex", gap: 8, marginBottom: 18 }}>
      {baseSteps.map((s) => {
        const active = step === s.key;
        const done = order[step] > order[s.key];
        return (
          <div
            key={s.key}
            style={{
              flex: 1,
              padding: 10,
              borderRadius: 8,
              backgroundColor: active ? productTheme.accentSoft : done ? "#f0fdf4" : "#f8fafc",
              border: `1px solid ${active ? productTheme.accent : "#e2e8f0"}`,
              color: active ? productTheme.accent : done ? "#059669" : "#94a3b8",
              fontSize: 12,
              fontWeight: 600,
            }}
          >
            {done ? "✓ " : ""}{s.label}
          </div>
        );
      })}
    </div>
  );
}

function SummaryGrid({ spec }: { spec: ParsedSpec }) {
  const ownerLabel = (() => {
    const owners = spec.owners || [];
    if (!owners.length) return "—";
    const first = owners[0];
    const name = first.name || first.username || first.email || "(unnamed)";
    return owners.length > 1 ? `${name} +${owners.length - 1}` : name;
  })();
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))",
        gap: 10,
      }}
    >
      <Stat label="Product name" value={spec.name || "—"} />
      <Stat label="Domain" value={spec.domain || "—"} />
      <Stat label="Primary owner" value={ownerLabel} />
      <Stat label="Description" value={spec.description || "—"} wide />
      {spec.purpose && <Stat label="Purpose" value={spec.purpose} wide />}
    </div>
  );
}

function Stat({ label, value, wide = false }: { label: string; value: string; wide?: boolean }) {
  return (
    <div
      style={{
        gridColumn: wide ? "1 / -1" : undefined,
        padding: 10,
        borderRadius: 8,
        backgroundColor: "#f8fafc",
        border: "1px solid #e2e8f0",
      }}
    >
      <div
        style={{
          fontSize: 10,
          fontWeight: 700,
          letterSpacing: 0.4,
          textTransform: "uppercase",
          color: "#94a3b8",
        }}
      >
        {label}
      </div>
      <div style={{ fontSize: 13, color: "#0f172a", marginTop: 4, lineHeight: 1.4 }}>{value}</div>
    </div>
  );
}

function DraftIndicator() {
  return (
    <div style={{ fontSize: 11, color: "#64748b", fontStyle: "italic" }}>
      Saving draft…
    </div>
  );
}

/**
 * Loading panel shown while parsing + classification is in flight. Matches
 * the spinner pattern used by NewProductWizard during column re-ranking.
 */
function AnalysisPanel({ title, detail }: { title: string; detail: string }) {
  return (
    <div
      style={{
        padding: "14px 16px",
        borderRadius: 8,
        backgroundColor: "#fff",
        border: `1px solid ${productTheme.accent}`,
        boxShadow: "0 2px 8px rgba(15, 23, 42, 0.05)",
        display: "flex",
        alignItems: "center",
        gap: 12,
      }}
    >
      <div
        style={{
          width: 16,
          height: 16,
          borderRadius: "50%",
          border: `3px solid ${productTheme.accentSoft}`,
          borderTopColor: productTheme.accent,
          animation: "ingest-spin 0.9s linear infinite",
          flexShrink: 0,
        }}
      />
      <div>
        <div style={{ fontSize: 13, fontWeight: 700, color: "#0f172a" }}>{title}</div>
        <div style={{ fontSize: 12, color: "#64748b", marginTop: 2 }}>{detail}</div>
      </div>
      <style>{`@keyframes ingest-spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }`}</style>
    </div>
  );
}

function Card({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children: React.ReactNode;
}) {
  return (
    <div
      style={{
        padding: 22,
        borderRadius: 12,
        backgroundColor: "#fff",
        border: "1px solid #e2e8f0",
      }}
    >
      <h2 style={{ fontSize: 16, fontWeight: 700, color: "#0f172a", margin: 0 }}>{title}</h2>
      <p style={{ fontSize: 12, color: "#64748b", margin: "4px 0 16px" }}>{description}</p>
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>{children}</div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <span style={{ fontSize: 12, fontWeight: 600, color: "#334155" }}>{label}</span>
      {children}
    </label>
  );
}

function ActionRow({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", justifyContent: "flex-end", gap: 10, marginTop: 6 }}>
      {children}
    </div>
  );
}

function PrimaryBtn({
  onClick,
  disabled,
  children,
}: {
  onClick: () => void;
  disabled?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      style={{
        padding: "9px 18px",
        borderRadius: 8,
        backgroundColor: disabled ? "#cbd5e1" : productTheme.accent,
        color: "#fff",
        border: "none",
        fontSize: 13,
        fontWeight: 700,
        cursor: disabled ? "not-allowed" : "pointer",
      }}
    >
      {children}
    </button>
  );
}

function SecondaryBtn({
  onClick,
  disabled,
  children,
}: {
  onClick: () => void;
  disabled?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      style={{
        padding: "9px 18px",
        borderRadius: 8,
        backgroundColor: "#fff",
        color: "#334155",
        border: "1px solid #cbd5e1",
        fontSize: 13,
        fontWeight: 600,
        cursor: disabled ? "not-allowed" : "pointer",
      }}
    >
      {children}
    </button>
  );
}

function extractErrorMessage(e: unknown): string {
  if (e && typeof e === "object" && "response" in e) {
    const resp = (e as { response?: { data?: { detail?: unknown; message?: unknown } } }).response;
    const detail = resp?.data?.detail;
    const message = resp?.data?.message;
    if (typeof detail === "string") return detail;
    if (detail && typeof detail === "object" && "message" in detail) {
      const msg = (detail as { message?: unknown }).message;
      if (typeof msg === "string") return msg;
    }
    if (typeof message === "string") return message;
    return JSON.stringify(detail ?? message ?? e);
  }
  return String(e);
}

const errorStyle: React.CSSProperties = {
  color: "#dc2626",
  backgroundColor: "#fef2f2",
  border: "1px solid #fecaca",
  padding: 10,
  borderRadius: 6,
  fontSize: 13,
};

const tableHeadStyle: React.CSSProperties = {
  padding: "6px 8px",
  fontWeight: 600,
  fontSize: 11,
  textTransform: "uppercase",
  letterSpacing: 0.4,
};

const tableCellStyle: React.CSSProperties = {
  padding: "6px 8px",
  verticalAlign: "top",
};
