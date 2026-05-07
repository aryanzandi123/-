import { useEffect, useMemo } from "react";
import { useParams } from "react-router-dom";

import { useVisualizeQuery } from "@/api/queries";
import {
  useSnapStore,
  selectActiveSnap,
  selectActiveInteractions,
} from "@/store/useSnapStore";
import { useViewStore } from "@/store/useViewStore";
import { CardView } from "@/app/views/card/CardView";
import { PathwayExplorer } from "@/app/views/card/PathwayExplorer";
import { PathwayBreadcrumb } from "@/app/views/card/PathwayBreadcrumb";
import { FilterChips } from "@/app/views/card/FilterChips";
import { DiagnosticsBanner } from "@/app/views/card/DiagnosticsBanner";
import { PipelineEventsDrawer } from "@/app/views/card/PipelineEventsDrawer";
import { ModalShell } from "@/app/modal/ModalShell";

export function Visualize(): JSX.Element {
  const { protein: rawProtein } = useParams<{ protein: string }>();
  const protein = rawProtein?.toUpperCase() ?? null;
  const setActive = useSnapStore((s) => s.setActiveProtein);
  const hasEntry = useSnapStore((s) => (protein ? s.snapshots.has(protein) : false));
  const snap = useSnapStore(selectActiveSnap);
  const interactionCount = useSnapStore(selectActiveInteractions).length;
  const pathwayCount = snap?.pathways?.length ?? 0;
  const view = useViewStore((s) => (protein ? s.byProtein.get(protein) : undefined));
  const activePathwaySummary = useMemo(() => {
    const sel = view?.selectedPathways;
    if (!sel || sel.size === 0) return { label: "All pathways", count: pathwayCount, scoped: false };
    if (sel.size === 1) return { label: Array.from(sel)[0] ?? "—", count: 1, scoped: true };
    return { label: `${sel.size} pathways`, count: sel.size, scoped: true };
  }, [view, pathwayCount]);

  useEffect(() => {
    setActive(protein);
  }, [protein, setActive]);

  const query = useVisualizeQuery(hasEntry ? null : protein);

  if (!protein) {
    return <main style={{ padding: 32 }}>Missing protein parameter.</main>;
  }

  const status = query.isFetching
    ? "Loading…"
    : query.isError
      ? `Load failed: ${query.error instanceof Error ? query.error.message : "unknown"}`
      : "Ready";

  return (
    <div style={{ height: "100vh", display: "flex", flexDirection: "column", overflow: "hidden" }}>
      <header
        role="banner"
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "10px 24px",
          background: "#0b1220",
          color: "#f8fafc",
          borderBottom: "1px solid #1e293b",
          fontFamily: "system-ui, sans-serif",
          height: 56,
          boxSizing: "border-box",
          flexShrink: 0,
          gap: 16,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 14, minWidth: 0, flex: 1 }}>
          <h1 style={{ margin: 0, fontSize: 16, fontWeight: 700, letterSpacing: 0.2 }}>
            ProPaths · {protein}
          </h1>
          <span
            title={activePathwaySummary.scoped ? "Pathway filter active. Click 'Clear filter' below to widen scope." : "No pathway filter — all pathways visible on canvas."}
            aria-label={activePathwaySummary.scoped ? `Active pathway: ${activePathwaySummary.label}` : "No pathway filter"}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              padding: "4px 12px",
              borderRadius: 999,
              fontSize: 12,
              fontWeight: 700,
              maxWidth: 420,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
              background: activePathwaySummary.scoped ? "rgba(99, 102, 241, 0.22)" : "rgba(100, 116, 139, 0.18)",
              color: activePathwaySummary.scoped ? "#c7d2fe" : "#cbd5e1",
              border: `1px solid ${activePathwaySummary.scoped ? "#6366f1" : "#334155"}`,
            }}
          >
            <span aria-hidden style={{ width: 6, height: 6, borderRadius: 999, background: activePathwaySummary.scoped ? "#a5b4fc" : "#64748b" }} />
            <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{activePathwaySummary.label}</span>
            <span style={{ color: activePathwaySummary.scoped ? "#a5b4fc" : "#94a3b8", fontWeight: 500, fontSize: 11 }}>
              · {activePathwaySummary.count} of {pathwayCount}
            </span>
          </span>
        </div>
        <div
          aria-live="polite"
          aria-atomic="true"
          style={{ fontSize: 11, color: "#94a3b8", display: "flex", gap: 12, flexShrink: 0 }}
        >
          <span>interactions: {interactionCount}</span>
          <span>{status}</span>
        </div>
      </header>
      <DiagnosticsBanner />
      <FilterChips protein={protein} />
      <PathwayBreadcrumb protein={protein} />
      <div style={{ display: "flex", flex: 1, minHeight: 0 }}>
        <PathwayExplorer protein={protein} />
        <main
          aria-label="Pathway canvas"
          style={{ flex: 1, minWidth: 0, position: "relative" }}
        >
          <CardView protein={protein} />
          <PipelineEventsDrawer protein={protein} />
        </main>
      </div>
      <ModalShell />
    </div>
  );
}
