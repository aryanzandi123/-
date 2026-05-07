/**
 * FilterChips: top-bar interaction filters.
 *
 * Phase 2 MVP exposes:
 *   - Pseudo on/off — drop interactions where source/target/primary is RNA,
 *     Ubiquitin, etc. (mirrors `static/cv_diagnostics.PSEUDO_NAMES`).
 *   - Filter mode — all / direct / indirect / chain (cycle).
 *
 * Cross-query / merge-cross-query / chains-split-vs-merged ship in Phase 2 polish.
 */

import { useViewStore, type FilterMode } from "@/store/useViewStore";
import type { ProteinKey } from "@/types/api";

interface FilterChipsProps {
  protein: ProteinKey;
}

const MODES: FilterMode[] = ["all", "direct", "indirect", "chain"];

const MODE_LABEL: Record<FilterMode, string> = {
  all: "All",
  direct: "Direct",
  indirect: "Indirect",
  chain: "Chain",
};

export function FilterChips({ protein }: FilterChipsProps): JSX.Element {
  const view = useViewStore((s) => s.byProtein.get(protein.toUpperCase()));
  const setFilter = useViewStore((s) => s.setFilter);

  const filters = view?.filters;
  if (!filters) return <div />;

  return (
    <div
      role="toolbar"
      aria-label="Interaction filters"
      style={{
        display: "flex",
        alignItems: "center",
        gap: 12,
        padding: "8px 16px",
        background: "#0b1220",
        borderBottom: "1px solid #1e293b",
        fontFamily: "system-ui, sans-serif",
        fontSize: 12,
        color: "#cbd5e1",
        boxSizing: "border-box",
      }}
    >
      <div style={{ display: "flex", gap: 4 }} role="group" aria-label="Filter mode">
        {MODES.map((mode) => {
          const isActive = filters.mode === mode;
          return (
            <button
              key={mode}
              type="button"
              onClick={() => setFilter(protein, "mode", mode)}
              aria-pressed={isActive}
              style={{
                padding: "4px 10px",
                background: isActive ? "#1e293b" : "transparent",
                border: `1px solid ${isActive ? "#6366f1" : "#1e293b"}`,
                color: isActive ? "#f1f5f9" : "#94a3b8",
                borderRadius: 999,
                fontSize: 11,
                fontWeight: isActive ? 600 : 400,
                cursor: "pointer",
              }}
            >
              {MODE_LABEL[mode]}
            </button>
          );
        })}
      </div>

      <div style={{ width: 1, height: 18, background: "#1e293b" }} aria-hidden />

      <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer", userSelect: "none" }}>
        <input
          type="checkbox"
          checked={filters.pseudo}
          onChange={(e) => setFilter(protein, "pseudo", e.target.checked)}
          style={{ accentColor: "#6366f1" }}
        />
        <span>Show pseudo</span>
      </label>

      <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer", userSelect: "none" }}>
        <input
          type="checkbox"
          checked={filters.crossQuery}
          onChange={(e) => setFilter(protein, "crossQuery", e.target.checked)}
          style={{ accentColor: "#6366f1" }}
        />
        <span>Cross-query</span>
      </label>

      <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer", userSelect: "none" }}>
        <input
          type="checkbox"
          checked={filters.chainsMerged}
          onChange={(e) => setFilter(protein, "chainsMerged", e.target.checked)}
          style={{ accentColor: "#6366f1" }}
        />
        <span>Merge chains</span>
      </label>
    </div>
  );
}
