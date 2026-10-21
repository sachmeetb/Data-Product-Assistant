import { useState } from "react";
import { productTheme } from "../../../theme";
import type { ResolveSlot, ResolveSlotCandidate } from "../../../types";
import ProductKindChip from "../../../components/ProductKindChip";

/**
 * ResolveAndBindSourcesStep
 *
 * Shared component used by:
 *   • NewProductWizard step 7 (dpe-cf authoring): PO binds upstream sources
 *     as the final step before submit
 *   • IngestExistingProductPage cf path: same UX, seeded from the
 *     classifier's inferred_dependencies + spec.inputs[]
 *
 * Each row represents one dependency slot. The PO picks a marketplace
 * candidate (pre-selected when the matcher returns score ≥ 70), or marks
 * the slot as a gap and clicks "Create now" to launch NewSourceProductWizard
 * with prefilled context. Submit is gated on every slot having
 * resolution === 'matched'.
 */
export interface ResolveAndBindSourcesStepProps {
  slots: ResolveSlot[];
  onSlotChange: (slotId: string, update: Partial<ResolveSlot>) => void;
  /**
   * Invoked when the PO clicks "Create now" on a gap. The host page is
   * expected to persist its draft state (if any) and then navigate to
   * NewSourceProductWizard with prefill params.
   */
  onCreateGap: (slot: ResolveSlot) => void;
  /**
   * Optional: if provided, manual slots (PO-added from the marketplace
   * picker) render an inline Remove control that calls this with the slot
   * id. Recommended/inferred slots don't get Remove — they use the
   * "Not the right fit" gap toggle instead.
   */
  onRemove?: (slotId: string) => void;
  /** 'wizard' or 'ingest' — drives a couple of copy tweaks only. */
  mode: "wizard" | "ingest";
  /** True while the parent is fetching /match-inputs. Hides the empty state. */
  matching?: boolean;
}

export default function ResolveAndBindSourcesStep({
  slots,
  onSlotChange,
  onCreateGap,
  onRemove,
  mode,
  matching = false,
}: ResolveAndBindSourcesStepProps) {
  const unresolved = slots.filter((s) => s.resolution !== "matched" || !s.selected_uri);
  const matchedCount = slots.length - unresolved.length;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <div
        style={{
          padding: 12,
          borderRadius: 8,
          backgroundColor: unresolved.length === 0 ? "#f0fdf4" : "#fefce8",
          border: `1px solid ${unresolved.length === 0 ? "#86efac" : "#fde68a"}`,
          fontSize: 13,
          color: unresolved.length === 0 ? "#065f46" : "#854d0e",
        }}
      >
        {slots.length === 0 ? (
          matching ? (
            <span>Looking for marketplace products that match this consumer's description and schema…</span>
          ) : (
            <span>
              No marketplace matches yet. You can pick an upstream product
              yourself below, or proceed to submit if this consumer genuinely needs none.
            </span>
          )
        ) : (
          <span>
            <strong>{matchedCount} of {slots.length}</strong> candidate{slots.length === 1 ? "" : "s"}{" "}
            confirmed.{" "}
            {unresolved.length > 0 && "Confirm the remaining candidates (or request new source-aligned data products for any gaps) before submitting."}
          </span>
        )}
      </div>

      {slots.map((slot) => (
        <SlotRow
          key={slot.slot_id}
          slot={slot}
          onChange={(update) => onSlotChange(slot.slot_id, update)}
          onCreateGap={() => onCreateGap(slot)}
          onRemove={onRemove ? () => onRemove(slot.slot_id) : undefined}
          mode={mode}
        />
      ))}

      {mode === "ingest" && slots.length === 0 && !matching && (
        <div style={{ fontSize: 12, color: "#64748b" }}>
          If the imported spec didn't declare any inputs and the classifier didn't
          infer any dependencies, this consumer product has nothing upstream to
          bind — you can submit as-is.
        </div>
      )}
    </div>
  );
}

interface SlotRowProps {
  slot: ResolveSlot;
  onChange: (update: Partial<ResolveSlot>) => void;
  onCreateGap: () => void;
  onRemove?: () => void;
  mode: "wizard" | "ingest";
}

function SlotRow({ slot, onChange, onCreateGap, onRemove, mode }: SlotRowProps) {
  const selectedCandidate = slot.candidates.find(
    (c) => c.uri === (slot.selected_uri ?? slot.preselected_candidate_uri),
  );
  const isMatched = slot.resolution === "matched" && !!slot.selected_uri;
  const isGap = slot.resolution === "gap";
  // Tentative — matched but the confidence band is borderline. The card
  // shifts to amber (same family as gap) instead of green so the PO
  // visually notices this isn't a high-confidence pick, and the "Not the
  // right fit" affordance below stays in scope as a prominent escape.
  const isTentative = isMatched && slot.confidence_band === "tentative"
    && slot.source !== "manual";

  const handleCandidateChange = (uri: string) => {
    if (uri === "__gap__") {
      onChange({
        resolution: "gap",
        selected_uri: null,
        selected_contract_id: null,
        selected_name: null,
      });
      return;
    }
    const c = slot.candidates.find((cc) => cc.uri === uri);
    if (!c) return;
    onChange({
      resolution: "matched",
      selected_uri: c.uri,
      selected_contract_id: c.contract_id,
      selected_name: c.name,
    });
  };

  const headerLabel =
    slot.declared.name ||
    slot.gap_suggestion?.name ||
    (slot.source === "inferred" ? "Inferred dependency" : "Declared source input");

  return (
    <div
      style={{
        borderRadius: 10,
        border: `1px solid ${
          isTentative ? "#fde68a"
          : isMatched ? "#86efac"
          : isGap ? "#fde68a"
          : "#e2e8f0"
        }`,
        backgroundColor:
          isTentative ? "#fffbeb"
          : isMatched ? "#f0fdf4"
          : isGap ? "#fefce8"
          : "#fff",
        padding: 14,
        display: "flex",
        flexDirection: "column",
        gap: 10,
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 10 }}>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 14, fontWeight: 700, color: "#0f172a" }}>{headerLabel}</div>
          {slot.encompasses_hint && (
            <div style={{ fontSize: 12, color: "#475569", marginTop: 4 }}>
              <strong>Encompasses:</strong> {slot.encompasses_hint}
            </div>
          )}
          {slot.declared.dprod_uri && (
            <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 4, fontFamily: "monospace" }}>
              {slot.declared.dprod_uri}
            </div>
          )}
        </div>
        <SlotStatusChip slot={slot} />
      </div>

      {/* Manual slots: the PO already picked this from the marketplace, so
          there's no decision left to make here. The row's header already
          shows the product name + description (via encompasses_hint) — no
          duplicate static block needed. Inline Remove control is the exit
          ramp when the host opted in via the onRemove prop.

          Inferred/recommended slots get the same Remove control, but only
          while in gap state — lets the PO dismiss a "needs new source"
          recommendation they don't want to act on, without binding to an
          imperfect candidate or spawning a source request. Matched inferred
          slots keep their candidate dropdown / "Not the right fit?" path. */}
      {(slot.source === "manual" || isGap) && onRemove && (
        <button
          type="button"
          onClick={onRemove}
          style={{
            alignSelf: "flex-start",
            background: "transparent",
            border: "none",
            color: "#dc2626",
            fontSize: 11,
            cursor: "pointer",
            textDecoration: "underline",
            padding: 0,
          }}
        >
          {slot.source === "manual"
            ? "Remove this upstream product"
            : "Ignore this recommendation"}
        </button>
      )}

      {/* Recommended / inferred slots with >1 candidate: let the PO swap
          between candidates if they don't like the top suggestion. Single-
          candidate slots from the system get a static confirmation block
          (same as manual) plus a "Not the right fit?" affordance to mark gap. */}
      {slot.source !== "manual" && slot.candidates.length > 1 && (
        <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <span style={{ fontSize: 12, fontWeight: 600, color: "#334155" }}>
            Top candidates (sorted by match score)
          </span>
          <select
            value={
              isGap
                ? "__gap__"
                : (slot.selected_uri ?? slot.preselected_candidate_uri ?? slot.candidates[0]?.uri ?? "")
            }
            onChange={(e) => handleCandidateChange(e.target.value)}
            style={{
              padding: "8px 10px",
              borderRadius: 6,
              border: "1px solid #cbd5e1",
              fontSize: 13,
            }}
          >
            {slot.candidates.map((c) => (
              <option key={c.uri} value={c.uri}>
                {c.name} · score {c.match_score}
              </option>
            ))}
          </select>
        </label>
      )}

      {slot.source !== "manual" && slot.candidates.length === 1 && selectedCandidate && (
        <div
          style={{
            padding: 10,
            borderRadius: 6,
            backgroundColor: "#fff",
            border: "1px solid #cbd5e1",
            fontSize: 13,
            color: "#0f172a",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <span style={{ fontWeight: 600 }}>{selectedCandidate.name}</span>
            <ProductKindChip kind={selectedCandidate.product_kind} compact />
          </div>
          {selectedCandidate.description && (
            <div style={{ fontSize: 12, color: "#475569", marginTop: 4 }}>
              {selectedCandidate.description}
            </div>
          )}
        </div>
      )}

      {selectedCandidate && isMatched && selectedCandidate.rationale && slot.source !== "manual" && (
        <div style={{ fontSize: 12, color: "#475569" }}>
          <strong>Why:</strong> {selectedCandidate.rationale}
        </div>
      )}

      {/* "Not the right fit?" toggle — only meaningful for system-suggested
          slots (manual slots have a Remove control instead). */}
      {slot.source !== "manual" && !isGap && (
        <button
          type="button"
          onClick={() =>
            onChange({
              resolution: "gap",
              selected_uri: null,
              selected_contract_id: null,
              selected_name: null,
            })
          }
          style={{
            alignSelf: "flex-start",
            background: "transparent",
            border: "none",
            color: "#475569",
            fontSize: 11,
            cursor: "pointer",
            textDecoration: "underline",
            padding: 0,
          }}
        >
          Not the right fit — request a new source-aligned data product instead
        </button>
      )}

      {isGap && slot.gap_suggestion && (
        <div
          style={{
            padding: 10,
            borderRadius: 8,
            backgroundColor: "#fff",
            border: "1px solid #fde68a",
            fontSize: 12,
            color: "#475569",
          }}
        >
          <div style={{ fontWeight: 700, color: "#854d0e", marginBottom: 4 }}>
            Suggested source-aligned data product to create:
          </div>
          <div>
            <strong>{slot.gap_suggestion.name}</strong>
            {slot.gap_suggestion.domain && <> · domain: {slot.gap_suggestion.domain}</>}
          </div>
          <div style={{ marginTop: 4 }}>{slot.gap_suggestion.encompasses}</div>
          {slot.gap_suggestion.minimal_columns.length > 0 && (
            <div style={{ marginTop: 6 }}>
              <strong>Minimal columns:</strong>{" "}
              {slot.gap_suggestion.minimal_columns
                .map((c) => (c.type ? `${c.name} (${c.type})` : c.name))
                .join(", ")}
            </div>
          )}
          {slot.spawned_request_id ? (
            <div style={{ marginTop: 8, fontSize: 12, color: "#065f46" }}>
              ✓ Source request <strong>#{slot.spawned_request_id}</strong> created.
              Waiting for it to be published before this slot auto-binds.
            </div>
          ) : (
            <div style={{ marginTop: 10 }}>
              <button
                type="button"
                onClick={onCreateGap}
                style={{
                  padding: "7px 14px",
                  borderRadius: 6,
                  backgroundColor: productTheme.accent,
                  color: "#fff",
                  border: "none",
                  fontSize: 12,
                  fontWeight: 700,
                  cursor: "pointer",
                }}
              >
                Create now →
              </button>
              <span style={{ fontSize: 11, color: "#64748b", marginLeft: 10 }}>
                {mode === "ingest"
                  ? "Saves this ingest as a draft; you'll return here once the source is published."
                  : "Opens the source product wizard with this slot's context prefilled."}
              </span>
            </div>
          )}
        </div>
      )}

      {/* Considered candidates — when the slot ended up as a gap (no
          preselect) but the classifier did rank some marketplace products
          against it, surface them as a "low confidence" disclosure with
          an explicit "Use anyway" override. Helps when the PO knows the
          classifier was right to refuse but wants to bind to an
          imperfect-but-good-enough existing product anyway. */}
      {isGap && slot.source !== "manual" && slot.candidates.length > 0 && (
        <ConsideredCandidates
          candidates={slot.candidates}
          onUseAnyway={(c) =>
            onChange({
              resolution: "matched",
              selected_uri: c.uri,
              selected_contract_id: c.contract_id,
              selected_name: c.name,
            })
          }
        />
      )}
    </div>
  );
}

function ConsideredCandidates({
  candidates,
  onUseAnyway,
}: {
  candidates: ResolveSlotCandidate[];
  onUseAnyway: (c: ResolveSlotCandidate) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div style={{ fontSize: 12 }}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        style={{
          background: "transparent",
          border: "none",
          color: "#475569",
          fontSize: 11,
          cursor: "pointer",
          padding: 0,
          textDecoration: "underline",
        }}
      >
        {open ? "▾" : "▸"} Considered candidates (low confidence) — {candidates.length}
      </button>
      {open && (
        <div style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 6 }}>
          {candidates.map((c) => (
            <div
              key={c.uri}
              style={{
                padding: 8,
                borderRadius: 6,
                backgroundColor: "#fff",
                border: "1px solid #e2e8f0",
                display: "flex",
                justifyContent: "space-between",
                alignItems: "flex-start",
                gap: 10,
              }}
            >
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 12, fontWeight: 700, color: "#0f172a", display: "flex", alignItems: "center", gap: 6 }}>
                  <span>{c.name}</span>
                  <ProductKindChip kind={c.product_kind} compact />
                  <span style={{ fontWeight: 400, color: "#94a3b8", fontSize: 11 }}>
                    · score {c.match_score}
                  </span>
                </div>
                {c.rationale && (
                  <div style={{ fontSize: 11, color: "#475569", marginTop: 3 }}>
                    {c.rationale}
                  </div>
                )}
              </div>
              <button
                type="button"
                onClick={() => onUseAnyway(c)}
                style={{
                  flexShrink: 0,
                  padding: "4px 10px",
                  borderRadius: 4,
                  border: "1px solid #cbd5e1",
                  backgroundColor: "#fff",
                  color: "#334155",
                  fontSize: 11,
                  fontWeight: 600,
                  cursor: "pointer",
                }}
                title="Bind this slot to this candidate despite the low-confidence rationale"
              >
                Use anyway →
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function SlotStatusChip({
  slot,
}: {
  slot: {
    resolution?: "matched" | "gap";
    selected_uri?: string | null;
    source?: "spec_inputs" | "inferred" | "manual";
    confidence_band?: "strong" | "tentative" | "gap";
  };
}) {
  if (slot.resolution === "matched" && slot.selected_uri) {
    // Distinguish "recommended candidate confirmed" (system suggested, PO
    // accepted) from "PO-picked" (PO manually added from the marketplace).
    if (slot.source === "manual") {
      return <Chip color="#1e3a8a" bg="#dbeafe" label="✓ Selected" />;
    }
    // Tentative — preselected but the top candidate's match score is in
    // the borderline band (threshold..80). The card still shows the
    // candidate but the chip + treatment make clear the PO should
    // verify, not rubber-stamp.
    if (slot.confidence_band === "tentative") {
      return <Chip color="#92400e" bg="#fef3c7" label="⚠ Tentative match" />;
    }
    return <Chip color="#065f46" bg="#dcfce7" label="✓ Recommended" />;
  }
  if (slot.resolution === "gap") {
    return <Chip color="#854d0e" bg="#fef3c7" label="● Needs new source" />;
  }
  return <Chip color="#64748b" bg="#f1f5f9" label="Pending" />;
}

function Chip({ color, bg, label }: { color: string; bg: string; label: string }) {
  return (
    <span
      style={{
        display: "inline-block",
        padding: "3px 9px",
        borderRadius: 999,
        fontSize: 11,
        fontWeight: 700,
        color,
        backgroundColor: bg,
        whiteSpace: "nowrap",
      }}
    >
      {label}
    </span>
  );
}


