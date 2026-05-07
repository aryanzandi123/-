/**
 * EmptyState — rendered when the CardView has no nodes (no pathway selected
 * or filters strip everything). Shows a helpful nudge plus the auto-suggested
 * top pathway as a clickable chip so the user can recover with one click.
 */

import { useMemo } from "react";

import { useSnapStore, selectActiveSnap } from "@/store/useSnapStore";
import { useViewStore } from "@/store/useViewStore";
import { derivePathwayStats, sortPathwayStats } from "@/lib/pathwayStats";
import type { ProteinKey } from "@/types/api";

interface EmptyStateProps {
  protein: ProteinKey;
}

export function EmptyState({ protein }: EmptyStateProps): JSX.Element {
  const proteinKey = protein.toUpperCase();
  const snap = useSnapStore(selectActiveSnap);
  const setStore = useViewStore.setState;

  const suggestion = useMemo(() => {
    if (!snap) return null;
    const stats = derivePathwayStats(snap);
    const ranked = sortPathwayStats(
      Array.from(stats.values()).filter(
        (s) => !s.isCatchAll && s.directCount + s.chainCount > 0,
      ),
      "relevance",
    );
    return ranked[0] ?? null;
  }, [snap]);

  const select = (name: string) => {
    setStore((s) => {
      const next = new Map(s.byProtein);
      const existing = next.get(proteinKey);
      if (!existing) return s;
      const cloned = { ...existing, selectedPathways: new Set(existing.selectedPathways) };
      cloned.selectedPathways.add(name);
      next.set(proteinKey, cloned);
      return { byProtein: next };
    });
  };

  return (
    <div
      style={{
        position: "absolute",
        inset: 0,
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: 12,
        color: "#94a3b8",
        fontFamily: "system-ui, -apple-system, sans-serif",
        textAlign: "center",
        pointerEvents: "none",
      }}
    >
      <div style={{ fontSize: 32, fontWeight: 200, color: "#475569" }}>○</div>
      <div style={{ fontSize: 14, fontWeight: 500, color: "#cbd5e1" }}>
        Select a pathway from the explorer to begin.
      </div>
      <div style={{ fontSize: 12, color: "#64748b", maxWidth: 360 }}>
        Each pathway groups the proteins, chains, and claims relevant to one biological process.
      </div>
      {suggestion ? (
        <button
          type="button"
          onClick={() => select(suggestion.name)}
          style={{
            pointerEvents: "auto",
            marginTop: 8,
            background: "rgba(99, 102, 241, 0.2)",
            color: "#a5b4fc",
            border: "1px solid #6366f1",
            padding: "6px 14px",
            borderRadius: 999,
            fontSize: 11,
            fontWeight: 600,
            cursor: "pointer",
          }}
        >
          → Try “{suggestion.name}” ({suggestion.directCount + suggestion.chainCount} interactions)
        </button>
      ) : null}
    </div>
  );
}
