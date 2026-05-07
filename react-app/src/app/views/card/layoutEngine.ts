/**
 * elkjs wrapper for the CardView. Uses `layered` (Sugiyama) with `RIGHT`
 * direction so cause→effect flows left-to-right per the canonicalized chain
 * order. Cycles are routed via elk's cycle-breaker, NOT collapsed to a tree.
 *
 * Why elkjs over d3.tree(): biology is a DAG. Multi-parent fan-ins,
 * convergence (A→C, B→C), mid-chain query (X→Q→Y), and cycles
 * (PI3K→AKT→mTOR→S6K1→PI3K) all need a layered layout that handles N parents
 * per node and back-edges. d3.tree() can't.
 */

import type { Node, Edge } from "@xyflow/react";

// Lazy-load elkjs (~700KB bundled). The dynamic import keeps it out of the
// main app.js chunk; first layout call resolves it and every subsequent call
// reuses the cached promise.
interface ElkInstance {
  layout(graph: unknown): Promise<{ children?: { id: string; x?: number; y?: number }[] }>;
}

let elkPromise: Promise<ElkInstance> | null = null;
async function getElk(): Promise<ElkInstance> {
  if (!elkPromise) {
    elkPromise = import("elkjs/lib/elk.bundled.js").then(
      (mod) => new (mod.default as { new (): ElkInstance })(),
    );
  }
  return elkPromise;
}

const DEFAULT_NODE_WIDTH = 168;
const DEFAULT_NODE_HEIGHT = 56;

const LAYOUT_OPTIONS: Record<string, string> = {
  "elk.algorithm": "layered",
  "elk.direction": "RIGHT",
  // Spacing knobs — values tuned against ATXN3 (90 interactions, 244 pathways).
  // `nodeNodeBetweenLayers` controls horizontal cascade spacing (between
  // chain hops). `nodeNode` controls vertical density within a layer.
  "elk.layered.spacing.nodeNodeBetweenLayers": "120",
  "elk.spacing.nodeNode": "60",
  "elk.layered.spacing.edgeNodeBetweenLayers": "40",
  "elk.layered.spacing.edgeEdgeBetweenLayers": "20",
  "elk.spacing.edgeNode": "24",
  "elk.layered.nodePlacement.strategy": "BRANDES_KOEPF",
  "elk.layered.crossingMinimization.strategy": "LAYER_SWEEP",
  "elk.layered.cycleBreaking.strategy": "GREEDY",
  "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
  "elk.edgeRouting": "POLYLINE",
  "elk.layered.thoroughness": "10",
};

interface LayoutInput {
  nodes: Node[];
  edges: Edge[];
}

export interface LayoutOutput {
  nodes: Node[];
  edges: Edge[];
}

interface ElkNodeShape {
  id: string;
  width: number;
  height: number;
  layoutOptions?: Record<string, string>;
}

interface ElkEdgeShape {
  id: string;
  sources: [string];
  targets: [string];
}

interface ElkResultNode {
  id: string;
  x?: number;
  y?: number;
  width?: number;
  height?: number;
}

export async function applyElkLayout({ nodes, edges }: LayoutInput): Promise<LayoutOutput> {
  if (nodes.length === 0) return { nodes, edges };

  const elkNodes: ElkNodeShape[] = nodes.map((n) => ({
    id: n.id,
    width: (n.width as number | undefined) ?? (n.data?.width as number | undefined) ?? DEFAULT_NODE_WIDTH,
    height: (n.height as number | undefined) ?? (n.data?.height as number | undefined) ?? DEFAULT_NODE_HEIGHT,
  }));

  const elkEdges: ElkEdgeShape[] = edges
    .filter((e) => e.source !== e.target)
    .map((e) => ({
      id: e.id,
      sources: [e.source],
      targets: [e.target],
    }));

  const graph = {
    id: "root",
    layoutOptions: LAYOUT_OPTIONS,
    children: elkNodes,
    edges: elkEdges,
  };

  const elk = await getElk();
  const layouted = await elk.layout(graph as never);
  const positions = new Map<string, ElkResultNode>();
  for (const child of layouted.children ?? []) {
    positions.set((child as ElkResultNode).id, child as ElkResultNode);
  }

  const positionedNodes: Node[] = nodes.map((n) => {
    const p = positions.get(n.id);
    if (!p) return n;
    return {
      ...n,
      position: { x: p.x ?? 0, y: p.y ?? 0 },
    };
  });

  return { nodes: positionedNodes, edges };
}
