# Step 6 & Step 7 Redesign - Comprehensive Design Document

**Date:** 2025-12-30
**Status:** Approved
**Approach:** Phase-by-Phase with Checkpoints (Approach C)

---

## Executive Summary

This document details the redesign of Step 6 (Reorganize Pathways) and creation of Step 7 (Verification Layer) for the protein pathway pipeline. The redesign addresses:

1. **JSON truncation** - LLM responses getting cut off during batch operations
2. **Hierarchy integrity** - Cycles, invalid parents, multi-parent violations
3. **Orphaned interactions** - Interactions pointing to deleted/missing pathways
4. **Missing verification** - No gatekeeper before data is considered production-ready

---

## Architecture Overview

### Core Principle: Validate-Then-Commit Per Phase

Each phase follows this pattern:

```
┌─────────────────────────────────────────────────────┐
│  PHASE N                                            │
│  ┌─────────────┐   ┌─────────────┐   ┌───────────┐ │
│  │ 1. Collect  │ → │ 2. Process  │ → │ 3. Verify │ │
│  │    (read)   │   │  (in-memory)│   │  (check)  │ │
│  └─────────────┘   └─────────────┘   └───────────┘ │
│         │                                   │       │
│         │         ┌─────────────┐           │       │
│         └────────►│ 4. Commit   │◄──────────┘       │
│                   │  (if valid) │                   │
│                   └─────────────┘                   │
└─────────────────────────────────────────────────────┘
```

### State Tracking

- Each phase writes a checkpoint to `Interaction.data['_step6_checkpoint']`
- Contains: `{phase: N, status: 'complete', changes: [...], timestamp: ...}`
- If Step 6 crashes mid-run, can resume from last checkpoint

### Phase Order (Revised)

1. **Deduplication** - Merge similar pathways (batched, retry-safe)
2. **Tree Enforcement** - Single parent per pathway (cycle-safe)
3. **Hierarchy Repair** - Fix shallow/broken chains
4. **Interaction Sync** - Ensure every interaction has valid pathway
5. **Pruning** - Remove truly orphaned pathways
6. **Pre-Flight Validation** - Quick sanity check before declaring success

### Failure Modes

- Phase fails validation → rollback that phase only, log error, continue to Step 7
- Step 7 finds issues → block commit, generate repair report

---

## Step 6: Phase Details

### Phase 1: Deduplication (JSON-Safe)

**Problem:** Current code batches 10 pathway pairs → LLM response truncates at 8192 tokens.

**Solution: Adaptive Batching with Retry Cascade**

```
BATCH STRATEGY:
  Initial batch: 5 pairs (not 10)
  On truncation: retry with 3 pairs
  On second fail: retry with 1 pair
  On third fail: skip pair, log for manual review
```

**Merge Decision Flow:**

```
┌──────────────────────────────────────────────────────────┐
│  1. Find Candidates (similarity > 0.85 OR containment)   │
└──────────────────────────────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────────┐
│  2. Batch LLM Call (5 pairs max)                         │
│     - If JSON incomplete → detect via bracket counting   │
│     - Retry with smaller batch                           │
│     - Partial extraction as last resort                  │
└──────────────────────────────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────────┐
│  3. For Each MERGE Decision:                             │
│     a) Identify keeper vs dropped pathway                │
│     b) Build migration plan (don't execute yet):         │
│        - Children to reparent                            │
│        - Interactions to reassign                        │
│        - Parent links to transfer                        │
│     c) Validate plan (no orphans, no cycles)             │
│     d) Execute migration atomically                      │
│     e) Record in changelog                               │
└──────────────────────────────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────────┐
│  4. Phase Verification:                                  │
│     - All merged pathways deleted                        │
│     - All interactions reassigned                        │
│     - No dangling PathwayInteraction records             │
└──────────────────────────────────────────────────────────┘
```

**Key Changes from Current:**
- Smaller initial batch (5, not 10)
- Truncation detection before parsing
- Migration plan built in-memory, validated, then executed
- Post-merge verification query

---

### Phase 2: Tree Enforcement (Cycle-Safe)

**Problem:** Current code can create cycles or assign non-existent parents. No detection = silent corruption.

**Solution: In-Memory Graph Validation Before Any DB Write**

```
TREE ENFORCEMENT ALGORITHM:

1. LOAD: Build adjacency map from PathwayParent table
   graph = {child_id: [parent_id, ...], ...}

2. DETECT MULTI-PARENT: Find all pathways with len(parents) > 1

3. FOR EACH MULTI-PARENT PATHWAY:
   ┌────────────────────────────────────────────────┐
   │  a) Get candidate parents from DB              │
   │  b) Validate ALL candidates exist              │
   │  c) LLM picks best parent                      │
   │  d) BEFORE applying:                           │
   │     - Simulate: graph[child] = [selected]      │
   │     - Run cycle detection (DFS from child)     │
   │     - If cycle found → reject, try next best   │
   │  e) If valid → queue for commit                │
   └────────────────────────────────────────────────┘

4. CYCLE DETECTION (DFS):
   def has_cycle(node, graph, visited, rec_stack):
       visited.add(node)
       rec_stack.add(node)
       for parent in graph.get(node, []):
           if parent in rec_stack:  # Cycle!
               return True
           if parent not in visited:
               if has_cycle(parent, graph, visited, rec_stack):
                   return True
       rec_stack.remove(node)
       return False

5. COMMIT: Apply all queued parent changes atomically

6. RECALCULATE LEVELS: BFS from roots (existing logic)
```

**Fallback When LLM Picks Invalid Parent:**
- If selected parent doesn't exist → log warning, keep first valid parent
- If all parents would create cycle → attach to nearest root category
- Never leave pathway orphaned

**Validation Query (Post-Phase):**
```sql
-- Find any remaining multi-parent pathways
SELECT child_pathway_id, COUNT(*)
FROM pathway_parents
GROUP BY child_pathway_id
HAVING COUNT(*) > 1;
-- Must return 0 rows
```

---

### Phase 3: Hierarchy Repair

**Problem:** Some pathways jump directly to roots (too shallow), others have broken chains.

**Solution: Two-Pass Repair**

```
PASS A: BROKEN CHAIN REPAIR
──────────────────────────────────────────────────────
1. Find pathways where parent_pathway_id points to non-existent pathway

   SELECT pp.child_pathway_id, pp.parent_pathway_id
   FROM pathway_parents pp
   LEFT JOIN pathways p ON pp.parent_pathway_id = p.id
   WHERE p.id IS NULL;

2. For each broken link:
   - Get child pathway name
   - LLM: "What should be the parent of '{child_name}'?"
   - Validate response exists OR create intermediate
   - Update link

PASS B: SHALLOW HIERARCHY DEEPENING
──────────────────────────────────────────────────────
1. Find level-1 pathways (direct children of roots)
   - EXCLUDE known "major categories" that belong at level 1:
     Apoptosis, Autophagy, MAPK Signaling, etc.

2. For each candidate:
   ┌─────────────────────────────────────────────────┐
   │  LLM: "Should '{child}' have intermediate      │
   │        parent between it and '{root}'?"        │
   │                                                │
   │  Response: KEEP or INSERT_INTERMEDIATE         │
   │                                                │
   │  If INSERT:                                    │
   │    - Check if intermediate already exists      │
   │    - If not, create it (linked to root)        │
   │    - Reparent child to intermediate            │
   │    - Reparent any siblings that should move    │
   └─────────────────────────────────────────────────┘

3. Rate limit: 0.5s between LLM calls
```

**Sibling Grouping Optimization:**
When creating intermediate parent, check if other level-1 pathways should also move under it.

**Validation (Post-Phase):**
- No pathways with hierarchy_level = -1 (unreachable)
- No broken parent links
- Level counts make sense

---

### Phase 4: Interaction Sync

**Problem:** After merges/moves, some interactions may point to deleted pathways.

**Solution: Complete Interaction Audit**

```
INTERACTION SYNC ALGORITHM:

1. BUILD VALID PATHWAY SET
   valid_pathway_ids = {p.id for p in Pathway.query.all()}

2. AUDIT ALL PATHWAY-INTERACTION LINKS
   For each PathwayInteraction record:
     if pathway_id NOT IN valid_pathway_ids:
       → Mark as orphaned
       → Queue interaction for reassignment

3. FIND UNLINKED INTERACTIONS
   SELECT i.id FROM interactions i
   LEFT JOIN pathway_interactions pi ON i.id = pi.interaction_id
   WHERE pi.id IS NULL;

4. REASSIGN ORPHANED/UNLINKED INTERACTIONS
   For each interaction needing assignment:

   a) Check interaction.data for step3_finalized_pathway
      - If exists AND pathway exists → link to it

   b) Check interaction.data for step2_proposal
      - If exists AND pathway exists → link to it

   c) Fallback: Use function context to pick pathway

   d) Last resort: Assign to "Protein Quality Control" root

5. CLEANUP ORPHANED LINKS
   DELETE FROM pathway_interactions
   WHERE pathway_id NOT IN (SELECT id FROM pathways);

6. UPDATE INTERACTION.DATA
   For each reassigned interaction:
   - Set data['_step6_reassigned'] = True
   - Set data['_step6_pathway'] = new_pathway_name
```

**Validation (Post-Phase):**
```sql
-- Every interaction must have at least one PathwayInteraction
SELECT COUNT(*) FROM interactions i
LEFT JOIN pathway_interactions pi ON i.id = pi.interaction_id
WHERE pi.id IS NULL;
-- Must return 0
```

---

### Phase 5: Safe Pruning

**Problem:** Current pruning might delete pathways that still have data.

**Solution: Multi-Check Before Delete**

```
SAFE PRUNING RULES:

A pathway is SAFE TO DELETE only if ALL conditions met:
  ✓ hierarchy_level = -1 (unreachable from roots)
  ✓ No PathwayInteraction records pointing to it
  ✓ No children in PathwayParent table
  ✓ NOT in the 10 hardcoded roots list

PRUNING FLOW:
  1. Find candidates: hierarchy_level = -1
  2. For each candidate:
     - Check interaction count
     - Check child count
     - If has interactions → rescue (reparent)
     - If has children → rescue children first
  3. Only delete truly orphaned pathways
  4. Log all deletions with reasoning

RESCUE OPERATION (for pathways with data):
  - Find nearest valid ancestor via step3 data
  - Reparent to that ancestor
  - Set hierarchy_level via BFS recalculation
  - DO NOT delete
```

---

### Phase 6: Pre-Flight Validation

Before declaring Step 6 complete:

```
PRE-FLIGHT CHECKLIST:
  □ All 10 roots exist at level 0
  □ No pathways with hierarchy_level = -1
  □ No multi-parent pathways
  □ No cycles detected (quick DFS)
  □ All interactions have PathwayInteraction link
  □ No PathwayInteraction points to missing pathway

If ANY check fails → set step6_status = 'NEEDS_REPAIR'
```

---

## Step 7: Verification Layer

**Purpose:** Final validation before data is considered "production ready."

### Structure

```
┌─────────────────────────────────────────────────────────┐
│                    VERIFICATION ENGINE                  │
├─────────────────────────────────────────────────────────┤
│  1. INTERACTION CHECKS                                  │
│  2. PATHWAY CHECKS                                      │
│  3. HIERARCHY CHECKS                                    │
│  4. AUTO-REPAIR (minor issues)                          │
│  5. VERDICT: PASS / FAIL                                │
│  6. GENERATE REPORT                                     │
└─────────────────────────────────────────────────────────┘
```

### Check Categories

**1. INTERACTION CHECKS:**
- Every interaction has exactly ONE primary pathway
- That pathway exists in pathways table
- No NULL/empty pathway assignments
- No leftover step2_proposal without step3_finalized
- PathwayInteraction.interaction_id is valid

**2. PATHWAY CHECKS:**
- No duplicate pathway names
- All 10 roots present
- No pathway with empty name
- usage_count matches actual PathwayInteraction count

**3. HIERARCHY CHECKS:**
- Graph is a proper TREE (single parent per node)
- No cycles (DFS verification)
- No orphan pathways (hierarchy_level != -1)
- Every pathway's parent exists
- Levels calculated correctly (root=0, child=parent+1)
- ancestor_ids JSONB matches actual path to root

### Auto-Repair Capabilities

| Issue | Severity | Auto-Fix |
|-------|----------|----------|
| usage_count mismatch | LOW | Yes - Recalculate |
| ancestor_ids stale | LOW | Yes - Rebuild from parents |
| is_leaf incorrect | LOW | Yes - Recalculate |
| Missing PathwayInteraction | MEDIUM | Yes - Create from step3 data |
| Orphan pathway with interactions | MEDIUM | Yes - Reparent to root |
| Cycle detected | HIGH | No - Block, manual fix |
| Duplicate pathway names | HIGH | No - Block, manual fix |
| Missing root pathway | CRITICAL | No - Block, abort |

### Verdict Logic

```python
if critical_issues > 0:
    return FAIL, "Critical issues found - manual intervention required"
if high_issues > 0 and not all_auto_fixed:
    return FAIL, "Unresolved high-severity issues"
if medium_issues > 0:
    auto_fix_medium_issues()
    return PASS_WITH_FIXES, "Auto-repaired medium issues"
return PASS, "All checks passed"
```

---

## Verification Report Format

```
═══════════════════════════════════════════════════════════
            PATHWAY VERIFICATION REPORT
            Generated: 2025-12-30 14:32:15
═══════════════════════════════════════════════════════════

SUMMARY
───────────────────────────────────────────────────────────
  Interactions verified:  247
  Pathways verified:      89
  Hierarchy depth:        0-5 levels

STATUS: PASS (with 3 auto-fixes)

CHECKS PASSED
───────────────────────────────────────────────────────────
  [OK] All interactions have pathway assignment
  [OK] No duplicate pathway names
  [OK] Hierarchy is a valid tree (no cycles)
  [OK] All 10 roots present
  [OK] No orphan pathways

AUTO-FIXES APPLIED
───────────────────────────────────────────────────────────
  [LOW] Recalculated usage_count for 12 pathways
  [LOW] Rebuilt ancestor_ids for 8 pathways
  [MEDIUM] Created PathwayInteraction for interaction #442

WARNINGS (non-blocking)
───────────────────────────────────────────────────────────
  [WARN] 3 pathways have no interactions (empty siblings)
  [WARN] Pathway "HDAC6 Activity" is at level 4 (deep)

═══════════════════════════════════════════════════════════
```

---

## Rollback Strategy

### Transaction Boundaries

- Step 6 Phases → Each phase commits independently
- Step 7 → Read-only verification (no commits except auto-fixes)
- Auto-fixes wrapped in single transaction

### If Step 7 Fails

1. DO NOT mark pipeline as complete
2. Save report to `logs/verification_failures/`
3. Set job status = 'verification_failed'
4. Return error to caller with report summary
5. Data remains in DB but flagged as unverified

### Manual Recovery Options

- Re-run Step 6 with `--repair` flag
- Re-run just Step 7 after manual DB fixes
- Full pipeline re-run from Step 1 (nuclear option)

---

## Implementation Plan

### Files to Modify

1. `scripts/pathway_v2/step6_reorganize_pathways.py` - Complete rewrite
2. `scripts/pathway_v2/verify_pipeline.py` - Expand to full Step 7
3. `scripts/pathway_v2/llm_utils.py` - Add truncation detection helpers

### New Files to Create

1. `scripts/pathway_v2/step6_utils.py` - Shared utilities (cycle detection, graph ops)
2. `scripts/pathway_v2/step7_checks.py` - Individual verification checks
3. `scripts/pathway_v2/step7_repairs.py` - Auto-repair functions

### Implementation Order

1. **step6_utils.py** - Build foundation (cycle detection, graph helpers)
2. **Phase 1** - Deduplication with adaptive batching
3. **Phase 2** - Tree enforcement with cycle safety
4. **Phase 3** - Hierarchy repair
5. **Phase 4** - Interaction sync
6. **Phase 5** - Safe pruning
7. **Phase 6** - Pre-flight validation
8. **step7_checks.py** - All verification checks
9. **step7_repairs.py** - Auto-repair functions
10. **verify_pipeline.py** - Orchestrate Step 7

---

*Design approved: 2025-12-30*
