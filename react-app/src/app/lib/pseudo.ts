/**
 * Pseudo-protein whitelist. Mirror of `utils/db_sync._PSEUDO_WHITELIST`.
 * Single source of truth for "is this name a real protein vs a token?"
 * Used by the FilterChips `pseudo on/off` toggle and per-node pseudo flags.
 *
 * If the backend grows new pseudo entries, sync this list. Long-term plan
 * (Phase 6): expose via `/api/pseudo_whitelist` and codegen.
 */

export const PSEUDO_NAMES: ReadonlySet<string> = new Set<string>([
  "RNA",
  "DNA",
  "MRNA",
  "RRNA",
  "TRNA",
  "MIRNA",
  "LNCRNA",
  "PRE-MRNA",
  "PRE_MRNA",
  "UBIQUITIN",
  "POLY_UB",
  "POLY-UB",
  "K48-UB",
  "K63-UB",
  "K48",
  "K63",
  "PROTEASOME",
  "RIBOSOME",
  "EXOSOME",
  "AGGRESOME",
  "AUTOPHAGOSOME",
  "LYSOSOME",
  "ATP",
  "ADP",
  "AMP",
  "GTP",
  "GDP",
  "CYTOCHROME C",
  "CYT C",
]);

export function isPseudoProtein(name: string | null | undefined): boolean {
  if (!name) return false;
  return PSEUDO_NAMES.has(name.toUpperCase().trim());
}
