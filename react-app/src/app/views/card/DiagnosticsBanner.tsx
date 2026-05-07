/**
 * DiagnosticsBanner: ports `static/cv_diagnostics.renderBanner` to React.
 *
 * Reads from `SNAP._diagnostics` (merged from `Logs/<protein>/pipeline_diagnostics.json`
 * by services/data_builder.py:1839-1860). Shows pass_rate, shallow funcs,
 * dropped, unrecoverable, partial chains, pathway drift counts.
 *
 * Click "details" expands per-entry breakdown — Phase 2 polish.
 */

import { useMemo, useState } from "react";

import { useSnapStore, selectActiveDiagnostics } from "@/store/useSnapStore";

type Severity = "good" | "warn" | "bad" | "neutral";

interface DiagItem {
  label: string;
  value: string;
  severity: Severity;
  tooltip?: string;
}

const SEVERITY_BG: Record<Severity, string> = {
  good: "#064e3b",
  warn: "#854d0e",
  bad: "#7f1d1d",
  neutral: "#1e293b",
};

const SEVERITY_FG: Record<Severity, string> = {
  good: "#6ee7b7",
  warn: "#fde68a",
  bad: "#fecaca",
  neutral: "#cbd5e1",
};

interface QualityReport {
  pass_rate?: number;
  total_functions?: number;
  flagged_functions?: number;
  violations?: unknown[];
}

interface DiagShape {
  quality_report?: QualityReport;
  pipeline_metadata?: { quality_report?: QualityReport };
  chain_incomplete_hops?: unknown[];
  chain_pair_unrecoverable?: unknown[];
  pathway_drifts?: { action?: string }[];
  dropped?: { count?: number };
  shallow?: { count?: number };
}

function buildItems(d: DiagShape | null | undefined): DiagItem[] {
  const out: DiagItem[] = [];
  if (!d) return out;

  const quality = d.quality_report ?? d.pipeline_metadata?.quality_report;
  if (quality && typeof quality.pass_rate === "number") {
    const pct = Math.round(quality.pass_rate * 100);
    const sev: Severity = pct >= 80 ? "good" : pct >= 50 ? "warn" : "bad";
    out.push({
      label: "PhD-depth",
      value: `${pct}%`,
      severity: sev,
      tooltip: `${quality.flagged_functions ?? 0} flagged of ${quality.total_functions ?? 0} functions`,
    });
  }

  if (typeof d.shallow?.count === "number" && d.shallow.count > 0) {
    out.push({ label: "shallow", value: String(d.shallow.count), severity: "warn" });
  }

  if (typeof d.dropped?.count === "number" && d.dropped.count > 0) {
    out.push({ label: "dropped", value: String(d.dropped.count), severity: "bad" });
  }

  if (Array.isArray(d.chain_incomplete_hops) && d.chain_incomplete_hops.length > 0) {
    out.push({
      label: "partial chains",
      value: String(d.chain_incomplete_hops.length),
      severity: "warn",
      tooltip: "Chains with at least one hop missing biology",
    });
  }

  if (Array.isArray(d.chain_pair_unrecoverable) && d.chain_pair_unrecoverable.length > 0) {
    out.push({
      label: "unrecoverable",
      value: String(d.chain_pair_unrecoverable.length),
      severity: "bad",
    });
  }

  if (Array.isArray(d.pathway_drifts)) {
    let corrected = 0;
    let reportOnly = 0;
    for (const e of d.pathway_drifts) {
      if (e?.action === "corrected") corrected++;
      else if (e?.action === "report_only") reportOnly++;
    }
    if (corrected > 0) {
      out.push({ label: "pathway rehomed", value: String(corrected), severity: "good" });
    }
    if (reportOnly > 0) {
      out.push({ label: "pathway drift", value: String(reportOnly), severity: "warn" });
    }
  }

  return out;
}

export function DiagnosticsBanner(): JSX.Element | null {
  const diag = useSnapStore(selectActiveDiagnostics);
  const items = useMemo(() => buildItems(diag as DiagShape | null), [diag]);
  const [showDetails, setShowDetails] = useState(false);

  if (items.length === 0) return null;

  return (
    <div
      role="status"
      aria-label="Pipeline quality diagnostics"
      aria-live="polite"
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: "6px 16px",
        background: "#070b14",
        borderBottom: "1px solid #1e293b",
        fontFamily: "system-ui, sans-serif",
        fontSize: 11,
        flexWrap: "wrap",
      }}
    >
      {items.map((it) => (
        <span
          key={it.label}
          title={it.tooltip ?? `${it.label}: ${it.value}`}
          style={{
            background: SEVERITY_BG[it.severity],
            color: SEVERITY_FG[it.severity],
            padding: "2px 8px",
            borderRadius: 999,
            fontSize: 10,
            fontWeight: 600,
            whiteSpace: "nowrap",
          }}
        >
          <span style={{ opacity: 0.8, marginRight: 4 }}>{it.label}</span>
          <span>{it.value}</span>
        </span>
      ))}
      <button
        type="button"
        onClick={() => setShowDetails((v) => !v)}
        style={{
          background: "transparent",
          border: "1px solid #334155",
          color: "#94a3b8",
          fontSize: 10,
          padding: "2px 8px",
          borderRadius: 999,
          cursor: "pointer",
        }}
      >
        {showDetails ? "hide" : "details"}
      </button>
      {showDetails ? (
        <pre
          style={{
            width: "100%",
            margin: "8px 0 0",
            padding: 12,
            background: "#0f172a",
            color: "#94a3b8",
            fontSize: 10,
            lineHeight: 1.5,
            borderRadius: 6,
            maxHeight: 240,
            overflow: "auto",
          }}
        >
          {diag ? JSON.stringify(diag, null, 2) : "no diagnostics"}
        </pre>
      ) : null}
    </div>
  );
}
