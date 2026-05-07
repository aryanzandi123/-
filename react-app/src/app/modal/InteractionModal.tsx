/**
 * InteractionModal — rendered when the user clicks an edge on the canvas.
 *
 * Layout follows the legacy single-edge anatomy:
 *   1. Header strip: source [arrow chip] target + close.
 *   2. MetadataGrid (TYPE / DIRECTION / FUNCTIONS / EVIDENCE / PATHWAYS / CONTEXT).
 *   3. Optional lead summary block (italic protein-pair sentence + bold lead
 *      paragraph) when the underlying claims carry first-sentence prose.
 *   4. ChainContextBanner stack for any chain context this edge participates in.
 *   5. "Functions (N)" header with the pathway-filter "Show all" toggle.
 *   6. List of FunctionCards (one per claim).
 */

import { useMemo, useState } from "react";

import { useSnapStore, selectActiveSnap } from "@/store/useSnapStore";
import { useViewStore } from "@/store/useViewStore";
import { useModalStore, type ModalArgs } from "@/store/useModalStore";
import { ARROW_COLORS, classifyArrow } from "@/lib/colors";
import { isPathwayInContext, isPlaceholderText } from "@/lib/claims";
import { claimsForInteraction, selectInteractionForEdge } from "@/lib/interactionSurface";
import type { ArrowClass, Claim } from "@/types/api";

import { ClaimRenderer } from "./ClaimRenderer";
import { ChainContextBanner } from "./ChainContextBanner";
import { MetadataGrid } from "./MetadataGrid";

import styles from "./InteractionModal.module.css";

interface InteractionModalProps {
  args: ModalArgs;
}

interface EdgePayload {
  sourceProtein?: string;
  targetProtein?: string;
  arrow?: ArrowClass | null;
  isChainEdge?: boolean;
  isReverse?: boolean;
  chainId?: number | null;
  chainPosition?: number | null;
  hopIndex?: number | null;
}

function distinctPmidCount(claims: Claim[]): number {
  const set = new Set<string>();
  for (const c of claims) {
    const ev = Array.isArray(c.evidence) ? c.evidence : [];
    for (const e of ev) {
      if (e?.pmid) set.add(String(e.pmid));
    }
  }
  return set.size;
}

function firstSentence(s: string): string {
  const m = s.match(/^[^.!?]+[.!?]/);
  return m ? m[0].trim() : s.trim();
}

export function InteractionModal({ args }: InteractionModalProps): JSX.Element {
  const close = useModalStore((s) => s.close);
  const snap = useSnapStore(selectActiveSnap);
  const view = useViewStore((s) => s.byProtein.get(args.protein.toUpperCase()));
  const payload = args.payload as unknown as EdgePayload;

  const [showAll, setShowAll] = useState(false);

  const inter = useMemo(() => {
    if (!snap || !payload.sourceProtein || !payload.targetProtein) return null;
    return selectInteractionForEdge(
      snap,
      payload.sourceProtein,
      payload.targetProtein,
      payload.chainId ?? null,
      payload.hopIndex ?? null,
    );
  }, [snap, payload.sourceProtein, payload.targetProtein, payload.chainId, payload.hopIndex]);

  const allChains = inter?.all_chains ?? (inter?._chain_entity ? [inter._chain_entity] : []);
  const focusedHop = payload.chainPosition ?? null;
  const arrow = (payload.arrow ?? inter?.arrow ?? null) as ArrowClass | null;
  const arrowColor = ARROW_COLORS[classifyArrow(arrow)];
  const sourceLabel = (inter?.source ?? payload.sourceProtein ?? "?").toUpperCase();
  const targetLabel = (inter?.target ?? payload.targetProtein ?? "?").toUpperCase();

  const pathwayContext = useMemo(() => {
    if (!view || view.selectedPathways.size === 0) return null;
    return Array.from(view.selectedPathways)[0] ?? null;
  }, [view]);

  const claims = inter ? claimsForInteraction(inter) : [];
  const filteredClaims = useMemo(() => {
    if (showAll || !pathwayContext) return claims;
    return claims.filter((c) => isPathwayInContext(c, pathwayContext));
  }, [claims, showAll, pathwayContext]);

  // Lead block: pull from the first non-placeholder claim's prose. The
  // italic sentence is a one-line summary (cellular_process first sentence);
  // the bold lead is the first sentence of effect_description. Both are
  // skipped when the data is empty so we never render placeholder text.
  const lead = useMemo(() => {
    const c = filteredClaims[0];
    if (!c) return null;
    const cell = !isPlaceholderText(c.cellular_process) ? String(c.cellular_process ?? "") : "";
    const eff = !isPlaceholderText(c.effect_description) ? String(c.effect_description ?? "") : "";
    if (!cell && !eff) return null;
    return {
      italic: cell ? firstSentence(cell) : "",
      bold: eff ? firstSentence(eff) : "",
    };
  }, [filteredClaims]);

  return (
    <>
      <header className={styles.header}>
        <div className={styles.titleRow}>
          <span className={styles.protein}>{sourceLabel}</span>
          {arrow ? (
            <span className={styles.arrowChip} style={{ color: arrowColor }}>
              <span className={styles.arrowGlyph}>↔</span>
              {arrow}
            </span>
          ) : (
            <span className={styles.arrowChip} style={{ color: "var(--color-text-faint)" }}>↔</span>
          )}
          <span className={styles.protein}>{targetLabel}</span>
          {payload.isChainEdge ? <span className={styles.kindChip}>chain hop</span> : null}
          {typeof inter?.depth === "number" && inter.depth > 1 ? (
            <span className={styles.depthChip} title={`Cascade depth: ${inter.depth} hops`}>
              depth {inter.depth}
            </span>
          ) : null}
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

      {inter ? (
        <MetadataGrid
          interaction={inter}
          visibleClaimCount={filteredClaims.length}
          totalClaimCount={claims.length}
          functionsLabel="Functions"
          evidenceCount={distinctPmidCount(filteredClaims)}
          claims={filteredClaims}
        />
      ) : null}

      <div className={styles.body}>
        {!inter ? (
          <div className={styles.empty}>
            No interaction row found for {sourceLabel} ↔ {targetLabel}
            {payload.chainId != null ? ` (chain ${payload.chainId})` : ""}.
          </div>
        ) : (
          <>
            {lead ? (
              <div className={styles.lead}>
                {lead.italic ? <div className={styles.leadItalic}>{lead.italic}</div> : null}
                {lead.bold ? <div>{lead.bold}</div> : null}
              </div>
            ) : null}

            {allChains.length > 0 ? (
              <div style={{ marginTop: "var(--space-3)" }}>
                {allChains.map((chain, i) => (
                  <ChainContextBanner
                    key={chain.chain_id ?? i}
                    chain={chain}
                    chainIndex={i}
                    totalChains={allChains.length}
                    focusedHop={chain.chain_id === payload.chainId ? focusedHop : null}
                  />
                ))}
              </div>
            ) : null}

            <div className={styles.functionsHeader}>
              <span className={styles.functionsHeaderLeft}>
                Functions <span className={styles.muted}>({filteredClaims.length})</span>
                {pathwayContext && !showAll ? (
                  <span className={styles.muted}>
                    {" · in "}<em>{pathwayContext}</em>
                  </span>
                ) : null}
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

            {filteredClaims.length === 0 ? (
              <div className={styles.empty}>No claims to render.</div>
            ) : (
              filteredClaims.map((c, i) => (
                <ClaimRenderer
                  key={i}
                  claim={c}
                  pathwayContext={pathwayContext}
                  defaultArrow={arrow}
                  initiallyExpanded={i === 0}
                  edgeEndpoints={[sourceLabel, targetLabel]}
                />
              ))
            )}
          </>
        )}
      </div>
    </>
  );
}
