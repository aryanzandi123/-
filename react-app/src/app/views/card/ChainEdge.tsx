/**
 * ChainEdge: ReactFlow custom edge for the CardView.
 *
 * Renders the verb at the midpoint, color-coded by arrow class. Reverse
 * verbs (`is_*_by`) render italic so the user sees biological direction
 * even when spatial layout has STUB1 below HSP90AA1 (the existing-data
 * direction-inversion case from `11_CHAIN_TOPOLOGY.md`).
 */

import { memo } from "react";
import {
  BaseEdge,
  EdgeLabelRenderer,
  getBezierPath,
  type EdgeProps,
} from "@xyflow/react";

import { ARROW_COLORS, classifyArrow, isReverseVerb } from "@/lib/colors";
import type { ArrowClass } from "@/types/api";

export interface ChainEdgeData {
  arrow?: ArrowClass | null;
  isChainEdge?: boolean;
  isReverse?: boolean;
  multipleArrows?: ArrowClass[];
  [key: string]: unknown;
}

function ChainEdgeImpl(props: EdgeProps): JSX.Element {
  const {
    id,
    sourceX,
    sourceY,
    targetX,
    targetY,
    sourcePosition,
    targetPosition,
    data,
    markerEnd,
  } = props;

  const d = (data as ChainEdgeData | undefined) ?? {};
  const arrow = d.arrow ?? null;
  const arrowKind = classifyArrow(arrow);
  const color = ARROW_COLORS[arrowKind];
  const reverse = d.isReverse ?? isReverseVerb(arrow);
  const isChain = d.isChainEdge ?? false;
  const multi = d.multipleArrows ?? [];

  const [edgePath, labelX, labelY] = getBezierPath({
    sourceX,
    sourceY,
    targetX,
    targetY,
    sourcePosition,
    targetPosition,
  });

  const verbLabel = multi.length > 1
    ? multi.map((a) => a).join(" / ")
    : (arrow ?? "");

  return (
    <>
      <BaseEdge
        id={id}
        path={edgePath}
        markerEnd={markerEnd}
        style={{
          stroke: color,
          strokeWidth: isChain ? 1.8 : 1.4,
          strokeDasharray: isChain ? undefined : "6 3",
        }}
      />
      {verbLabel ? (
        <EdgeLabelRenderer>
          <div
            style={{
              position: "absolute",
              transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
              pointerEvents: "none",
              padding: "1px 6px",
              borderRadius: 4,
              background: "rgba(11, 18, 32, 0.95)",
              color,
              fontSize: 10,
              fontWeight: arrowKind === "negative" ? 700 : 600,
              fontStyle: reverse ? "italic" : "normal",
              fontFamily: "ui-monospace, Menlo, monospace",
              border: `1px solid ${color}66`,
              whiteSpace: "nowrap",
              userSelect: "none",
              zIndex: 1000,
              maxWidth: "none",
              lineHeight: 1.3,
            }}
            title={verbLabel}
            className="propaths-chain-edge-label nodrag nopan"
          >
            {verbLabel}
          </div>
        </EdgeLabelRenderer>
      ) : null}
    </>
  );
}

export const ChainEdge = memo(ChainEdgeImpl);
