/**
 * Multi-protein snapshot store. v1 ships single-active-view UI on top of a
 * Map-keyed store so multi-protein UI can land later without retrofitting
 * every selector and component (Q2 resolution from frontend-overhaul.md).
 *
 * Frozen-after-set discipline mirrors `Object.freeze(SNAP)` from
 * templates/visualize.html — once a snapshot lands in the store, neither
 * `snap` nor `ctx` may be mutated. Replacing an entry is fine; mutating
 * an existing entry's payload is forbidden.
 */

import { create } from "zustand";

import type { Snapshot, Context, Diagnostics, ProteinKey } from "@/types/api";
import type { SnapshotEntry } from "@/types/workspace";

interface SnapStoreState {
  snapshots: Map<ProteinKey, SnapshotEntry>;
  activeProtein: ProteinKey | null;
}

interface SnapStoreActions {
  setEntry: (
    protein: ProteinKey,
    snap: Snapshot,
    ctx: Context,
    diagnostics?: Diagnostics | null,
    schemaVersion?: string | null,
  ) => void;
  setActiveProtein: (protein: ProteinKey | null) => void;
  removeEntry: (protein: ProteinKey) => void;
  reset: () => void;
}

export type SnapStore = SnapStoreState & SnapStoreActions;

const INITIAL_STATE: SnapStoreState = {
  snapshots: new Map(),
  activeProtein: null,
};

export const useSnapStore = create<SnapStore>((set) => ({
  ...INITIAL_STATE,

  setEntry: (protein, snap, ctx, diagnostics = null, schemaVersion = null) => {
    const key = protein.toUpperCase();
    const entry: SnapshotEntry = {
      protein: key,
      snap: Object.freeze(snap) as Snapshot,
      ctx: Object.freeze(ctx) as Context,
      diagnostics,
      schemaVersion,
      loadedAt: Date.now(),
    };
    set((s) => {
      const next = new Map(s.snapshots);
      next.set(key, entry);
      return { snapshots: next };
    });
  },

  setActiveProtein: (protein) => {
    set({ activeProtein: protein ? protein.toUpperCase() : null });
  },

  removeEntry: (protein) => {
    const key = protein.toUpperCase();
    set((s) => {
      if (!s.snapshots.has(key)) return s;
      const next = new Map(s.snapshots);
      next.delete(key);
      const stillActive = s.activeProtein === key ? null : s.activeProtein;
      return { snapshots: next, activeProtein: stillActive };
    });
  },

  reset: () => set({ ...INITIAL_STATE, snapshots: new Map() }),
}));

// Stable empty references so selectors that fall back to `[]` return the
// same reference on every call. Without this, useSyncExternalStore sees a
// new array literal each render and schedules an infinite re-render.
const EMPTY_ARRAY: readonly never[] = Object.freeze([]);
const empty = <T,>(): readonly T[] => EMPTY_ARRAY as readonly T[];

export const selectActiveEntry = (s: SnapStore): SnapshotEntry | null => {
  if (!s.activeProtein) return null;
  return s.snapshots.get(s.activeProtein) ?? null;
};

export const selectActiveSnap = (s: SnapStore): Snapshot | null =>
  selectActiveEntry(s)?.snap ?? null;

export const selectActiveCtx = (s: SnapStore): Context | null =>
  selectActiveEntry(s)?.ctx ?? null;

export const selectActiveDiagnostics = (s: SnapStore): Diagnostics | null =>
  selectActiveEntry(s)?.diagnostics ?? null;

export const selectActiveInteractions = (s: SnapStore) =>
  selectActiveSnap(s)?.interactions ?? empty();

export const selectActivePathways = (s: SnapStore) =>
  selectActiveSnap(s)?.pathways ?? empty();

export const selectActiveProteins = (s: SnapStore) =>
  selectActiveSnap(s)?.proteins ?? empty();

/**
 * Subscribe to the Map directly (stable reference until setEntry / removeEntry
 * creates a new Map). Components derive the key list with `useMemo`. Returning
 * `Array.from(...)` from a selector creates a fresh array on every snapshot
 * read and triggers an infinite render loop via useSyncExternalStore.
 */
export const selectSnapshotsMap = (s: SnapStore) => s.snapshots;

export const selectSnapFor = (s: SnapStore, protein: ProteinKey): Snapshot | null =>
  s.snapshots.get(protein.toUpperCase())?.snap ?? null;

export const selectEntryFor = (
  s: SnapStore,
  protein: ProteinKey,
): SnapshotEntry | null => s.snapshots.get(protein.toUpperCase()) ?? null;
