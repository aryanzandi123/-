/**
 * Per-pathway derived statistics for the PathwayExplorer.
 *
 * Pure transform: snap → Map<pathwayId, PathwayStat>. No I/O, no React.
 * Memoize at the consumer with `useMemo([snap])`.
 *
 * Stats that drive the explorer rows:
 *   - directCount + chainCount          → relevance / mini-bar
 *   - partialChainCount                 → "partial" dot
 *   - driftCorrected / driftReportOnly  → drift dot
 *   - pseudoTouching                    → pseudo dot
 *   - passRateMean                      → letter grade
 *   - memberProteins                    → member-protein search
 *   - hasIssues                         → quick-filter chip
 */

import { isPlaceholderText } from "@/lib/claims";
import { isPseudoProtein } from "@/lib/pseudo";
import { normalizePathwayName } from "@/lib/normalize";
import type {
  Snapshot,
  Interaction,
  Pathway,
  Claim,
  ChainSummary,
} from "@/types/api";

export type PathwayLetterGrade = "A+" | "A" | "B" | "C" | "—";

export interface PathwayStat {
  id: string;
  name: string;
  description: string | null;
  depth: number;
  parentIds: string[];
  childIds: string[];
  ancestorIds: string[];
  interactorIds: ReadonlySet<string>;
  directCount: number;
  chainCount: number;
  partialChainCount: number;
  driftCorrected: number;
  driftReportOnly: number;
  pseudoTouching: boolean;
  passRateMean: number | null;
  letterGrade: PathwayLetterGrade;
  hasIssues: boolean;
  memberProteins: ReadonlySet<string>;
  /** Pathway looks like a top-level catch-all that swallows almost every interactor. */
  isCatchAll: boolean;
}

const upperOrEmpty = (v: unknown): string =>
  typeof v === "string" ? v.toUpperCase().trim() : "";

const safeNumber = (v: unknown): number | null => {
  if (typeof v === "number" && Number.isFinite(v)) return v;
  return null;
};

const sentenceCount = (s: unknown): number => {
  if (typeof s !== "string") return 0;
  if (isPlaceholderText(s)) return 0;
  return s.split(/[.!?]+\s+/).map((x) => x.trim()).filter((x) => x.length > 0).length;
};

const consequencesCount = (v: unknown): number => {
  if (Array.isArray(v)) {
    return v.filter((x) => typeof x === "string" && !isPlaceholderText(x)).length;
  }
  if (typeof v === "string" && !isPlaceholderText(v)) return 1;
  return 0;
};

const evidenceCount = (v: unknown): number => {
  if (!Array.isArray(v)) return 0;
  const seen = new Set<string>();
  for (const e of v) {
    if (!e || typeof e !== "object") continue;
    const pmid = (e as { pmid?: unknown }).pmid;
    if (typeof pmid === "string" && pmid.trim()) seen.add(pmid.trim());
    else if (typeof pmid === "number") seen.add(String(pmid));
  }
  return seen.size;
};

/**
 * Per-claim pass score (0..1) using the user's PhD-depth thresholds:
 *   ≥6 effect sentences, ≥3 cascades, ≥3 unique PMIDs. 1/3 weight each.
 * Returns null if all three signals are unmeasurable (placeholder claim).
 */
export function claimPassScore(claim: Claim): number | null {
  const d = sentenceCount(claim.effect_description);
  const c = consequencesCount(claim.biological_consequences);
  const e = evidenceCount(claim.evidence);
  if (d === 0 && c === 0 && e === 0) return null;
  const dPass = d >= 6 ? 1 : d >= 3 ? 0.5 : 0;
  const cPass = c >= 3 ? 1 : c >= 1 ? 0.5 : 0;
  const ePass = e >= 3 ? 1 : e >= 1 ? 0.5 : 0;
  return (dPass + cPass + ePass) / 3;
}

function letterGradeFor(passRateMean: number | null): PathwayLetterGrade {
  if (passRateMean == null) return "—";
  if (passRateMean >= 0.95) return "A+";
  if (passRateMean >= 0.8) return "A";
  if (passRateMean >= 0.5) return "B";
  return "C";
}

interface DiagShape {
  pathway_drifts?: { interactor?: string; action?: string; to?: string; from?: string }[];
  chain_incomplete_hops?: { interactor?: string }[];
}

function gatherChainsFor(
  inter: Interaction,
): ChainSummary[] {
  if (Array.isArray(inter.all_chains) && inter.all_chains.length > 0) return inter.all_chains;
  if (inter._chain_entity && (inter._chain_entity as ChainSummary).chain_id != null) {
    return [inter._chain_entity];
  }
  return [];
}

export function derivePathwayStats(snap: Snapshot | null | undefined): Map<string, PathwayStat> {
  const out = new Map<string, PathwayStat>();
  if (!snap) return out;

  const pathways = Array.isArray(snap.pathways) ? snap.pathways : [];
  const interactions = Array.isArray(snap.interactions) ? snap.interactions : [];
  const diag = (snap._diagnostics ?? {}) as DiagShape;

  // Build pathway-id index + a normalized-name → id index for joining
  // chain.pathway_name and inter.pathways (which are name strings).
  const byId = new Map<string, Pathway>();
  const idByNormName = new Map<string, string>();
  for (const p of pathways) {
    byId.set(p.id, p);
    if (p.name) idByNormName.set(normalizePathwayName(p.name), p.id);
  }

  // Resolve depth from hierarchy_level if available; fall back to ancestor walk.
  const depthOf = (p: Pathway): number => {
    if (typeof p.hierarchy_level === "number") return p.hierarchy_level;
    if (Array.isArray(p.ancestor_ids)) return p.ancestor_ids.length;
    return 0;
  };

  // Initialize per-pathway accumulators.
  type Accum = {
    direct: number;
    chain: number;
    partial: number;
    pseudo: boolean;
    members: Set<string>;
    pass: { sum: number; count: number };
  };
  const acc = new Map<string, Accum>();
  for (const p of pathways) {
    acc.set(p.id, {
      direct: 0,
      chain: 0,
      partial: 0,
      pseudo: false,
      members: new Set<string>(Array.isArray(p.interactor_ids) ? p.interactor_ids.map(upperOrEmpty).filter(Boolean) : []),
      pass: { sum: 0, count: 0 },
    });
  }

  const resolvePathwayIds = (raw: unknown): string[] => {
    if (!Array.isArray(raw)) return [];
    const ids: string[] = [];
    for (const name of raw) {
      if (typeof name !== "string") continue;
      const id = idByNormName.get(normalizePathwayName(name));
      if (id) ids.push(id);
    }
    return ids;
  };

  // Walk every interaction, attribute counts to pathways it touches.
  for (const inter of interactions) {
    const queryPathwayIds = resolvePathwayIds(inter.pathways);
    const chainPathwayIds = resolvePathwayIds(inter.chain_pathways);
    const isChainLink = Boolean(inter._is_chain_link);
    const isPseudo =
      Boolean(inter._source_is_pseudo) ||
      Boolean(inter._target_is_pseudo) ||
      Boolean(inter._partner_is_pseudo) ||
      isPseudoProtein(inter.source) ||
      isPseudoProtein(inter.target);

    // Direct vs chain
    if (isChainLink || gatherChainsFor(inter).length > 0) {
      // Chain hop / chain row — count under chain_pathways AND under pathway_name of each chain
      const chainIds = new Set<string>([...chainPathwayIds]);
      for (const ch of gatherChainsFor(inter)) {
        if (ch.pathway_name) {
          const id = idByNormName.get(normalizePathwayName(ch.pathway_name));
          if (id) chainIds.add(id);
        }
        if (Array.isArray(ch.chain_pathways)) {
          for (const pn of ch.chain_pathways) {
            const id = idByNormName.get(normalizePathwayName(pn));
            if (id) chainIds.add(id);
          }
        }
        if (Array.isArray(ch.chain_proteins)) {
          for (const cp of ch.chain_proteins) {
            const upper = upperOrEmpty(cp);
            if (!upper) continue;
            for (const pid of chainIds) {
              const a = acc.get(pid);
              if (a) a.members.add(upper);
            }
          }
        }
      }
      for (const pid of chainIds) {
        const a = acc.get(pid);
        if (!a) continue;
        a.chain += 1;
        if (isPseudo) a.pseudo = true;
      }
    } else {
      for (const pid of queryPathwayIds) {
        const a = acc.get(pid);
        if (!a) continue;
        a.direct += 1;
        if (isPseudo) a.pseudo = true;
      }
    }

    // Members from inter.source/target → attribute to all pathways the row touches
    const involved = new Set<string>([...queryPathwayIds, ...chainPathwayIds]);
    if (involved.size > 0) {
      for (const sym of [inter.source, inter.target]) {
        const upper = upperOrEmpty(sym);
        if (!upper) continue;
        for (const pid of involved) {
          const a = acc.get(pid);
          if (a) a.members.add(upper);
        }
      }
    }

    // Per-claim pass scores attributed to the claim's own pathway
    const claimsList = (inter.claims ?? inter.functions ?? []) as Claim[];
    for (const c of claimsList) {
      const score = claimPassScore(c);
      if (score == null) continue;
      const pwName = (() => {
        const raw = c.pathway;
        if (typeof raw === "string") return raw;
        if (raw && typeof raw === "object") {
          const obj = raw as { canonical_name?: string; name?: string };
          return obj.canonical_name ?? obj.name ?? "";
        }
        return "";
      })();
      if (!pwName) continue;
      const id = idByNormName.get(normalizePathwayName(pwName));
      if (!id) continue;
      const a = acc.get(id);
      if (!a) continue;
      a.pass.sum += score;
      a.pass.count += 1;
    }
  }

  // Drift + partial counts come from diagnostics. Build per-protein index.
  const driftByInteractor = new Map<string, { corrected: number; reportOnly: number }>();
  for (const entry of diag.pathway_drifts ?? []) {
    const partner = upperOrEmpty(entry?.interactor);
    if (!partner) continue;
    const slot = driftByInteractor.get(partner) ?? { corrected: 0, reportOnly: 0 };
    if (entry.action === "corrected") slot.corrected += 1;
    else slot.reportOnly += 1;
    driftByInteractor.set(partner, slot);
  }
  const partialByInteractor = new Set<string>();
  for (const entry of diag.chain_incomplete_hops ?? []) {
    const partner = upperOrEmpty(entry?.interactor);
    if (partner) partialByInteractor.add(partner);
  }

  // Now finalize per-pathway: drift / partial come from members ∩ diagnostic sets.
  const totalInteractors = interactions.length;

  for (const p of pathways) {
    const a = acc.get(p.id)!;
    const members = a.members;
    let driftCorrected = 0;
    let driftReportOnly = 0;
    let partial = 0;
    for (const m of members) {
      const d = driftByInteractor.get(m);
      if (d) {
        driftCorrected += d.corrected;
        driftReportOnly += d.reportOnly;
      }
      if (partialByInteractor.has(m)) partial += 1;
    }

    const passRateMean = a.pass.count > 0 ? a.pass.sum / a.pass.count : null;
    const letter = letterGradeFor(passRateMean);
    const hasIssues = partial > 0 || driftReportOnly > 0 || (passRateMean != null && passRateMean < 0.5);

    const total = a.direct + a.chain;
    const isCatchAll = depthOf(p) <= 0 && totalInteractors > 0 && total >= totalInteractors * 0.8;

    out.set(p.id, {
      id: p.id,
      name: p.name,
      description: p.description ?? null,
      depth: depthOf(p),
      parentIds: Array.isArray(p.parent_ids) ? p.parent_ids : [],
      childIds: Array.isArray(p.child_ids) ? p.child_ids : [],
      ancestorIds: Array.isArray(p.ancestor_ids) ? p.ancestor_ids : [],
      interactorIds: new Set(Array.isArray(p.interactor_ids) ? p.interactor_ids.map(upperOrEmpty).filter(Boolean) : []),
      directCount: a.direct,
      chainCount: a.chain,
      partialChainCount: partial,
      driftCorrected,
      driftReportOnly,
      pseudoTouching: a.pseudo,
      passRateMean,
      letterGrade: letter,
      hasIssues,
      memberProteins: members,
      isCatchAll,
    });
  }

  void safeNumber;
  return out;
}

export type PathwaySortMode =
  | "relevance"
  | "alphabetical"
  | "hierarchy"
  | "drift"
  | "lowestPass"
  | "mostChains";

export function sortPathwayStats(
  stats: PathwayStat[],
  mode: PathwaySortMode,
): PathwayStat[] {
  const arr = stats.slice();
  switch (mode) {
    case "alphabetical":
      return arr.sort((a, b) => a.name.localeCompare(b.name));
    case "hierarchy":
      return arr.sort((a, b) => a.depth - b.depth || a.name.localeCompare(b.name));
    case "drift":
      return arr.sort(
        (a, b) =>
          b.driftCorrected + b.driftReportOnly - (a.driftCorrected + a.driftReportOnly) ||
          a.name.localeCompare(b.name),
      );
    case "lowestPass":
      return arr.sort((a, b) => {
        const av = a.passRateMean ?? 2;
        const bv = b.passRateMean ?? 2;
        return av - bv || a.name.localeCompare(b.name);
      });
    case "mostChains":
      return arr.sort(
        (a, b) => b.chainCount - a.chainCount || a.name.localeCompare(b.name),
      );
    case "relevance":
    default:
      return arr.sort(
        (a, b) =>
          b.directCount + b.chainCount - (a.directCount + a.chainCount) ||
          a.depth - b.depth ||
          a.name.localeCompare(b.name),
      );
  }
}

