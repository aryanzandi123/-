/**
 * Legend — collapsible verb-color reference for the CardView.
 *
 * Shows each arrow class with its rendered color so the user can decode
 * the canvas at a glance. Collapsed by default to keep canvas real estate
 * free; click the header to expand.
 */

import { useState } from "react";

import { ARROW_COLORS } from "@/lib/colors";

const ROWS: { kind: keyof typeof ARROW_COLORS; example: string; note: string }[] = [
  { kind: "positive", example: "activates", note: "phosphorylates · stabilizes · induces · recruits · deubiquitinates" },
  { kind: "negative", example: "inhibits", note: "ubiquitinates · degrades · destabilizes · represses · cleaves" },
  { kind: "binding", example: "binds", note: "" },
  { kind: "regulatory", example: "regulates", note: "" },
  { kind: "reverse", example: "is_substrate_of", note: "italic — reverse-direction verb" },
];

export function Legend(): JSX.Element {
  const [open, setOpen] = useState(false);

  return (
    <div
      style={{
        position: "absolute",
        top: 12,
        right: 12,
        zIndex: 5,
        background: "rgba(11, 18, 32, 0.92)",
        border: "1px solid #1e293b",
        borderRadius: 8,
        fontFamily: "system-ui, -apple-system, sans-serif",
        fontSize: 11,
        color: "#cbd5e1",
        boxShadow: "0 4px 12px rgba(0, 0, 0, 0.4)",
        backdropFilter: "blur(4px)",
      }}
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-label="Toggle verb color legend"
        style={{
          background: "transparent",
          border: "none",
          color: "#cbd5e1",
          padding: "6px 10px",
          cursor: "pointer",
          display: "flex",
          alignItems: "center",
          gap: 6,
          fontSize: 11,
          fontWeight: 600,
        }}
      >
        <span style={{ color: "#94a3b8" }}>Legend</span>
        <span style={{ color: "#64748b", fontSize: 9 }}>{open ? "▾" : "▸"}</span>
      </button>
      {open ? (
        <div style={{ padding: "0 10px 10px", display: "grid", gap: 4, minWidth: 200 }}>
          {ROWS.map((r) => (
            <div key={r.kind} style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <span
                style={{
                  display: "inline-block",
                  width: 18,
                  height: 2,
                  background: ARROW_COLORS[r.kind],
                  borderRadius: 1,
                  flexShrink: 0,
                }}
              />
              <span
                style={{
                  color: ARROW_COLORS[r.kind],
                  fontFamily: "ui-monospace, Menlo, monospace",
                  fontWeight: 600,
                  fontStyle: r.kind === "reverse" ? "italic" : "normal",
                }}
              >
                {r.example}
              </span>
              {r.note ? (
                <span style={{ color: "#64748b", fontSize: 10 }}>{r.note}</span>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
