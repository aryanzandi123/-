/**
 * FunctionCard renders a single claim using the legacy modal's anatomy:
 * pathway chip + title + arrow badge in the header, then sub-cards for
 * EFFECTS SUMMARY (when arrow context disagrees), MECHANISM (always amber),
 * EFFECT (colored by arrow class), BIOLOGICAL CASCADE (vertical timeline),
 * SPECIFIC EFFECTS, and EVIDENCE.
 *
 * Data is read directly off `Claim`. Special claim kinds (synthetic / thin
 * / router) keep their honest placeholder rendering — those branches stay
 * in `ClaimRenderer` so this component focuses on the normal-claim layout.
 */

import { useEffect, useRef, useState } from "react";

import {
  ARROW_COLORS,
  classifyArrow,
  type ArrowKind,
} from "@/lib/colors";
import {
  isPlaceholderText,
  mentionedEndpoints,
  pickEvidence,
  pickStringList,
} from "@/lib/claims";
import type { ArrowClass, Claim } from "@/types/api";

import cardStyles from "./FunctionCard.module.css";
import cascadeStyles from "./cascade.module.css";

interface DCERubric {
  depth: { score: 0 | 1 | 2; count: number };
  cascade: { score: 0 | 1 | 2; count: number };
  evidence: { score: 0 | 1 | 2; count: number };
}

const RUBRIC_COLOR: Record<0 | 1 | 2, string> = {
  0: "#ef4444",
  1: "#f59e0b",
  2: "#10b981",
};

function sentenceCount(s: string): number {
  return s.split(/[.!?]+\s+/).map((x) => x.trim()).filter((x) => x.length > 0).length;
}

function deriveRubric(claim: Claim): DCERubric {
  const ed = typeof claim.effect_description === "string" && !isPlaceholderText(claim.effect_description)
    ? claim.effect_description
    : "";
  const dCount = ed ? sentenceCount(ed) : 0;
  const cascade = pickStringList(claim.biological_consequences);
  const ev = pickEvidence(claim.evidence);
  const uniqPmids = new Set<string>();
  for (const e of ev) {
    if (e.pmid) uniqPmids.add(String(e.pmid));
  }
  return {
    depth: { score: dCount >= 6 ? 2 : dCount >= 3 ? 1 : 0, count: dCount },
    cascade: { score: cascade.length >= 3 ? 2 : cascade.length >= 1 ? 1 : 0, count: cascade.length },
    evidence: { score: uniqPmids.size >= 3 ? 2 : uniqPmids.size >= 1 ? 1 : 0, count: uniqPmids.size },
  };
}

function RubricDots({ rubric, depthIssues }: { rubric: DCERubric; depthIssues?: string[] | null }): JSX.Element {
  const issuesText = Array.isArray(depthIssues) && depthIssues.length > 0
    ? ` · failing: ${depthIssues.join(", ")}`
    : "";
  return (
    <span style={{ display: "inline-flex", gap: 3, alignItems: "center" }}>
      {(
        [
          { letter: "D", v: rubric.depth, label: "Depth", unit: "sentences", thresh: "≥6", extra: rubric.depth.score < 2 ? issuesText : "" },
          { letter: "C", v: rubric.cascade, label: "Cascade", unit: "consequences", thresh: "≥3", extra: "" },
          { letter: "E", v: rubric.evidence, label: "Evidence", unit: "PMIDs", thresh: "≥3", extra: "" },
        ] as const
      ).map((r) => (
        <span
          key={r.letter}
          title={`${r.label}: ${r.v.count} ${r.unit} (PhD-depth target ${r.thresh})${r.extra}`}
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 2,
            background: "rgba(15, 23, 42, 0.6)",
            border: `1px solid ${RUBRIC_COLOR[r.v.score]}66`,
            color: RUBRIC_COLOR[r.v.score],
            fontSize: "var(--font-size-xs)",
            fontWeight: 700,
            padding: "1px 4px",
            borderRadius: "var(--radius-full)",
            fontFamily: "var(--font-mono)",
            letterSpacing: 0.3,
          }}
        >
          {r.letter}
          <span
            style={{
              width: 5,
              height: 5,
              borderRadius: "50%",
              background: RUBRIC_COLOR[r.v.score],
              display: "inline-block",
            }}
          />
        </span>
      ))}
    </span>
  );
}

const FUNCTION_CONTEXT_LABEL: Record<string, string> = {
  direct: "direct",
  indirect: "indirect",
  chain: "chain",
};

function FunctionContextBadge({ ctx }: { ctx: string | null | undefined }): JSX.Element | null {
  if (!ctx) return null;
  const label = FUNCTION_CONTEXT_LABEL[ctx] ?? ctx;
  return (
    <span
      title={`Claim function context: ${label}`}
      className={cardStyles.directBadge}
    >
      {label}
    </span>
  );
}

function PmidLink({ pmid }: { pmid: string }): JSX.Element {
  const safe = pmid.replace(/[^0-9a-zA-Z]/g, "");
  return (
    <a
      href={`https://pubmed.ncbi.nlm.nih.gov/${safe}/`}
      target="_blank"
      rel="noopener noreferrer"
      className={cardStyles.pmidLink}
    >
      PMID:{safe}
    </a>
  );
}

const ARROW_KIND_TO_ACCENT: Record<ArrowKind, string> = {
  positive: "var(--section-effect-positive)",
  negative: "var(--section-effect-negative)",
  binding: "var(--section-effect-binding)",
  regulatory: "var(--section-effect-regulatory)",
  reverse: "var(--section-effect-neutral)",
  neutral: "var(--section-effect-neutral)",
};

interface FunctionCardProps {
  claim: Claim;
  /** Pathway selected in the visualizer; used downstream by callers when needed. */
  pathwayContext?: string | null;
  /** Default arrow if the claim itself doesn't carry one. */
  defaultArrow?: ArrowClass | null;
  /** Initially expanded — the modal opens claim 1 by default. */
  initiallyExpanded?: boolean;
  /** Forces expanded state — used by keyboard nav from ModalShell. */
  forceExpanded?: boolean;
  /** Endpoint pair currently inspected; cascade items get an endpoint tag. */
  edgeEndpoints?: ReadonlyArray<string>;
}

export function FunctionCard({
  claim,
  defaultArrow = null,
  initiallyExpanded = false,
  forceExpanded,
  edgeEndpoints,
}: FunctionCardProps): JSX.Element {
  const [expanded, setExpanded] = useState(initiallyExpanded);
  const headerRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    if (forceExpanded === undefined) return;
    setExpanded(forceExpanded);
    if (forceExpanded) {
      headerRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }, [forceExpanded]);

  const arrow = (claim.arrow as ArrowClass | undefined) ?? defaultArrow ?? null;
  const arrowKind = classifyArrow(arrow);
  const arrowColor = ARROW_COLORS[arrowKind];
  const effectAccent = ARROW_KIND_TO_ACCENT[arrowKind];

  const mechanism = isPlaceholderText(claim.cellular_process) ? "" : String(claim.cellular_process ?? "");
  const effectDescription = isPlaceholderText(claim.effect_description)
    ? ""
    : String(claim.effect_description ?? "");
  const cascade = pickStringList(claim.biological_consequences);
  const specifics = pickStringList(claim.specific_effects);
  const evidence = pickEvidence(claim.evidence)
    .slice()
    .sort((a, b) => (b.year ?? 0) - (a.year ?? 0));
  const hasEmpiricalText =
    !!effectDescription || !!mechanism || cascade.length > 0 || specifics.length > 0;
  const citedPmidCount = evidence.reduce((n, e) => n + (e.pmid ? 1 : 0), 0);
  const isUncited = hasEmpiricalText && citedPmidCount === 0;

  const rubric = deriveRubric(claim);
  const pathwayLabel = (() => {
    const raw = claim.pathway;
    if (typeof raw === "string") return raw;
    if (raw && typeof raw === "object") {
      const obj = raw as { canonical_name?: string; name?: string };
      return obj.canonical_name ?? obj.name ?? "";
    }
    return "";
  })();

  const claimTitle = (typeof claim.function === "string" && claim.function) || "Function";

  return (
    <article
      data-uncited={isUncited ? "true" : undefined}
      className={`${cardStyles.card}${isUncited ? ` ${cardStyles.cardUncited}` : ""}`}
      style={{ "--effect-accent": effectAccent } as React.CSSProperties}
    >
      <button
        type="button"
        ref={headerRef}
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        data-claim-header
        className={cardStyles.header}
      >
        <span className={cardStyles.headerLeft}>
          {pathwayLabel ? <span className={cardStyles.pathwayChip}>{pathwayLabel}</span> : null}
          <span className={cardStyles.titleSerif}>{claimTitle}</span>
          {arrow ? (
            <span className={cardStyles.effectBadge} style={{ color: arrowColor }}>
              {arrow}
            </span>
          ) : null}
          <FunctionContextBadge
            ctx={typeof claim.function_context === "string" ? claim.function_context : null}
          />
          {isUncited ? (
            <span
              className={cardStyles.uncitedPill}
              title="This claim has descriptive text but zero PMIDs. Treat the body as model-synthesized, not primary literature."
            >
              No citations
            </span>
          ) : null}
        </span>
        <span className={cardStyles.headerRight}>
          <RubricDots rubric={rubric} depthIssues={Array.isArray(claim._depth_issues) ? claim._depth_issues : null} />
          <span aria-hidden style={{ color: "var(--color-text-faint)" }}>{expanded ? "▾" : "▸"}</span>
        </span>
      </button>

      {expanded ? (
        <div className={cardStyles.body}>
          {mechanism ? (
            <div
              className={cardStyles.section}
              style={{ "--section-accent": "var(--section-mechanism)" } as React.CSSProperties}
            >
              <div className={cardStyles.sectionLabel}>
                <span aria-hidden>⚙</span> Mechanism
              </div>
              <div className={cardStyles.prose}>{mechanism}</div>
            </div>
          ) : null}

          {effectDescription ? (
            <div
              className={cardStyles.section}
              style={{ "--section-accent": effectAccent } as React.CSSProperties}
            >
              <div className={cardStyles.sectionLabel}>
                <span aria-hidden>⌁</span> Effect
              </div>
              <div className={`${cardStyles.prose} ${cardStyles.proseEffect}`}>{effectDescription}</div>
            </div>
          ) : null}

          {cascade.length > 0 ? (
            <div
              className={cardStyles.section}
              style={{ "--section-accent": "var(--section-mechanism)" } as React.CSSProperties}
            >
              <div className={cardStyles.sectionLabel}>Biological cascade</div>
              <div className={cascadeStyles.scenarioWrap}>
                <div className={cascadeStyles.scenarioLabel}>Scenario 1</div>
                <ol className={cascadeStyles.timeline}>
                  {cascade.map((c, i) => {
                    const labels = edgeEndpoints && edgeEndpoints.length === 2
                      ? mentionedEndpoints(c, edgeEndpoints)
                      : null;
                    const offEdge = labels && labels.size === 0;
                    return (
                      <li
                        key={i}
                        className={`${cascadeStyles.item}${offEdge ? ` ${cascadeStyles.offEdge}` : ""}`}
                      >
                        {c}
                        {labels ? (
                          <span
                            className={`${cascadeStyles.endpointTag}${offEdge ? ` ${cascadeStyles.endpointTagOff}` : ""}`}
                            title={
                              offEdge
                                ? `Does not mention ${edgeEndpoints!.join(" or ")} — likely describes a neighbouring leg of the chain`
                                : `Mentions: ${Array.from(labels).join(", ")}`
                            }
                          >
                            {labels.size === 0 ? "off-edge" : Array.from(labels).join("·")}
                          </span>
                        ) : null}
                      </li>
                    );
                  })}
                </ol>
              </div>
            </div>
          ) : null}

          {specifics.length > 0 ? (
            <div
              className={cardStyles.section}
              style={{ "--section-accent": "var(--section-specifics)" } as React.CSSProperties}
            >
              <div className={cardStyles.sectionLabel}>Specific effects</div>
              <ul className={cardStyles.bullets}>
                {specifics.map((s, i) => (
                  <li key={i}>{s}</li>
                ))}
              </ul>
            </div>
          ) : null}

          {evidence.length > 0 ? (
            <div
              className={cardStyles.section}
              style={{ "--section-accent": "var(--color-border-strong)" } as React.CSSProperties}
            >
              <div className={cardStyles.sectionLabel}>Evidence ({evidence.length})</div>
              <div className={cardStyles.evidenceList}>
                {evidence.map((e, i) => (
                  <div key={i} className={cardStyles.evidenceCard}>
                    {e.pmid ? <PmidLink pmid={String(e.pmid)} /> : null}
                    {e.year ? <span style={{ color: "var(--color-text-faint)", marginLeft: 6 }}>({e.year})</span> : null}
                    {e.quote ? <div className={cardStyles.evidenceQuote}>“{e.quote}”</div> : null}
                  </div>
                ))}
              </div>
            </div>
          ) : null}

          {isUncited ? (
            <div
              className={cardStyles.section}
              style={{ "--section-accent": "var(--color-warn)" } as React.CSSProperties}
            >
              <div className={cardStyles.sectionLabel}>Evidence</div>
              <div className={cardStyles.uncitedNotice}>
                No PMIDs attached to this claim. Quantitative or empirical statements above are
                model-synthesized; verify against primary literature before citing.
              </div>
            </div>
          ) : null}
        </div>
      ) : null}
    </article>
  );
}
