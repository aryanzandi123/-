/**
 * ChainContextBanner: single-chain banner with clickable protein chips +
 * prev/next nav. Multi-chain support stacks N banners for an interaction
 * that participates in N chains via `all_chains[]`.
 *
 * Each banner shows the pathway pill, "chain N of M" tag, and the
 * cause→effect protein sequence with verb edges between chips.
 */

import { ARROW_COLORS, classifyArrow, isReverseVerb } from "@/lib/colors";
import { useModalStore } from "@/store/useModalStore";
import { useSnapStore } from "@/store/useSnapStore";
import type { ChainSummary } from "@/types/api";

import styles from "./ChainContextBanner.module.css";

interface ChainContextBannerProps {
  chain: ChainSummary;
  chainIndex: number;
  totalChains: number;
  /** 0-based position of the hop currently in focus (the one whose claims are being shown). */
  focusedHop?: number | null;
  onSelectHop?: (chainId: number, hop: number) => void;
}

export function ChainContextBanner({
  chain,
  chainIndex,
  totalChains,
  focusedHop = null,
  onSelectHop,
}: ChainContextBannerProps): JSX.Element {
  const proteins = Array.isArray(chain.chain_proteins) ? chain.chain_proteins : [];
  const arrows = Array.isArray(chain.chain_with_arrows) ? chain.chain_with_arrows : [];
  const pushModal = useModalStore((s) => s.push);
  const activeProtein = useSnapStore((s) => s.activeProtein);

  const onChipClick = (chipProtein: string, chipIndex: number) => {
    if (chipProtein === activeProtein) {
      onSelectHop?.(chain.chain_id, chipIndex);
      return;
    }
    pushModal({
      kind: "aggregated",
      protein: activeProtein ?? chipProtein,
      payload: {
        baseProtein: chipProtein,
        variant: "chain",
        chainId: chain.chain_id,
        chainPosition: chipIndex,
        chainLength: proteins.length,
      },
    });
  };

  return (
    <div className={styles.banner} data-chain-id={chain.chain_id}>
      <div className={styles.metaRow}>
        <span className={styles.pathwayChip}>{chain.pathway_name ?? "Unassigned"}</span>
        <span className={styles.faint}>
          chain {chainIndex + 1} of {totalChains}
        </span>
        <span className={styles.faint}>· id {chain.chain_id}</span>
        {chain.discovered_in_query ? (
          <span
            className={`${styles.faint} ${styles.mono}`}
            title={`Chain first discovered when querying ${chain.discovered_in_query}`}
          >
            · found via {chain.discovered_in_query}
          </span>
        ) : null}
        <span className={styles.spacer} />
        {onSelectHop && proteins.length > 1 ? (
          <>
            <button
              type="button"
              disabled={focusedHop == null || focusedHop <= 1}
              onClick={() => {
                if (focusedHop == null || focusedHop <= 1) return;
                onSelectHop(chain.chain_id, focusedHop - 1);
              }}
              className={styles.navBtn}
              aria-label="Previous hop"
            >
              ◀
            </button>
            <button
              type="button"
              disabled={focusedHop == null || focusedHop >= proteins.length - 1}
              onClick={() => {
                if (focusedHop == null || focusedHop >= proteins.length - 1) return;
                onSelectHop(chain.chain_id, focusedHop + 1);
              }}
              className={styles.navBtn}
              aria-label="Next hop"
            >
              ▶
            </button>
          </>
        ) : null}
      </div>

      <div className={styles.chipRow}>
        {proteins.map((p, i) => {
          const arrow = arrows[i]?.arrow;
          const arrowKind = classifyArrow(arrow);
          const arrowColor = ARROW_COLORS[arrowKind];
          const reverse = isReverseVerb(arrow);
          const isFocused = focusedHop === i;
          return (
            <span key={`${p}-${i}`} className={styles.chipWrap}>
              <button
                type="button"
                onClick={() => onChipClick(p, i)}
                title={p === activeProtein ? `Hop ${i + 1}` : `Drill into ${p}`}
                className={`${styles.chip}${isFocused ? ` ${styles.chipFocused}` : ""}`}
                aria-pressed={isFocused}
              >
                {p}
              </button>
              {i < proteins.length - 1 && arrow ? (
                <span
                  className={`${styles.verb}${reverse ? ` ${styles.verbReverse}` : ""}`}
                  style={{
                    color: arrowColor,
                    fontWeight: arrowKind === "negative" ? 700 : 600,
                  }}
                >
                  {arrow} →
                </span>
              ) : null}
            </span>
          );
        })}
      </div>
    </div>
  );
}
