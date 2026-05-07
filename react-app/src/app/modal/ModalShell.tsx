/**
 * ModalShell: backdrop + focus trap + escape close.
 *
 * Listens to `useModalStore` and dispatches the right child based on
 * `args.kind`. Focus management ports from `static/modal.js:_focusModal` —
 * remember the previously-focused element on open, restore it on close.
 *
 * Phase 3 polish (Phase 6) may swap this for a full <dialog> element + the
 * native browser focus trap; for now a pure React implementation keeps
 * portability and ARIA control explicit.
 */

import { useEffect, useMemo, useRef } from "react";

import { useModalStore } from "@/store/useModalStore";
import { useSnapStore, selectActiveSnap } from "@/store/useSnapStore";
import { ARROW_COLORS, classifyArrow } from "@/lib/colors";
import type { ArrowClass, Interaction } from "@/types/api";

import { InteractionModal } from "./InteractionModal";
import { AggregatedModal } from "./AggregatedModal";

import styles from "./ModalShell.module.css";

const FOCUSABLE_SELECTOR =
  "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled])," +
  " textarea:not([disabled]), [tabindex]:not([tabindex='-1'])";

export function ModalShell(): JSX.Element | null {
  const open = useModalStore((s) => s.open);
  const args = useModalStore((s) => s.args);
  const close = useModalStore((s) => s.close);
  const snap = useSnapStore(selectActiveSnap);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const lastFocusedRef = useRef<HTMLElement | null>(null);

  // Pick the accent color from the modal context. For an interaction edge we
  // read the click payload's arrow; for an aggregated node modal we look up
  // the focused chain (if any) so the chip on the canvas and the modal's
  // left rule visually agree. Fallback is the indigo accent.
  const accentColor = useMemo(() => {
    if (!args) return undefined;
    const payload = (args.payload ?? {}) as { arrow?: ArrowClass | null; chainId?: number | null };
    if (payload.arrow) return ARROW_COLORS[classifyArrow(payload.arrow)];
    if (args.kind === "aggregated" && payload.chainId != null && snap?.interactions) {
      for (const inter of snap.interactions as Interaction[]) {
        const all = inter.all_chains ?? (inter._chain_entity ? [inter._chain_entity] : []);
        if (all.some((c) => c.chain_id === payload.chainId)) {
          return ARROW_COLORS[classifyArrow(inter.arrow ?? null)];
        }
      }
    }
    return undefined;
  }, [args, snap]);

  useEffect(() => {
    if (!open) return;
    lastFocusedRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;

    const stepClaim = (direction: 1 | -1) => {
      const root = containerRef.current;
      if (!root) return;
      const buttons = Array.from(
        root.querySelectorAll<HTMLButtonElement>("button[data-claim-header]"),
      );
      if (buttons.length === 0) return;
      const expandedIdx = buttons.findIndex((b) => b.getAttribute("aria-expanded") === "true");
      const fromIdx = expandedIdx === -1 ? 0 : expandedIdx;
      const nextIdx = ((fromIdx + direction) % buttons.length + buttons.length) % buttons.length;
      if (expandedIdx !== -1 && expandedIdx !== nextIdx) buttons[expandedIdx]!.click();
      buttons[nextIdx]!.click();
      requestAnimationFrame(() => {
        buttons[nextIdx]!.scrollIntoView({ behavior: "smooth", block: "nearest" });
      });
    };

    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        close();
        return;
      }
      if (
        (e.key === "ArrowRight" || e.key === "ArrowDown" || e.key.toLowerCase() === "j") &&
        !e.metaKey &&
        !e.ctrlKey &&
        !e.altKey
      ) {
        // Don't hijack typing in inputs/textareas.
        const t = e.target as HTMLElement | null;
        const tag = t?.tagName;
        if (tag === "INPUT" || tag === "TEXTAREA" || (t && t.isContentEditable)) return;
        e.preventDefault();
        stepClaim(1);
        return;
      }
      if (
        (e.key === "ArrowLeft" || e.key === "ArrowUp" || e.key.toLowerCase() === "k") &&
        !e.metaKey &&
        !e.ctrlKey &&
        !e.altKey
      ) {
        const t = e.target as HTMLElement | null;
        const tag = t?.tagName;
        if (tag === "INPUT" || tag === "TEXTAREA" || (t && t.isContentEditable)) return;
        e.preventDefault();
        stepClaim(-1);
        return;
      }
      if (e.key !== "Tab") return;
      const root = containerRef.current;
      if (!root) return;
      const focusable = Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
        (el) => el.offsetParent !== null,
      );
      if (focusable.length === 0) {
        e.preventDefault();
        return;
      }
      const first = focusable[0]!;
      const last = focusable[focusable.length - 1]!;
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);

    requestAnimationFrame(() => {
      const root = containerRef.current;
      if (!root) return;
      const closeBtn = root.querySelector<HTMLElement>(".propaths-modal-close");
      const target =
        closeBtn ??
        root.querySelector<HTMLElement>(FOCUSABLE_SELECTOR) ??
        root;
      target.focus({ preventScroll: true });
    });

    return () => {
      document.removeEventListener("keydown", onKey);
      const last = lastFocusedRef.current;
      lastFocusedRef.current = null;
      if (last && document.contains(last)) {
        requestAnimationFrame(() => last.focus({ preventScroll: true }));
      }
    };
  }, [open, close]);

  if (!open || !args) return null;

  return (
    <div
      role="presentation"
      className={styles.backdrop}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) close();
      }}
    >
      <div
        ref={containerRef}
        role="dialog"
        aria-modal="true"
        aria-label={args.kind === "interaction" ? "Interaction details" : "Aggregated interactions"}
        tabIndex={-1}
        className={styles.dialog}
        style={accentColor ? ({ "--modal-accent": accentColor } as React.CSSProperties) : undefined}
        onMouseDown={(e) => e.stopPropagation()}
      >
        {args.kind === "interaction" ? <InteractionModal args={args} /> : <AggregatedModal args={args} />}
      </div>
    </div>
  );
}
