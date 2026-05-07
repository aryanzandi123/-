/**
 * Arrow class → color mapping. Mirrors the legacy `static/styles.css`
 * `.cv-chain-edge-label.arrow-*` rules so the new CardView reads from the
 * same CSS variable theme the user already knows.
 *
 * "Modernize existing aesthetic" was Q1's resolution — keep the verb-color
 * relationships continuous with the legacy app.
 */

import type { ArrowClass } from "@/types/api";

export type ArrowKind = "positive" | "negative" | "binding" | "regulatory" | "reverse" | "neutral";

const POSITIVE = new Set<string>([
  "activates",
  "phosphorylates",
  "stabilizes",
  "induces",
  "recruits",
  "deubiquitinates",
]);

const NEGATIVE = new Set<string>([
  "inhibits",
  "degrades",
  "destabilizes",
  "represses",
  "ubiquitinates",
  "cleaves",
  "sequesters",
]);

const BINDING = new Set<string>(["binds"]);

const REGULATORY = new Set<string>(["regulates"]);

// Mirror of `utils/chain_resolution._REVERSE_VERBS` (Python). Reverse-direction
// verbs render italic in edge labels so the user sees biological direction
// even when spatial layout has source spatially below target.
const REVERSE = new Set<string>([
  "is_substrate_of",
  "is_activated_by",
  "is_inhibited_by",
  "is_phosphorylated_by",
  "is_ubiquitinated_by",
  "is_degraded_by",
  "is_cleaved_by",
  "is_regulated_by",
  "is_recruited_by",
  "is_stabilized_by",
  "is_destabilized_by",
  "is_repressed_by",
]);

export function classifyArrow(arrow: ArrowClass | string | null | undefined): ArrowKind {
  if (!arrow) return "neutral";
  const a = String(arrow).toLowerCase();
  if (REVERSE.has(a)) return "reverse";
  // Generic suffix fallback for unlisted reverse verbs (`is_*_by`, `is_*_of`).
  if (a.startsWith("is_") && (a.endsWith("_by") || a.endsWith("_of"))) return "reverse";
  if (POSITIVE.has(a)) return "positive";
  if (NEGATIVE.has(a)) return "negative";
  if (BINDING.has(a)) return "binding";
  if (REGULATORY.has(a)) return "regulatory";
  return "neutral";
}

/** Hex (dark-theme) palette tuned to match `static/styles.css` accents. */
export const ARROW_COLORS: Record<ArrowKind, string> = {
  positive: "#10b981",
  negative: "#ef4444",
  binding: "#a78bfa",
  regulatory: "#f59e0b",
  reverse: "#94a3b8",
  neutral: "#64748b",
};

/** Verb display: italic for reverse-direction so the user sees direction
 *  even when spatial layout is constrained (STUB1 case). */
export function isReverseVerb(arrow: ArrowClass | string | null | undefined): boolean {
  return classifyArrow(arrow) === "reverse";
}

/** Exposed for unit tests + future migration tooling. */
export const REVERSE_VERBS = REVERSE;

/** Forward / reverse classification used by the canonical-direction port. */
export function isForwardVerb(arrow: ArrowClass | string | null | undefined): boolean {
  if (!arrow) return false;
  const a = String(arrow).toLowerCase();
  return POSITIVE.has(a) || NEGATIVE.has(a) || BINDING.has(a) || REGULATORY.has(a);
}
