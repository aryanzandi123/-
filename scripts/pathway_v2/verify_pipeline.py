#!/usr/bin/env python3
"""
Step 7: Pathway Verification Layer
===================================
The final gatekeeper before data is considered production-ready.

Runs comprehensive checks, auto-fixes minor issues, and generates a detailed report.
Blocks commit if serious inconsistencies remain.

Usage:
    python3 scripts/pathway_v2/verify_pipeline.py [--auto-fix] [--report-only]
"""

import sys
import logging
import argparse
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

from scripts.pathway_v2.step7_checks import (
    CheckResult,
    Severity,
    run_all_checks,
    get_all_issues,
    get_issues_by_severity,
    get_auto_fixable_issues,
)
from scripts.pathway_v2.step7_repairs import run_auto_repairs, RepairSummary


# ==============================================================================
# DATA STRUCTURES
# ==============================================================================

class VerificationStatus:
    PASS = "PASS"
    PASS_WITH_FIXES = "PASS_WITH_FIXES"
    FAIL = "FAIL"
    CRITICAL_FAIL = "CRITICAL_FAIL"


@dataclass
class VerificationReport:
    """Complete verification report."""
    timestamp: str
    status: str
    checks_passed: int
    checks_failed: int
    total_issues: int
    issues_by_severity: Dict[str, int]
    auto_fixes_applied: int
    blocking_issues: List[str]
    warnings: List[str]
    check_results: Dict[str, CheckResult]
    repair_summary: Optional[RepairSummary] = None

    def to_string(self) -> str:
        """Generate formatted report string."""
        lines = []

        # Header
        lines.append("=" * 65)
        lines.append("            PATHWAY VERIFICATION REPORT")
        lines.append(f"            Generated: {self.timestamp}")
        lines.append("=" * 65)
        lines.append("")

        # Summary
        lines.append("SUMMARY")
        lines.append("-" * 65)

        # Get stats from check results
        total_interactions = 0
        total_pathways = 0
        for name, result in self.check_results.items():
            if 'total_interactions' in result.stats:
                total_interactions = result.stats['total_interactions']
            if 'total_pathways' in result.stats:
                total_pathways = result.stats['total_pathways']

        lines.append(f"  Interactions verified:  {total_interactions}")
        lines.append(f"  Pathways verified:      {total_pathways}")
        lines.append(f"  Checks passed:          {self.checks_passed}")
        lines.append(f"  Checks failed:          {self.checks_failed}")
        lines.append("")

        # Status with emoji-style indicator
        status_indicator = {
            VerificationStatus.PASS: "[OK]",
            VerificationStatus.PASS_WITH_FIXES: "[OK*]",
            VerificationStatus.FAIL: "[FAIL]",
            VerificationStatus.CRITICAL_FAIL: "[CRITICAL]"
        }
        lines.append(f"STATUS: {status_indicator.get(self.status, '?')} {self.status}")

        if self.auto_fixes_applied > 0:
            lines.append(f"        ({self.auto_fixes_applied} auto-fixes applied)")
        lines.append("")

        # Checks summary
        lines.append("CHECKS")
        lines.append("-" * 65)
        for name, result in self.check_results.items():
            status = "[OK]" if result.passed else "[FAIL]"
            issue_count = len(result.issues)
            if issue_count > 0:
                lines.append(f"  {status} {name} ({issue_count} issues)")
            else:
                lines.append(f"  {status} {name}")
        lines.append("")

        # Issues by severity
        if self.total_issues > 0:
            lines.append("ISSUES BY SEVERITY")
            lines.append("-" * 65)
            for severity, count in self.issues_by_severity.items():
                if count > 0:
                    lines.append(f"  [{severity.upper()}] {count} issues")
            lines.append("")

        # Blocking issues
        if self.blocking_issues:
            lines.append("BLOCKING ISSUES (must fix manually)")
            lines.append("-" * 65)
            for issue in self.blocking_issues[:10]:
                lines.append(f"  [!] {issue}")
            if len(self.blocking_issues) > 10:
                lines.append(f"  ... and {len(self.blocking_issues) - 10} more")
            lines.append("")

        # Auto-fixes applied
        if self.repair_summary and self.repair_summary.succeeded > 0:
            lines.append("AUTO-FIXES APPLIED")
            lines.append("-" * 65)
            for result in self.repair_summary.results:
                if result.success:
                    lines.append(f"  [FIXED] {result.action_taken}")
            lines.append("")

        # Warnings
        if self.warnings:
            lines.append("WARNINGS (non-blocking)")
            lines.append("-" * 65)
            for warning in self.warnings[:10]:
                lines.append(f"  [WARN] {warning}")
            if len(self.warnings) > 10:
                lines.append(f"  ... and {len(self.warnings) - 10} more")
            lines.append("")

        lines.append("=" * 65)
        return "\n".join(lines)

    def save_to_file(self, filepath: Path):
        """Save report to file."""
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, 'w') as f:
            f.write(self.to_string())
        logger.info(f"Report saved to: {filepath}")


def _merge_repair_summaries(summaries: List[RepairSummary]) -> Optional[RepairSummary]:
    """Concatenate per-pass RepairSummary objects into one.

    The repair loop in ``run_verification`` runs up to three passes; the
    final report shows the aggregated outcome across all passes (every
    ``[FIXED] …`` line surfaces once) so operators see the full story.
    Returns ``None`` when the loop never ran.
    """
    if not summaries:
        return None
    merged = RepairSummary(
        total_issues=sum(s.total_issues for s in summaries),
        attempted=sum(s.attempted for s in summaries),
        succeeded=sum(s.succeeded for s in summaries),
        failed=sum(s.failed for s in summaries),
        skipped=sum(s.skipped for s in summaries),
        results=[r for s in summaries for r in s.results],
    )
    return merged


# ==============================================================================
# MAIN VERIFICATION LOGIC
# ==============================================================================

def run_verification(auto_fix: bool = False, report_only: bool = False,
                     scope_interaction_ids=None) -> VerificationReport:
    """Run complete Step 7 verification.

    Args:
        auto_fix: If True, attempt to auto-fix issues
        report_only: If True, only report without modifying anything
        scope_interaction_ids: If provided, only check these interaction IDs
            and skip expensive hierarchy checks.
    """
    try:
        from app import app, db
        from models import Pathway, PathwayParent, PathwayInteraction, Interaction
    except ImportError as e:
        logger.error(f"Failed to import app/db: {e}")
        raise

    with app.app_context():
        logger.info("=" * 70)
        logger.info("STEP 7: PATHWAY VERIFICATION")
        scope_info = f" (scoped to {len(scope_interaction_ids)} interactions)" if scope_interaction_ids else ""
        logger.info(f"Mode: {'Report Only' if report_only else 'Auto-Fix' if auto_fix else 'Check Only'}{scope_info}")
        logger.info("=" * 70)

        # Run all checks
        logger.info("Running verification checks...")
        check_results = run_all_checks(
            db, Pathway, PathwayParent, PathwayInteraction, Interaction,
            scope_interaction_ids=scope_interaction_ids,
        )

        # Analyze results
        all_issues = get_all_issues(check_results)
        issues_by_severity = get_issues_by_severity(check_results)

        checks_passed = sum(1 for r in check_results.values() if r.passed)
        checks_failed = sum(1 for r in check_results.values() if not r.passed)

        # Categorize issues
        blocking_issues = []
        warnings = []

        for issue in all_issues:
            if issue.severity in (Severity.HIGH, Severity.CRITICAL):
                blocking_issues.append(issue.message)
            elif issue.severity == Severity.MEDIUM and not issue.auto_fixable:
                blocking_issues.append(issue.message)
            else:
                warnings.append(issue.message)

        # Determine initial status
        if Severity.CRITICAL in issues_by_severity and issues_by_severity[Severity.CRITICAL]:
            status = VerificationStatus.CRITICAL_FAIL
        elif blocking_issues and not auto_fix:
            status = VerificationStatus.FAIL
        elif all_issues:
            status = VerificationStatus.PASS_WITH_FIXES if auto_fix else VerificationStatus.FAIL
        else:
            status = VerificationStatus.PASS

        # Run auto-repairs if requested. Some repairs create new
        # auto-fixable issues (chain unification can invalidate
        # PathwayInteraction rows, which a follow-up pass deletes), so
        # the loop runs until no more fixable issues remain or the cap
        # is hit. Cap is intentionally small — if we haven't converged
        # in 3 passes the problem isn't repair-shaped and manual
        # intervention is the right answer.
        repair_summary = None
        MAX_REPAIR_PASSES = 3
        if auto_fix and not report_only and all_issues:
            logger.info("")
            logger.info("Running auto-repairs...")
            repair_summaries: List[RepairSummary] = []
            for pass_num in range(1, MAX_REPAIR_PASSES + 1):
                fixable = get_auto_fixable_issues(check_results)
                if not fixable:
                    break
                logger.info(
                    f"Auto-repair pass {pass_num}/{MAX_REPAIR_PASSES} — "
                    f"{len(fixable)} fixable issue(s)"
                )
                repair_summaries.append(run_auto_repairs(
                    fixable, db, Pathway, PathwayParent, PathwayInteraction, Interaction
                ))

                # Re-run checks after repairs
                logger.info("")
                logger.info(f"Re-running verification after repair pass {pass_num}...")
                check_results = run_all_checks(
                    db, Pathway, PathwayParent, PathwayInteraction, Interaction,
                    scope_interaction_ids=scope_interaction_ids,
                )
                all_issues = get_all_issues(check_results)
                issues_by_severity = get_issues_by_severity(check_results)
                checks_passed = sum(1 for r in check_results.values() if r.passed)
                checks_failed = sum(1 for r in check_results.values() if not r.passed)

            repair_summary = _merge_repair_summaries(repair_summaries)

            if repair_summaries:
                # Recategorize once after the loop converges/caps.
                blocking_issues = []
                warnings = []
                for issue in all_issues:
                    if issue.severity in (Severity.HIGH, Severity.CRITICAL):
                        blocking_issues.append(issue.message)
                    elif issue.severity == Severity.MEDIUM and not issue.auto_fixable:
                        blocking_issues.append(issue.message)
                    else:
                        warnings.append(issue.message)

                if blocking_issues:
                    status = VerificationStatus.FAIL
                elif repair_summary and repair_summary.succeeded > 0:
                    status = VerificationStatus.PASS_WITH_FIXES
                else:
                    status = VerificationStatus.PASS

        # Build report
        severity_counts = {
            sev.value: len(issues)
            for sev, issues in issues_by_severity.items()
        }

        report = VerificationReport(
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            status=status,
            checks_passed=checks_passed,
            checks_failed=checks_failed,
            total_issues=len(all_issues),
            issues_by_severity=severity_counts,
            auto_fixes_applied=repair_summary.succeeded if repair_summary else 0,
            blocking_issues=blocking_issues,
            warnings=warnings,
            check_results=check_results,
            repair_summary=repair_summary
        )

        # Print report
        print("")
        print(report.to_string())

        # Save report to file
        log_dir = PROJECT_ROOT / 'logs' / 'verification_reports'
        report_file = log_dir / f"verification_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        report.save_to_file(report_file)

        # Final status message
        logger.info("")
        if status == VerificationStatus.PASS:
            logger.info("VERIFICATION PASSED - Data is ready for production")
        elif status == VerificationStatus.PASS_WITH_FIXES:
            logger.info("VERIFICATION PASSED WITH FIXES - Review applied changes")
        elif status == VerificationStatus.FAIL:
            logger.error("VERIFICATION FAILED - Manual intervention required")
        else:
            logger.critical("CRITICAL FAILURE - Do not use this data")

        return report


def verify(auto_fix: bool = True, scope_interaction_ids=None) -> dict:
    """Pipeline verification entry point.

    Args:
        auto_fix: If True (default), attempt to auto-fix minor issues.
        scope_interaction_ids: If provided, only check these interaction IDs.
    """
    try:
        report = run_verification(
            auto_fix=auto_fix, report_only=False,
            scope_interaction_ids=scope_interaction_ids,
        )
        return {
            'passed': report.status in (VerificationStatus.PASS, VerificationStatus.PASS_WITH_FIXES),
            'status': report.status,
            'checks_passed': report.checks_passed,
            'checks_failed': report.checks_failed,
            'issues_fixed': report.auto_fixes_applied,
            'blocking_issues': len(report.blocking_issues)
        }
    except Exception as e:
        logger.error(f"Verification failed with error: {e}")
        return {
            'passed': False,
            'status': 'ERROR',
            'checks_passed': 0,
            'checks_failed': 0,
            'issues_fixed': 0,
            'blocking_issues': 0,
            'error': str(e)
        }


# ==============================================================================
# CLI ENTRY POINT
# ==============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Step 7: Pathway Verification")
    parser.add_argument(
        "--auto-fix",
        action="store_true",
        help="Attempt to auto-fix issues"
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Only generate report, don't modify anything"
    )
    args = parser.parse_args()

    try:
        report = run_verification(
            auto_fix=args.auto_fix,
            report_only=args.report_only
        )

        # Exit with appropriate code
        if report.status in (VerificationStatus.PASS, VerificationStatus.PASS_WITH_FIXES):
            sys.exit(0)
        else:
            sys.exit(1)

    except Exception as e:
        logger.error(f"Verification failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(2)
