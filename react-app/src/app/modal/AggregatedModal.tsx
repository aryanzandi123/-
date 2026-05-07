/**
 * AggregatedModal — opened when the user clicks a ProteinCard on the canvas.
 *
 * Body is a list of compact expandable interaction rows. Each row renders
 * the biological direction (`source — verb → target`) with the clicked
 * protein bolded; expand to see the FunctionCard set for that interaction's
 * claims. Pathway filter, chain-leg badge, and "Show all" toggle behave the
 * same as InteractionModal.
 */

import { useMemo, useState } from "react";

import { useSnapStore, selectActiveSnap, selectActivePathways } from "@/store/useSnapStore";
import { useViewStore } from "@/store/useViewStore";
import { useModalStore, type ModalArgs } from "@/store/useModalStore";
import { ARROW_COLORS, classifyArrow } from "@/lib/colors";
import { isPathwayInContext } from "@/lib/claims";
import { claimsForInteraction } from "@/lib/interactionSurface";
import type { Claim, Interaction, Snapshot } from "@/types/api";

import { ClaimRenderer } from "./ClaimRenderer";
import { ChainContextBanner } from "./ChainContextBanner";
import { MetadataGrid } from "./MetadataGrid";

import styles from "./AggregatedModal.module.css";

interface AggregatedModalProps {
  args: ModalArgs;
}

interface NodePayload {
  baseProtein?: string;
  variant?: string;
  chainId?: number | null;
  chainPosition?: number | null;
  chainLength?: number | null;
}

function interactionsFor(snap: Snapshot, protein: string): Interaction[] {
  const list = Array.isArray(snap.interactions) ? snap.interactions : [];
  const TARGET = protein.toUpperCase();
  return list.filter(
    (i) =>
      (i.source ?? "").toUpperCase() === TARGET || (i.target ?? "").toUpperCase() === TARGET,
  );
}

export function AggregatedModal({ args }: AggregatedModalProps): JSX.Element {
  const close = useModalStore((s) => s.close);
  const snap = useSnapStore(selectActiveSnap);
  const allPathways = useSnapStore(selectActivePathways);
  const view = useViewStore((s) => s.byProtein.get(args.protein.toUpperCase()));
  const payload = args.payload as unknown as NodePayload;
  const baseProtein = (payload.baseProtein ?? "").toUpperCase();

  const [showAll, setShowAll] = useState(false);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const pathwayContext = useMemo(() => {
    if (!view || view.selectedPathways.size === 0) return null;
    return Array.from(view.selectedPathways)[0] ?? null;
  }, [view]);

  const interactions = useMemo(
    () => (snap ? interactionsFor(snap, baseProtein) : []),
    [snap, baseProtein],
  );

  // Aggregate metadata for the MetadataGrid header. We compute total +
  // visible (post-filter) so the header agrees with the body when a
  // pathway filter is active.
  const meta = useMemo(() => {
    let totalClaims = 0;
    let visibleClaims = 0;
    const pwSet = new Set<string>();
    const visiblePwSet = new Set<string>();
    const pmidSet = new Set<string>();
    const visiblePmidSet = new Set<string>();
    const filterActive = !showAll && !!pathwayContext;
    const allClaimsFlat: Claim[] = [];
    for (const inter of interactions) {
      const claims = claimsForInteraction(inter);
      totalClaims += claims.length;
      for (const c of claims) {
        allClaimsFlat.push(c);
        const raw = c.pathway;
        const pw =
          typeof raw === "string"
            ? raw
            : (raw as { canonical_name?: string; name?: string } | null | undefined)?.canonical_name ??
              (raw as { name?: string } | null | undefined)?.name;
        const inCtx = filterActive ? isPathwayInContext(c, pathwayContext as string) : true;
        if (typeof pw === "string" && pw) {
          pwSet.add(pw);
          if (inCtx) visiblePwSet.add(pw);
        }
        const ev = Array.isArray(c.evidence) ? c.evidence : [];
        for (const e of ev) {
          if (e?.pmid) {
            pmidSet.add(String(e.pmid));
            if (inCtx) visiblePmidSet.add(String(e.pmid));
          }
        }
        if (inCtx) visibleClaims += 1;
      }
      if (Array.isArray(inter.pathways)) {
        for (const p of inter.pathways) if (p) pwSet.add(p);
      }
      if (Array.isArray(inter.chain_pathways)) {
        for (const p of inter.chain_pathways) if (p) pwSet.add(p);
      }
    }
    return {
      totalClaims,
      visibleClaims,
      pathwayCount: pwSet.size,
      visiblePathwayCount: visiblePwSet.size,
      pmidCount: pmidSet.size,
      visiblePmidCount: visiblePmidSet.size,
      filterActive,
      allClaimsFlat,
    };
  }, [interactions, showAll, pathwayContext]);

  // Pathway descriptor for the protein (best-effort): first pathway entry
  // whose interactor_ids includes this base protein.
  const primaryPathway = useMemo(() => {
    if (!allPathways) return null;
    for (const p of allPathways) {
      if (Array.isArray(p.interactor_ids) && p.interactor_ids.some((id) => id?.toUpperCase() === baseProtein)) {
        return p;
      }
    }
    return null;
  }, [allPathways, baseProtein]);

  const focusChain = useMemo(() => {
    if (payload.chainId == null || !snap) return null;
    for (const inter of snap.interactions ?? []) {
      const all = inter.all_chains ?? (inter._chain_entity ? [inter._chain_entity] : []);
      const match = all.find((c) => c.chain_id === payload.chainId);
      if (match) return match;
    }
    return null;
  }, [payload.chainId, snap]);

  const interactionsLabelValue = meta.filterActive
    ? `${meta.visibleClaims} of ${meta.totalClaims}`
    : `${meta.totalClaims}`;

  return (
    <>
      <header className={styles.header}>
        <div className={styles.titleCol}>
          <div className={styles.titleRow}>
            <span className={styles.protein}>{baseProtein}</span>
            <span className={styles.muted}>
              {interactions.length} interaction{interactions.length === 1 ? "" : "s"}
            </span>
            {payload.variant === "chain" ? <span className={styles.kindChip}>chain participant</span> : null}
          </div>
        </div>
        <button
          type="button"
          className={`propaths-modal-close ${styles.closeBtn}`}
          onClick={close}
          aria-label="Close"
        >
          ×
        </button>
      </header>

      <MetadataGrid
        interaction={null}
        visibleClaimCount={meta.visibleClaims}
        totalClaimCount={meta.totalClaims}
        functionsLabel="Claims"
        evidenceCount={meta.filterActive ? meta.visiblePmidCount : meta.pmidCount}
        pathwayCrumbs={
          primaryPathway?.ancestor_ids && primaryPathway?.name
            ? [
                ...(primaryPathway.ancestor_ids
                  .map((aid) => allPathways.find((p) => p.id === aid)?.name)
                  .filter((s): s is string => typeof s === "string" && s.length > 0)),
                primaryPathway.name,
              ]
            : undefined
        }
        claims={meta.allClaimsFlat}
        extra={[
          {
            label: "Pathways",
            value: meta.filterActive
              ? `${meta.visiblePathwayCount} of ${meta.pathwayCount}`
              : `${meta.pathwayCount}`,
          },
          ...(primaryPathway?.ontology_id
            ? [{ label: "Ontology", value: primaryPathway.ontology_id }]
            : []),
        ]}
      />

      <div className={styles.body}>
        {focusChain ? (
          <div style={{ marginTop: "var(--space-3)" }}>
            <ChainContextBanner
              chain={focusChain}
              chainIndex={0}
              totalChains={1}
              focusedHop={payload.chainPosition ?? null}
            />
          </div>
        ) : null}

        <div className={styles.toolbar}>
          <span className={styles.toolbarLeft}>
            Interactions <span className={styles.muted}>({interactionsLabelValue})</span>
          </span>
          {pathwayContext ? (
            <button
              type="button"
              onClick={() => setShowAll((v) => !v)}
              className={styles.toggleBtn}
            >
              {showAll ? "Pathway only" : "Show all"}
            </button>
          ) : null}
        </div>

        {interactions.length === 0 ? (
          <div className={styles.empty}>No interactions for {baseProtein}.</div>
        ) : (
          interactions.map((inter, idx) => {
            const claims = claimsForInteraction(inter);
            const filtered =
              showAll || !pathwayContext
                ? claims
                : claims.filter((c) => isPathwayInContext(c, pathwayContext));
            const arrow = inter.arrow ?? null;
            const arrowColor = ARROW_COLORS[classifyArrow(arrow)];
            const interSource = (inter.source ?? "?").toUpperCase();
            const interTarget = (inter.target ?? "?").toUpperCase();
            const isBaseSource = interSource === baseProtein;
            const isOpen = expanded[String(idx)] ?? idx === 0;
            // Chain-leg label when this row is part of the focused chain.
            const chainLegLabel = (() => {
              if (!focusChain) return null;
              const chainProteins = (focusChain.chain_proteins ?? []).map((p) => (p ?? "").toUpperCase());
              for (let h = 0; h < chainProteins.length - 1; h++) {
                const a = chainProteins[h];
                const b = chainProteins[h + 1];
                if ((a === interSource && b === interTarget) || (a === interTarget && b === interSource)) {
                  return `leg ${h + 1} of ${chainProteins.length - 1}`;
                }
              }
              return null;
            })();
            return (
              <div
                key={idx}
                className={`${styles.row} ${styles.rowAccent}`}
                style={{ "--row-accent": arrowColor } as React.CSSProperties}
              >
                <button
                  type="button"
                  onClick={() => setExpanded((p) => ({ ...p, [String(idx)]: !isOpen }))}
                  aria-expanded={isOpen}
                  data-claim-header
                  className={styles.rowHeader}
                >
                  <span className={styles.rowTitle}>
                    <span className={isBaseSource ? styles.rowProteinStrong : styles.rowProtein}>
                      {interSource}
                    </span>
                    <span className={styles.rowVerb}>{arrow ? `— ${arrow} →` : "↔"}</span>
                    <span className={!isBaseSource ? styles.rowProteinStrong : styles.rowProtein}>
                      {interTarget}
                    </span>
                  </span>
                  <span className={styles.rowMeta}>
                    {chainLegLabel ? (
                      <span className={styles.legChip} title={`This pair is ${chainLegLabel}`}>
                        {chainLegLabel}
                      </span>
                    ) : null}
                    <span className={styles.countChip}>
                      {filtered.length}/{claims.length}
                    </span>
                    {inter._is_chain_link ? <span className={styles.chainChip}>chain</span> : null}
                    <span style={{ color: "var(--color-text-faint)", fontSize: 11 }}>{isOpen ? "▾" : "▸"}</span>
                  </span>
                </button>
                {isOpen ? (
                  <div className={styles.expandedBody}>
                    {filtered.length === 0 ? (
                      <div className={styles.empty}>No claims in current pathway context.</div>
                    ) : (
                      filtered.map((c, ci) => (
                        <ClaimRenderer
                          key={ci}
                          claim={c}
                          pathwayContext={pathwayContext}
                          defaultArrow={arrow}
                          initiallyExpanded={ci === 0}
                          edgeEndpoints={[interSource, interTarget]}
                        />
                      ))
                    )}
                  </div>
                ) : null}
              </div>
            );
          })
        )}
      </div>
    </>
  );
}
