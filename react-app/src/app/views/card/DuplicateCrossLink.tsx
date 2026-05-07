/**
 * DuplicateCrossLink: faint dashed connector between same-protein instances
 * (e.g. HDAC6 as direct interactor + HDAC6 as chain participant under the
 * same pathway). Hover any instance highlights all instances + their
 * cross-links — Layer 3b of the chain topology fix from
 * `11_CHAIN_TOPOLOGY.md`.
 */

import { memo } from "react";
import {
  BaseEdge,
  getBezierPath,
  type EdgeProps,
} from "@xyflow/react";

import { useViewStore } from "@/store/useViewStore";

export interface DuplicateCrossLinkData {
  baseProtein: string;
  [key: string]: unknown;
}

function DuplicateCrossLinkImpl(props: EdgeProps): JSX.Element {
  const { id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, data } = props;
  const d = (data as DuplicateCrossLinkData | undefined) ?? { baseProtein: "" };
  const hoveredBase = useViewStore((s) => s.hoveredBaseProtein);
  const isHovered = Boolean(hoveredBase && d.baseProtein && hoveredBase === d.baseProtein.toUpperCase());

  const [edgePath] = getBezierPath({
    sourceX,
    sourceY,
    targetX,
    targetY,
    sourcePosition,
    targetPosition,
    curvature: 0.4,
  });

  return (
    <BaseEdge
      id={id}
      path={edgePath}
      style={{
        stroke: isHovered ? "#a5b4fc" : "#94a3b8",
        strokeWidth: isHovered ? 1.6 : 1,
        strokeDasharray: "2 4",
        opacity: isHovered ? 0.9 : 0.45,
        pointerEvents: "none",
        transition: "opacity 140ms ease-out, stroke-width 140ms ease-out, stroke 140ms ease-out",
      }}
      data-base-protein={d.baseProtein}
      className="propaths-duplicate-crosslink"
    />
  );
}

export const DuplicateCrossLink = memo(DuplicateCrossLinkImpl);
