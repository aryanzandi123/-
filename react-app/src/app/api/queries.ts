/**
 * TanStack Query hooks for ProPaths data.
 *
 * `useVisualizeQuery(protein)` fetches /api/results/<protein> and writes the
 * snapshot into `useSnapStore` on success. `useVisualizeQueries` does the
 * same in parallel for a list of proteins (multi-protein workspace per Q2).
 *
 * Server-side hydration via `window.__PROPATHS_BOOTSTRAP__` (set by
 * templates/visualize_spa.html) seeds the store BEFORE any query fires,
 * so single-protein navigation hits the cached entry immediately.
 */

import { useQueries, useQuery, type UseQueryResult } from "@tanstack/react-query";

import { getJSON } from "./client";
import { useSnapStore } from "@/store/useSnapStore";
import { EXPECTED_SCHEMA_VERSION } from "@/app/main";
import type { ProteinKey, VisualizeApiPayload } from "@/types/api";

const STALE_TIME_MS = 5 * 60 * 1000;

const seenSchemaWarnings = new Set<string>();

const writeIntoStore = (protein: ProteinKey, payload: VisualizeApiPayload) => {
  const snap = payload.snapshot_json;
  const ctx = payload.ctx_json ?? {};
  const diagnostics = payload._diagnostics ?? null;
  const schemaVersion = payload._schema_version ?? null;
  // Warn-once per drift value so we don't spam the console for every fetch.
  if (schemaVersion !== EXPECTED_SCHEMA_VERSION) {
    const key = `${protein}:${schemaVersion ?? "null"}`;
    if (!seenSchemaWarnings.has(key)) {
      seenSchemaWarnings.add(key);
      console.warn(
        `[ProPaths SPA] /api/results/${protein} returned _schema_version="${schemaVersion ?? "null"}", ` +
          `expected "${EXPECTED_SCHEMA_VERSION}". Render may have gaps.`,
      );
    }
  }
  useSnapStore.getState().setEntry(protein, snap, ctx, diagnostics, schemaVersion);
};

const fetchAndStore = async (protein: ProteinKey): Promise<VisualizeApiPayload> => {
  const data = await getJSON<VisualizeApiPayload>(
    `/api/results/${encodeURIComponent(protein)}`,
  );
  writeIntoStore(protein, data);
  return data;
};

export function useVisualizeQuery(
  protein: ProteinKey | null,
): UseQueryResult<VisualizeApiPayload, Error> {
  return useQuery<VisualizeApiPayload, Error>({
    queryKey: ["visualize", protein?.toUpperCase()],
    queryFn: () => fetchAndStore(protein!),
    enabled: Boolean(protein),
    staleTime: STALE_TIME_MS,
  });
}

export function useVisualizeQueries(
  proteins: ProteinKey[],
): UseQueryResult<VisualizeApiPayload, Error>[] {
  return useQueries({
    queries: proteins.map((protein) => ({
      queryKey: ["visualize", protein.toUpperCase()],
      queryFn: () => fetchAndStore(protein),
      staleTime: STALE_TIME_MS,
    })),
  });
}
