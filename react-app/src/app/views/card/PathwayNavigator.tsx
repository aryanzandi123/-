/**
 * PathwayNavigator: left sidebar listing all pathways for the active query.
 *
 * Hierarchical tree built from `pathway.parent_ids` / `child_ids`.
 * Cascade rules:
 *   - Selecting pathway P auto-selects every ancestor (so P stays visible
 *     even if a parent is collapsed)
 *   - Deselecting P cascades to descendants
 *   - Search filters the tree by case-insensitive substring match
 *
 * State lives in `useViewStore.byProtein.<key>.selectedPathways`.
 */

import { useMemo, useState } from "react";

import {
  useSnapStore,
  selectActivePathways,
} from "@/store/useSnapStore";
import { useViewStore } from "@/store/useViewStore";
import type { Pathway, ProteinKey } from "@/types/api";

interface PathwayNode {
  pathway: Pathway;
  depth: number;
  children: PathwayNode[];
}

function buildHierarchy(pathways: readonly Pathway[]): PathwayNode[] {
  const byId = new Map<string, Pathway>();
  for (const p of pathways) byId.set(p.id, p);

  const isRoot = (p: Pathway): boolean => {
    const parents = p.parent_ids ?? [];
    if (parents.length === 0) return true;
    return !parents.some((pid) => byId.has(pid));
  };

  const visited = new Set<string>();
  const buildNode = (p: Pathway, depth: number): PathwayNode => {
    visited.add(p.id);
    const childIds = p.child_ids ?? [];
    const children: PathwayNode[] = [];
    for (const cid of childIds) {
      const child = byId.get(cid);
      if (!child || visited.has(child.id)) continue;
      children.push(buildNode(child, depth + 1));
    }
    children.sort((a, b) => a.pathway.name.localeCompare(b.pathway.name));
    return { pathway: p, depth, children };
  };

  const roots: PathwayNode[] = [];
  for (const p of pathways) {
    if (visited.has(p.id)) continue;
    if (isRoot(p)) roots.push(buildNode(p, 0));
  }
  // Catch any orphans (cycles or dangling parent_ids) at depth 0.
  for (const p of pathways) {
    if (!visited.has(p.id)) roots.push(buildNode(p, 0));
  }
  roots.sort((a, b) => a.pathway.name.localeCompare(b.pathway.name));
  return roots;
}

function collectAncestors(target: Pathway, byId: Map<string, Pathway>): Set<string> {
  const out = new Set<string>();
  const queue = [...(target.parent_ids ?? [])];
  while (queue.length) {
    const id = queue.shift()!;
    if (out.has(id)) continue;
    out.add(id);
    const p = byId.get(id);
    if (p?.parent_ids) queue.push(...p.parent_ids);
  }
  return out;
}

function collectDescendants(target: Pathway, byId: Map<string, Pathway>): Set<string> {
  const out = new Set<string>();
  const queue = [...(target.child_ids ?? [])];
  while (queue.length) {
    const id = queue.shift()!;
    if (out.has(id)) continue;
    out.add(id);
    const p = byId.get(id);
    if (p?.child_ids) queue.push(...p.child_ids);
  }
  return out;
}

interface NavigatorProps {
  protein: ProteinKey;
}

export function PathwayNavigator({ protein }: NavigatorProps): JSX.Element {
  const pathways = useSnapStore(selectActivePathways);
  const view = useViewStore((s) => s.byProtein.get(protein.toUpperCase()));
  const setStore = useViewStore.setState;

  const [search, setSearch] = useState("");
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());

  const byId = useMemo(() => {
    const m = new Map<string, Pathway>();
    for (const p of pathways) m.set(p.id, p);
    return m;
  }, [pathways]);

  const roots = useMemo(() => buildHierarchy(pathways), [pathways]);

  const selected = view?.selectedPathways ?? new Set<string>();
  const selectedNames = useMemo(() => new Set(selected), [selected]);

  const toggleSelection = (target: Pathway) => {
    const key = protein.toUpperCase();
    setStore((s) => {
      const next = new Map(s.byProtein);
      const existing = next.get(key);
      if (!existing) return s;
      const cloned = {
        ...existing,
        selectedPathways: new Set(existing.selectedPathways),
      };
      const isSelected = cloned.selectedPathways.has(target.name);
      if (isSelected) {
        cloned.selectedPathways.delete(target.name);
        const descendants = collectDescendants(target, byId);
        for (const id of descendants) {
          const p = byId.get(id);
          if (p) cloned.selectedPathways.delete(p.name);
        }
      } else {
        cloned.selectedPathways.add(target.name);
        const ancestors = collectAncestors(target, byId);
        for (const id of ancestors) {
          const p = byId.get(id);
          if (p) cloned.selectedPathways.add(p.name);
        }
      }
      next.set(key, cloned);
      return { byProtein: next };
    });
  };

  const toggleCollapse = (id: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const matchesSearch = (p: Pathway): boolean => {
    if (!search.trim()) return true;
    const q = search.trim().toLowerCase();
    return p.name.toLowerCase().includes(q) || p.id.toLowerCase().includes(q);
  };

  const renderNode = (node: PathwayNode): JSX.Element | null => {
    const { pathway, children } = node;
    const isSelected = selectedNames.has(pathway.name);
    const childMatches = children.some((c) => containsMatch(c, matchesSearch));
    const selfMatch = matchesSearch(pathway);
    if (!selfMatch && !childMatches) return null;
    const isCollapsed = collapsed.has(pathway.id);
    const interactorCount = pathway.interactor_ids?.length ?? 0;

    return (
      <li key={pathway.id} style={{ listStyle: "none", margin: 0 }}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            padding: "3px 4px",
            borderRadius: 4,
            background: isSelected ? "rgba(99, 102, 241, 0.15)" : "transparent",
            cursor: "pointer",
            fontSize: 12,
          }}
        >
          {children.length > 0 ? (
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                toggleCollapse(pathway.id);
              }}
              aria-label={isCollapsed ? "Expand" : "Collapse"}
              style={{
                background: "transparent",
                border: "none",
                color: "#94a3b8",
                cursor: "pointer",
                fontSize: 10,
                width: 14,
                padding: 0,
              }}
            >
              {isCollapsed ? "▸" : "▾"}
            </button>
          ) : (
            <span style={{ width: 14 }} />
          )}
          <input
            type="checkbox"
            checked={isSelected}
            onChange={() => toggleSelection(pathway)}
            style={{ margin: 0, accentColor: "#6366f1" }}
            aria-label={`Toggle pathway ${pathway.name}`}
          />
          <span
            onClick={() => toggleSelection(pathway)}
            style={{
              flex: 1,
              color: isSelected ? "#f1f5f9" : "#cbd5e1",
              fontWeight: isSelected ? 600 : 400,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
            title={pathway.name}
          >
            {pathway.name}
          </span>
          <span style={{ color: "#64748b", fontSize: 10 }}>{interactorCount}</span>
        </div>
        {!isCollapsed && children.length > 0 ? (
          <ul style={{ paddingLeft: 14, margin: 0 }}>
            {children.map(renderNode)}
          </ul>
        ) : null}
      </li>
    );
  };

  return (
    <aside
      style={{
        width: 280,
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
      <div style={{ padding: "12px 12px 8px" }}>
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
          {selected.size > 0 ? (
            <button
              type="button"
              onClick={() => {
                const key = protein.toUpperCase();
                setStore((s) => {
                  const next = new Map(s.byProtein);
                  const existing = next.get(key);
                  if (!existing) return s;
                  next.set(key, { ...existing, selectedPathways: new Set() });
                  return { byProtein: next };
                });
              }}
              style={{
                background: "transparent",
                border: "1px solid #334155",
                color: "#94a3b8",
                fontSize: 10,
                padding: "2px 6px",
                borderRadius: 4,
                cursor: "pointer",
              }}
            >
              clear ({selected.size})
            </button>
          ) : null}
        </div>
        <input
          type="search"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Filter pathways…"
          aria-label="Filter pathways"
          style={{
            width: "100%",
            background: "#0f172a",
            border: "1px solid #1e293b",
            color: "#f1f5f9",
            fontSize: 12,
            padding: "6px 8px",
            borderRadius: 4,
            boxSizing: "border-box",
          }}
        />
      </div>
      <div style={{ flex: 1, overflowY: "auto", padding: "0 8px 12px" }}>
        <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
          {roots.map(renderNode)}
        </ul>
      </div>
    </aside>
  );
}

function containsMatch(node: PathwayNode, predicate: (p: Pathway) => boolean): boolean {
  if (predicate(node.pathway)) return true;
  return node.children.some((c) => containsMatch(c, predicate));
}
