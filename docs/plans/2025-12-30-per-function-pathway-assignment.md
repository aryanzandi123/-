# Per-Function Pathway Assignment Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix the bug where all interactions between a protein pair are forced into the same pathway, instead of assigning each biological function to its correct pathway.

**Architecture:** Modify the V2 pipeline (Steps 2-4) to evaluate and assign pathways at the FUNCTION level, then create multiple PathwayInteraction records (one per unique pathway per interaction). The frontend already supports function-level pathway comparison via the blue/grey badge logic.

**Tech Stack:** Python (Flask/SQLAlchemy), PostgreSQL JSONB, JavaScript (D3 visualization)

---

## Root Cause Analysis

### The Bug
- **Database constraint**: `UniqueConstraint('protein_a_id', 'protein_b_id')` allows only ONE Interaction record per protein pair
- **Current V2 behavior**: Assigns ONE `step3_finalized_pathway` per Interaction
- **Result**: Multiple biological functions (e.g., "Transcriptional Co-regulation" AND "TBP Sequestration") get the SAME pathway assignment

### Example
ATXN3 ↔ TBP has two biological functions:
1. **Transcriptional Co-regulation** → should be "Transcriptional Regulation"
2. **TBP Sequestration in Aggregates** → should be "Protein Aggregation"

Both get forced to whichever pathway is picked first.

### The Fix
1. Assign pathways to FUNCTIONS, not just interactions
2. Create PathwayInteraction records for EACH unique pathway
3. Frontend already handles display correctly (blue = match, grey = different)

---

## Task 1: Update Step 2 to Assign Per-Function Pathways

**Files:**
- Modify: `scripts/pathway_v2/step2_assign_initial_terms.py`

**Step 1: Update STEP2_PROMPT to evaluate functions**

Replace the interaction-level prompt with function-level evaluation:

```python
STEP2_PROMPT = """You are a biological pathway curator with a "Goldilocks" mindset.
Task: Assign a SINGLE, highly appropriate Pathway Name to EACH FUNCTION of each protein-protein interaction.

## THE "GOLDILOCKS" RULE
The pathway name must be specific enough to be meaningful, but broad enough to be a category.
- **TOO BROAD (Avoid)**: "Metabolism", "Cell Signaling", "Disease", "Interaction".
- **TOO SPECIFIC (Avoid)**: "Phosphorylation of Protein X", "Binding of A to B", "Complex Formation".
- **JUST RIGHT**: "mTOR Signaling", "Aggrephagy", "Wnt Signaling Pathway", "DNA Mismatch Repair".

## EXISTING PATHWAYS IN DATABASE
{existing_pathways}

## CRITICAL: EVALUATE EACH FUNCTION INDEPENDENTLY
Different functions between the SAME protein pair can belong to DIFFERENT pathways!
Example: ATXN3 ↔ TBP could have:
- Function 1: "binds TBP to modulate transcription" → "Transcriptional Regulation"
- Function 2: "polyQ-expanded ATXN3 sequesters TBP in aggregates" → "Protein Aggregation"

## INTERACTIONS AND THEIR FUNCTIONS TO ASSIGN
{interactions_list}

## RESPONSE FORMAT (Strict JSON)
{{
  "assignments": [
    {{
      "interaction_id": "ID",
      "function_pathways": [
        {{"function_index": 0, "pathway": "Pathway Name", "reasoning": "Why"}},
        {{"function_index": 1, "pathway": "Different Pathway", "reasoning": "Why"}}
      ],
      "primary_pathway": "Most representative pathway for the interaction overall"
    }}
  ]
}}
Respond with ONLY the JSON. You MUST provide assignments for EVERY function of EVERY interaction.
"""
```

**Step 2: Update `_format_interaction` to include function details**

```python
def _format_interaction(item) -> str:
    """Format a single interaction with ALL its functions for the prompt."""
    funcs = item.data.get('functions', []) if item.data else []

    if not funcs:
        return f"- ID: {item.id} | Proteins: {item.protein_a.symbol} <-> {item.protein_b.symbol} | Functions: [No functions - assign based on interaction type]"

    func_details = []
    for idx, f in enumerate(funcs):
        desc = f.get('description') or f.get('function') or str(f) if isinstance(f, dict) else str(f)
        func_details.append(f"    [{idx}] {desc[:150]}")

    func_str = "\n".join(func_details)
    return f"- ID: {item.id} | Proteins: {item.protein_a.symbol} <-> {item.protein_b.symbol}\n  Functions:\n{func_str}"
```

**Step 3: Update `_process_batch` to handle function-level responses**

```python
def _process_batch(batch: List, existing_pathways: Set[str], db) -> Dict[str, Dict]:
    """
    Process a batch of interactions. Returns dict of:
    {interaction_id: {"function_pathways": [...], "primary_pathway": "..."}}
    """
    if not batch:
        return {}

    batch_map = {str(item.id): item for item in batch}
    items_str = "\n".join([_format_interaction(item) for item in batch])

    pathway_list = ", ".join(sorted(existing_pathways)[:50]) if existing_pathways else "None yet"

    prompt = STEP2_PROMPT.format(
        existing_pathways=pathway_list,
        interactions_list=items_str
    )

    resp = _call_gemini_json(prompt, temperature=0.2)
    assignments = resp.get('assignments', [])

    results = {}
    for a in assignments:
        str_id = str(a.get('interaction_id'))
        if str_id in batch_map:
            results[str_id] = {
                "function_pathways": a.get('function_pathways', []),
                "primary_pathway": a.get('primary_pathway')
            }

    return results
```

**Step 4: Update database write to store per-function pathways**

```python
# In assign_initial_terms(), replace lines 325-331:
for interaction in todo:
    str_id = str(interaction.id)
    if str_id in all_results:
        result = all_results[str_id]
        d = dict(interaction.data or {})

        # Store function-level pathways
        d['step2_function_proposals'] = result.get('function_pathways', [])
        d['step2_proposal'] = result.get('primary_pathway')  # Backward compat

        # Also update each function in the data
        functions = d.get('functions', [])
        for fp in result.get('function_pathways', []):
            idx = fp.get('function_index')
            if idx is not None and idx < len(functions):
                functions[idx]['step2_pathway'] = fp.get('pathway')
        d['functions'] = functions

        interaction.data = d
        success_count += 1
```

**Step 5: Run Step 2 with test data**

```bash
python scripts/pathway_v2/step2_assign_initial_terms.py
```

Expected: Interactions now have `step2_function_proposals` array in their data JSONB.

**Step 6: Commit**

```bash
git add scripts/pathway_v2/step2_assign_initial_terms.py
git commit -m "$(cat <<'EOF'
feat(pipeline): Step 2 assigns pathways per-function not per-interaction

- Updated STEP2_PROMPT to evaluate each function independently
- Functions between same protein pair can now get different pathways
- Stores step2_function_proposals array alongside step2_proposal
- Updates each function's step2_pathway field

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Update Step 3 to Refine Per-Function Pathways

**Files:**
- Modify: `scripts/pathway_v2/step3_refine_pathways.py`

**Step 1: Update STEP3_PROMPT for function-level refinement**

```python
STEP3_PROMPT = """You are a biological pathway standardization expert.
Task: REFINE and STANDARDIZE the proposed pathway names for EACH FUNCTION.

## GLOBAL CONTEXT (Existing & Proposed Pathways)
{global_context_list}

## INTERACTIONS TO REFINE
{interactions_list}

## INSTRUCTIONS
1. If a similar term exists in Global Context, use the standard/canonical one.
2. Apply Goldilocks refinement if proposal is too broad/specific.
3. Each function should map to the pathway that BEST describes its biological mechanism.
4. Different functions CAN and SHOULD have different pathways if biologically appropriate.

## RESPONSE FORMAT
{{
  "refinements": [
    {{
      "interaction_id": "ID",
      "function_refinements": [
        {{"function_index": 0, "finalized_pathway": "Final Name"}},
        {{"function_index": 1, "finalized_pathway": "Different Final Name"}}
      ],
      "primary_pathway": "Most representative pathway overall"
    }}
  ]
}}
"""
```

**Step 2: Update `_format_interaction_for_step3`**

```python
def _format_interaction_for_step3(item) -> str:
    """Format interaction with step2 proposals for refinement."""
    proposals = item.data.get('step2_function_proposals', [])
    fallback = item.data.get('step2_proposal', 'Unknown')

    if not proposals:
        return f"- ID: {item.id} | Proposal: {fallback}"

    lines = [f"- ID: {item.id}"]
    for p in proposals:
        idx = p.get('function_index', '?')
        pw = p.get('pathway', fallback)
        lines.append(f"    [{idx}] {pw}")

    return "\n".join(lines)
```

**Step 3: Update database write for function refinements**

```python
# Store refined function pathways
d['step3_function_pathways'] = result.get('function_refinements', [])
d['step3_finalized_pathway'] = result.get('primary_pathway')  # Backward compat

# Update each function
functions = d.get('functions', [])
for fr in result.get('function_refinements', []):
    idx = fr.get('function_index')
    if idx is not None and idx < len(functions):
        functions[idx]['pathway'] = fr.get('finalized_pathway')  # Final pathway on function
d['functions'] = functions
```

**Step 4: Run and verify**

```bash
python scripts/pathway_v2/step3_refine_pathways.py
```

**Step 5: Commit**

```bash
git add scripts/pathway_v2/step3_refine_pathways.py
git commit -m "$(cat <<'EOF'
feat(pipeline): Step 3 refines per-function pathway assignments

- Updated STEP3_PROMPT for function-level refinement
- Stores step3_function_pathways array
- Sets fn['pathway'] on each function for frontend use

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Update Step 4 to Create Multiple PathwayInteraction Records

**Files:**
- Modify: `scripts/pathway_v2/step4_build_hierarchy_backwards.py`

**Step 1: Collect ALL unique pathways from functions**

Replace the current `term_to_interactions` building logic:

```python
def build_hierarchy():
    """Build pathway hierarchy backwards from leaf terms to roots."""
    # ... existing imports and recovery ...

    with app.app_context():
        interactions = Interaction.query.filter(
            Interaction.data.has_key('step3_finalized_pathway') |
            Interaction.data.has_key('step3_function_pathways')
        ).all()

        # Build term -> interactions map (now supports multiple terms per interaction)
        term_to_interactions = {}
        interaction_to_terms = {}  # Track all terms for each interaction

        for i in interactions:
            interaction_terms = set()

            # Method 1: Function-level pathways (preferred)
            func_pathways = i.data.get('step3_function_pathways', [])
            for fp in func_pathways:
                pw = fp.get('finalized_pathway')
                if pw:
                    interaction_terms.add(pw)

            # Method 2: Function's own pathway field
            for func in i.data.get('functions', []):
                pw = func.get('pathway')
                if isinstance(pw, str) and pw:
                    interaction_terms.add(pw)
                elif isinstance(pw, dict) and pw.get('name'):
                    interaction_terms.add(pw['name'])

            # Method 3: Fallback to interaction-level
            if not interaction_terms:
                primary = i.data.get('step3_finalized_pathway')
                if primary:
                    interaction_terms.add(primary)

            # Add interaction to each of its pathways
            for term in interaction_terms:
                if term not in term_to_interactions:
                    term_to_interactions[term] = []
                term_to_interactions[term].append(i)

            interaction_to_terms[i.id] = interaction_terms

        unique_terms = list(term_to_interactions.keys())
        logger.info(f"Processing hierarchy for {len(unique_terms)} unique terms from {len(interactions)} interactions.")
```

**Step 2: Create PathwayInteraction for EACH term**

The existing loop already handles this correctly! Each interaction in `term_to_interactions[leaf_name]` gets a PathwayInteraction. Since we now add interactions to multiple terms, they'll get multiple PathwayInteraction records.

**Step 3: Update verification to check for function-level coverage**

```python
# Final verification: ensure all interactions have PathwayInteraction records
unlinked = []
for i in interactions:
    has_any_link = PathwayInteraction.query.filter_by(interaction_id=i.id).first()
    if not has_any_link:
        unlinked.append(i)

    # Also verify each function pathway has a link
    expected_terms = interaction_to_terms.get(i.id, set())
    actual_links = PathwayInteraction.query.filter_by(interaction_id=i.id).all()
    actual_pathways = set()
    for link in actual_links:
        if link.pathway:
            actual_pathways.add(link.pathway.name)

    missing = expected_terms - actual_pathways
    if missing:
        logger.warning(f"Interaction {i.id} missing PathwayInteraction for: {missing}")
```

**Step 4: Run and verify**

```bash
python scripts/pathway_v2/step4_build_hierarchy_backwards.py
```

**Step 5: Verify in database**

```sql
-- Should show interactions with multiple pathway links
SELECT i.id, COUNT(pi.id) as pathway_count
FROM interactions i
JOIN pathway_interactions pi ON pi.interaction_id = i.id
GROUP BY i.id
HAVING COUNT(pi.id) > 1;
```

**Step 6: Commit**

```bash
git add scripts/pathway_v2/step4_build_hierarchy_backwards.py
git commit -m "$(cat <<'EOF'
feat(pipeline): Step 4 creates PathwayInteraction per function pathway

- Collects ALL unique pathways from function-level assignments
- Creates separate PathwayInteraction record for each pathway
- Single interaction can now belong to multiple pathways
- Verifies coverage at function level, not just interaction level

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Update Backend API to Include Function Pathways

**Files:**
- Modify: `app.py` (around line 625)

**Step 1: Keep V2 injection but preserve function-level pathways**

The current injection at lines 625-643 overwrites function pathways with `step3_finalized_pathway`. We need to preserve existing function pathways instead:

```python
# V2 PATHWAY INJECTION (UPDATED)
# Inject pathways to functions that don't have one, but DON'T overwrite existing
v2_pathway = interaction.data.get('step3_finalized_pathway')

if interaction_data.get("functions"):
    for func in interaction_data["functions"]:
        current_pw = func.get("pathway")

        # Already has a string pathway from Step 3 - keep it!
        if isinstance(current_pw, str) and current_pw and current_pw != "Uncategorized":
            continue

        # Already has a dict pathway - extract name
        if isinstance(current_pw, dict) and current_pw.get("name"):
            func["pathway"] = current_pw["name"]  # Flatten to string
            continue

        # No pathway - use interaction-level fallback
        if v2_pathway:
            func["pathway"] = v2_pathway
```

**Step 2: Also add `step3_finalized_pathway` to interaction_data for modal use**

```python
# Add interaction-level pathway for modal display (line ~645)
if v2_pathway:
    interaction_data["step3_finalized_pathway"] = v2_pathway
```

**Step 3: Test API response**

```bash
curl http://localhost:5000/api/results/ATXN3 | jq '.snapshot_json.interactions[0].functions[].pathway'
```

Expected: Different functions should show different pathway names.

**Step 4: Commit**

```bash
git add app.py
git commit -m "$(cat <<'EOF'
fix(api): preserve function-level pathways in API response

- Don't overwrite existing function pathways with interaction-level
- Only use step3_finalized_pathway as fallback for empty functions
- Add step3_finalized_pathway to interaction_data for modal display

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Verify Frontend Badge Logic Works

**Files:**
- Read-only verification: `static/visualizer.js:5486-5510`

**Step 1: Verify badge logic**

The existing code at lines 5493-5510 already handles function-level pathways correctly:

```javascript
if (fnPathway) {
    const matchesContext = pathwayContext?.name &&
        fnPathway.toLowerCase() === pathwayContext.name.toLowerCase();

    if (matchesContext) {
        // GREEN - function belongs to current pathway view
        pathwayBadgeHTML = `<span class="pathway-badge current" style="background: #10b981;">...`;
    } else {
        // GREY - function belongs to different pathway
        pathwayBadgeHTML = `<span class="pathway-badge other" style="background: #6b7280; opacity: 0.7;">...`;
    }
}
```

**Step 2: Manual test**

1. Run the full pipeline for ATXN3: `python scripts/pathway_v2/run_all_v2.py`
2. Load visualization in browser
3. Expand "Transcriptional Regulation" pathway
4. Click on TBP node
5. Verify modal shows:
   - "Transcriptional Co-regulation" function with BLUE badge
   - "TBP Sequestration" function with GREY badge

**No code changes needed for frontend - existing logic handles it!**

---

## Task 6: Run Full Pipeline Test

**Step 1: Clear existing data for clean test**

```bash
python scripts/clear_pathway_tables.py --confirm
```

**Step 2: Run V2 pipeline**

```bash
python scripts/pathway_v2/run_all_v2.py
```

**Step 3: Check logs for multi-pathway assignments**

Look for log messages like:
```
Processing hierarchy for 45 unique terms from 30 interactions.
```

If interactions have multiple function pathways, unique terms should exceed interaction count.

**Step 4: Verify in UI**

1. Open browser to visualization
2. Expand different root pathways
3. Look for same protein appearing under multiple pathways
4. Click and verify blue/grey badges match context

**Step 5: Commit verification results**

Document any edge cases found during testing.

---

## Summary of Changes

| File | Change |
|------|--------|
| `step2_assign_initial_terms.py` | Evaluate per-function, store `step2_function_proposals` |
| `step3_refine_pathways.py` | Refine per-function, store `step3_function_pathways`, set `fn['pathway']` |
| `step4_build_hierarchy_backwards.py` | Collect all function pathways, create multiple PathwayInteraction records |
| `app.py` | Preserve function pathways, don't overwrite with interaction-level |
| `visualizer.js` | No changes needed - existing logic handles correctly |

## Key Invariants

1. **Function-level pathways are authoritative** - interaction-level is fallback only
2. **PathwayInteraction is many-to-many** - one interaction can link to multiple pathways
3. **Frontend badge logic unchanged** - compares function pathway to viewing context
4. **Backward compatible** - `step3_finalized_pathway` still exists for legacy/fallback
