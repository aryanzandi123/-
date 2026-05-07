/**
 * Claim utility helpers ported from `static/modal.js`.
 *
 * - `_PLACEHOLDER_SNIPPETS` and `isPlaceholderText` — drop pipeline stub text
 *   like "discovered via chain resolution" so the modal doesn't render it
 *   five times per claim card.
 * - `isPathwayInContext` — port of the P3.3 logic that decides whether a
 *   claim renders under the currently-selected pathway. Two signals:
 *   (a) the claim's own pathway matches, or
 *   (b) the claim's `_hierarchy` ancestor chain contains the context.
 *   Sibling-claim leak (rule 3 in the legacy code) was deliberately
 *   removed — don't re-add.
 */

import type { Claim } from "@/types/api";

export const PLACEHOLDER_SNIPPETS: readonly string[] = [
  "not fully characterized",
  "not specified",
  "discovered via chain resolution",
  "function data not generated",
  "data not generated",
  "uncharacterized interaction",
  "no mechanism documented",
];

export function isPlaceholderText(s: unknown): boolean {
  if (typeof s !== "string" || !s) return true;
  const lower = s.toLowerCase();
  return PLACEHOLDER_SNIPPETS.some((frag) => lower.includes(frag));
}

const _norm = (s: string): string =>
  s.toLowerCase().replace(/[_-]/g, " ").replace(/\s+/g, " ").trim();

export function isPathwayInContext(
  claim: Claim,
  pathwayContext: string | null,
): boolean {
  if (!pathwayContext) return false;
  const ctxNorm = _norm(pathwayContext);
  const fnPathwayRaw = claim.pathway;
  const fnPathway =
    typeof fnPathwayRaw === "string"
      ? fnPathwayRaw
      : (fnPathwayRaw as { canonical_name?: string; name?: string } | null | undefined)?.canonical_name ??
        (fnPathwayRaw as { name?: string } | null | undefined)?.name;
  if (typeof fnPathway === "string" && _norm(fnPathway) === ctxNorm) return true;
  const fnHierarchy = Array.isArray(claim._hierarchy) ? claim._hierarchy : [];
  return fnHierarchy.some((h) => typeof h === "string" && _norm(h) === ctxNorm);
}

export type ClaimSpecial =
  | { kind: "synthetic"; pathway: string }
  | { kind: "thin"; title: string; prose: string }
  | { kind: "router"; title: string; outcome: string }
  | { kind: "garbage"; rawName: string }
  | { kind: "normal"; functionName: string };

export function classifyClaim(claim: Claim): ClaimSpecial {
  if (claim._synthetic) {
    return { kind: "synthetic", pathway: String(claim.pathway ?? "Unassigned") };
  }
  if (claim._thin_claim) {
    return {
      kind: "thin",
      title: String(claim.function ?? "Pair biology not characterized"),
      prose: String(claim.cellular_process ?? ""),
    };
  }
  if (claim._synthetic_from_router) {
    return {
      kind: "router",
      title: String(claim.function ?? "Pair-specific biology pending manual curation"),
      outcome: String(claim._router_outcome_summary ?? claim.cellular_process ?? ""),
    };
  }
  const rawName = String(claim.function ?? "");
  const garbageRe = /^__fallback__$|^(activates?|inhibits?|binds?|regulates?|interacts?) interaction$/i;
  if (garbageRe.test(rawName)) return { kind: "garbage", rawName };
  return { kind: "normal", functionName: rawName || "Function" };
}

export function pickStringList(v: unknown): string[] {
  if (!v) return [];
  if (Array.isArray(v)) return v.filter((x) => typeof x === "string" && !isPlaceholderText(x));
  if (typeof v === "string" && !isPlaceholderText(v)) return [v];
  return [];
}

export interface EvidenceEntry {
  pmid?: string;
  quote?: string;
  year?: number;
}

export function pickEvidence(v: unknown): EvidenceEntry[] {
  if (!Array.isArray(v)) return [];
  const out: EvidenceEntry[] = [];
  for (const e of v) {
    if (!e || typeof e !== "object") continue;
    const ev = e as EvidenceEntry;
    if (ev.pmid || ev.quote) out.push(ev);
  }
  return out;
}

/**
 * Endpoint-mention check used by the modal to label which leg of a chain a
 * given cascade sentence is talking about. Matches whole-word, case-insensitive,
 * and tolerates the symbol style sentences mostly use ("CANX recruits PERK to
 * MAM", "CANX-deficient cells…"). Returns the set of endpoints in `proteins`
 * that the text references at least once.
 *
 * Used in ClaimRenderer to surface CANX↔PERK vs CANX↔HSPA9 mismatches between
 * the chain edge and the prose, which previously rendered without context.
 */
export function mentionedEndpoints(
  text: string,
  proteins: ReadonlyArray<string>,
): Set<string> {
  const hits = new Set<string>();
  if (!text || proteins.length === 0) return hits;
  const upper = text.toUpperCase();
  for (const p of proteins) {
    if (!p) continue;
    const sym = p.toUpperCase();
    // Word-boundary match. JS \b respects ASCII letters/digits/underscore which
    // is sufficient for gene symbols (HSPA9, ATXN3, CANX, etc.).
    const re = new RegExp(`(^|[^A-Z0-9])${sym.replace(/[-/\\^$*+?.()|[\]{}]/g, "\\$&")}([^A-Z0-9]|$)`);
    if (re.test(upper)) hits.add(sym);
  }
  return hits;
}
