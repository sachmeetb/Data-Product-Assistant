// Field-level diff between a deployed contract version and an in-flight
// edit. Used in two places (kept identical so the PO and engineer see the
// same picture):
//   1. Wizard EditDiffPanel — PO-side, computes diff against the deployed
//      snapshot taken at hydration time.
//   2. Engineer-side ProjectEditBanner — computes server-side via
//      /api/projects/{id}/edit-diff which serializes the same shape.
//
// The diff is intentionally column-level + metadata-level only. Rule diffs
// are deferred (the wizard's pending rules state is local-only until
// /persist-user-rules fires; doing a meaningful rules diff would require
// a parallel snapshot of approved rules from the deployed graph).

export interface ColumnSnapshot {
  name: string;
  physicalType?: string | null;
  logicalType?: string | null;
  description?: string | null;
  primaryKey?: boolean;
}

// Source-aligned product the consumer :CONSUMES. Identity is the dprod_uri;
// name carries through for display so removed entries can render usefully.
export interface InputSnapshot {
  dprod_uri: string;
  name?: string;
}

export interface SpecSnapshot {
  name: string;
  description: string;
  purpose: string;
  datasetName: string;
  columns: ColumnSnapshot[];
  // Consumer-aligned only — empty array for SA / dq / dd archetypes. Drives
  // the inputs section of the edit diff (added/removed source products).
  inputs?: InputSnapshot[];
}

export interface ColumnDiff {
  added: string[];
  removed: string[];
  typeChanged: { name: string; from: string; to: string }[];
  descriptionChanged: { name: string; from: string; to: string }[];
}

export interface MetadataDiff {
  changedFields: { field: string; from: string; to: string }[];
}

export interface InputsDiff {
  added: { dprod_uri: string; name: string }[];
  removed: { dprod_uri: string; name: string }[];
}

export interface EditDiff {
  columns: ColumnDiff;
  metadata: MetadataDiff;
  inputs: InputsDiff;
}

const norm = (v: string | null | undefined): string => (v ?? "").trim();

export function computeEditDiff(deployed: SpecSnapshot, current: SpecSnapshot): EditDiff {
  const deployedByName = new Map(deployed.columns.map((c) => [c.name, c]));
  const currentByName = new Map(current.columns.map((c) => [c.name, c]));

  const added: string[] = [];
  const removed: string[] = [];
  const typeChanged: ColumnDiff["typeChanged"] = [];
  const descriptionChanged: ColumnDiff["descriptionChanged"] = [];

  for (const [name, col] of currentByName) {
    if (!deployedByName.has(name)) {
      added.push(name);
      continue;
    }
    const old = deployedByName.get(name)!;
    const oldType = norm(old.physicalType) || norm(old.logicalType);
    const newType = norm(col.physicalType) || norm(col.logicalType);
    if (oldType && newType && oldType !== newType) {
      typeChanged.push({ name, from: oldType, to: newType });
    }
    const oldDesc = norm(old.description);
    const newDesc = norm(col.description);
    if (oldDesc !== newDesc) {
      descriptionChanged.push({ name, from: oldDesc, to: newDesc });
    }
  }

  for (const name of deployedByName.keys()) {
    if (!currentByName.has(name)) {
      removed.push(name);
    }
  }

  const changedFields: MetadataDiff["changedFields"] = [];
  const metaPairs: [keyof SpecSnapshot, string][] = [
    ["name", "Product name"],
    ["description", "Description"],
    ["purpose", "Purpose"],
    ["datasetName", "Dataset name"],
  ];
  for (const [key, label] of metaPairs) {
    const before = norm(deployed[key] as string);
    const after = norm(current[key] as string);
    if (before !== after) {
      changedFields.push({ field: label, from: before, to: after });
    }
  }

  // Inputs diff (consumer-aligned only). Source-aligned products and other
  // archetypes have empty arrays on both sides → both diff lists empty.
  const deployedInputs = new Map((deployed.inputs ?? []).map((i) => [i.dprod_uri, i]));
  const currentInputs = new Map((current.inputs ?? []).map((i) => [i.dprod_uri, i]));
  const inputsAdded: InputsDiff["added"] = [];
  const inputsRemoved: InputsDiff["removed"] = [];
  for (const [uri, inp] of currentInputs) {
    if (!deployedInputs.has(uri)) inputsAdded.push({ dprod_uri: uri, name: inp.name ?? "" });
  }
  for (const [uri, inp] of deployedInputs) {
    if (!currentInputs.has(uri)) inputsRemoved.push({ dprod_uri: uri, name: inp.name ?? "" });
  }
  inputsAdded.sort((a, b) => a.dprod_uri.localeCompare(b.dprod_uri));
  inputsRemoved.sort((a, b) => a.dprod_uri.localeCompare(b.dprod_uri));

  return {
    columns: { added, removed, typeChanged, descriptionChanged },
    metadata: { changedFields },
    inputs: { added: inputsAdded, removed: inputsRemoved },
  };
}

export function isEmptyDiff(diff: EditDiff): boolean {
  return (
    diff.columns.added.length === 0
    && diff.columns.removed.length === 0
    && diff.columns.typeChanged.length === 0
    && diff.columns.descriptionChanged.length === 0
    && diff.metadata.changedFields.length === 0
    && diff.inputs.added.length === 0
    && diff.inputs.removed.length === 0
  );
}

// Pluralize a noun by count without bringing in a library.
const plural = (n: number, singular: string, pluralForm?: string): string =>
  `${n} ${n === 1 ? singular : (pluralForm ?? `${singular}s`)}`;

// Human-readable impact strings shown in the diff panel + engineer banner.
// Keep these short — they sit alongside the raw diff so the prose is
// summary, not narration.
export function summarizeImpact(diff: EditDiff): string[] {
  const out: string[] = [];
  const c = diff.columns;
  const i = diff.inputs;
  if (c.added.length > 0) {
    out.push(`${plural(c.added.length, "column")} added — engineering must rerun Mapping and the serving DDL.`);
  }
  if (c.removed.length > 0) {
    out.push(`${plural(c.removed.length, "column")} removed — engineering must regenerate the virtual view DDL.`);
  }
  if (c.typeChanged.length > 0) {
    out.push(`${plural(c.typeChanged.length, "column")} with type changes — DQ tests for affected columns may need to be regenerated.`);
  }
  if (i.added.length > 0 || i.removed.length > 0) {
    const bits: string[] = [];
    if (i.added.length > 0) bits.push(`${plural(i.added.length, "input")} added`);
    if (i.removed.length > 0) bits.push(`${plural(i.removed.length, "input")} removed`);
    out.push(`${bits.join(", ")} — engineering must rerun ODCS→dprod, Mapping, and the serving DDL.`);
  }
  if (c.descriptionChanged.length > 0 && c.added.length === 0 && c.removed.length === 0 && c.typeChanged.length === 0 && i.added.length === 0 && i.removed.length === 0) {
    out.push("Only descriptions changed — no engineering rerun needed.");
  }
  if (diff.metadata.changedFields.length > 0 && out.length === 0) {
    out.push("Only metadata changed — no engineering rerun needed.");
  }
  return out;
}

// Suggested stage IDs to rerun based on the diff. The engineer banner
// uses this to render deep links into Pipeline.tsx Rerun controls.
export function suggestedRerunStages(diff: EditDiff): string[] {
  const stages = new Set<string>();
  const c = diff.columns;
  const i = diff.inputs;
  if (c.added.length > 0) {
    stages.add("data_mapping");
    stages.add("metadata_enrichment");
    stages.add("serving_virtual_view");
  }
  if (c.removed.length > 0) {
    stages.add("data_mapping");
    stages.add("serving_virtual_view");
  }
  if (c.typeChanged.length > 0) {
    stages.add("data_mapping");
    stages.add("dq_test_generation_gx");
  }
  if (i.added.length > 0 || i.removed.length > 0) {
    stages.add("odcs_to_dprod");
    stages.add("data_mapping");
    stages.add("serving_virtual_view");
  }
  // Serving is an exclusive group — if the view stage is flagged, flag every
  // member so the ACTIVE serving mode (materialized / lakehouse / transfer)
  // resets too. Mirrors edits.py:_expand_serving_group.
  if (stages.has("serving_virtual_view")) {
    stages.add("serving_physical_copy");
    stages.add("serving_lakehouse_export");
    stages.add("serving_transfer");
  }
  return Array.from(stages);
}
