/**
 * Entry point for the pipeline-events React island. Mounts at every DOM
 * node with id starting ``pipeline-events-``; the trailing slug is the
 * protein name. The backend template creates these nodes per-job, so
 * one page can host multiple concurrent drawers (one per in-flight
 * query) without any cross-wiring.
 */
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { PipelineEventsDrawer } from "./PipelineEventsDrawer";

function mountAll() {
  const nodes = document.querySelectorAll<HTMLElement>("[id^='pipeline-events-']");
  nodes.forEach((node) => {
    if (node.dataset.reactMounted === "1") return;
    const protein = (node.id.split("pipeline-events-")[1] || "").trim();
    if (!protein) return;
    const root = createRoot(node);
    root.render(
      <StrictMode>
        <PipelineEventsDrawer protein={protein} />
      </StrictMode>,
    );
    node.dataset.reactMounted = "1";
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", mountAll, { once: true });
} else {
  mountAll();
}

// Re-scan when the page dynamically inserts a drawer mount point
// (job tracker can spawn new per-protein nodes without a reload).
const observer = new MutationObserver(() => mountAll());
observer.observe(document.body, { childList: true, subtree: true });

// Expose a manual remount hook for debugging.
(window as typeof window & { ProPathsReact?: { mount: () => void } }).ProPathsReact = {
  mount: mountAll,
};
