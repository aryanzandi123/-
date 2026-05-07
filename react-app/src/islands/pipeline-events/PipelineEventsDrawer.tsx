/**
 * Pipeline-events drawer — first React island for ProPaths.
 *
 * Displays every event the backend's log_event() pushes into
 * ``jobs[protein]["events"]`` (see utils/observability.py and
 * routes/query.py SSE stream). Filters by severity level, auto-scrolls
 * to the newest event, and surfaces key router / drift tags for quick
 * triage.
 *
 * Renders nothing until the first event arrives, so stale mounts on
 * pages where no job is running don't pollute the layout.
 */
import { useMemo, useState } from "react";
import { useSSE, type PipelineEvent } from "../../shared/useSSE";

interface Props {
  protein: string;
}

type LevelFilter = "all" | "info" | "warn" | "error" | "debug";

const LEVEL_COLORS: Record<string, string> = {
  error: "var(--color-error-text, #991b1b)",
  warn: "var(--color-badge-indirect, #f59e0b)",
  warning: "var(--color-badge-indirect, #f59e0b)",
  info: "var(--color-text-secondary, #6b7280)",
  debug: "var(--color-border-medium, #9ca3af)",
};

function fieldEntries(ev: PipelineEvent): Array<[string, string]> {
  const out: Array<[string, string]> = [];
  for (const [k, v] of Object.entries(ev)) {
    if (["t", "event", "level", "tag"].includes(k)) continue;
    if (v === undefined || v === null) continue;
    out.push([k, typeof v === "object" ? JSON.stringify(v) : String(v)]);
  }
  return out;
}

export function PipelineEventsDrawer({ protein }: Props) {
  const { events, state } = useSSE(protein);
  const [filter, setFilter] = useState<LevelFilter>("all");

  const visible = useMemo(() => {
    if (filter === "all") return events;
    return events.filter((e) => (e.level || "info").toLowerCase() === filter);
  }, [events, filter]);

  if (!protein) return null;

  return (
    <div
      className="pipeline-events-drawer"
      style={{
        background: "var(--color-drawer-bg, #f9fafb)",
        border: "1px solid var(--color-border-subtle, #e5e7eb)",
        borderRadius: "var(--radius-lg, 8px)",
        padding: "12px 16px",
        fontFamily: "var(--font-sans, sans-serif)",
        color: "var(--color-text-primary, #111)",
        marginTop: "16px",
      }}
      role="region"
      aria-label="Pipeline events"
    >
      <header
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: "8px",
        }}
      >
        <strong style={{ fontSize: "0.95rem" }}>
          Pipeline events
          <span
            aria-label={`stream status: ${state}`}
            title={state}
            style={{
              display: "inline-block",
              width: 8,
              height: 8,
              borderRadius: "50%",
              marginLeft: 8,
              background:
                state === "open"
                  ? "var(--color-pathway-current, #10b981)"
                  : state === "error"
                  ? "var(--color-error-border, #dc2626)"
                  : "var(--color-text-secondary, #6b7280)",
            }}
          />
        </strong>
        <LevelPicker value={filter} onChange={setFilter} />
      </header>
      {visible.length === 0 ? (
        <div
          style={{
            fontStyle: "italic",
            color: "var(--color-text-secondary, #6b7280)",
            fontSize: "0.85rem",
          }}
        >
          No pipeline events yet.
        </div>
      ) : (
        <ol
          style={{
            listStyle: "none",
            padding: 0,
            margin: 0,
            maxHeight: "300px",
            overflowY: "auto",
            fontFamily: "var(--font-mono, monospace)",
            fontSize: "0.75rem",
            lineHeight: 1.4,
          }}
        >
          {visible.map((ev, i) => {
            const lvl = (ev.level || "info").toLowerCase();
            const tag = ev.tag || ev.event;
            const fields = fieldEntries(ev);
            return (
              <li
                key={i}
                style={{
                  padding: "4px 0",
                  borderBottom: "1px solid var(--color-border-subtle, #e5e7eb)",
                }}
              >
                <span
                  style={{
                    color: LEVEL_COLORS[lvl] || LEVEL_COLORS.info,
                    fontWeight: 600,
                  }}
                >
                  [{tag}]
                </span>{" "}
                <span>{ev.event}</span>{" "}
                {fields.map(([k, v]) => (
                  <span key={k} style={{ color: "var(--color-text-secondary)" }}>
                    <span style={{ opacity: 0.7 }}>{k}</span>=
                    <span style={{ color: "var(--color-text-primary)" }}>{v}</span>{" "}
                  </span>
                ))}
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}

function LevelPicker({
  value,
  onChange,
}: {
  value: LevelFilter;
  onChange: (v: LevelFilter) => void;
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value as LevelFilter)}
      aria-label="Filter events by level"
      style={{
        fontSize: "0.75rem",
        padding: "2px 6px",
        borderRadius: "4px",
        border: "1px solid var(--color-border-medium, #d1d5db)",
        background: "var(--color-bg-primary, #fff)",
        color: "var(--color-text-primary, #111)",
      }}
    >
      <option value="all">all</option>
      <option value="info">info</option>
      <option value="warn">warn</option>
      <option value="error">error</option>
      <option value="debug">debug</option>
    </select>
  );
}
