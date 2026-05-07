/**
 * PathwayExplorer — biology-first investigative panel for the active query's
 * pathway membership. Replaces the flat PathwayNavigator.
 *
 * Each row shows derived statistics (mini-bar of direct/chain split, letter
 * grade from per-claim PhD-depth, drift / partial / pseudo dots), so the user
 * sees the SHAPE of every pathway at a glance — not just its name.
 *
 * Cross-cutting features that the legacy navigator lacked:
 *   - Smart sort (relevance, alphabetical, hierarchy depth, drift, lowest pass, most chains)
 *   - Quick filter chips (composable; "Has interactors" defaults ON)
 *   - Two-mode search: by pathway NAME or by MEMBER PROTEIN symbol
 *   - Hover preview into the canvas without committing
 *   - Selected pathways pinned to top
 *   - Cascade rules carried forward from PathwayNavigator
 */

import { useEffect, useMemo, useRef, useState } from "react";

import {
  selectActivePathways,
  selectActiveSnap,
  useSnapStore,
} from "@/store/useSnapStore";
import { useViewStore } from "@/store/useViewStore";
import {
  derivePathwayStats,
  sortPathwayStats,
  type PathwayLetterGrade,
  type PathwaySortMode,
  type PathwayStat,
} from "@/lib/pathwayStats";
import type { ProteinKey } from "@/types/api";

interface ExplorerProps {
  protein: ProteinKey;
}

type SearchMode = "name" | "member";

interface QuickFilters {
  hasInteractors: boolean;
  hasChains: boolean;
  hasDrift: boolean;
  hasIssues: boolean;
  mineOnly: boolean;
}

const SORT_LABELS: Record<PathwaySortMode, string> = {
  relevance: "Relevance",
  alphabetical: "A → Z",
  hierarchy: "Hierarchy",
  drift: "Most drift",
  lowestPass: "Lowest pass",
  mostChains: "Most chains",
};

const GRADE_COLOR: Record<PathwayLetterGrade, string> = {
  "A+": "#10b981",
  A: "#34d399",
  B: "#f59e0b",
  C: "#ef4444",
  "—": "#475569",
};

export function PathwayExplorer({ protein }: ExplorerProps): JSX.Element {
  const proteinKey = protein.toUpperCase();
  const pathways = useSnapStore(selectActivePathways);
  const snap = useSnapStore(selectActiveSnap);

  const view = useViewStore((s) => s.byProtein.get(proteinKey));
  const ensureView = useViewStore((s) => s.ensure);
  const setStore = useViewStore.setState;
  const setPreview = useViewStore((s) => s.setPreviewPathway);

  const [searchMode, setSearchMode] = useState<SearchMode>("name");
  const [search, setSearch] = useState("");
  const [sortMode, setSortMode] = useState<PathwaySortMode>("relevance");
  const [filters, setFilters] = useState<QuickFilters>({
    hasInteractors: true,
    hasChains: false,
    hasDrift: false,
    hasIssues: false,
    mineOnly: false,
  });

  useEffect(() => {
    ensureView(proteinKey);
  }, [proteinKey, ensureView]);

  const stats = useMemo(() => derivePathwayStats(snap), [snap]);
  const statList = useMemo(() => Array.from(stats.values()), [stats]);

  // Build name → id map for member-protein search.
  const idByName = useMemo(() => {
    const m = new Map<string, string>();
    for (const p of pathways) m.set(p.name, p.id);
    return m;
  }, [pathways]);

  const selectedNames = view?.selectedPathways ?? new Set<string>();
  const selectedIds = useMemo(() => {
    const set = new Set<string>();
    for (const name of selectedNames) {
      const id = idByName.get(name);
      if (id) set.add(id);
    }
    return set;
  }, [selectedNames, idByName]);

  const autoSelectedRef = useRef(false);

  // Auto-select highest-relevance non-catch-all pathway on first hydration.
  useEffect(() => {
    if (autoSelectedRef.current) return;
    if (!snap || statList.length === 0) return;
    if (selectedNames.size > 0) {
      autoSelectedRef.current = true;
      return;
    }
    const ranked = sortPathwayStats(
      statList.filter((s) => !s.isCatchAll && s.directCount + s.chainCount > 0),
      "relevance",
    );
    const top = ranked[0];
    if (!top) return;
    autoSelectedRef.current = true;
    setStore((s) => {
      const next = new Map(s.byProtein);
      const existing = next.get(proteinKey);
      if (!existing) return s;
      const cloned = {
        ...existing,
        selectedPathways: new Set(existing.selectedPathways),
      };
      cloned.selectedPathways.add(top.name);
      next.set(proteinKey, cloned);
      return { byProtein: next };
    });
  }, [snap, statList, selectedNames.size, proteinKey, setStore]);

  // Toggle with cascade rules (ports from legacy PathwayNavigator).
  const toggleSelection = (target: PathwayStat) => {
    setStore((s) => {
      const next = new Map(s.byProtein);
      const existing = next.get(proteinKey);
      if (!existing) return s;
      const cloned = {
        ...existing,
        selectedPathways: new Set(existing.selectedPathways),
      };
      const isSelected = cloned.selectedPathways.has(target.name);
      if (isSelected) {
        cloned.selectedPathways.delete(target.name);
        for (const cid of collectDescendants(target, stats)) {
          const ch = stats.get(cid);
          if (ch) cloned.selectedPathways.delete(ch.name);
        }
      } else {
        cloned.selectedPathways.add(target.name);
        for (const aid of collectAncestors(target, stats)) {
          const an = stats.get(aid);
          if (an) cloned.selectedPathways.add(an.name);
        }
      }
      next.set(proteinKey, cloned);
      return { byProtein: next };
    });
  };

  const clearAll = () => {
    setStore((s) => {
      const next = new Map(s.byProtein);
      const existing = next.get(proteinKey);
      if (!existing) return s;
      next.set(proteinKey, { ...existing, selectedPathways: new Set() });
      return { byProtein: next };
    });
  };

  // Filter + search the list.
  const filtered = useMemo(() => {
    const q = search.trim();
    const qUpper = q.toUpperCase();
    const qLower = q.toLowerCase();
    const matchPredicate = (s: PathwayStat): boolean => {
      if (!q) return true;
      if (searchMode === "name") {
        return s.name.toLowerCase().includes(qLower);
      }
      // member mode — check upper-case symbol against memberProteins set
      return s.memberProteins.has(qUpper);
    };
    const passQuickFilter = (s: PathwayStat): boolean => {
      const total = s.directCount + s.chainCount;
      if (filters.hasInteractors && total === 0) return false;
      if (filters.hasChains && s.chainCount === 0) return false;
      if (filters.hasDrift && s.driftCorrected + s.driftReportOnly === 0) return false;
      if (filters.hasIssues && !s.hasIssues) return false;
      if (filters.mineOnly && !selectedIds.has(s.id)) return false;
      return true;
    };
    return statList.filter((s) => matchPredicate(s) && passQuickFilter(s));
  }, [statList, search, searchMode, filters, selectedIds]);

  const sorted = useMemo(() => sortPathwayStats(filtered, sortMode), [filtered, sortMode]);

  // Pin selected pathways to top of the list.
  const partitioned = useMemo(() => {
    const sel: PathwayStat[] = [];
    const rest: PathwayStat[] = [];
    for (const s of sorted) {
      if (selectedIds.has(s.id)) sel.push(s);
      else rest.push(s);
    }
    return { sel, rest };
  }, [sorted, selectedIds]);

  return (
    <aside
      aria-label="Pathway explorer"
      style={{
        width: 320,
        height: "calc(100vh - 56px)",
        background: "#0b1220",
        borderRight: "1px solid #1e293b",
        color: "#cbd5e1",
        fontFamily: "system-ui, -apple-system, sans-serif",
        display: "flex",
        flexDirection: "column",
        boxSizing: "border-box",
      }}
    >
      <div style={{ padding: "12px 12px 6px" }}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            marginBottom: 8,
          }}
        >
          <h2 style={{ margin: 0, fontSize: 12, color: "#94a3b8", letterSpacing: 0.5 }}>
            PATHWAYS · {pathways.length}
          </h2>
          {selectedNames.size > 0 ? (
            <button type="button" onClick={clearAll} style={btnGhost}>
              clear ({selectedNames.size})
            </button>
          ) : null}
        </div>

        <div style={{ display: "flex", gap: 4, marginBottom: 6 }}>
          <button
            type="button"
            onClick={() => setSearchMode("name")}
            aria-pressed={searchMode === "name"}
            style={modeBtn(searchMode === "name")}
          >
            Name
          </button>
          <button
            type="button"
            onClick={() => setSearchMode("member")}
            aria-pressed={searchMode === "member"}
            style={modeBtn(searchMode === "member")}
          >
            Member
          </button>
        </div>

        <input
          type="search"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder={searchMode === "name" ? "Filter pathways…" : "Find pathways with protein…"}
          aria-label={searchMode === "name" ? "Filter pathways by name" : "Find pathways with member protein"}
          style={searchInput}
        />

        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            marginTop: 8,
            fontSize: 11,
          }}
        >
          <span style={{ color: "#64748b" }}>Sort:</span>
          <select
            value={sortMode}
            onChange={(e) => setSortMode(e.target.value as PathwaySortMode)}
            style={selectStyle}
            aria-label="Sort mode"
          >
            {Object.entries(SORT_LABELS).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </select>
        </div>

        <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 8 }}>
          {[
            ["hasInteractors", "Has interactors"],
            ["hasChains", "Has chains"],
            ["hasDrift", "Has drift"],
            ["hasIssues", "Has issues"],
            ["mineOnly", "Mine only"],
          ].map(([k, label]) => {
            const key = k as keyof QuickFilters;
            const on = filters[key];
            return (
              <button
                key={k}
                type="button"
                onClick={() => setFilters((f) => ({ ...f, [key]: !on }))}
                aria-pressed={on}
                style={chip(on)}
              >
                {label}
              </button>
            );
          })}
        </div>
      </div>

      <div
        style={{
          flex: 1,
          overflowY: "auto",
          padding: "0 6px 12px",
          fontSize: 12,
        }}
        onMouseLeave={() => setPreview(null)}
      >
        {partitioned.sel.length > 0 ? (
          <Section label={`Selected (${partitioned.sel.length})`}>
            {partitioned.sel.map((s) => (
              <Row
                key={s.id}
                stat={s}
                selected
                onToggle={() => toggleSelection(s)}
                onHover={(name) => setPreview(name)}
              />
            ))}
          </Section>
        ) : null}

        <Section label={`Available (${partitioned.rest.length} of ${stats.size})`}>
          {partitioned.rest.length === 0 ? (
            <div style={{ padding: "10px 8px", color: "#64748b", fontSize: 11 }}>
              No pathways match the current filters.
            </div>
          ) : (
            partitioned.rest.map((s) => (
              <Row
                key={s.id}
                stat={s}
                selected={false}
                onToggle={() => toggleSelection(s)}
                onHover={(name) => setPreview(name)}
              />
            ))
          )}
        </Section>
      </div>
    </aside>
  );
}

function Section({ label, children }: { label: string; children: React.ReactNode }): JSX.Element {
  return (
    <div style={{ marginBottom: 4 }}>
      <div
        style={{
          padding: "8px 8px 4px",
          fontSize: 10,
          letterSpacing: 0.5,
          color: "#475569",
          textTransform: "uppercase",
        }}
      >
        {label}
      </div>
      <div>{children}</div>
    </div>
  );
}

interface RowProps {
  stat: PathwayStat;
  selected: boolean;
  onToggle: () => void;
  onHover: (name: string | null) => void;
}

function Row({ stat, selected, onToggle, onHover }: RowProps): JSX.Element {
  const total = stat.directCount + stat.chainCount;
  const dimmed = total === 0;
  const directRatio = total > 0 ? stat.directCount / total : 0;
  const chainRatio = total > 0 ? stat.chainCount / total : 0;

  return (
    <div
      onMouseEnter={() => onHover(stat.name)}
      onFocus={() => onHover(stat.name)}
      onMouseLeave={() => onHover(null)}
      onBlur={() => onHover(null)}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 6,
        padding: "5px 6px",
        borderRadius: 4,
        background: selected ? "rgba(99, 102, 241, 0.15)" : "transparent",
        cursor: "pointer",
        opacity: dimmed && !selected ? 0.45 : 1,
        marginLeft: stat.depth * 8,
      }}
      role="presentation"
    >
      <input
        type="checkbox"
        checked={selected}
        onChange={onToggle}
        style={{ margin: 0, accentColor: "#6366f1", flexShrink: 0 }}
        aria-label={`Toggle pathway ${stat.name}`}
      />
      <span
        onClick={onToggle}
        title={`${stat.name}${stat.description ? "\n\n" + stat.description : ""}`}
        style={{
          flex: 1,
          color: selected ? "#f1f5f9" : "#cbd5e1",
          fontWeight: selected ? 600 : 400,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
      >
        {stat.name}
      </span>

      <span
        style={{
          color: "#94a3b8",
          fontSize: 10,
          fontFamily: "ui-monospace, Menlo, monospace",
          width: 28,
          textAlign: "right",
        }}
      >
        {total > 0 ? total : ""}
      </span>

      <MiniBar directRatio={directRatio} chainRatio={chainRatio} dim={dimmed} />

      <span
        title={
          stat.passRateMean != null
            ? `Avg PhD-depth pass rate: ${(stat.passRateMean * 100).toFixed(0)}%`
            : "No measurable claims"
        }
        style={{
          fontSize: 9,
          fontWeight: 700,
          color: GRADE_COLOR[stat.letterGrade],
          width: 18,
          textAlign: "center",
        }}
      >
        {stat.letterGrade === "—" ? "" : stat.letterGrade}
      </span>

      <Dot
        on={stat.driftCorrected + stat.driftReportOnly > 0}
        color={stat.driftReportOnly > 0 ? "#f59e0b" : "#10b981"}
        title={
          stat.driftCorrected + stat.driftReportOnly > 0
            ? `Drift: ${stat.driftCorrected} corrected, ${stat.driftReportOnly} report-only`
            : "No pathway drift"
        }
      />
      <Dot
        on={stat.partialChainCount > 0}
        color="#f59e0b"
        title={
          stat.partialChainCount > 0
            ? `${stat.partialChainCount} partial chain interactor(s)`
            : "All chains complete"
        }
      />
      <Dot
        on={stat.pseudoTouching}
        color="#94a3b8"
        title={stat.pseudoTouching ? "Includes pseudo-protein hops (RNA, Ubiquitin, …)" : "No pseudo proteins"}
      />
    </div>
  );
}

function MiniBar({
  directRatio,
  chainRatio,
  dim,
}: {
  directRatio: number;
  chainRatio: number;
  dim: boolean;
}): JSX.Element {
  const w = 44;
  const dPx = directRatio * w;
  const cPx = chainRatio * w;
  return (
    <span
      title={`Direct: ${(directRatio * 100).toFixed(0)}% · Chain: ${(chainRatio * 100).toFixed(0)}%`}
      style={{
        display: "inline-flex",
        width: w,
        height: 6,
        background: "#1e293b",
        borderRadius: 2,
        overflow: "hidden",
        opacity: dim ? 0.3 : 1,
      }}
    >
      <span style={{ width: dPx, background: "#6366f1" }} />
      <span style={{ width: cPx, background: "#0ea5e9" }} />
    </span>
  );
}

function Dot({ on, color, title }: { on: boolean; color: string; title: string }): JSX.Element {
  return (
    <span
      title={title}
      style={{
        width: 7,
        height: 7,
        borderRadius: "50%",
        background: on ? color : "transparent",
        border: `1px solid ${on ? color : "#334155"}`,
        flexShrink: 0,
      }}
    />
  );
}

const btnGhost: React.CSSProperties = {
  background: "transparent",
  border: "1px solid #334155",
  color: "#94a3b8",
  fontSize: 10,
  padding: "2px 6px",
  borderRadius: 4,
  cursor: "pointer",
};

const modeBtn = (active: boolean): React.CSSProperties => ({
  flex: 1,
  background: active ? "#1e293b" : "transparent",
  border: `1px solid ${active ? "#6366f1" : "#1e293b"}`,
  color: active ? "#f1f5f9" : "#94a3b8",
  fontSize: 11,
  padding: "4px 6px",
  borderRadius: 4,
  cursor: "pointer",
  fontWeight: active ? 600 : 400,
});

const searchInput: React.CSSProperties = {
  width: "100%",
  background: "#0f172a",
  border: "1px solid #1e293b",
  color: "#f1f5f9",
  fontSize: 12,
  padding: "6px 8px",
  borderRadius: 4,
  boxSizing: "border-box",
};

const selectStyle: React.CSSProperties = {
  flex: 1,
  background: "#0f172a",
  border: "1px solid #1e293b",
  color: "#f1f5f9",
  fontSize: 11,
  padding: "3px 6px",
  borderRadius: 4,
};

const chip = (active: boolean): React.CSSProperties => ({
  background: active ? "rgba(99, 102, 241, 0.2)" : "transparent",
  border: `1px solid ${active ? "#6366f1" : "#1e293b"}`,
  color: active ? "#a5b4fc" : "#94a3b8",
  fontSize: 10,
  padding: "3px 8px",
  borderRadius: 999,
  cursor: "pointer",
  fontWeight: active ? 600 : 400,
  whiteSpace: "nowrap",
});

function collectAncestors(
  target: PathwayStat,
  byId: Map<string, PathwayStat>,
): Set<string> {
  const out = new Set<string>();
  const queue = [...target.parentIds];
  while (queue.length) {
    const id = queue.shift()!;
    if (out.has(id)) continue;
    out.add(id);
    const p = byId.get(id);
    if (p) queue.push(...p.parentIds);
  }
  return out;
}

function collectDescendants(
  target: PathwayStat,
  byId: Map<string, PathwayStat>,
): Set<string> {
  const out = new Set<string>();
  const queue = [...target.childIds];
  while (queue.length) {
    const id = queue.shift()!;
    if (out.has(id)) continue;
    out.add(id);
    const p = byId.get(id);
    if (p) queue.push(...p.childIds);
  }
  return out;
}
