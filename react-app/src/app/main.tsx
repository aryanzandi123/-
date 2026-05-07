import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import "./styles/tokens.css";
import { App } from "./App";
import { useSnapStore } from "@/store/useSnapStore";
import type { VisualizeApiPayload, ProteinKey } from "@/types/api";

/**
 * Snapshot/ctx contract version the SPA expects. Mirrors
 * `services/data_builder.SCHEMA_VERSION`. When the backend ships a payload
 * whose `_schema_version` differs, the SPA logs a console warning so the
 * dev sees the drift early. The SPA still renders — best-effort tolerance,
 * not a blocker. Bump in lockstep with the backend.
 */
export const EXPECTED_SCHEMA_VERSION = "2026-05-07";

interface BootstrapPayload {
  protein: ProteinKey;
  payload: VisualizeApiPayload;
}

declare global {
  interface Window {
    __PROPATHS_BOOTSTRAP__?: BootstrapPayload;
  }
}

function checkSchemaVersion(actual: string | null | undefined): void {
  if (!actual) {
    console.warn(
      `[ProPaths SPA] Backend payload has no _schema_version field (expected "${EXPECTED_SCHEMA_VERSION}"). ` +
        "Backend may be older than the SPA expects.",
    );
    return;
  }
  if (actual !== EXPECTED_SCHEMA_VERSION) {
    console.warn(
      `[ProPaths SPA] Schema version mismatch: backend emitted "${actual}", SPA expects "${EXPECTED_SCHEMA_VERSION}". ` +
        "Some fields may render incorrectly. Update both sides in lockstep.",
    );
  }
}

function hydrateFromBootstrap(): void {
  const boot = window.__PROPATHS_BOOTSTRAP__;
  if (!boot?.protein || !boot.payload?.snapshot_json) return;
  const { protein, payload } = boot;
  const snap = payload.snapshot_json;
  const ctx = payload.ctx_json ?? {};
  const diagnostics = payload._diagnostics ?? null;
  const schemaVersion = payload._schema_version ?? null;
  checkSchemaVersion(schemaVersion);
  const store = useSnapStore.getState();
  store.setEntry(protein, snap, ctx, diagnostics, schemaVersion);
  store.setActiveProtein(protein);
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5 * 60 * 1000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

const rootEl = document.getElementById("root");
if (!rootEl) throw new Error("ProPaths SPA: #root element missing");

hydrateFromBootstrap();

createRoot(rootEl).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
);
