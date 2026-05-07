import { useMemo } from "react";

import { isPlaceholderText } from "@/lib/claims";
import type { Claim, Interaction } from "@/types/api";

import styles from "./MetadataGrid.module.css";

export interface MetadataGridProps {
  /**
   * The interaction context for this modal. Only required for the edge
   * (single-pair) case; the aggregated (node) case passes `null` and uses
   * the `extra` rows for protein-level metadata instead.
   */
  interaction?: Interaction | null;
  /** Total claim count visible in the modal body (post-filter). */
  visibleClaimCount: number;
  /** Total claim count in the underlying interaction(s). */
  totalClaimCount: number;
  /** Override label for the FUNCTIONS row (e.g. "Interactions" for nodes). */
  functionsLabel?: string;
  /** Distinct PMID count. */
  evidenceCount: number;
  /** Pre-derived pathway breadcrumb: a list of ancestor names, last is leaf. */
  pathwayCrumbs?: string[];
  /** Optional extra label/value rows appended at the bottom (used by AggregatedModal). */
  extra?: Array<{ label: string; value: React.ReactNode }>;
  /** Claims used to derive a default CONTEXT row when no specific claim is focused. */
  claims?: Claim[] | null;
}

function deriveType(inter: Interaction | null | undefined): string {
  if (!inter) return "—";
  if ((inter as { _is_shared_link?: boolean })._is_shared_link) return "Shared";
  if (inter._is_chain_link) return "Chain";
  const t = (inter.interaction_type ?? inter.type ?? "").toLowerCase();
  if (t === "indirect") return "Indirect";
  return "Direct";
}

function deriveDirection(inter: Interaction | null | undefined): string {
  if (!inter) return "—";
  const d = (inter.direction ?? "").toLowerCase();
  if (d === "main_to_primary" || d === "a_to_b") return "Downstream";
  if (d === "primary_to_main" || d === "b_to_a") return "Upstream";
  return "—";
}

function deriveContext(inter: Interaction | null | undefined, claims?: Claim[] | null): string {
  // Prefer the per-claim function_context when present; fall back to
  // chain-vs-direct from the interaction-level flags.
  const fc = claims?.find((c) => typeof c.function_context === "string")?.function_context;
  if (typeof fc === "string" && fc) return fc.charAt(0).toUpperCase() + fc.slice(1);
  if (inter?._is_chain_link) return "Chain";
  return "Direct";
}

function pathwayCrumbsFromClaims(claims: Claim[] | null | undefined): string[] {
  if (!claims || claims.length === 0) return [];
  // Use the first claim with a usable hierarchy, falling back to its
  // own pathway name. Placeholder text is skipped.
  for (const c of claims) {
    const h = Array.isArray(c._hierarchy)
      ? c._hierarchy.filter((s): s is string => typeof s === "string" && !isPlaceholderText(s))
      : [];
    if (h.length > 0) {
      const raw = c.pathway;
      const leaf =
        typeof raw === "string"
          ? raw
          : (raw as { canonical_name?: string; name?: string } | null | undefined)?.canonical_name ??
            (raw as { name?: string } | null | undefined)?.name ??
            "";
      const trail = leaf && !h.includes(leaf) ? [...h, leaf] : h;
      return trail;
    }
  }
  // No hierarchy — surface just the claim's own pathway name when present.
  for (const c of claims) {
    const raw = c.pathway;
    const leaf =
      typeof raw === "string"
        ? raw
        : (raw as { canonical_name?: string; name?: string } | null | undefined)?.canonical_name;
    if (typeof leaf === "string" && leaf) return [leaf];
  }
  return [];
}

export function MetadataGrid({
  interaction,
  visibleClaimCount,
  totalClaimCount,
  functionsLabel = "Functions",
  evidenceCount,
  pathwayCrumbs,
  extra,
  claims,
}: MetadataGridProps): JSX.Element {
  const crumbs = useMemo(
    () => pathwayCrumbs ?? pathwayCrumbsFromClaims(claims),
    [pathwayCrumbs, claims],
  );
  const typeLabel = deriveType(interaction);
  const directionLabel = deriveDirection(interaction);
  const contextLabel = deriveContext(interaction, claims);
  const functionsValue =
    visibleClaimCount === totalClaimCount
      ? `${totalClaimCount}`
      : `${visibleClaimCount} of ${totalClaimCount}`;
  const evidenceValue = `${evidenceCount} ${evidenceCount === 1 ? "Paper" : "Papers"}`;

  return (
    <div className={styles.grid} role="list" aria-label="Interaction metadata">
      <div className={styles.label}>Type</div>
      <div className={styles.value}>{typeLabel}</div>
      <div className={styles.label}>Direction</div>
      <div className={styles.value}>{directionLabel}</div>
      <div className={styles.label}>{functionsLabel}</div>
      <div className={styles.value}>{functionsValue}</div>
      <div className={styles.label}>Evidence</div>
      <div className={styles.value}>{evidenceValue}</div>
      {crumbs.length > 0 ? (
        <>
          <div className={styles.label}>Pathways</div>
          <div className={styles.value}>
            {crumbs.map((c, i) => (
              <span key={`${i}-${c}`} className={i === crumbs.length - 1 ? styles.crumbActive : styles.crumb}>
                {c}
                {i < crumbs.length - 1 ? <span className={styles.sep}> › </span> : null}
              </span>
            ))}
          </div>
        </>
      ) : null}
      <div className={styles.label}>Context</div>
      <div className={styles.value}>{contextLabel}</div>
      {extra?.map((row, i) => (
        <span key={`extra-${i}`} style={{ display: "contents" }}>
          <div className={styles.label}>{row.label}</div>
          <div className={styles.value}>{row.value}</div>
        </span>
      ))}
    </div>
  );
}
