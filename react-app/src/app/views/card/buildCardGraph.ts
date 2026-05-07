/**
 * Convert a Snapshot into ReactFlow nodes + edges for the CardView.
 *
 * Implements Layer 3 of `11_CHAIN_TOPOLOGY.md`:
 *   1. Direct interactors → real nodes connected to the query node
 *   2. Chain pre-pass: every chain renders as a complete sequence with
 *      `_uid = ${protein}::chain::${chainId}::${position}`. Chain instances
 *      coexist with direct nodes — same protein can appear in BOTH places.
 *   3. Cross-link post-pass: pair every same-protein instance with a faint
 *      dashed DuplicateCrossLink edge.
 *
 * Pathway filtering: when `selectedPathways.size > 0`, only chains/interactions
 * touching one of those pathways are emitted. When 0, all are emitted.
 *
 * Filter modes:
 *   - "all"      → direct + indirect + chain
 *   - "direct"   → only direct (depth==1, no chain link)
 *   - "indirect" → only indirect (depth>=2 or chain)
 *   - "chain"    → only chain rows
 */

import type { Node, Edge } from "@xyflow/react";

import type { Snapshot, Interaction, ChainSummary, ArrowClass } from "@/types/api";
import type { FilterMode, ViewFilters } from "@/store/useViewStore";
import { isPseudoProtein } from "@/lib/pseudo";
import { normalizePathwayName } from "@/lib/normalize";
import { classifyArrow } from "@/lib/colors";
import { deriveBadges } from "@/lib/diagnostics";

import type { ProteinCardData, ProteinBadge } from "./ProteinCard";
import type { ChainEdgeData } from "./ChainEdge";
import type { DuplicateCrossLinkData } from "./DuplicateCrossLink";

const MAX_CROSSLINKS_PER_PROTEIN = 5;

interface BuildArgs {
  snap: Snapshot;
  selectedPathways: ReadonlySet<string>;
  filters: ViewFilters;
}

function passesFilterMode(inter: Interaction, mode: FilterMode): boolean {
  switch (mode) {
    case "all":
      return true;
    case "direct":
      return inter.type === "direct" && !inter._is_chain_link;
    case "indirect":
      return inter.type === "indirect" || Boolean(inter._is_chain_link);
    case "chain":
      return Boolean(inter._is_chain_link) || (Array.isArray(inter.all_chains) && inter.all_chains.length > 0);
  }
}

function passesPseudoFilter(inter: Interaction, allowPseudo: boolean): boolean {
  if (allowPseudo) return true;
  if (inter._source_is_pseudo || inter._target_is_pseudo || inter._partner_is_pseudo) return false;
  if (isPseudoProtein(inter.source) || isPseudoProtein(inter.target)) return false;
  return true;
}

function pathwayTouches(inter: Interaction, selected: ReadonlySet<string>): boolean {
  // Empty selection = empty canvas. The PathwayExplorer auto-selects the
  // top-relevance pathway on first hydration so users never face this on
  // entry; clearing the selection later surfaces EmptyState with a suggestion.
  if (selected.size === 0) return false;
  const checks = new Set<string>();
  if (Array.isArray(inter.pathways)) inter.pathways.forEach((p) => checks.add(normalizePathwayName(p)));
  if (Array.isArray(inter.chain_pathways)) inter.chain_pathways.forEach((p) => checks.add(normalizePathwayName(p)));
  if (Array.isArray(inter.all_chains)) {
    inter.all_chains.forEach((c) => {
      if (c.pathway_name) checks.add(normalizePathwayName(c.pathway_name));
      if (Array.isArray(c.chain_pathways)) c.chain_pathways.forEach((p) => checks.add(normalizePathwayName(p)));
    });
  }
  for (const want of selected) {
    if (checks.has(normalizePathwayName(want))) return true;
  }
  return false;
}

interface ChainKey {
  chainId: number;
  proteins: string[];
  arrows: { from: string; to: string; arrow: ArrowClass }[];
  pathwayName: string | null;
  discoveredIn: string;
}

function gatherChains(
  interactions: Interaction[],
  filterMode: FilterMode,
  selected: ReadonlySet<string>,
  allowPseudo: boolean,
): Map<number, ChainKey> {
  const out = new Map<number, ChainKey>();
  const includeChains = filterMode === "all" || filterMode === "indirect" || filterMode === "chain";
  if (!includeChains) return out;
  if (selected.size === 0) return out;

  for (const inter of interactions) {
    if (!passesPseudoFilter(inter, allowPseudo)) continue;
    if (!pathwayTouches(inter, selected)) continue;

    const sources: ChainSummary[] = [];
    if (Array.isArray(inter.all_chains) && inter.all_chains.length > 0) sources.push(...inter.all_chains);
    else if (inter._chain_entity && (inter._chain_entity as ChainSummary).chain_id != null) sources.push(inter._chain_entity);

    for (const ch of sources) {
      if (!ch || typeof ch.chain_id !== "number") continue;
      if (out.has(ch.chain_id)) continue;
      out.set(ch.chain_id, {
        chainId: ch.chain_id,
        proteins: Array.isArray(ch.chain_proteins) ? ch.chain_proteins.slice() : [],
        arrows: Array.isArray(ch.chain_with_arrows) ? ch.chain_with_arrows.slice() : [],
        pathwayName: ch.pathway_name ?? null,
        discoveredIn: ch.discovered_in_query ?? "",
      });
    }
  }
  return out;
}

function predominantArrow(inter: Interaction): ArrowClass | null {
  if (inter.arrow) return inter.arrow;
  const a = inter.arrows?.a_to_b?.[0];
  if (a) return a;
  return null;
}

export interface BuildResult {
  nodes: Node[];
  edges: Edge[];
}

export function buildCardGraph({ snap, selectedPathways, filters }: BuildArgs): BuildResult {
  const nodes: Node[] = [];
  const edges: Edge[] = [];
  const queryName = snap.main;
  if (!queryName) return { nodes, edges };
  if (selectedPathways.size === 0) return { nodes, edges };

  const badges = deriveBadges(snap);

  const queryId = `protein::${queryName}`;
  const directNodeIds = new Map<string, string>();
  directNodeIds.set(queryName, queryId);
  const queryBadges = badges.byProtein.get(queryName.toUpperCase()) ?? [];
  nodes.push({
    id: queryId,
    type: "protein",
    position: { x: 0, y: 0 },
    data: {
      label: queryName,
      variant: "query",
      isPseudo: isPseudoProtein(queryName),
      baseProtein: queryName,
      badges: queryBadges.length > 0 ? queryBadges : undefined,
    } satisfies ProteinCardData as unknown as Record<string, unknown>,
  });

  const interactions = Array.isArray(snap.interactions) ? snap.interactions : [];

  // Direct interactor pass
  if (filters.mode === "all" || filters.mode === "direct") {
    for (const inter of interactions) {
      if (inter._is_chain_link) continue;
      if (inter.type !== "direct") continue;
      if (!passesFilterMode(inter, filters.mode)) continue;
      if (!passesPseudoFilter(inter, filters.pseudo)) continue;
      if (!pathwayTouches(inter, selectedPathways)) continue;

      const partner = inter.source === queryName ? inter.target : inter.source;
      if (!partner || partner === queryName) continue;

      const partnerId = `protein::${partner}`;
      if (!directNodeIds.has(partner)) {
        directNodeIds.set(partner, partnerId);
        const arrowClass = predominantArrow(inter);
        const partnerBadges: ProteinBadge[] = [];
        if (typeof inter.depth === "number" && inter.depth > 1) {
          partnerBadges.push({ kind: "depth", label: `d${inter.depth}`, tooltip: `Depth ${inter.depth}` });
        }
        if (isPseudoProtein(partner)) {
          partnerBadges.push({ kind: "pseudo", label: "pseudo", tooltip: "Pseudo-protein (filterable)" });
        }
        const diagBadges = badges.byProtein.get(partner.toUpperCase()) ?? [];
        for (const db of diagBadges) {
          if (!partnerBadges.some((b) => b.kind === db.kind && b.label === db.label)) {
            partnerBadges.push(db);
          }
        }
        const partnerPathways = Array.isArray(inter.pathways) ? inter.pathways : [];
        nodes.push({
          id: partnerId,
          type: "protein",
          position: { x: 0, y: 0 },
          data: {
            label: partner,
            variant: "direct",
            arrowClass,
            isPseudo: isPseudoProtein(partner),
            baseProtein: partner,
            alsoIn: partnerPathways.slice(0, 6),
            pathways: partnerPathways,
            badges: partnerBadges,
          } satisfies ProteinCardData as unknown as Record<string, unknown>,
        });
      }

      const direction = inter.direction;
      const sourceId = direction === "primary_to_main" ? partnerId : queryId;
      const targetId = direction === "primary_to_main" ? queryId : partnerId;
      const arrow = predominantArrow(inter);
      const arrowsForward = Array.isArray(inter.arrows?.a_to_b) ? inter.arrows!.a_to_b! : [];
      edges.push({
        id: `direct::${sourceId}::${targetId}::${edges.length}`,
        source: sourceId,
        target: targetId,
        type: "chain",
        data: {
          arrow,
          isChainEdge: false,
          multipleArrows: arrowsForward.length > 1 ? arrowsForward : undefined,
        } satisfies ChainEdgeData as unknown as Record<string, unknown>,
      });
    }
  }

  // Chain pre-pass — every chain as a complete sequence
  const chains = gatherChains(interactions, filters.mode, selectedPathways, filters.pseudo);
  for (const chain of chains.values()) {
    const proteins = chain.proteins;
    if (proteins.length === 0) continue;

    let prevId: string | null = null;
    let prevProtein: string | null = null;
    for (let k = 0; k < proteins.length; k++) {
      const p = proteins[k];
      if (!p) continue;
      const uid = `chain::${chain.chainId}::${k}::${p}`;
      const arrowAt = chain.arrows[k - 1];
      const arrow = arrowAt?.arrow ?? null;

      const chainBadges: ProteinBadge[] = [];
      if (isPseudoProtein(p)) chainBadges.push({ kind: "pseudo", label: "pseudo" });
      if (k > 0 && prevProtein) {
        const hopKey = `${prevProtein.toUpperCase()}->${p.toUpperCase()}`;
        const hopBadge = badges.byHop.get(hopKey);
        if (hopBadge) chainBadges.push(hopBadge);
      }
      const diagBadges = badges.byProtein.get(p.toUpperCase()) ?? [];
      for (const db of diagBadges) {
        if (!chainBadges.some((b) => b.kind === db.kind && b.label === db.label)) {
          chainBadges.push(db);
        }
      }

      const chainPathwaysList: string[] = [];
      if (chain.pathwayName) chainPathwaysList.push(chain.pathwayName);
      nodes.push({
        id: uid,
        type: "protein",
        position: { x: 0, y: 0 },
        data: {
          label: p,
          variant: p === queryName ? "query" : "chain",
          arrowClass: arrow,
          baseProtein: p,
          chainId: chain.chainId,
          chainPosition: k,
          chainLength: proteins.length,
          isPseudo: isPseudoProtein(p),
          contextText:
            k === 0 ? "chain start" : k === proteins.length - 1 ? "chain end" : `hop ${k + 1}/${proteins.length}`,
          pathways: chainPathwaysList,
          badges: chainBadges,
        } satisfies ProteinCardData as unknown as Record<string, unknown>,
      });

      if (prevId) {
        const arrowKind = classifyArrow(arrow);
        edges.push({
          id: `chain::${chain.chainId}::edge::${k}`,
          source: prevId,
          target: uid,
          type: "chain",
          data: {
            arrow,
            isChainEdge: true,
            isReverse: arrowKind === "reverse",
            chainId: chain.chainId,
            chainPosition: k,
            hopIndex: k - 1,
          } satisfies ChainEdgeData as unknown as Record<string, unknown>,
        });
      }
      prevId = uid;
      prevProtein = p;
    }
  }

  // Cross-link post-pass: group every node by `baseProtein`, emit dashed
  // DuplicateCrossLink between any pair (capped at MAX_CROSSLINKS_PER_PROTEIN).
  const byBase = new Map<string, string[]>();
  for (const n of nodes) {
    const base = (n.data as ProteinCardData | undefined)?.baseProtein;
    if (!base) continue;
    const list = byBase.get(base) ?? [];
    list.push(n.id);
    byBase.set(base, list);
  }
  for (const [base, ids] of byBase) {
    if (ids.length < 2) continue;
    // Stamp `hasDuplicates: true` on every node sharing this base so the
    // ProteinCard can pulse all instances + their cross-links on hover.
    for (const id of ids) {
      const node = nodes.find((n) => n.id === id);
      if (!node) continue;
      const data = node.data as ProteinCardData;
      (node.data as ProteinCardData) = { ...data, hasDuplicates: true };
    }
    let pairCount = 0;
    for (let i = 0; i < ids.length - 1 && pairCount < MAX_CROSSLINKS_PER_PROTEIN; i++) {
      for (let j = i + 1; j < ids.length && pairCount < MAX_CROSSLINKS_PER_PROTEIN; j++) {
        edges.push({
          id: `duplink::${base}::${ids[i]}::${ids[j]}`,
          source: ids[i]!,
          target: ids[j]!,
          type: "duplicate-crosslink",
          data: { baseProtein: base } satisfies DuplicateCrossLinkData as unknown as Record<string, unknown>,
        });
        pairCount++;
      }
    }
  }

  return { nodes, edges };
}
