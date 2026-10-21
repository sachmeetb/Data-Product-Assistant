import type { Confidence } from "../../components/ConfidenceChip";

export type ReviewState = "suggested" | "confirmed" | "edited" | "excluded";

export interface ConfidenceField {
  value: string | null;
  confidence: Confidence;
  why?: string;
  review_state?: ReviewState;
}

export interface ColumnCandidate {
  candidate_id?: string | null;
  name: ConfidenceField;
  data_type?: ConfidenceField;
  note?: string;
}

export interface DatasetCandidate {
  candidate_id?: string | null;
  name: ConfidenceField;
  columns?: ColumnCandidate[];
  incremental_cursor?: ConfidenceField;
  review_state?: ReviewState;
}

export interface Gap {
  field: string;
  why?: string;
}

export interface MigrationBlueprint {
  scenario: "migration";
  blueprint_version?: string;
  overall_confidence?: Confidence;
  project_name: ConfidenceField;
  domain?: ConfidenceField;
  source_platform?: ConfidenceField;
  target_platform?: ConfidenceField;
  write_disposition?: ConfidenceField;
  datasets?: DatasetCandidate[];
  gaps?: Gap[];
  rationale?: string;
}

export interface ProductCandidate {
  candidate_id?: string | null;
  name: ConfidenceField;
  domain?: ConfidenceField;
  description?: string;
  product_idea?: string;
  purpose?: string;
  odcs?: Record<string, unknown> | null;
  datasets?: DatasetCandidate[];
  confidence?: Confidence;
  review_state?: ReviewState;
}

export interface Dependency {
  dependency_id?: string | null;
  from_candidate_id: string;
  to_candidate_id?: string | null;
  to_external_uri?: string | null;
  confidence?: Confidence;
  review_state?: ReviewState;
}

export interface ModernizationBlueprint {
  scenario: "modernization";
  blueprint_version?: string;
  overall_confidence?: Confidence;
  source_aligned?: ProductCandidate[];
  consumer_aligned?: ProductCandidate[];
  dependencies?: Dependency[];
  gaps?: Gap[];
  rationale?: string;
}

export type Blueprint = MigrationBlueprint | ModernizationBlueprint;

export interface PlatformAdvisory {
  field: string;                 // "source_platform" | "target_platform"
  platform: string;              // the free-form value the parser emitted
  resolved_id: string | null;    // registry manifest id, or null if unknown
  level: string;                 // capability level (or "unknown")
  severity: "ok" | "warning" | "error";
  message: string;
}

export interface IntakeSubmission {
  id: number;
  source_system: string;
  external_ref: string;
  scenario: "migration" | "modernization";
  status: string;
  execution_mode?: "live" | "schema_only";
  blueprint: Blueprint | null;
  blueprint_revision: number;
  parse_meta?: { error?: string; [k: string]: unknown } | null;
  reviewed_by?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  approval_blockers?: string[];
  platform_advisories?: PlatformAdvisory[];
}
