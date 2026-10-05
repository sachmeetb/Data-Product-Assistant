import type { ResolveSlot, ResolveSlotCandidate } from "../../../types";

/**
 * Helpers for the shared ResolveAndBindSourcesStep component. Lives in a
 * separate file so the .tsx module exports only React components (Vite's
 * react-refresh plugin warns otherwise).
 */

/**
 * True if every slot is bound to a real marketplace product. The
 * submit-disable predicate is shared with the server-side gate in
 * routers/ingest_products.py:from-odcs.
 */
export function allSlotsMatched(slots: ResolveSlot[]): boolean {
  if (slots.length === 0) return true;
  return slots.every(
    (s) => s.resolution === "matched" && !!s.selected_uri,
  );
}

/**
 * Convert a /match-inputs response slot into the hydrated UI shape with
 * resolution + selected_* prefilled from preselected_candidate_uri. Used by
 * both the wizard step-7 entry and the ingest resolve step.
 */
export function hydrateSlotFromMatch(slot: {
  slot_id: string;
  source: ResolveSlot["source"];
  declared: ResolveSlot["declared"];
  encompasses_hint?: string | null;
  candidates: ResolveSlotCandidate[];
  preselected_candidate_uri: string | null;
  confidence_band?: ResolveSlot["confidence_band"];
  gap_suggestion: ResolveSlot["gap_suggestion"];
}): ResolveSlot {
  const preselect = slot.candidates.find((c) => c.uri === slot.preselected_candidate_uri);
  return {
    ...slot,
    confidence_band: slot.confidence_band,
    resolution: preselect ? "matched" : "gap",
    selected_uri: preselect?.uri ?? null,
    selected_contract_id: preselect?.contract_id ?? null,
    selected_name: preselect?.name ?? null,
    spawned_request_id: null,
    spawned_project_id: null,
  };
}
