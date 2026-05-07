#!/usr/bin/env python3
"""
Database-Wide Pathway Cleanup
==============================
Iteratively runs Step 6 (reorganize) and Step 7 (verify + auto-repair)
across ALL pathways in the database until convergence.

Usage:
    python3 scripts/pathway_v2/cleanup_all_pathways.py --yes
    python3 scripts/pathway_v2/cleanup_all_pathways.py --dry-run
    python3 scripts/pathway_v2/cleanup_all_pathways.py --max-passes 5 --yes
"""

import sys
import logging
import time
import argparse
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Configure logging BEFORE importing modules that call basicConfig
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ==============================================================================
# DATA STRUCTURES
# ==============================================================================

@dataclass
class PassResult:
    """Result of a single cleanup pass (step6 + step7)."""
    pass_number: int
    step6_changes: int = 0
    step6_errors: int = 0
    step6_phases_ok: int = 0
    step6_phases_total: int = 0
    step7_fixes: int = 0
    step7_checks_passed: int = 0
    step7_checks_failed: int = 0
    remaining_issues: int = 0  # MEDIUM+ only
    elapsed_seconds: float = 0.0


@dataclass
class CleanupReport:
    """Aggregated report across all cleanup passes."""
    timestamp: str
    dry_run: bool
    passes: List[PassResult] = field(default_factory=list)
    converged: bool = False
    pre_counts: Dict[str, int] = field(default_factory=dict)
    post_counts: Dict[str, int] = field(default_factory=dict)
    ontology_enriched: int = 0
    final_status: str = "UNKNOWN"  # CLEAN, ISSUES_REMAIN, ABORTED
    total_elapsed: float = 0.0

    def to_string(self) -> str:
        """Generate formatted report string."""
        lines = []
        lines.append("=" * 65)
        lines.append("          PATHWAY CLEANUP REPORT")
        lines.append(f"          Generated: {self.timestamp}")
        lines.append(f"          Mode: {'DRY RUN' if self.dry_run else 'LIVE'}")
        lines.append("=" * 65)
        lines.append("")

        # Pre-cleanup state
        lines.append("PRE-CLEANUP STATE")
        lines.append("-" * 65)
        for key, val in self.pre_counts.items():
            lines.append(f"  {key:30s} {val}")
        lines.append("")

        # Passes
        lines.append("CLEANUP PASSES")
        lines.append("-" * 65)
        for p in self.passes:
            lines.append(
                f"  Pass {p.pass_number}: "
                f"step6 ({p.step6_changes} changes, {p.step6_errors} errors), "
                f"step7 ({p.step7_fixes} fixes), "
                f"{p.remaining_issues} issues remain "
                f"[{p.elapsed_seconds:.1f}s]"
            )
        if self.converged:
            lines.append(f"  Converged after {len(self.passes)} pass(es).")
        else:
            lines.append(f"  Did NOT converge after {len(self.passes)} pass(es).")
        lines.append("")

        # Post-cleanup state
        lines.append("POST-CLEANUP STATE")
        lines.append("-" * 65)
        for key in self.pre_counts:
            pre = self.pre_counts.get(key, 0)
            post = self.post_counts.get(key, 0)
            diff = post - pre
            diff_str = f" ({diff:+d})" if diff != 0 else " (unchanged)"
            lines.append(f"  {key:30s} {post}{diff_str}")
        if self.ontology_enriched > 0:
            lines.append(f"  {'Ontology enriched':30s} {self.ontology_enriched}")
        lines.append("")

        # Final status
        lines.append(f"FINAL STATUS: {self.final_status}")
        lines.append(f"TOTAL TIME:   {self.total_elapsed:.1f}s")
        lines.append("=" * 65)
        return "\n".join(lines)

    def save_to_file(self, filepath: Path):
        """Save report to file."""
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, 'w') as f:
            f.write(self.to_string())
        logger.info(f"Report saved to: {filepath}")


# ==============================================================================
# HELPERS
# ==============================================================================

def get_db_counts() -> Dict[str, int]:
    """Snapshot current database counts for pathways, interactions, links."""
    from app import app, db
    from models import Pathway, PathwayParent, PathwayInteraction, Interaction

    with app.app_context():
        return {
            "Pathways": db.session.query(Pathway).count(),
            "Interactions": db.session.query(Interaction).count(),
            "Pathway-Interaction links": db.session.query(PathwayInteraction).count(),
            "Pathway-Parent links": db.session.query(PathwayParent).count(),
        }


def apply_ontology_enrichment() -> int:
    """Enrich Pathway records that lack ontology_id using static mappings."""
    from scripts.pathway_v2.ontology_mappings import enrich_pathway_with_ontology
    from app import app, db
    from models import Pathway

    with app.app_context():
        pathways = Pathway.query.filter(
            (Pathway.ontology_id.is_(None)) | (Pathway.ontology_id == "")
        ).all()

        enriched = 0
        for pw in pathways:
            match = enrich_pathway_with_ontology(pw.name)
            if match:
                pw.ontology_id = match["ontology_id"]
                pw.ontology_source = match["ontology_source"]
                if match.get("canonical_name"):
                    pw.canonical_term = match["canonical_name"]
                enriched += 1

        if enriched:
            db.session.commit()
            logger.info("Ontology enrichment: %d pathways updated", enriched)

    return enriched


def count_significant_issues(verification_report) -> int:
    """Count MEDIUM+ issues from a VerificationReport (the convergence metric)."""
    from scripts.pathway_v2.step7_checks import Severity

    if not verification_report or not hasattr(verification_report, 'check_results'):
        return 999  # Assume worst case

    from scripts.pathway_v2.step7_checks import get_all_issues
    all_issues = get_all_issues(verification_report.check_results)
    return sum(
        1 for i in all_issues
        if i.severity in (Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL)
    )


# ==============================================================================
# CORE LOGIC
# ==============================================================================

def run_cleanup_pass(pass_number: int, dry_run: bool) -> PassResult:
    """Run one pass of step6 reorganize + step7 verify/repair."""
    from scripts.pathway_v2.step6_reorganize_pathways import reorganize_pathways
    from scripts.pathway_v2.verify_pipeline import run_verification

    result = PassResult(pass_number=pass_number)
    t0 = time.time()

    # --- Step 6: Reorganize ---
    logger.info("")
    logger.info(f"{'='*65}")
    logger.info(f"PASS {pass_number} - STEP 6: REORGANIZE PATHWAYS")
    logger.info(f"{'='*65}")

    step6_results = reorganize_pathways(dry_run=dry_run)

    for name, phase_result in step6_results.items():
        result.step6_changes += len(phase_result.changes)
        result.step6_errors += len(phase_result.errors)
        result.step6_phases_total += 1
        if phase_result.success:
            result.step6_phases_ok += 1

    logger.info(
        f"Step 6 done: {result.step6_changes} changes, "
        f"{result.step6_errors} errors, "
        f"{result.step6_phases_ok}/{result.step6_phases_total} phases OK"
    )

    # --- Step 7: Verify + Repair ---
    logger.info("")
    logger.info(f"{'='*65}")
    logger.info(f"PASS {pass_number} - STEP 7: VERIFY + REPAIR")
    logger.info(f"{'='*65}")

    if dry_run:
        report = run_verification(auto_fix=False, report_only=True)
    else:
        report = run_verification(auto_fix=True)

    result.step7_checks_passed = report.checks_passed
    result.step7_checks_failed = report.checks_failed
    result.step7_fixes = report.auto_fixes_applied
    result.remaining_issues = count_significant_issues(report)
    result.elapsed_seconds = round(time.time() - t0, 2)

    logger.info(
        f"Step 7 done: {result.step7_checks_passed} passed, "
        f"{result.step7_checks_failed} failed, "
        f"{result.step7_fixes} auto-fixes, "
        f"{result.remaining_issues} MEDIUM+ issues remain"
    )

    return result


def run_cleanup(
    dry_run: bool = False,
    max_passes: int = 3,
    skip_confirm: bool = False,
) -> CleanupReport:
    """Run the full iterative cleanup process."""
    report = CleanupReport(
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        dry_run=dry_run,
    )
    total_start = time.time()

    # --- Banner ---
    print(f"\n{'='*65}")
    print(f"  PATHWAY DATABASE CLEANUP")
    print(f"  Mode: {'DRY RUN (no changes)' if dry_run else 'LIVE (will modify database)'}")
    print(f"  Max passes: {max_passes}")
    print(f"{'='*65}\n")

    # --- Confirmation ---
    if not dry_run and not skip_confirm:
        print("WARNING: This will modify your database.")
        print("Make sure you have a backup before proceeding.\n")
        answer = input("Continue? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted.")
            report.final_status = "ABORTED"
            return report

    # --- Pre-flight counts ---
    logger.info("Snapshot pre-cleanup DB counts...")
    report.pre_counts = get_db_counts()
    for key, val in report.pre_counts.items():
        logger.info(f"  {key}: {val}")

    # --- Init roots ---
    logger.info("")
    logger.info("Ensuring root pathways exist...")
    from scripts.pathway_v2.step1_init_roots import init_roots
    init_roots()

    # --- Convergence loop ---
    prev_issues = None

    for pass_num in range(1, max_passes + 1):
        logger.info("")
        logger.info(f"{'#'*65}")
        logger.info(f"#  CLEANUP PASS {pass_num} of {max_passes}")
        logger.info(f"{'#'*65}")

        pass_result = run_cleanup_pass(pass_num, dry_run)
        report.passes.append(pass_result)

        current_issues = pass_result.remaining_issues

        # Convergence checks
        if current_issues == 0:
            logger.info(f"\nConverged! 0 MEDIUM+ issues after pass {pass_num}.")
            report.converged = True
            break

        if prev_issues is not None and current_issues >= prev_issues:
            logger.warning(
                f"\nNot converging: {current_issues} issues "
                f"(was {prev_issues}). Stopping."
            )
            break

        if pass_num < max_passes:
            logger.info(
                f"\n{current_issues} issues remain "
                f"(was {prev_issues or 'N/A'}). Running another pass..."
            )

        prev_issues = current_issues

    # --- Ontology enrichment ---
    if not dry_run:
        logger.info("")
        logger.info("Running ontology enrichment...")
        report.ontology_enriched = apply_ontology_enrichment()

    # --- Final read-only verification ---
    logger.info("")
    logger.info(f"{'='*65}")
    logger.info("FINAL VERIFICATION (read-only)")
    logger.info(f"{'='*65}")
    from scripts.pathway_v2.verify_pipeline import run_verification
    final_report = run_verification(auto_fix=False, report_only=True)
    final_issues = count_significant_issues(final_report)

    # --- Post-flight counts ---
    report.post_counts = get_db_counts() if not dry_run else report.pre_counts.copy()

    # --- Determine final status ---
    if final_issues == 0:
        report.final_status = "CLEAN"
    else:
        report.final_status = "ISSUES_REMAIN"

    report.total_elapsed = round(time.time() - total_start, 2)

    # --- Print and save report ---
    print("")
    print(report.to_string())

    log_dir = PROJECT_ROOT / 'logs' / 'cleanup_reports'
    report_file = log_dir / f"cleanup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    report.save_to_file(report_file)

    # --- Final message ---
    logger.info("")
    if report.final_status == "CLEAN":
        logger.info("CLEANUP COMPLETE - All pathways are clean and consistent.")
    else:
        logger.warning(
            f"CLEANUP FINISHED with {final_issues} remaining issues. "
            "Manual intervention may be needed."
        )

    return report


# ==============================================================================
# CLI
# ==============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Clean up ALL pathways in the database (dedup, hierarchy repair, verification)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Simulate changes without committing to database"
    )
    parser.add_argument(
        "--max-passes", type=int, default=3,
        help="Maximum convergence passes (default: 3)"
    )
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip confirmation prompt"
    )
    args = parser.parse_args()

    report = run_cleanup(
        dry_run=args.dry_run,
        max_passes=args.max_passes,
        skip_confirm=args.yes,
    )

    sys.exit(0 if report.final_status == "CLEAN" else 1)
