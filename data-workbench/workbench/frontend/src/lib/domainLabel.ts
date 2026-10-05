// Human display name for a data-product domain.
//
// The internal `domain` value is a slug (e.g. "products_sales", "retail banking")
// stored on products and used for domain-catalog path resolution. The UI should
// show a proper name instead of the raw slug. Domain catalogs may carry an
// explicit `label` (from playbook/domain_catalogs/*.yaml, surfaced by
// /api/domain-catalogs); when absent, fall back to a title-cased slug.

/** Title-case a domain slug: "products_sales" -> "Products Sales". */
export function titleCaseDomain(slug: string | null | undefined): string {
  if (!slug) return "";
  return slug
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/\b\w/g, (ch) => ch.toUpperCase());
}

export interface DomainLabelSource {
  domain: string;
  label?: string | null;
}

/** Prefer an explicit catalog `label` for `slug`; else title-case the slug. */
export function domainLabel(
  slug: string | null | undefined,
  catalogs?: DomainLabelSource[] | null,
): string {
  if (!slug) return "";
  const hit = catalogs?.find((c) => c.domain === slug);
  const label = hit?.label?.trim();
  return label || titleCaseDomain(slug);
}
