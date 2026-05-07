/**
 * PathwayBreadcrumb — header chips above the canvas showing the current
 * pathway scope. When a single deep pathway is selected, also shows
 * ancestor chips (Cell Death › Autophagy › Autophagosome Maturation) so the
 * user knows where they are in the biology hierarchy.
 *
 * Each selected chip has × to deselect; ancestor "ghost" chips can be
 * clicked to add that ancestor (cascade rules apply via PathwayExplorer
 * already, so we just mutate the set directly here).
 */

import { useMemo } from "react";

import { useSnapStore, selectActivePathways } from "@/store/useSnapStore";
import { useViewStore } from "@/store/useViewStore";
import type { ProteinKey } from "@/types/api";

interface BreadcrumbProps {
  protein: ProteinKey;
}

export function PathwayBreadcrumb({ protein }: BreadcrumbProps): JSX.Element | null {
  const proteinKey = protein.toUpperCase();
  const pathways = useSnapStore(selectActivePathways);
  const view = useViewStore((s) => s.byProtein.get(proteinKey));
  const setStore = useViewStore.setState;

  const selected = view?.selectedPathways ?? new Set<string>();

  const byName = useMemo(() => {
    const m = new Map<string, (typeof pathways)[number]>();
    for (const p of pathways) m.set(p.name, p);
    return m;
  }, [pathways]);
  const byId = useMemo(() => {
    const m = new Map<string, (typeof pathways)[number]>();
    for (const p of pathways) m.set(p.id, p);
    return m;
  }, [pathways]);

  const selectedList = useMemo(() => Array.from(selected), [selected]);

  const ancestorTrail = useMemo(() => {
    if (selectedList.length !== 1) return [] as { id: string; name: string; selected: boolean }[];
    const only = byName.get(selectedList[0] ?? "");
    if (!only) return [];
    const ancestors = (only.ancestor_ids ?? []).map((aid) => byId.get(aid)).filter(Boolean);
    return ancestors.map((a) => ({
      id: a!.id,
      name: a!.name,
      selected: selected.has(a!.name),
    }));
  }, [selectedList, byName, byId, selected]);

  const remove = (name: string) => {
    setStore((s) => {
      const next = new Map(s.byProtein);
      const existing = next.get(proteinKey);
      if (!existing) return s;
      const cloned = { ...existing, selectedPathways: new Set(existing.selectedPathways) };
      cloned.selectedPathways.delete(name);
      next.set(proteinKey, cloned);
      return { byProtein: next };
    });
  };

  const add = (name: string) => {
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

  // A0 wayfinding: render the strip even when nothing is selected, so the
  // user can always tell what scope they're in. "All pathways · N total" is
  // the unfiltered state; selected chips replace it when one or more
  // pathways are picked.
  const totalPathways = pathways.length;

  return (
    <nav
      aria-label="Active pathway scope"
      style={{
        display: "flex",
        flexWrap: "wrap",
        gap: 8,
        padding: "10px 16px",
        background: "#070b14",
        borderBottom: "1px solid var(--color-accent)",
        borderTop: "1px solid #1e293b",
        fontSize: 12,
        color: "#cbd5e1",
        alignItems: "center",
        position: "sticky",
        top: 0,
        zIndex: 4,
      }}
    >
      <span style={{ color: "#94a3b8", fontSize: 10, fontWeight: 700, letterSpacing: 0.8, textTransform: "uppercase" }}>
        Viewing
      </span>

      {selectedList.length === 0 ? (
        <span
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 6,
            background: "rgba(100, 116, 139, 0.15)",
            color: "#cbd5e1",
            border: "1px solid #334155",
            padding: "3px 10px",
            borderRadius: 999,
            fontWeight: 600,
            fontSize: 12,
          }}
        >
          All pathways
          <span style={{ color: "#94a3b8", fontWeight: 400, fontSize: 11 }}>· {totalPathways} total</span>
        </span>
      ) : null}

      {ancestorTrail.length > 0 ? (
        <>
          {ancestorTrail.map((anc) => (
            <span key={anc.id} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
              <button
                type="button"
                onClick={() => add(anc.name)}
                title={anc.selected ? "Already selected" : `Add ancestor "${anc.name}"`}
                style={ghostChip(anc.selected)}
                disabled={anc.selected}
              >
                {anc.name}
              </button>
              <span style={{ color: "#475569" }}>›</span>
            </span>
          ))}
        </>
      ) : null}

      {selectedList.map((name) => (
        <span
          key={name}
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 4,
            background: "rgba(99, 102, 241, 0.25)",
            color: "#c7d2fe",
            border: "1px solid #6366f1",
            padding: "3px 4px 3px 10px",
            borderRadius: 999,
            fontWeight: 700,
            fontSize: 12,
            boxShadow: "0 0 0 1px rgba(99, 102, 241, 0.25)",
          }}
        >
          {name}
          <button
            type="button"
            onClick={() => remove(name)}
            aria-label={`Remove ${name}`}
            style={removeBtn}
          >
            ×
          </button>
        </span>
      ))}

      {selectedList.length > 0 ? (
        <>
          <span style={{ flex: 1 }} />
          <button
            type="button"
            onClick={() => {
              setStore((s) => {
                const next = new Map(s.byProtein);
                const existing = next.get(proteinKey);
                if (!existing) return s;
                const cloned = { ...existing, selectedPathways: new Set<string>() };
                next.set(proteinKey, cloned);
                return { byProtein: next };
              });
            }}
            style={{
              background: "transparent",
              border: "1px solid #334155",
              color: "#94a3b8",
              fontSize: 11,
              padding: "3px 10px",
              borderRadius: 4,
              cursor: "pointer",
            }}
          >
            Clear filter
          </button>
        </>
      ) : null}
    </nav>
  );
}

const ghostChip = (selected: boolean): React.CSSProperties => ({
  background: "transparent",
  border: `1px dashed ${selected ? "#475569" : "#334155"}`,
  color: selected ? "#475569" : "#94a3b8",
  padding: "2px 8px",
  borderRadius: 999,
  fontSize: 11,
  cursor: selected ? "default" : "pointer",
  whiteSpace: "nowrap",
});

const removeBtn: React.CSSProperties = {
  background: "transparent",
  border: "none",
  color: "#a5b4fc",
  fontSize: 14,
  width: 18,
  height: 18,
  borderRadius: 999,
  cursor: "pointer",
  lineHeight: 1,
  padding: 0,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
};
