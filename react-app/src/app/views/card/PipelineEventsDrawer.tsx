/**
 * PipelineEventsDrawer — SPA-native replacement for the legacy
 * `react-app/src/islands/pipeline-events/` mount.
 *
 * Subscribes to `/api/stream/<protein>` via the existing `useSSE` hook.
 * Renders a collapsible drawer in the bottom-right of the canvas while
 * the pipeline is active. Auto-collapses to a tiny status pill once the
 * stream closes (job done) — non-intrusive when nothing's happening.
 *
 * Without this component, kicking off a new query INSIDE the SPA gives
 * the user no progress feedback. Tier C cutover blocker.
 */

import { useEffect, useMemo, useRef, useState } from "react";

import { useSSE, type PipelineEvent } from "@/shared/useSSE";

interface PipelineEventsDrawerProps {
  protein: string;
}

const LEVEL_COLOR: Record<string, string> = {
  info: "#94a3b8",
  warn: "#f59e0b",
  warning: "#f59e0b",
  error: "#ef4444",
  debug: "#64748b",
};

const LEVELS: Array<"all" | "info" | "warn" | "error" | "debug"> = [
  "all",
  "info",
  "warn",
  "error",
  "debug",
];

export function PipelineEventsDrawer({ protein }: PipelineEventsDrawerProps): JSX.Element | null {
  const { events, state } = useSSE(protein);
  const [filter, setFilter] = useState<typeof LEVELS[number]>("all");
  const [open, setOpen] = useState(true);
  const [dismissed, setDismissed] = useState(false);
  const listRef = useRef<HTMLOListElement | null>(null);

  // Auto-scroll to newest event when stream is open + drawer expanded.
  useEffect(() => {
    if (!open || state !== "open") return;
    const el = listRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [events, open, state]);

  // Auto-collapse when stream closes (job done) — doesn't auto-dismiss.
  useEffect(() => {
    if (state === "closed") setOpen(false);
  }, [state]);

  const filteredEvents = useMemo(() => {
    if (filter === "all") return events;
    return events.filter((e: PipelineEvent) => (e.level ?? "info").toLowerCase() === filter);
  }, [events, filter]);

  // No connection yet AND no events → don't render at all (idle state for
  // already-completed protein views).
  if (dismissed) return null;
  if (state === "closed" && events.length === 0) return null;
  if (state === "error" && events.length === 0) return null;

  const statusDot =
    state === "open"
      ? "#10b981"
      : state === "error"
        ? "#ef4444"
        : state === "connecting"
          ? "#fbbf24"
          : "#64748b";

  return (
    <div
      role="region"
      aria-label="Pipeline events"
      style={{
        position: "absolute",
        right: 12,
        bottom: 12,
        zIndex: 6,
        background: "rgba(11, 18, 32, 0.95)",
        border: "1px solid #1e293b",
        borderRadius: 8,
        boxShadow: "0 8px 24px rgba(0, 0, 0, 0.5)",
        backdropFilter: "blur(4px)",
        minWidth: 320,
        maxWidth: 480,
        fontFamily: "system-ui, -apple-system, sans-serif",
        color: "#cbd5e1",
        fontSize: 11,
        overflow: "hidden",
      }}
    >
      <header
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 8,
          padding: "8px 10px",
          borderBottom: open ? "1px solid #1e293b" : "none",
          cursor: "pointer",
        }}
        onClick={() => setOpen((v) => !v)}
        role="button"
        aria-expanded={open}
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            setOpen((v) => !v);
          }
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span
            aria-label={`stream status: ${state}`}
            title={`stream status: ${state}`}
            style={{
              width: 8,
              height: 8,
              borderRadius: "50%",
              background: statusDot,
              flexShrink: 0,
            }}
          />
          <strong style={{ fontSize: 11, color: "#f1f5f9" }}>Pipeline events</strong>
          <span style={{ color: "#64748b", fontSize: 10 }}>
            {filteredEvents.length} / {events.length}
          </span>
        </div>
        <div style={{ display: "flex", gap: 4, alignItems: "center" }} onClick={(e) => e.stopPropagation()}>
          {open ? (
            <select
              value={filter}
              onChange={(e) => setFilter(e.target.value as typeof LEVELS[number])}
              aria-label="Filter events by level"
              style={{
                background: "#0f172a",
                border: "1px solid #1e293b",
                color: "#cbd5e1",
                fontSize: 10,
                padding: "2px 4px",
                borderRadius: 4,
              }}
            >
              {LEVELS.map((l) => (
                <option key={l} value={l}>
                  {l}
                </option>
              ))}
            </select>
          ) : null}
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              setOpen((v) => !v);
            }}
            aria-label={open ? "Collapse pipeline events" : "Expand pipeline events"}
            style={iconBtn}
          >
            {open ? "▾" : "▸"}
          </button>
          {state === "closed" ? (
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                setDismissed(true);
              }}
              aria-label="Dismiss pipeline events"
              style={iconBtn}
            >
              ×
            </button>
          ) : null}
        </div>
      </header>

      {open ? (
        <ol
          ref={listRef}
          style={{
            listStyle: "none",
            padding: "4px 10px",
            margin: 0,
            maxHeight: 220,
            overflowY: "auto",
            fontFamily: "ui-monospace, Menlo, monospace",
            fontSize: 10,
            lineHeight: 1.4,
          }}
        >
          {filteredEvents.length === 0 ? (
            <li style={{ color: "#64748b", padding: "6px 0", fontStyle: "italic" }}>
              {state === "connecting"
                ? "Connecting to event stream…"
                : state === "open"
                  ? "Waiting for first event…"
                  : "No events for this query."}
            </li>
          ) : (
            filteredEvents.map((ev: PipelineEvent, idx: number) => {
              const level = (ev.level ?? "info").toLowerCase();
              const tag = ev.tag ?? ev.event;
              const extras = Object.entries(ev).filter(
                ([k]) => !["t", "event", "level", "tag"].includes(k),
              );
              return (
                <li
                  key={idx}
                  style={{
                    padding: "2px 0",
                    borderBottom: "1px solid rgba(30, 41, 59, 0.5)",
                  }}
                >
                  <span
                    style={{
                      color: LEVEL_COLOR[level] ?? LEVEL_COLOR.info,
                      fontWeight: 600,
                    }}
                  >
                    [{tag}]
                  </span>{" "}
                  <span style={{ color: "#e2e8f0" }}>{ev.event}</span>{" "}
                  {extras.map(([k, v]) => (
                    <span key={k} style={{ color: "#64748b" }}>
                      <span style={{ opacity: 0.7 }}>{k}</span>=
                      <span style={{ color: "#cbd5e1" }}>
                        {typeof v === "object" ? JSON.stringify(v) : String(v)}
                      </span>{" "}
                    </span>
                  ))}
                </li>
              );
            })
          )}
        </ol>
      ) : null}
    </div>
  );
}

const iconBtn: React.CSSProperties = {
  background: "transparent",
  border: "1px solid #334155",
  color: "#cbd5e1",
  fontSize: 11,
  width: 22,
  height: 22,
  borderRadius: 4,
  cursor: "pointer",
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
};
