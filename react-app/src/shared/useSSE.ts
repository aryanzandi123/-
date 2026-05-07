/**
 * Thin React hook around EventSource for the ProPaths /api/stream/<protein>
 * SSE feed. Returns the latest parsed payload, connection status, and a
 * ring-buffered slice of pipeline events extracted from the payload.
 *
 * Shape matches the vanilla tracker in static/script.js — both consumers
 * can coexist during the incremental React migration.
 */
import { useEffect, useRef, useState } from "react";

export interface PipelineEvent {
  t?: number;
  event: string;
  level?: string;
  tag?: string;
  // Arbitrary extra fields from log_event(**fields).
  [key: string]: unknown;
}

export interface JobPayload {
  status?: string;
  progress?: string | { text?: string } | null;
  events?: PipelineEvent[];
  [key: string]: unknown;
}

export type ConnectionState = "connecting" | "open" | "closed" | "error";

export function useSSE(protein: string | null): {
  payload: JobPayload | null;
  events: PipelineEvent[];
  state: ConnectionState;
} {
  const [payload, setPayload] = useState<JobPayload | null>(null);
  const [events, setEvents] = useState<PipelineEvent[]>([]);
  const [state, setState] = useState<ConnectionState>("connecting");
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    if (!protein) {
      setState("closed");
      return;
    }
    if (typeof EventSource === "undefined") {
      setState("error");
      return;
    }
    const es = new EventSource(`/api/stream/${encodeURIComponent(protein)}`);
    esRef.current = es;
    setState("connecting");

    const handleData = (ev: MessageEvent) => {
      try {
        const data = JSON.parse(ev.data) as JobPayload;
        setPayload(data);
        if (Array.isArray(data.events)) {
          setEvents(data.events);
        }
        setState("open");
      } catch {
        // Ignore malformed frames; we'll get another one.
      }
    };

    es.onmessage = handleData;
    es.addEventListener("done", handleData as EventListener);
    es.onerror = () => {
      setState("error");
      es.close();
    };

    return () => {
      es.close();
      esRef.current = null;
    };
  }, [protein]);

  return { payload, events, state };
}
