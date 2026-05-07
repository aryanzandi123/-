# Comprehensive Protein Pathway Pipeline Audit Report

**Date:** 2025-12-30
**Codebase:** Protein-Protein Interaction Pathway Pipeline
**Purpose:** Foundation document for all subsequent fix sessions

---

## PLANNED FIX SESSIONS

This audit will be addressed in focused sessions:

| Session | Focus | Status |
|---------|-------|--------|
| **Session 1** | Pathway assignment bugs | Pending |
| **Session 2** | Step 6 perfection + Step 7 creation | Pending |
| **Session 3** | Performance optimization | Pending |
| **Session 4** | Query flow + UI integration | Pending |

---

## AUDIT TASK 1: CODEBASE FILE TREE

```
/
├── app.py                          # Flask API (119KB, 3000+ lines) - ALL endpoints
├── runner.py                       # Main pipeline orchestrator (128KB) - run_full_job()
├── visualizer.py                   # Python visualization generator (26KB)
├── models.py                       # SQLAlchemy models (14KB) - 5 tables
│
├── scripts/
│   ├── pathway_v2/                 # V2 PATHWAY PIPELINE (6 steps)
│   │   ├── run_all_v2.py          # Orchestrator - runs steps 1-6 sequentially
│   │   ├── step1_init_roots.py    # Initialize 10 hardcoded root pathways (4.9KB)
│   │   ├── step2_assign_initial_terms.py  # Goldilocks LLM assignment (13.5KB)
│   │   ├── step3_refine_pathways.py       # Standardize terms (10.4KB)
│   │   ├── step4_build_hierarchy_backwards.py  # Build DAG upward (11KB)
│   │   ├── step5_discover_siblings.py     # Populate taxonomy (4.8KB)
│   │   ├── step6_reorganize_pathways.py   # Cleanup & enforce rules (20.8KB)
│   │   ├── llm_utils.py           # Gemini API wrapper + JSON parsing (8.4KB)
│   │   └── verify_pipeline.py     # Status checker (2.1KB)
│   └── clear_pathway_tables.py    # Data cleanup utility (4.5KB)
│
├── utils/
│   ├── db_sync.py                 # DatabaseSyncLayer - sync to PostgreSQL
│   ├── pathway_assigner.py        # Three-stage pathway assignment (631 lines)
│   ├── evidence_validator.py      # Gemini + Google Search validation
│   ├── claim_fact_checker.py      # Fact-checking claims
│   ├── llm_response_parser.py     # JSON extraction from LLM responses
│   ├── protein_database.py        # File-based cache fallback
│   └── pruner.py                  # Subgraph pruning logic
│
├── static/
│   ├── script.js                  # Main app logic, job tracking (850 lines)
│   ├── visualizer.js              # D3 v7 graph visualization (11,451 lines!)
│   ├── card_view.js               # Card/tree view (843 lines)
│   ├── pipeline_controls.js       # Pipeline UI controls (151 lines)
│   └── viz-styles.css             # Visualization styles
│
├── templates/
│   └── index.html                 # Main HTML template (135 lines)
│
├── cache/
│   ├── {PROTEIN}.json             # Snapshot for visualization
│   ├── {PROTEIN}_metadata.json    # Rich ctx_json metadata
│   └── proteins/{A}/interactions/{B}.json  # Bidirectional cache
│
└── tests/                         # Test files
```

---

## AUDIT TASK 2: PIPELINE STEPS DOCUMENTATION

### Step 1: Initialize Root Pathways
**File:** [step1_init_roots.py](scripts/pathway_v2/step1_init_roots.py)

| Aspect | Details |
|--------|---------|
| **PURPOSE** | Enforce exactly 10 hardcoded root pathways at hierarchy_level=0 |
| **INPUTS** | Database (reads existing pathways) |
| **OUTPUTS** | 10 root Pathway records created/verified |
| **LLM CALLS** | None |
| **DB READS** | Pathway (filter by name, hierarchy_level=0), PathwayParent |
| **DB WRITES** | Pathway (create/update), PathwayParent (delete illegal parents) |
| **DURATION** | ~1-2 seconds |

**10 Hardcoded Roots:**
1. Cellular Signaling (GO:0023052)
2. Metabolism (GO:0008152)
3. Protein Quality Control (GO:0006457)
4. Cell Death (GO:0008219)
5. Cell Cycle (GO:0007049)
6. DNA Damage Response (GO:0006974)
7. Vesicle Transport (GO:0016192)
8. Immune Response (GO:0006955)
9. Neuronal Function (GO:0007399)
10. Cytoskeleton Organization (GO:0007010)

---

### Step 2: Assign Initial Terms (Goldilocks)
**File:** [step2_assign_initial_terms.py](scripts/pathway_v2/step2_assign_initial_terms.py)

| Aspect | Details |
|--------|---------|
| **PURPOSE** | Assign "Goldilocks" specific pathway to EVERY interaction |
| **INPUTS** | All interactions without step2_proposal |
| **OUTPUTS** | `interaction.data['step2_proposal']` for each interaction |
| **LLM CALLS** | Gemini (temp 0.2-0.3), batches of 20 |
| **DB READS** | Interaction (all), Pathway (for consistency) |
| **DB WRITES** | Interaction.data JSONB field |
| **DURATION** | 30-120 seconds (depends on interaction count) |

**Retry Cascade:** 20 → 10 → 5 → 3 → 1 interactions per batch
**Max Retry Rounds:** 5
**GUARANTEE:** 100% of interactions get step2_proposal

---

### Step 3: Refine Pathway Terms
**File:** [step3_refine_pathways.py](scripts/pathway_v2/step3_refine_pathways.py)

| Aspect | Details |
|--------|---------|
| **PURPOSE** | Standardize and normalize pathway names from Step 2 |
| **INPUTS** | Interactions with step2_proposal |
| **OUTPUTS** | `interaction.data['step3_finalized_pathway']` |
| **LLM CALLS** | Gemini (temp 0.1-0.2), batches of 20 |
| **DB READS** | Interaction, Pathway (global context) |
| **DB WRITES** | Interaction.data JSONB field |
| **DURATION** | 20-60 seconds |

**Auto-Recovery:** Detects missing step2_proposal and calls Step 2
**Fallback:** If LLM fails, uses step2_proposal as-is
**GUARANTEE:** 100% coverage

---

### Step 4: Build Hierarchy Backwards
**File:** [step4_build_hierarchy_backwards.py](scripts/pathway_v2/step4_build_hierarchy_backwards.py)

| Aspect | Details |
|--------|---------|
| **PURPOSE** | Build pathway hierarchy from leaves upward to roots |
| **INPUTS** | Interactions with step3_finalized_pathway |
| **OUTPUTS** | Pathway records, PathwayInteraction links, PathwayParent hierarchy |
| **LLM CALLS** | Gemini (temp 0.1), one call per unique pathway |
| **DB READS** | Interaction, Pathway, PathwayParent |
| **DB WRITES** | Pathway, PathwayInteraction, PathwayParent |
| **DURATION** | 60-300 seconds (0.5s rate limit per LLM call) |

**Algorithm:**
1. Create leaf Pathway for each unique step3_finalized_pathway
2. Create PathwayInteraction linking interaction → leaf
3. For each leaf, recursively ask LLM for parent until hitting root
4. Create PathwayParent links upward

**Fallback:** Unlinked interactions → "Protein Quality Control"
**GUARANTEE:** 100% of interactions get PathwayInteraction records

---

### Step 5: Discover Siblings
**File:** [step5_discover_siblings.py](scripts/pathway_v2/step5_discover_siblings.py)

| Aspect | Details |
|--------|---------|
| **PURPOSE** | Populate taxonomy with sibling pathways |
| **INPUTS** | Parent pathways from Step 4 |
| **OUTPUTS** | New Pathway records (siblings), PathwayParent links |
| **LLM CALLS** | Gemini (temp 0.3), one call per parent |
| **DB READS** | Pathway, PathwayParent |
| **DB WRITES** | Pathway, PathwayParent |
| **DURATION** | 30-120 seconds (1s rate limit) |

---

### Step 6: Reorganize Pathways
**File:** [step6_reorganize_pathways.py](scripts/pathway_v2/step6_reorganize_pathways.py)

| Aspect | Details |
|--------|---------|
| **PURPOSE** | Enforce strict tree, deduplicate, deepen shallow hierarchies |
| **INPUTS** | All pathways and relationships |
| **OUTPUTS** | Cleaned pathway hierarchy |
| **LLM CALLS** | Multiple prompts for merge/parent decisions |
| **DB READS** | Pathway, PathwayParent, PathwayInteraction |
| **DB WRITES** | All pathway tables |
| **DURATION** | 60-180 seconds |

**5 Phases:**
1. **Deduplication** - Merge similar pathway names (>0.85 similarity)
2. **Tree Enforcement** - Single parent per pathway (LLM picks best)
3. **Hierarchy Deepening** - Add intermediate pathways for level-1 children
4. **Interaction Goldilocks** - Move interactions if too granular
5. **Pruning** - Delete orphaned pathways

---

## AUDIT TASK 3: PATHWAY ASSIGNMENT LOCATIONS

### CRITICAL: Every Place Pathways Get Assigned

| Location | File:Line | When | Storage | Overwritable? |
|----------|-----------|------|---------|---------------|
| Step 2 LLM | step2_assign_initial_terms.py:325-331 | V2 Pipeline | `interaction.data['step2_proposal']` | Yes (re-run) |
| Step 3 LLM | step3_refine_pathways.py:~320 | V2 Pipeline | `interaction.data['step3_finalized_pathway']` | Yes (re-run) |
| Step 4 DB | step4_build_hierarchy_backwards.py:154-168 | V2 Pipeline | PathwayInteraction table | Yes (re-run) |
| Fallback | step4_build_hierarchy_backwards.py:237-256 | Step 4 failure | PathwayInteraction (to "Protein Quality Control") | Yes |
| Repair endpoint | app.py:2595-2684 | `/api/repair-pathways` | Clears & re-runs steps 1-5 | Yes |
| Legacy assigner | utils/pathway_assigner.py:307-402 | Old pipeline (conditional) | interactor objects | N/A |

### Pathway Assignment Data Flow:
```
Query → run_full_job() → DB Sync → V2 Pipeline
                                      │
                                      ├─ Step 2: step2_proposal (JSONB)
                                      ├─ Step 3: step3_finalized_pathway (JSONB)
                                      ├─ Step 4: PathwayInteraction records
                                      └─ Steps 5-6: Hierarchy refinement
```

---

## AUDIT TASK 4: QUERY FLOW DOCUMENTATION

### When User Queries "ATXN3":

```
┌─────────────────────────────────────────────────────────────────┐
│ 1. POST /api/query {"protein": "ATXN3", ...}                   │
│    File: app.py:194-271                                         │
│    - Regex validation: ^[a-zA-Z0-9_-]+$                        │
│    - Spawns background thread                                   │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 2. run_full_job() - runner.py:1476-2055                        │
│                                                                 │
│    STAGE 0: Load known interactions from PostgreSQL            │
│    - Query Interaction table bidirectionally                    │
│    - Format: [{"primary": partner, "confidence": ...}, ...]    │
│                                                                 │
│    STAGE 1: Run main pipeline                                  │
│    - _run_main_pipeline_for_web() with known context           │
│    - Discovers new interactors/functions via LLM               │
│    - Returns pipeline_payload                                   │
│                                                                 │
│    STAGES 2-9: Post-processing chain                           │
│    - Schema validation                                          │
│    - Deduplication (AI)                                         │
│    - Evidence validation (Gemini + Google Search)              │
│    - Fact-checking                                              │
│    - PMID validation                                            │
│    - Arrow validation                                           │
│                                                                 │
│    STAGE 10.5: Save to file cache                              │
│    - cache/ATXN3.json (snapshot)                               │
│    - cache/ATXN3_metadata.json (full metadata)                 │
│    - cache/proteins/ATXN3/interactions/*.json                  │
│                                                                 │
│    STAGE 10.75: Sync to PostgreSQL                             │
│    - DatabaseSyncLayer.sync_query_results()                    │
│    - Creates/updates Protein records                            │
│    - Creates/updates Interaction records with FULL JSONB       │
│                                                                 │
│    STAGE 11: Run V2 Pathway Pipeline                           │
│    - Steps 1-6 execute sequentially                            │
│    - Assigns pathways to ALL interactions                      │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 3. GET /api/results/ATXN3                                      │
│    File: app.py:1305-1327                                       │
│    - Queries Interaction table                                  │
│    - Hydrates with PathwayInteraction data                     │
│    - Returns JSON for visualization                            │
└─────────────────────────────────────────────────────────────────┘
```

---

## AUDIT TASK 5: PROBLEMS FOUND

### A. Critical Issues

| Issue | Location | Severity | Description |
|-------|----------|----------|-------------|
| **11K line file** | visualizer.js | HIGH | Single 11,451 line file - unmaintainable |
| **50+ global variables** | visualizer.js:2-59 | HIGH | No namespace/module pattern |
| **Bare except** | app.py:2492 | MEDIUM | Swallows errors silently |
| **Hardcoded API key fallback** | app.py:8-10 | HIGH | Security risk in production |

### B. Code Duplication

| Duplicate | Files | Lines |
|-----------|-------|-------|
| Sidebar logic | visualizer.js, card_view.js:726-806 | ~80 lines each |
| Color mappings | visualizer.js, card_view.js:812-836 | Independent implementations |
| Config handling | script.js:161-192, index.html:34-72 | No centralized store |
| Protein detection | pipeline_controls.js:65-99 | 6 detection methods, could be shared |

### C. Hardcoded Values

| Value | Location | Should Be |
|-------|----------|-----------|
| 30000ms timeout | script.js:9 | Configurable |
| 5000ms poll interval | script.js:620 | Configurable |
| 2000ms pipeline poll | pipeline_controls.js:58 | Configurable |
| Batch size 20 | step2, step3 | Environment variable |
| 10 root pathways | step1_init_roots.py:37-48 | Config file |

### D. TODO/FIXME Comments

| File | Line | Comment |
|------|------|---------|
| visualizer.js | 6080 | `// TODO: Could enhance to look up arrow types here too` |
| Multiple files | Various | DEBUG console.log statements left in production |

### E. Missing Error Handling

- pipeline_controls.js:56 - Silent polling errors
- visualizer.js modal operations - No try-catch on DOM queries
- app.py:2492 - Bare except swallows errors

### F. Inconsistencies

| Area | Inconsistency |
|------|---------------|
| Status naming | script.js: 'processing' vs pipeline_controls.js: '[RUNNING]' |
| Progress structure | Some endpoints return {current, total, text}, others return string |
| URL encoding | Most use encodeURIComponent(), some direct concatenation |

---

## AUDIT TASK 6: DATABASE SCHEMA

### Tables Overview

```
┌─────────────────┐     ┌─────────────────────┐     ┌─────────────────┐
│    Protein      │     │    Interaction      │     │    Pathway      │
├─────────────────┤     ├─────────────────────┤     ├─────────────────┤
│ id (PK)         │◄────│ protein_a_id (FK)   │     │ id (PK)         │
│ symbol (UNIQUE) │◄────│ protein_b_id (FK)   │     │ name (UNIQUE)   │
│ query_count     │     │ confidence          │     │ ontology_id     │
│ first_queried   │     │ direction           │     │ hierarchy_level │
│ last_queried    │     │ arrow/arrows        │     │ is_leaf         │
│ total_interacts │     │ interaction_type    │     │ ancestor_ids    │
│ extra_data      │     │ data (JSONB) ◄──────│────►│ ai_generated    │
└─────────────────┘     │ depth               │     └────────┬────────┘
                        │ mediator_chain      │              │
                        └──────────┬──────────┘              │
                                   │                         │
                        ┌──────────▼──────────┐   ┌─────────▼─────────┐
                        │ PathwayInteraction  │   │  PathwayParent    │
                        ├─────────────────────┤   ├───────────────────┤
                        │ pathway_id (FK)     │   │ child_pathway_id  │
                        │ interaction_id (FK) │   │ parent_pathway_id │
                        │ assignment_method   │   │ relationship_type │
                        │ confidence          │   │ confidence        │
                        └─────────────────────┘   │ source            │
                                                  └───────────────────┘
```

### Interaction.data JSONB Structure (Critical)

```json
{
  "step2_proposal": "Autophagy Regulation",
  "step3_finalized_pathway": "Selective Autophagy",
  "evidence": [{
    "paper_title": "...",
    "journal": "...",
    "year": 2006,
    "relevant_quote": "...",
    "pmid": "16822850"
  }],
  "functions": ["deubiquitinates", "binds"],
  "pmids": ["16822850"],
  "support_summary": "...",
  "_inferred_from_chain": false,
  "_net_effect": null
}
```

### Pathway Hierarchy Storage

- **PathwayParent table:** DAG structure (child → parent links)
- **relationship_type:** 'is_a', 'part_of', 'regulates'
- **ancestor_ids JSONB:** Materialized path `[1, 5, 23, ...]` for fast queries
- **hierarchy_level:** 0 = root, higher = deeper in tree

---

## KEY INVARIANTS & GUARANTEES

1. **Strict Tree (Step 6):** Each pathway has exactly 1 parent (except roots)
2. **100% Coverage:** Steps 2-4 guarantee all interactions get pathway assignments
3. **10 Roots Only:** Hardcoded at hierarchy_level=0
4. **Unique Names:** Pathway.name is unique constraint
5. **Canonical Ordering:** protein_a_id < protein_b_id prevents duplicates
6. **JSONB Preservation:** Full pipeline output stored in Interaction.data

---

## RECOMMENDED FIX PRIORITIES

### Priority 1: Critical
- [ ] Split visualizer.js into modules (11K lines is unmaintainable)
- [ ] Remove hardcoded API key fallback
- [ ] Add proper error handling to bare except blocks

### Priority 2: High
- [ ] Consolidate duplicate sidebar/color logic
- [ ] Create shared config store for frontend
- [ ] Extract hardcoded values to config

### Priority 3: Medium
- [ ] Add namespacing to global variables
- [ ] Standardize status field naming
- [ ] Clean up debug console.log statements

### Priority 4: Low
- [ ] Address TODO comments
- [ ] Add rate limiting to API endpoints
- [ ] Implement HTTP caching strategy

---

## APPENDIX A: LLM PROMPTS (DEEP DIVE)

### Step 2: STEP2_PROMPT (Goldilocks Assignment)
**File:** [step2_assign_initial_terms.py:39-71](scripts/pathway_v2/step2_assign_initial_terms.py#L39-L71)

```
You are a biological pathway curator with a "Goldilocks" mindset.
Task: Assign a SINGLE, highly appropriate Pathway Name to each protein-protein interaction.

## THE "GOLDILOCKS" RULE
The pathway name must be specific enough to be meaningful, but broad enough to be a category.
- **TOO BROAD (Avoid)**: "Metabolism", "Cell Signaling", "Disease", "Interaction".
- **TOO SPECIFIC (Avoid)**: "Phosphorylation of Protein X", "Binding of A to B".
- **JUST RIGHT**: "mTOR Signaling", "Aggrephagy", "Wnt Signaling Pathway".

## EXISTING PATHWAYS IN DATABASE
{existing_pathways}

## INTERACTIONS TO ASSIGN
{interactions_list}

## RESPONSE FORMAT (Strict JSON)
{"assignments": [{"interaction_id": "ID", "specific_pathway": "Name", "reasoning": "..."}]}
```

**Context:** Up to 50 existing pathways + interactions formatted as `ID | Proteins: A <-> B | Context: functions`
**Temperature:** 0.2 | **Batch:** 20 interactions

---

### Step 3: STEP3_PROMPT (Standardization)
**File:** [step3_refine_pathways.py:33-59](scripts/pathway_v2/step3_refine_pathways.py#L33-L59)

```
You are a biological pathway standardization expert.
Task: REFINE and STANDARDIZE pathway names.

## GLOBAL CONTEXT (Existing & Proposed Pathways)
{global_context_list}

## INTERACTIONS TO REFINE
{interactions_list}

## INSTRUCTIONS
1. If similar term exists in Global Context, use the standard/canonical one.
2. Apply Goldilocks refinement if proposal too broad/specific.
3. The name must perfectly describe the interaction.

## RESPONSE FORMAT
{"refinements": [{"interaction_id": "ID", "finalized_pathway": "Final Name"}]}
```

**Context:** Up to 500 existing + proposed terms
**Temperature:** 0.1 (very strict)

---

### Step 4: PARENT_PROMPT (Hierarchy Building)
**File:** [step4_build_hierarchy_backwards.py:38-74](scripts/pathway_v2/step4_build_hierarchy_backwards.py#L38-L74)

```
You are a biological taxonomy expert building a detailed pathway hierarchy.
Task: Identify the IMMEDIATE biological parent pathway for: "{child_name}".

## CRITICAL RULES
1. DO NOT jump directly to a Root unless truly top-level.
2. Most pathways should have 3-6 levels between them and a Root.
3. Parent must be ONE LEVEL more general.

## EXAMPLES OF CORRECT HIERARCHIES
- "Aggrephagy" → "Selective Macroautophagy" → "Macroautophagy" → "Autophagy" → "Protein Quality Control"
- "mTOR Signaling" → "Nutrient Sensing" → "Growth Factor Signaling" → "Cellular Signaling"

## RESPONSE FORMAT
{"child": "{child_name}", "parent": "Immediate Parent", "reasoning": "..."}
```

**Temperature:** 0.1 (deterministic)

---

### Step 6: Key Prompts

**MERGE_PROMPT** (deduplication): Decides if pathway pairs are synonyms → merge or keep distinct
**BEST_PARENT_PROMPT** (tree enforcement): Picks single best parent when multiple exist
**GOLDILOCKS_PROMPT** (level validation): Checks if interaction at correct hierarchy level
**SHALLOW_HIERARCHY_PROMPT** (deepening): Inserts intermediate pathways for level-1 children

---

### LLM Configuration (`llm_utils.py`)

| Parameter | Value |
|-----------|-------|
| Model | `gemini-3-flash-preview` |
| max_output_tokens | 8192 |
| top_p | 0.95 |
| response_mime_type | `application/json` |
| retry_count | 3 with exponential backoff |

**6-Strategy JSON Extraction Fallback:**
1. Direct `json.loads()`
2. Extract from ` ```json ``` ` blocks
3. Fix single quotes → double quotes
4. Balanced bracket search
5. Truncation recovery (close unclosed braces)
6. Regex partial extraction (salvage individual fields)

---

## APPENDIX B: EVIDENCE VALIDATION (DEEP DIVE)

### Validation Pipeline Stages

```
STAGE 4: validate_and_enrich_evidence()
         ├─ Model: gemini-3.1-pro-preview
         ├─ Tools: Google Search enabled
         ├─ Temp: 0.3
         ├─ Max tokens: 60,192
         └─ "Scientific Adversary" approach
              ↓
STAGE 6: fact_check_json()
         ├─ 100% independent research (no papers from pipeline)
         ├─ Model: Gemini 2.5 Pro
         ├─ Thinking: 32,768 tokens
         ├─ Output: 65,536 tokens
         ├─ Tools: Google Search
         └─ Validity: TRUE | CORRECTED | FALSE | DELETED
              ↓
STAGE 6B: Remove duplicates from corrections
              ↓
STAGE 7: update_payload_pmids()
         ├─ Extract PMIDs from paper titles
         ├─ PubMed API lookup
         └─ Prune empty evidence/functions/interactors
```

### Validity Classifications

| Validity | Meaning | Action |
|----------|---------|--------|
| TRUE | Claim + all function fields supported by literature | Keep, add `evidence_source: 'fact_checker_validated'` |
| CORRECTED | Same interactor, different function found | Replace all fields, mark `'fact_checker_corrected'` |
| FALSE→CORRECTED | Claim wrong but salvageable function found | Replace, mark `'fact_checker_salvaged_from_false'` |
| FALSE | No valid function found | DELETE function |
| DELETED | Wrong interactor or fabricated | DELETE entire interactor |

### "Mechanistic Opposites" Detection

System guards against:
- Confusing transcriptional repression with protein instability
- Mistaking activators for repressors
- "ATXN3 stabilizes PTEN" vs "ATXN3 represses PTEN gene" (different mechanisms!)

### "Guilty by Association" Prevention

**NOT sufficient:**
- Both proteins participate in same process separately
- Both proteins co-localize in same structure
- Papers mention both proteins (without showing interaction)

**REQUIRED:**
- Papers explicitly show THIS interaction causes THIS function

---

## APPENDIX C: FRONTEND-BACKEND INTEGRATION (DEEP DIVE)

### All API Endpoints

| Endpoint | Method | Purpose | Polling? |
|----------|--------|---------|----------|
| `/api/search/{protein}` | GET | Check protein exists | No |
| `/api/query` | POST | Start pipeline job | No |
| `/api/status/{protein}` | GET | Poll job progress | 5s interval |
| `/api/cancel/{protein}` | POST | Cancel running job | No |
| `/api/results/{protein}` | GET | Get full network data | No |
| `/api/pathway/{id}/interactors` | GET | Lazy-load pathway interactors | No |
| `/api/expand/pruned` | POST | Request pruned subgraph | No |
| `/api/expand/status/{job_id}` | GET | Poll pruning progress | Varies |
| `/api/expand/results/{job_id}` | GET | Get pruning results | No |
| `/api/chat` | POST | LLM-assisted chat | No |
| `/api/pipeline/run` | POST | Run pathway pipeline | No |
| `/api/pipeline/status` | GET | Poll pipeline progress | 2s interval |
| `/api/repair-pathways/{protein}` | POST | Rebuild pathway assignments | No |

### Response Formats

**`/api/status/{protein}`:**
```json
{
  "status": "processing|complete|error|cancelling|cancelled",
  "progress": {"current": 5, "total": 10, "text": "Step description"}
}
```

**`/api/results/{protein}`:**
```json
{
  "snapshot_json": {
    "main": "ATXN3",
    "proteins": ["ATXN3", "VCP", ...],
    "interactions": [{
      "type": "direct|shared",
      "source": "ATXN3",
      "target": "VCP",
      "arrow": "binds|activates|inhibits|regulates",
      "confidence": 0.85
    }]
  }
}
```

### Error Handling

- **fetchWithTimeout:** 30s timeout on all requests
- **fetchWithRetry:** Exponential backoff (1s, 2s, 4s), 3 retries
- **409 Conflict:** Pipeline already running
- **Fallback chains:** Pruning fails → full query flow

### Client-Side State

| Component | State Storage |
|-----------|---------------|
| script.js | `JobTracker` Map: protein → {status, progress} |
| visualizer.js | Global vars: nodes, links, expandedPathways |
| card_view.js | `cvState`: {expandedNodes, selectedRoots} |

---

## NEXT STEPS

This audit provides the foundation for targeted fixes. Use this document to:

1. **Understand before modifying** - Reference flow diagrams before touching code
2. **Identify impact** - Check which files/tables a change affects
3. **Maintain invariants** - Ensure changes preserve 100% coverage guarantees
4. **Test properly** - Know what database tables to verify after changes

---

*Generated: 2025-12-30*
*Audit Session: Complete*
