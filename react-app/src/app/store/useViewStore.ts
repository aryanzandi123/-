/**
 * Per-protein view state (mode, filters, expanded pathways).
 *
 * Map-keyed so view state survives switching between proteins in a
 * multi-protein workspace. Phase 1 ships skeletons; Phase 2f wires up
 * the filter chips, Phase 2e wires the pathway navigator selectors.
 */

import { create } from "zustand";

import type { ProteinKey } from "@/types/api";

export type ViewMode = "card" | "table" | "chat" | "graph";

export type FilterMode = "all" | "direct" | "indirect" | "chain";

export interface ViewFilters {
  pseudo: boolean;
  mode: FilterMode;
  crossQuery: boolean;
  mergeCrossQuery: boolean;
  chainsMerged: boolean;
  arrowTypes: Set<string>;
}

export interface PerProteinViewState {
  mode: ViewMode;
  selectedPathways: Set<string>;
  expandedPathways: Set<string>;
  hiddenPathways: Set<string>;
  filters: ViewFilters;
}

interface ViewStoreState {
  byProtein: Map<ProteinKey, PerProteinViewState>;
  /** Ephemeral hover preview from PathwayExplorer. Not persisted across mounts. */
  previewPathway: string | null;
  /** Ephemeral hover from a ProteinCard with N>1 instances. Used for cross-link pulse. */
  hoveredBaseProtein: string | null;
}

interface ViewStoreActions {
  ensure: (protein: ProteinKey) => void;
  setMode: (protein: ProteinKey, mode: ViewMode) => void;
  togglePathwaySelection: (protein: ProteinKey, pathwayId: string) => void;
  togglePathwayExpansion: (protein: ProteinKey, pathwayId: string) => void;
  setFilter: <K extends keyof ViewFilters>(
    protein: ProteinKey,
    key: K,
    value: ViewFilters[K],
  ) => void;
  setPreviewPathway: (name: string | null) => void;
  setHoveredBaseProtein: (base: string | null) => void;
  reset: (protein: ProteinKey) => void;
  resetAll: () => void;
}

export type ViewStore = ViewStoreState & ViewStoreActions;

const DEFAULT_VIEW: PerProteinViewState = {
  mode: "card",
  selectedPathways: new Set(),
  expandedPathways: new Set(),
  hiddenPathways: new Set(),
  filters: {
    pseudo: false,
    mode: "all",
    crossQuery: false,
    mergeCrossQuery: false,
    chainsMerged: false,
    arrowTypes: new Set(),
  },
};

const cloneView = (v: PerProteinViewState): PerProteinViewState => ({
  mode: v.mode,
  selectedPathways: new Set(v.selectedPathways),
  expandedPathways: new Set(v.expandedPathways),
  hiddenPathways: new Set(v.hiddenPathways),
  filters: { ...v.filters, arrowTypes: new Set(v.filters.arrowTypes) },
});

const freshView = (): PerProteinViewState => cloneView(DEFAULT_VIEW);

const updateOne = (
  state: ViewStoreState,
  protein: ProteinKey,
  mut: (view: PerProteinViewState) => PerProteinViewState,
): Pick<ViewStoreState, "byProtein"> => {
  const key = protein.toUpperCase();
  const next = new Map(state.byProtein);
  const current = next.get(key) ?? freshView();
  next.set(key, mut(cloneView(current)));
  return { byProtein: next };
};

export const useViewStore = create<ViewStore>((set) => ({
  byProtein: new Map(),
  previewPathway: null,
  hoveredBaseProtein: null,

  setPreviewPathway: (name) => set({ previewPathway: name }),
  setHoveredBaseProtein: (base) => set({ hoveredBaseProtein: base ? base.toUpperCase() : null }),

  ensure: (protein) =>
    set((s) => {
      const key = protein.toUpperCase();
      if (s.byProtein.has(key)) return s;
      const next = new Map(s.byProtein);
      next.set(key, freshView());
      return { byProtein: next };
    }),

  setMode: (protein, mode) =>
    set((s) =>
      updateOne(s, protein, (view) => {
        view.mode = mode;
        return view;
      }),
    ),

  togglePathwaySelection: (protein, pathwayId) =>
    set((s) =>
      updateOne(s, protein, (view) => {
        if (view.selectedPathways.has(pathwayId)) view.selectedPathways.delete(pathwayId);
        else view.selectedPathways.add(pathwayId);
        return view;
      }),
    ),

  togglePathwayExpansion: (protein, pathwayId) =>
    set((s) =>
      updateOne(s, protein, (view) => {
        if (view.expandedPathways.has(pathwayId)) view.expandedPathways.delete(pathwayId);
        else view.expandedPathways.add(pathwayId);
        return view;
      }),
    ),

  setFilter: (protein, key, value) =>
    set((s) =>
      updateOne(s, protein, (view) => {
        view.filters = { ...view.filters, [key]: value };
        return view;
      }),
    ),

  reset: (protein) =>
    set((s) => {
      const key = protein.toUpperCase();
      if (!s.byProtein.has(key)) return s;
      const next = new Map(s.byProtein);
      next.set(key, freshView());
      return { byProtein: next };
    }),

  resetAll: () =>
    set({ byProtein: new Map(), previewPathway: null, hoveredBaseProtein: null }),
}));

export const selectViewFor = (
  s: ViewStore,
  protein: ProteinKey | null,
): PerProteinViewState | null =>
  protein ? (s.byProtein.get(protein.toUpperCase()) ?? null) : null;
