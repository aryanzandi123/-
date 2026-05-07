/**
 * SSE re-export. Single source of truth lives in `src/shared/useSSE.ts`
 * (already used by the pipeline-events island). The SPA consumes the
 * same hook for the pipeline drawer + diagnostics live updates.
 */

export { useSSE } from "@/shared/useSSE";
