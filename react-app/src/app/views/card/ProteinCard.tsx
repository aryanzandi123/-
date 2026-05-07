/**
 * ProteinCard: ReactFlow custom node for the CardView.
 *
 * Variants (chosen via `data.variant`):
 *   - "query" — centered glow, larger
 *   - "direct" — theme-colored by predominant arrow class
 *   - "chain" — chain-color border, position-in-chain indicator
 *   - "pathway-header" — slate, expand/collapse affordance (Phase 2e)
 *
 * Badges (`data.badges`) port from the legacy `cv_diagnostics.applyDepthBadges`
 * + `applyPartialChainBadges` + `applyPathwayDriftBadges`. Each is a small
 * pill in the top-right corner.
 */

import { memo } from "react";
import { Handle, Position, type NodeProps } from "@xyflow/react";

import { ARROW_COLORS, classifyArrow } from "@/lib/colors";
import { normalizePathwayName } from "@/lib/normalize";
import { useViewStore } from "@/store/useViewStore";
import type { ArrowClass } from "@/types/api";

export type ProteinVariant = "query" | "direct" | "chain" | "pathway-header" | "duplicate";

export interface ProteinBadge {
  kind: "depth" | "partial-chain" | "drift" | "pseudo" | "no-biology" | "duplicate-count";
  label: string;
  tooltip?: string;
}

export interface ProteinCardData {
  label: string;
  variant: ProteinVariant;
  arrowClass?: ArrowClass | null;
  contextText?: string;
  alsoIn?: string[];
  /** Pathway names this protein touches (for previewPathway ring matching). */
  pathways?: string[];
  badges?: ProteinBadge[];
  isDuplicate?: boolean;
  baseProtein?: string;
  chainId?: number;
  chainPosition?: number;
  chainLength?: number;
  pathwayCount?: number;
  isPseudo?: boolean;
  /** Whether this base protein renders as multiple instances in the current view. */
  hasDuplicates?: boolean;
  [key: string]: unknown;
}

const BADGE_BG: Record<ProteinBadge["kind"], string> = {
  depth: "#1e293b",
  "partial-chain": "#854d0e",
  drift: "#854d0e",
  pseudo: "#475569",
  "no-biology": "#7f1d1d",
  "duplicate-count": "#312e81",
};

const BADGE_FG: Record<ProteinBadge["kind"], string> = {
  depth: "#fbbf24",
  "partial-chain": "#fde68a",
  drift: "#fde68a",
  pseudo: "#cbd5e1",
  "no-biology": "#fecaca",
  "duplicate-count": "#c7d2fe",
};

function variantStyle(variant: ProteinVariant, arrow: ArrowClass | null | undefined): {
  background: string;
  border: string;
  color: string;
  glow: string;
  fontSize: number;
  width: number;
  minHeight: number;
} {
  const arrowKind = classifyArrow(arrow);
  const arrowColor = ARROW_COLORS[arrowKind];

  switch (variant) {
    case "query":
      return {
        background: "linear-gradient(135deg, #0f172a 0%, #1e293b 100%)",
        border: `2px solid #6366f1`,
        color: "#f8fafc",
        glow: "0 0 0 1px #6366f1, 0 0 18px rgba(99, 102, 241, 0.45)",
        fontSize: 16,
        width: 200,
        minHeight: 64,
      };
    case "pathway-header":
      return {
        background: "#0b1220",
        border: `1px solid #334155`,
        color: "#cbd5e1",
        glow: "none",
        fontSize: 13,
        width: 240,
        minHeight: 48,
      };
    case "duplicate":
    case "chain":
      return {
        background: "#111827",
        border: `1.5px dashed ${arrowColor}`,
        color: "#f1f5f9",
        glow: "none",
        fontSize: 14,
        width: 168,
        minHeight: 56,
      };
    case "direct":
    default:
      return {
        background: "#0f172a",
        border: `1.5px solid ${arrowColor}`,
        color: "#f8fafc",
        glow: "none",
        fontSize: 14,
        width: 168,
        minHeight: 56,
      };
  }
}

const matchesPreview = (pathways: string[] | undefined, preview: string | null): boolean => {
  if (!preview || !pathways || pathways.length === 0) return false;
  const target = normalizePathwayName(preview);
  return pathways.some((p) => normalizePathwayName(p) === target);
};

function ProteinCardImpl({ data }: NodeProps): JSX.Element {
  const d = data as unknown as ProteinCardData;
  const style = variantStyle(d.variant, d.arrowClass ?? null);
  const dupAttr = d.baseProtein ? { "data-base-protein": d.baseProtein } : {};

  const previewPathway = useViewStore((s) => s.previewPathway);
  const hoveredBase = useViewStore((s) => s.hoveredBaseProtein);
  const setHoveredBase = useViewStore((s) => s.setHoveredBaseProtein);

  const isPreviewMatch = matchesPreview(d.pathways, previewPathway);
  const isHoveredSibling =
    Boolean(d.hasDuplicates && hoveredBase && d.baseProtein && hoveredBase === d.baseProtein.toUpperCase());

  // Pseudo styling: italic label + dotted border (overrides arrow-class border).
  const finalBorder = d.isPseudo ? `1.5px dotted ${style.color === "#cbd5e1" ? "#64748b" : "#94a3b8"}` : style.border;
  const finalBackground = d.isPseudo ? "#1e293b" : style.background;
  const finalLabelStyle: React.CSSProperties = d.isPseudo ? { fontStyle: "italic" } : {};

  // Composite glow: preview ring + hover pulse + variant glow.
  const glowParts: string[] = [];
  if (style.glow !== "none") glowParts.push(style.glow);
  if (isPreviewMatch) glowParts.push("0 0 0 2px rgba(165, 180, 252, 0.85)");
  if (isHoveredSibling) glowParts.push("0 0 14px rgba(99, 102, 241, 0.7), 0 0 0 2px #6366f1");
  const finalGlow = glowParts.length > 0 ? glowParts.join(", ") : undefined;

  const onMouseEnter = () => {
    if (d.hasDuplicates && d.baseProtein) {
      setHoveredBase(d.baseProtein);
    }
  };
  const onMouseLeave = () => {
    if (d.hasDuplicates) setHoveredBase(null);
  };

  return (
    <div
      data-variant={d.variant}
      data-pseudo={d.isPseudo ? "1" : "0"}
      data-preview-match={isPreviewMatch ? "1" : "0"}
      data-hovered-sibling={isHoveredSibling ? "1" : "0"}
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
      {...dupAttr}
      style={{
        background: finalBackground,
        border: finalBorder,
        color: style.color,
        boxShadow: finalGlow,
        width: style.width,
        minHeight: style.minHeight,
        borderRadius: 10,
        padding: "8px 12px",
        fontFamily: "system-ui, -apple-system, sans-serif",
        position: "relative",
        display: "flex",
        flexDirection: "column",
        justifyContent: "center",
        gap: 2,
        transition: "box-shadow 140ms ease-out",
      }}
    >
      <Handle type="target" position={Position.Left} style={{ background: "#475569", width: 8, height: 8 }} />

      <div
        style={{
          fontSize: style.fontSize,
          fontWeight: d.variant === "query" ? 700 : 600,
          letterSpacing: 0.2,
          lineHeight: 1.15,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
          ...finalLabelStyle,
        }}
        title={d.label}
      >
        {d.label}
      </div>

      {d.contextText ? (
        <div
          style={{
            fontSize: 10,
            color: "#94a3b8",
            lineHeight: 1.3,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
          title={d.contextText}
        >
          {d.contextText}
        </div>
      ) : null}

      {d.alsoIn && d.alsoIn.length > 0 ? (
        <div
          style={{
            fontSize: 9,
            color: "#64748b",
            fontStyle: "italic",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
          title={`Also in: ${d.alsoIn.join(", ")}`}
        >
          Also in: {d.alsoIn.slice(0, 3).join(", ")}
          {d.alsoIn.length > 3 ? "…" : ""}
        </div>
      ) : null}

      {d.badges && d.badges.length > 0 ? (
        <div
          style={{
            position: "absolute",
            top: 4,
            right: 4,
            display: "flex",
            gap: 3,
          }}
        >
          {d.badges.map((b, i) => (
            <span
              key={`${b.kind}-${i}`}
              title={b.tooltip ?? b.label}
              style={{
                background: BADGE_BG[b.kind],
                color: BADGE_FG[b.kind],
                fontSize: 9,
                padding: "1px 5px",
                borderRadius: 999,
                fontWeight: 600,
              }}
            >
              {b.label}
            </span>
          ))}
        </div>
      ) : null}

      <Handle type="source" position={Position.Right} style={{ background: "#475569", width: 8, height: 8 }} />
    </div>
  );
}

export const ProteinCard = memo(ProteinCardImpl);
