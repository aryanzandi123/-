import { useEffect, useMemo } from "react";
import { useParams } from "react-router-dom";

import { useVisualizeQueries } from "@/api/queries";
import { parseProteinList } from "@/types/workspace";
import { useSnapStore, selectSnapshotsMap } from "@/store/useSnapStore";

/**
 * Multi-protein workspace route. Phase 1 ships the route handler + parallel
 * fetch wiring (per Q2 "architect from day one") but no UI — that's Phase ≥ 5.
 * Verification target: visiting /workspace/ATXN3,REST?spa=1 should populate
 * useSnapStore with both ATXN3 and REST entries even though no canvas renders.
 */
export function Workspace(): JSX.Element {
  const { proteinList } = useParams<{ proteinList: string }>();
  const parsed = useMemo(() => parseProteinList(proteinList), [proteinList]);
  const setActive = useSnapStore((s) => s.setActiveProtein);

  useEffect(() => {
    if (parsed.proteins[0]) setActive(parsed.proteins[0]);
  }, [parsed.proteins, setActive]);

  const queries = useVisualizeQueries(parsed.proteins);
  const snapshots = useSnapStore(selectSnapshotsMap);
  const loaded = useMemo(() => Array.from(snapshots.keys()), [snapshots]);

  return (
    <main
      style={{
        padding: 32,
        fontFamily: "system-ui, sans-serif",
        color: "#f8fafc",
        background: "#0b1220",
        minHeight: "100vh",
      }}
    >
      <h1 style={{ fontSize: 18, marginBottom: 12 }}>Workspace (skeleton)</h1>
      <p style={{ color: "#94a3b8", fontSize: 13, marginBottom: 24 }}>
        Multi-protein UI is Phase ≥ 5. This route exists so the URL shape is
        locked from day one and the multi-snapshot store is exercised.
      </p>

      <section style={{ marginBottom: 16 }}>
        <h2 style={{ fontSize: 13, color: "#cbd5e1", marginBottom: 6 }}>
          Requested
        </h2>
        <ul style={{ fontFamily: "ui-monospace, Menlo, monospace", fontSize: 12 }}>
          {parsed.proteins.map((p, idx) => {
            const status = queries[idx]?.status ?? "idle";
            return (
              <li key={p} style={{ marginBottom: 4 }}>
                {p} <span style={{ color: "#94a3b8" }}>· {status}</span>
              </li>
            );
          })}
        </ul>
        {parsed.invalid.length > 0 ? (
          <p style={{ color: "#f87171", fontSize: 12 }}>
            Skipped invalid: {parsed.invalid.join(", ")}
          </p>
        ) : null}
      </section>

      <section>
        <h2 style={{ fontSize: 13, color: "#cbd5e1", marginBottom: 6 }}>
          Loaded into store
        </h2>
        <ul style={{ fontFamily: "ui-monospace, Menlo, monospace", fontSize: 12 }}>
          {loaded.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      </section>
    </main>
  );
}
