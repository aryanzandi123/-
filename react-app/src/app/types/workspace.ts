/**
 * Multi-protein workspace types. Q2 of frontend-overhaul.md open questions
 * resolved as "architect from day one" — v1 ships single-protein UI but the
 * store, route, and type contracts support multi-protein.
 */

import type { ProteinKey, Snapshot, Context, Diagnostics } from "./api";

/** One per-protein entry in `useSnapStore.snapshots`. */
export interface SnapshotEntry {
  protein: ProteinKey;
  snap: Snapshot;
  ctx: Context;
  diagnostics: Diagnostics | null;
  schemaVersion: string | null;
  loadedAt: number;
}

/** Parsed `:proteinList` route param (e.g. "ATXN3,REST,TDP43" → ["ATXN3", "REST", "TDP43"]). */
export interface ParsedWorkspace {
  proteins: ProteinKey[];
  invalid: string[];
}

const SYMBOL_RE = /^[A-Za-z][A-Za-z0-9-]{0,15}$/;

export function parseProteinList(raw: string | undefined): ParsedWorkspace {
  if (!raw) return { proteins: [], invalid: [] };
  const proteins: ProteinKey[] = [];
  const invalid: string[] = [];
  const seen = new Set<string>();
  for (const piece of raw.split(",")) {
    const trimmed = piece.trim().toUpperCase();
    if (!trimmed) continue;
    if (!SYMBOL_RE.test(trimmed)) {
      invalid.push(piece);
      continue;
    }
    if (seen.has(trimmed)) continue;
    seen.add(trimmed);
    proteins.push(trimmed);
  }
  return { proteins, invalid };
}
