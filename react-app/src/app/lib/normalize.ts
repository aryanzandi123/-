/**
 * Pathway name + protein pair normalization. Ports from
 * `utils/chain_resolution.canonical_pair_key` and the `.trim().toLowerCase()`
 * pathway compare in `static/card_view.js`. Used by the chain pathway gate
 * and pathway navigator selection logic.
 */

/** Canonical pair key: order-independent so {A,B} === {B,A}. */
export function canonicalPairKey(a: string, b: string): string {
  const A = a.toUpperCase().trim();
  const B = b.toUpperCase().trim();
  return A < B ? `${A}|${B}` : `${B}|${A}`;
}

/** Lowercase + collapse whitespace + drop punctuation that varies between
 *  emit sites (`Protein Quality Control` vs `protein quality_control`). */
export function normalizePathwayName(name: string | null | undefined): string {
  if (!name) return "";
  return name
    .toLowerCase()
    .replace(/[_\s]+/g, " ")
    .replace(/[^a-z0-9 &/-]/g, "")
    .trim();
}

/** Equality check using the normalized form. */
export function pathwayNameMatches(a: string | null | undefined, b: string | null | undefined): boolean {
  if (!a || !b) return false;
  return normalizePathwayName(a) === normalizePathwayName(b);
}
