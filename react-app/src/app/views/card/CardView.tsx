/**
 * CardView orchestration. Subscribes to the active snap + view state,
 * rebuilds the graph + runs elkjs layout whenever inputs change, renders
 * via ReactFlow with our custom node/edge types.
 *
 * Re-render flow:
 *   snap / pathway selection / filters → build → elk layout → setNodes/setEdges
 *
 * Heavy work is debounced 100ms so dragging filters doesn't thrash elkjs.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ReactFlow,
  ReactFlowProvider,
  Background,
  Controls,
  MiniMap,
  useReactFlow,
  type Node,
  type Edge,
  type EdgeTypes,
  type NodeTypes,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import {
  useSnapStore,
  selectActiveSnap,
  selectActiveInteractions,
} from "@/store/useSnapStore";
import { useViewStore } from "@/store/useViewStore";
import { useModalStore } from "@/store/useModalStore";
import type { ProteinKey } from "@/types/api";

import { buildCardGraph } from "./buildCardGraph";
import { applyElkLayout } from "./layoutEngine";
import { ProteinCard, type ProteinCardData } from "./ProteinCard";
import { ChainEdge, type ChainEdgeData } from "./ChainEdge";
import { DuplicateCrossLink } from "./DuplicateCrossLink";
import { Legend } from "./Legend";
import { EmptyState } from "./EmptyState";

const NODE_TYPES: NodeTypes = {
  protein: ProteinCard,
};

const EDGE_TYPES: EdgeTypes = {
  chain: ChainEdge,
  "duplicate-crosslink": DuplicateCrossLink,
};

const LAYOUT_DEBOUNCE_MS = 80;

interface CardViewProps {
  protein: ProteinKey;
}

export function CardView({ protein }: CardViewProps): JSX.Element {
  return (
    <ReactFlowProvider>
      <CardViewInner protein={protein} />
    </ReactFlowProvider>
  );
}

function CardViewInner({ protein }: CardViewProps): JSX.Element {
  const snap = useSnapStore(selectActiveSnap);
  const interactionCount = useSnapStore(selectActiveInteractions).length;

  const view = useViewStore((s) => s.byProtein.get(protein.toUpperCase()));
  const ensureView = useViewStore((s) => s.ensure);

  useEffect(() => {
    ensureView(protein);
  }, [protein, ensureView]);

  const filters = view?.filters;
  const selectedPathways = view?.selectedPathways;

  const buildKey = useMemo(() => {
    if (!snap || !filters) return null;
    const parts = [
      snap.main,
      String(interactionCount),
      filters.pseudo ? "p" : "P",
      filters.mode,
      filters.crossQuery ? "cq" : "CQ",
      filters.mergeCrossQuery ? "m" : "M",
      filters.chainsMerged ? "cm" : "CM",
      Array.from(selectedPathways ?? []).sort().join(","),
    ];
    return parts.join("|");
  }, [snap, filters, selectedPathways, interactionCount]);

  const [nodes, setNodes] = useState<Node[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  const [layoutPending, setLayoutPending] = useState(false);
  const [layoutError, setLayoutError] = useState<string | null>(null);
  const debounceRef = useRef<number | null>(null);
  const seqRef = useRef(0);
  const { fitView } = useReactFlow();
  const pushModal = useModalStore((s) => s.push);

  const onNodeClick = useCallback(
    (_e: React.MouseEvent, node: Node) => {
      const data = node.data as ProteinCardData;
      // Skip if it's the query node — clicking it shouldn't open an aggregated
      // modal for the user's own query (Phase 3 polish may re-enable).
      if (data.variant === "query" && !data.chainId) return;
      pushModal({
        kind: "aggregated",
        protein: protein.toUpperCase(),
        payload: {
          baseProtein: data.baseProtein,
          variant: data.variant,
          chainId: data.chainId,
          chainPosition: data.chainPosition,
          chainLength: data.chainLength,
          nodeId: node.id,
        },
      });
    },
    [protein, pushModal],
  );

  const onEdgeClick = useCallback(
    (_e: React.MouseEvent, edge: Edge) => {
      if (edge.type === "duplicate-crosslink") return;
      const data = edge.data as ChainEdgeData | undefined;
      const sourceNode = nodes.find((n) => n.id === edge.source);
      const targetNode = nodes.find((n) => n.id === edge.target);
      const sourceProtein = (sourceNode?.data as ProteinCardData | undefined)?.baseProtein;
      const targetProtein = (targetNode?.data as ProteinCardData | undefined)?.baseProtein;
      // chain edge id encoding: `chain::<chainId>::edge::<position>`
      const chainEdgeMatch = edge.id.match(/^chain::(\d+)::edge::(\d+)$/);
      const chainId = chainEdgeMatch ? Number(chainEdgeMatch[1]) : null;
      const chainPosition = chainEdgeMatch ? Number(chainEdgeMatch[2]) : null;
      pushModal({
        kind: "interaction",
        protein: protein.toUpperCase(),
        payload: {
          sourceProtein,
          targetProtein,
          arrow: data?.arrow ?? null,
          isChainEdge: data?.isChainEdge ?? false,
          isReverse: data?.isReverse ?? false,
          chainId,
          chainPosition,
          edgeId: edge.id,
        },
      });
    },
    [protein, pushModal, nodes],
  );

  useEffect(() => {
    if (!snap || !filters) {
      setNodes([]);
      setEdges([]);
      return;
    }
    setLayoutPending(true);
    if (debounceRef.current) window.clearTimeout(debounceRef.current);
    debounceRef.current = window.setTimeout(() => {
      const seq = ++seqRef.current;
      const built = buildCardGraph({
        snap,
        selectedPathways: selectedPathways ?? new Set(),
        filters,
      });
      applyElkLayout(built)
        .then((res) => {
          if (seq !== seqRef.current) return;
          setNodes(res.nodes);
          setEdges(res.edges);
          setLayoutError(null);
          // Refit camera once the layout lands. ReactFlow's `fitView` prop
          // only auto-fits on initial mount; node updates after layout
          // need an explicit fit.
          requestAnimationFrame(() => {
            fitView({ padding: 0.18, duration: 220 });
          });
        })
        .catch((err: unknown) => {
          if (seq !== seqRef.current) return;
          const message = err instanceof Error ? err.message : "layout failed";
          setLayoutError(message);
          console.error("[CardView] elk layout failed", err);
        })
        .finally(() => {
          if (seq === seqRef.current) setLayoutPending(false);
        });
    }, LAYOUT_DEBOUNCE_MS);
    return () => {
      if (debounceRef.current) window.clearTimeout(debounceRef.current);
    };
  }, [buildKey, snap, filters, selectedPathways, fitView]);

  const visibleNodeCount = nodes.length;
  const isEmpty = !layoutPending && visibleNodeCount === 0;

  const onResetView = () => fitView({ padding: 0.18, duration: 220 });

  return (
    <div style={{ position: "relative", width: "100%", height: "100%" }}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        edgeTypes={EDGE_TYPES}
        fitView
        fitViewOptions={{ padding: 0.18, includeHiddenNodes: false }}
        minZoom={0.05}
        maxZoom={2.5}
        nodesDraggable
        nodesConnectable={false}
        edgesFocusable={false}
        panOnScroll
        proOptions={{ hideAttribution: true }}
        onNodeClick={onNodeClick}
        onEdgeClick={onEdgeClick}
      >
        <Background gap={24} size={1} />
        <Controls showInteractive={false} />
        <MiniMap
          pannable
          zoomable
          nodeColor={(n) => {
            const v = (n.data as ProteinCardData | undefined)?.variant;
            if (v === "query") return "#6366f1";
            if (v === "chain") return "#0ea5e9";
            return "#475569";
          }}
          style={{ background: "rgba(11, 18, 32, 0.85)" }}
        />
      </ReactFlow>

      <Legend />

      {isEmpty ? <EmptyState protein={protein} /> : null}

      <button
        type="button"
        onClick={onResetView}
        title="Fit view"
        aria-label="Fit view"
        style={{
          position: "absolute",
          left: 18,
          bottom: 56,
          background: "rgba(15, 23, 42, 0.9)",
          border: "1px solid #334155",
          color: "#cbd5e1",
          fontSize: 11,
          padding: "4px 10px",
          borderRadius: 4,
          cursor: "pointer",
          zIndex: 6,
        }}
      >
        ⟲ Fit
      </button>

      <div
        style={{
          position: "absolute",
          left: 16,
          bottom: 16,
          fontSize: 11,
          fontFamily: "ui-monospace, Menlo, monospace",
          color: "#94a3b8",
          background: "rgba(15, 23, 42, 0.9)",
          padding: "6px 10px",
          borderRadius: 6,
          display: "flex",
          gap: 12,
          alignItems: "center",
          pointerEvents: "none",
        }}
      >
        <span>nodes: {visibleNodeCount}</span>
        <span>edges: {edges.length}</span>
        {layoutPending ? <span style={{ color: "#fbbf24" }}>laying out…</span> : null}
        {layoutError ? <span style={{ color: "#ef4444" }}>err: {layoutError}</span> : null}
      </div>
    </div>
  );
}
