/**
 * Rule-type → category mapping shared by all rule-list surfaces (engineering
 * project DQ Rules detail, marketplace Quality tab). Keeping this in one
 * place stops the two surfaces from drifting on naming.
 *
 * The vocabulary mixes outputs from several skills:
 *   - data-quality-rule-generation writes mandatory / unique / range /
 *     allowedValues / referentialIntegrity (observation rules).
 *   - domain catalogs use notNull / unique / allowedValues / range /
 *     regex / maxLength.
 *   - ODCS specs use completeness / uniqueness / validity / accuracy /
 *     timeliness (dimension-flavoured).
 * We bucket all of them into four user-facing categories.
 */

export type RuleCategory = "Structural" | "Value" | "Length" | "Other";

export const RULE_CATEGORIES: ReadonlyArray<RuleCategory> = [
  "Structural",
  "Value",
  "Length",
  "Other",
];

export function categorizeRuleType(ruleType: string | null | undefined): RuleCategory {
  const t = (ruleType || "").trim().toLowerCase();
  if (
    t === "notnull" ||
    t === "mandatory" ||
    t === "completeness" ||
    t === "unique" ||
    t === "uniqueness" ||
    t === "primarykey" ||
    t === "referentialintegrity"
  ) return "Structural";
  if (
    t === "allowedvalues" ||
    t === "range" ||
    t === "regex" ||
    t === "validity" ||
    t === "accuracy"
  ) return "Value";
  if (t === "maxlength" || t === "minlength" || t === "length") return "Length";
  return "Other";
}

export const CATEGORY_COLORS: Record<RuleCategory, { bg: string; fg: string }> = {
  Structural: { bg: "#dbeafe", fg: "#1d4ed8" },
  Value:      { bg: "#fef3c7", fg: "#92400e" },
  Length:     { bg: "#dcfce7", fg: "#15803d" },
  Other:      { bg: "#f1f5f9", fg: "#475569" },
};
