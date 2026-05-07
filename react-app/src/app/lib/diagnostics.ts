/**
 * Per-node badge derivation from `snap._diagnostics`.
 *
 * Ports `applyDepthBadges`, `applyPartialChainBadges`, `applyPathwayDriftBadges`
 * from `static/cv_diagnostics.js`. Returned maps are consumed by buildCardGraph.
 *
 * - `byProtein`: badges that apply to ANY node carrying that base protein
 *   (parent indirect interactor "partial", "rehomed", "drift", "depth-issue").
 * - `byHop`: badges that apply only to a specific chain-hop instance,
 *   keyed by `${prevProtein}->${currProtein}` upper-cased. The "no biology"
 *   per-hop badge from cv_diagnostics applyPartialChainBadges PASS 2.
 */

import type { Snapshot } from "@/types/api";
import type { ProteinBadge } from "@/app/views/card/ProteinCard";

interface ChainIncompleteEntry {
  interactor?: string;
  missing_hops?: string[];
}

interface PathwayDriftEntry {
  interactor?: string;
  function?: string;
  from?: string;
  to?: string;
  from_score?: number;
  to_score?: number;
  action?: "corrected" | "report_only" | string;
}

interface ClaimWithDepth {
  _depth_issues?: string[];
  [k: string]: unknown;
}

interface InteractionWithFunctions {
  source?: string;
  target?: string;
  primary?: string;
  partner?: string;
  functions?: ClaimWithDepth[];
  claims?: ClaimWithDepth[];
}

export interface DiagnosticsBadgeMaps {
  byProtein: Map<string, ProteinBadge[]>;
  byHop: Map<string, ProteinBadge>;
}

const upperOrEmpty = (v: unknown): string => (typeof v === "string" ? v.toUpperCase().trim() : "");

function pushBadge(map: Map<string, ProteinBadge[]>, key: string, badge: ProteinBadge): void {
  if (!key) return;
  const list = map.get(key) ?? [];
  if (!list.some((b) => b.kind === badge.kind && b.label === badge.label)) {
    list.push(badge);
    map.set(key, list);
  }
}

function depthIssuesByPartner(snap: Snapshot): Map<string, Set<string>> {
  const out = new Map<string, Set<string>>();
  const interactions = (snap.interactions ?? []) as InteractionWithFunctions[];
  for (const inter of interactions) {
    const partner = upperOrEmpty(inter.target ?? inter.primary ?? inter.partner);
    if (!partner) continue;
    const claims = inter.functions ?? inter.claims ?? [];
    let issues: Set<string> | null = null;
    for (const c of claims) {
      const di = c?._depth_issues;
      if (Array.isArray(di) && di.length) {
        if (!issues) issues = new Set<string>();
        for (const r of di) {
          if (typeof r === "string") issues.add(r);
        }
      }
    }
    if (issues && issues.size > 0) {
      const merged = out.get(partner) ?? new Set<string>();
      for (const r of issues) merged.add(r);
      out.set(partner, merged);
    }
  }
  return out;
}

export function deriveBadges(snap: Snapshot | null | undefined): DiagnosticsBadgeMaps {
  const byProtein = new Map<string, ProteinBadge[]>();
  const byHop = new Map<string, ProteinBadge>();
  if (!snap) return { byProtein, byHop };

  const diag = (snap._diagnostics ?? {}) as Record<string, unknown>;

  // Depth issues — per partner
  const depthByPartner = depthIssuesByPartner(snap);
  for (const [partner, issues] of depthByPartner) {
    const rules = Array.from(issues).join(", ");
    pushBadge(byProtein, partner, {
      kind: "depth",
      label: "!",
      tooltip: `Depth issues: ${rules}. Re-run query to redispatch.`,
    });
  }

  // Partial chains — parent-level "partial" + per-hop "no biology"
  const incomplete = Array.isArray(diag.chain_incomplete_hops)
    ? (diag.chain_incomplete_hops as ChainIncompleteEntry[])
    : [];
  for (const entry of incomplete) {
    const interactor = upperOrEmpty(entry?.interactor);
    if (!interactor) continue;
    const hops = Array.isArray(entry.missing_hops) ? entry.missing_hops : [];
    if (hops.length > 0) {
      pushBadge(byProtein, interactor, {
        kind: "partial-chain",
        label: "partial",
        tooltip: `Missing chain hops: ${hops.join(", ")}`,
      });
    }
    for (const h of hops) {
      if (typeof h !== "string" || !h.includes("->")) continue;
      byHop.set(h.toUpperCase(), {
        kind: "no-biology",
        label: "no biology",
        tooltip: `Hop ${h} has no validated claim. Cascade still renders so structure is visible; re-run query to attempt recovery.`,
      });
    }
  }

  // Pathway drift — corrected vs report_only
  const drifts = Array.isArray(diag.pathway_drifts)
    ? (diag.pathway_drifts as PathwayDriftEntry[])
    : [];
  for (const entry of drifts) {
    const interactor = upperOrEmpty(entry?.interactor);
    if (!interactor) continue;
    const action = entry.action ?? "report_only";
    const tooltip =
      `Pathway drift on "${entry.function ?? "?"}":` +
      ` ${entry.from ?? "?"} (score ${entry.from_score ?? "?"})` +
      ` → ${entry.to ?? "?"} (score ${entry.to_score ?? "?"})`;
    if (action === "corrected") {
      pushBadge(byProtein, interactor, {
        kind: "drift",
        label: "rehomed",
        tooltip,
      });
    } else {
      pushBadge(byProtein, interactor, {
        kind: "drift",
        label: "drift",
        tooltip,
      });
    }
  }

  return { byProtein, byHop };
}
