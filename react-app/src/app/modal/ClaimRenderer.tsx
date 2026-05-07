/**
 * ClaimRenderer: dispatches a Claim to the correct surface.
 *
 * - Normal claims render through `<FunctionCard>` (legacy modal anatomy:
 *   pathway chip + serif title + arrow badge in the header; sub-cards for
 *   MECHANISM, EFFECT, BIOLOGICAL CASCADE, SPECIFIC EFFECTS, EVIDENCE).
 * - Special claim kinds render as honest placeholders so the user sees the
 *   absence of curated biology rather than fabricated text:
 *     `_synthetic` → "No pipeline-generated mechanism"
 *     `_thin_claim` → "Pair biology not characterized"
 *     `_synthetic_from_router` → "Router placeholder — Awaiting curation"
 *
 * The component preserves the `data-claim-header` attribute on the
 * underlying button (via `FunctionCard`) so `ModalShell`'s ←/→ keyboard
 * navigation still finds claim headers across the rendered list.
 */

import { classifyClaim } from "@/lib/claims";
import type { ArrowClass, Claim } from "@/types/api";

import { FunctionCard } from "./FunctionCard";

interface ClaimRendererProps {
  claim: Claim;
  pathwayContext?: string | null;
  defaultArrow?: ArrowClass | null;
  initiallyExpanded?: boolean;
  forceExpanded?: boolean;
  edgeEndpoints?: ReadonlyArray<string>;
}

export function ClaimRenderer({
  claim,
  pathwayContext = null,
  defaultArrow = null,
  initiallyExpanded = false,
  forceExpanded,
  edgeEndpoints,
}: ClaimRendererProps): JSX.Element {
  const special = classifyClaim(claim);

  if (special.kind === "synthetic") {
    return (
      <div
        style={{
          padding: 12,
          opacity: 0.7,
          borderLeft: "3px dashed var(--color-text-faint)",
          borderRadius: 4,
          background: "rgba(100, 116, 139, 0.08)",
          marginBottom: 8,
        }}
      >
        <div style={{ fontStyle: "italic", color: "var(--color-text-muted)" }}>
          No pipeline-generated mechanism for this interaction yet.
        </div>
        <div style={{ marginTop: 4, fontSize: 11 }}>
          Pathway: <strong>{special.pathway}</strong>
        </div>
      </div>
    );
  }

  if (special.kind === "thin") {
    return (
      <div
        style={{
          padding: 12,
          opacity: 0.8,
          borderLeft: "3px solid var(--color-text-muted)",
          background: "rgba(148, 163, 184, 0.08)",
          borderRadius: 4,
          marginBottom: 8,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
          <span style={{ color: "var(--color-text-muted)", fontWeight: 600, fontSize: 12 }}>Thin claim</span>
          <span style={{ background: "#cbd5e1", color: "#1e293b", padding: "2px 6px", borderRadius: 3, fontSize: 10 }}>
            Pair biology not characterized
          </span>
        </div>
        <div style={{ fontWeight: 500 }}>{special.title}</div>
        {special.prose ? (
          <div style={{ marginTop: 6, fontSize: 12, color: "var(--color-text-muted)" }}>{special.prose}</div>
        ) : null}
      </div>
    );
  }

  if (special.kind === "router") {
    return (
      <div
        style={{
          padding: 12,
          opacity: 0.7,
          borderLeft: "3px dashed var(--color-warn)",
          background: "rgba(245, 158, 11, 0.06)",
          borderRadius: 4,
          marginBottom: 8,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
          <span style={{ color: "#fbbf24", fontWeight: 600, fontSize: 12 }}>Router placeholder</span>
          <span style={{ background: "#fde68a", color: "#78350f", padding: "2px 6px", borderRadius: 3, fontSize: 10 }}>
            Awaiting curation
          </span>
        </div>
        <div style={{ fontWeight: 500 }}>{special.title}</div>
        {special.outcome ? (
          <div style={{ marginTop: 6, fontSize: 12, color: "var(--color-text-muted)", fontStyle: "italic" }}>
            {special.outcome}
          </div>
        ) : null}
      </div>
    );
  }

  // Normal claim → delegate to FunctionCard. pathwayContext is currently
  // unused inside FunctionCard but kept on the API so callers don't need to
  // change when the rebuild flips back on.
  return (
    <FunctionCard
      claim={claim}
      pathwayContext={pathwayContext}
      defaultArrow={defaultArrow}
      initiallyExpanded={initiallyExpanded}
      forceExpanded={forceExpanded}
      edgeEndpoints={edgeEndpoints}
    />
  );
}
