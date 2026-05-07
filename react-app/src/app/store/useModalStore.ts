/**
 * Modal state. Phase 1 ships open/close skeleton; Phase 3 wires
 * InteractionModal, AggregatedModal, ChainContextBanner.
 *
 * Modal args carry a `ProteinKey` so a modal opened in one workspace tab
 * stays scoped when the user switches active protein.
 */

import { create } from "zustand";

import type { ProteinKey } from "@/types/api";

export type ModalKind = "interaction" | "aggregated";

export interface ModalArgs {
  kind: ModalKind;
  protein: ProteinKey;
  payload: Record<string, unknown>;
}

interface ModalStoreState {
  open: boolean;
  args: ModalArgs | null;
  history: ModalArgs[];
}

interface ModalStoreActions {
  push: (args: ModalArgs) => void;
  close: () => void;
  pop: () => void;
}

export type ModalStore = ModalStoreState & ModalStoreActions;

export const useModalStore = create<ModalStore>((set) => ({
  open: false,
  args: null,
  history: [],

  push: (args) =>
    set((s) => ({
      open: true,
      args,
      history: [...s.history, args],
    })),

  close: () => set({ open: false, args: null, history: [] }),

  pop: () =>
    set((s) => {
      if (s.history.length <= 1) return { open: false, args: null, history: [] };
      const next = s.history.slice(0, -1);
      return { open: true, args: next[next.length - 1] ?? null, history: next };
    }),
}));
