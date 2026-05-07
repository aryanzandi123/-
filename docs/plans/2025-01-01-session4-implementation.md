# Session 4: Query-Specific Pipeline + UI Integration - Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Enable query-specific pathway pipeline runs and update visualization for multi-pathway proteins.

**Architecture:** Add query filter parameter to pipeline endpoint and step functions. Update frontend to show query dropdown. Modify visualizer to render proteins with compound IDs when they appear in multiple pathways.

**Tech Stack:** Python/Flask (backend), JavaScript/D3.js (frontend), PostgreSQL (database)

---

## Task 1: Add /api/queries Endpoint

**Files:**
- Modify: `app.py:2515` (add new endpoint before /api/pipeline/run)

**Step 1: Add the new endpoint**

Add this code at line 2515 (before the existing `/api/pipeline/run` route):

```python
@app.route('/api/queries', methods=['GET'])
def get_queries():
    """Return list of unique queries with interaction counts."""
    from sqlalchemy import func

    results = db.session.query(
        Interaction.discovered_in_query,
        func.count(Interaction.id).label('count')
    ).filter(
        Interaction.discovered_in_query.isnot(None)
    ).group_by(
        Interaction.discovered_in_query
    ).order_by(
        func.count(Interaction.id).desc()
    ).all()

    return jsonify({
        "queries": [
            {"name": q, "interaction_count": c}
            for q, c in results
        ]
    })


```

**Step 2: Test the endpoint**

Run:
```bash
curl http://127.0.0.1:5000/api/queries
```

Expected: JSON with list of queries and counts:
```json
{"queries": [{"name": "ATXN3", "interaction_count": 42}, ...]}
```

**Step 3: Commit**

```bash
git add app.py
git commit -m "feat: add /api/queries endpoint for query-specific pipeline"
```

---

## Task 2: Add Query Parameter to Pipeline Endpoint

**Files:**
- Modify: `app.py:2515-2535` (modify existing endpoint)
- Modify: `app.py:2457-2512` (modify run_pipeline_task)

**Step 1: Update the /api/pipeline/run endpoint**

Change lines 2515-2535 to:

```python
@app.route('/api/pipeline/run', methods=['POST'])
def run_pipeline():
    """Start the V2 pipeline."""
    data = request.json or {}
    mode = data.get('mode', 'full')  # full, single, downstream
    query_filter = data.get('query', None)  # NEW: filter by query name
    try:
        start_step = int(data.get('step', 1))
    except:
        start_step = 1

    with PIPELINE_LOCK:
        if PIPELINE_STATUS["is_running"]:
            return jsonify({"error": "Pipeline is already running"}), 409

    # Start background thread with query filter
    thread = threading.Thread(
        target=run_pipeline_task,
        args=(mode, start_step, query_filter)
    )
    thread.daemon = True
    thread.start()

    return jsonify({
        "message": "Pipeline started",
        "mode": mode,
        "step": start_step,
        "query": query_filter or "all"
    })
```

**Step 2: Update run_pipeline_task signature and logic**

Change line 2457 and the function to:

```python
def run_pipeline_task(mode, start_step, query_filter=None):
    """Background task to run pipeline steps."""
    global PIPELINE_STATUS

    # Get interaction IDs if filtering by query
    interaction_ids = None
    if query_filter:
        with app.app_context():
            from models import Interaction
            interactions = Interaction.query.filter_by(
                discovered_in_query=query_filter
            ).all()
            interaction_ids = [i.id for i in interactions]
            logging.info(f"Pipeline filtering to {len(interaction_ids)} interactions from query: {query_filter}")

    steps = [
        (1, "Initialize Roots", lambda: init_roots()),
        (2, "Assign Initial Terms", lambda: assign_initial_terms(interaction_ids=interaction_ids)),
        (3, "Refine Pathways", lambda: refine_pathways(interaction_ids=interaction_ids)),
        (4, "Build Hierarchy", lambda: build_hierarchy(interaction_ids=interaction_ids)),
        (5, "Discover Siblings", lambda: discover_siblings()),
        (6, "Reorganize & Cleanup", lambda: reorganize_pathways()),
        (7, "Verify Results", lambda: verify())
    ]

    with app.app_context():
        try:
            with PIPELINE_LOCK:
                PIPELINE_STATUS["is_running"] = True
                PIPELINE_STATUS["error"] = None
                PIPELINE_STATUS["logs"] = []
                query_info = f" for query '{query_filter}'" if query_filter else ""
                PIPELINE_STATUS["logs"].append(f"Starting pipeline (Mode: {mode}, Start: {start_step}){query_info}...")

            for step_num, step_name, step_func in steps:
                # Logic for skipping based on mode
                should_run = False
                if mode == "full":
                    should_run = True
                elif mode == "single":
                    if step_num == start_step:
                        should_run = True
                elif mode == "downstream":
                    if step_num >= start_step:
                        should_run = True

                if should_run:
                    with PIPELINE_LOCK:
                        PIPELINE_STATUS["current_step"] = f"Step {step_num}: {step_name}"
                        PIPELINE_STATUS["logs"].append(f"Running Step {step_num}: {step_name}...")

                    # Execute Step
                    step_func()

                    with PIPELINE_LOCK:
                         PIPELINE_STATUS["logs"].append(f"Step {step_num} Completed.")

            with PIPELINE_LOCK:
                PIPELINE_STATUS["is_running"] = False
                PIPELINE_STATUS["current_step"] = "Complete"
                PIPELINE_STATUS["logs"].append("Pipeline execution finished successfully.")

        except Exception as e:
            logging.error(f"Pipeline failed: {e}")
            with PIPELINE_LOCK:
                PIPELINE_STATUS["is_running"] = False
                PIPELINE_STATUS["error"] = str(e)
                PIPELINE_STATUS["logs"].append(f"Error: {str(e)}")
```

**Step 3: Commit**

```bash
git add app.py
git commit -m "feat: add query filter parameter to pipeline endpoint"
```

---

## Task 3: Update Step 2 to Accept Interaction Filter

**Files:**
- Modify: `scripts/pathway_v2/step2_assign_initial_terms.py:290-310`

**Step 1: Update assign_initial_terms signature**

Change the function at line 290:

```python
def assign_initial_terms(interaction_ids: List[int] = None):
    """
    Assign pathway terms to interactions. Guarantees 100% coverage.

    Args:
        interaction_ids: Optional list of interaction IDs to process.
                        If None, processes all interactions.
    """
    try:
        from app import app, db
        from models import Interaction
    except ImportError as e:
        logger.error(f"Failed to import app/db: {e}")
        return

    with app.app_context():
        # Fix any interactions with None data
        null_data_query = Interaction.query.filter(Interaction.data.is_(None))
        if interaction_ids:
            null_data_query = null_data_query.filter(Interaction.id.in_(interaction_ids))
        null_data_interactions = null_data_query.all()

        if null_data_interactions:
            logger.info(f"Fixing {len(null_data_interactions)} interactions with NULL data...")
            for i in null_data_interactions:
                i.data = {}
            db.session.commit()

        # Get interactions needing assignment
        query = Interaction.query.filter(
            ~Interaction.data.has_key('step2_proposal')
        )
        if interaction_ids:
            query = query.filter(Interaction.id.in_(interaction_ids))
            logger.info(f"Filtering to {len(interaction_ids)} interactions from query filter")

        interactions = query.all()
```

**Step 2: Add import for List type**

At the top of the file (around line 10), ensure this import exists:

```python
from typing import List, Dict, Optional
```

**Step 3: Commit**

```bash
git add scripts/pathway_v2/step2_assign_initial_terms.py
git commit -m "feat: step2 accepts optional interaction_ids filter"
```

---

## Task 4: Update Step 3 to Accept Interaction Filter

**Files:**
- Modify: `scripts/pathway_v2/step3_refine_pathways.py:213-233`

**Step 1: Update refine_pathways signature**

Change the function at line 213:

```python
def refine_pathways(interaction_ids: List[int] = None):
    """
    Refine pathway terms. Guarantees 100% coverage.

    Args:
        interaction_ids: Optional list of interaction IDs to process.
                        If None, processes all interactions.
    """
    try:
        from app import app, db
        from models import Interaction, Pathway
    except ImportError as e:
        logger.error(f"Failed to import app/db: {e}")
        return

    with app.app_context():
        # RECOVERY: First check for interactions missing step2_proposal
        missing_step2_query = Interaction.query.filter(
            ~Interaction.data.has_key('step2_proposal')
        )
        if interaction_ids:
            missing_step2_query = missing_step2_query.filter(Interaction.id.in_(interaction_ids))
        missing_step2 = missing_step2_query.all()

        if missing_step2:
            logger.warning(f"Found {len(missing_step2)} interactions missing step2_proposal. Running recovery...")
            from scripts.pathway_v2.step2_assign_initial_terms import assign_initial_terms_for_interactions
            assign_initial_terms_for_interactions(missing_step2)

        # Gather Global Context (always use full context for consistency)
        existing = {p.name for p in Pathway.query.all()}

        # Get interactions to refine
        needs_refine_query = Interaction.query.filter(
            Interaction.data.has_key('step2_proposal'),
            ~Interaction.data.has_key('step3_finalized_pathway')
        )
        if interaction_ids:
            needs_refine_query = needs_refine_query.filter(Interaction.id.in_(interaction_ids))
            logger.info(f"Filtering to {len(interaction_ids)} interactions from query filter")

        needs_refine = needs_refine_query.all()
```

**Step 2: Add import for List type**

At the top of the file (around line 10), ensure this import exists:

```python
from typing import List, Dict, Optional
```

**Step 3: Commit**

```bash
git add scripts/pathway_v2/step3_refine_pathways.py
git commit -m "feat: step3 accepts optional interaction_ids filter"
```

---

## Task 5: Update Step 4 to Accept Interaction Filter

**Files:**
- Modify: `scripts/pathway_v2/step4_build_hierarchy_backwards.py:137-157`

**Step 1: Update build_hierarchy signature**

Change the function at line 137:

```python
def build_hierarchy(interaction_ids: List[int] = None) -> None:
    """
    Build pathway hierarchy using two-phase parallel climbing.

    Args:
        interaction_ids: Optional list of interaction IDs to process.
                        If None, processes all interactions.
    """
    try:
        from app import app, db
        from models import Interaction, Pathway, PathwayParent, PathwayInteraction
    except ImportError as e:
        logger.error(f"Failed to import app/db: {e}")
        return

    # First run recovery for any missing steps
    _run_recovery_for_missing_steps(interaction_ids)

    with app.app_context():
        # Get interactions with finalized pathways
        query = Interaction.query.filter(
            Interaction.data.has_key('step3_finalized_pathway') |
            Interaction.data.has_key('step3_function_pathways')
        )
        if interaction_ids:
            query = query.filter(Interaction.id.in_(interaction_ids))
            logger.info(f"Filtering to {len(interaction_ids)} interactions from query filter")

        interactions = query.all()
```

**Step 2: Update _run_recovery_for_missing_steps to accept filter**

Find the function `_run_recovery_for_missing_steps` and update it:

```python
def _run_recovery_for_missing_steps(interaction_ids: List[int] = None):
    """Ensure all interactions have step2 and step3 data."""
    try:
        from app import app, db
        from models import Interaction
    except ImportError:
        return

    with app.app_context():
        # Check for missing step2
        query2 = Interaction.query.filter(~Interaction.data.has_key('step2_proposal'))
        if interaction_ids:
            query2 = query2.filter(Interaction.id.in_(interaction_ids))
        missing2 = query2.all()

        if missing2:
            logger.warning(f"Recovery: {len(missing2)} missing step2_proposal")
            from scripts.pathway_v2.step2_assign_initial_terms import assign_initial_terms_for_interactions
            assign_initial_terms_for_interactions(missing2)

        # Check for missing step3
        query3 = Interaction.query.filter(
            Interaction.data.has_key('step2_proposal'),
            ~Interaction.data.has_key('step3_finalized_pathway')
        )
        if interaction_ids:
            query3 = query3.filter(Interaction.id.in_(interaction_ids))
        missing3 = query3.all()

        if missing3:
            logger.warning(f"Recovery: {len(missing3)} missing step3_finalized_pathway")
            from scripts.pathway_v2.step3_refine_pathways import refine_pathways_for_interactions
            refine_pathways_for_interactions(missing3)
```

**Step 3: Commit**

```bash
git add scripts/pathway_v2/step4_build_hierarchy_backwards.py
git commit -m "feat: step4 accepts optional interaction_ids filter"
```

---

## Task 6: Update Frontend Pipeline Controls

**Files:**
- Modify: `static/pipeline_controls.js`

**Step 1: Add query dropdown initialization**

Add this function after line 7:

```javascript
async function initQueryDropdown() {
    try {
        const response = await fetch('/api/queries');
        const data = await response.json();

        const select = document.getElementById('pipeline-query');
        if (!select) return;

        select.innerHTML = '<option value="">All queries</option>';

        data.queries.forEach(q => {
            const option = document.createElement('option');
            option.value = q.name;
            option.textContent = `${q.name} (${q.interaction_count} interactions)`;
            select.appendChild(option);
        });
    } catch (e) {
        console.error('Failed to load queries:', e);
    }
}

// Initialize on page load
document.addEventListener('DOMContentLoaded', initQueryDropdown);
```

**Step 2: Update runPipeline to include query**

Update the runPipeline function (lines 9-35) to include query:

```javascript
async function runPipeline() {
    const mode = document.getElementById('pipeline-mode').value;
    const step = document.getElementById('pipeline-step').value;
    const querySelect = document.getElementById('pipeline-query');
    const query = querySelect ? querySelect.value : null;
    const statusDiv = document.getElementById('pipeline-status');
    const logsDiv = document.getElementById('pipeline-status');

    logsDiv.style.display = 'block';
    const queryInfo = query ? ` for query "${query}"` : '';
    logsDiv.innerText = `Starting ${mode} pipeline${queryInfo}...`;

    try {
        const response = await fetch('/api/pipeline/run', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode, step, query: query || null })
        });

        const data = await response.json();
        if (response.status === 409) {
            logsDiv.innerText = "Error: Pipeline is already running.";
        } else {
            logsDiv.innerText = `Pipeline started${queryInfo}. Monitoring...`;
            pollPipelineStatus();
        }
    } catch (e) {
        logsDiv.innerText = "Error triggering pipeline: " + e;
    }
}
```

**Step 3: Export initQueryDropdown**

Update line 148-151 to include:

```javascript
// Attach to window for HTML access
window.updatePipelineUI = updatePipelineUI;
window.runPipeline = runPipeline;
window.repairPathways = repairPathways;
window.initQueryDropdown = initQueryDropdown;
```

**Step 4: Commit**

```bash
git add static/pipeline_controls.js
git commit -m "feat: add query dropdown to pipeline controls"
```

---

## Task 7: Add Query Dropdown to HTML

**Files:**
- Modify: `templates/index.html` (find pipeline controls section)
- OR if pipeline controls are in visualizer: Check `visualizer.py`

**Step 1: Find where pipeline controls HTML lives**

Search for "pipeline-mode" in templates or visualizer.py

**Step 2: Add query dropdown**

Add this HTML after the existing pipeline controls:

```html
<div class="control-group">
    <label for="pipeline-query">Query Filter:</label>
    <select id="pipeline-query">
        <option value="">All queries</option>
        <!-- Populated dynamically -->
    </select>
</div>
```

**Step 3: Commit**

```bash
git add templates/index.html  # or visualizer.py
git commit -m "feat: add query dropdown UI element"
```

---

## Task 8: Test Query-Specific Pipeline

**Step 1: Start the server**

```bash
python app.py
```

**Step 2: Test /api/queries endpoint**

```bash
curl http://127.0.0.1:5000/api/queries | jq
```

Expected: List of queries with counts

**Step 3: Test pipeline with query filter**

```bash
curl -X POST http://127.0.0.1:5000/api/pipeline/run \
  -H "Content-Type: application/json" \
  -d '{"mode": "full", "query": "ATXN3"}'
```

Expected:
```json
{"message": "Pipeline started", "mode": "full", "step": 1, "query": "ATXN3"}
```

**Step 4: Monitor pipeline status**

```bash
curl http://127.0.0.1:5000/api/pipeline/status | jq
```

Expected: See filtering message in logs

**Step 5: Commit test verification**

```bash
git add -A
git commit -m "test: verify query-specific pipeline works"
```

---

## Task 9: Update PIPELINE_STATUS to Include Query Info

**Files:**
- Modify: `app.py` (PIPELINE_STATUS global and status endpoint)

**Step 1: Find PIPELINE_STATUS definition**

Search for `PIPELINE_STATUS = ` in app.py

**Step 2: Add query field**

Update the definition to include query:

```python
PIPELINE_STATUS = {
    "is_running": False,
    "current_step": None,
    "logs": [],
    "error": None,
    "query_filter": None  # NEW
}
```

**Step 3: Update run_pipeline_task to set query_filter**

In run_pipeline_task, after setting is_running = True:

```python
PIPELINE_STATUS["query_filter"] = query_filter
```

And when complete:

```python
PIPELINE_STATUS["query_filter"] = None
```

**Step 4: Commit**

```bash
git add app.py
git commit -m "feat: track query filter in pipeline status"
```

---

## Task 10: Commit All Backend Changes

**Step 1: Verify all files modified**

```bash
git status
```

**Step 2: Create summary commit if needed**

```bash
git add -A
git commit -m "feat: complete backend for query-specific pipeline runs

- Add /api/queries endpoint
- Add query parameter to /api/pipeline/run
- Update Steps 2-4 to accept interaction_ids filter
- Update pipeline controls UI with query dropdown
- Track query filter in pipeline status"
```

---

## Task 11: Design Multi-Pathway Node IDs (Frontend Prep)

**Files:**
- Read: `static/visualizer.js` - understand current node structure

**Step 1: Document current node ID format**

Current: `node.id = proteinSymbol` (e.g., "TBP")

**Step 2: Design new compound ID format**

New: `node.id = proteinSymbol__pw_pathwayId` (e.g., "TBP__pw_123")

Helper functions needed:
```javascript
function makeProteinNodeId(proteinSymbol, pathwayId) {
    return `${proteinSymbol}__pw_${pathwayId}`;
}

function parseProteinNodeId(nodeId) {
    const match = nodeId.match(/^(.+)__pw_(\d+)$/);
    if (match) {
        return { proteinId: match[1], pathwayId: parseInt(match[2]) };
    }
    return { proteinId: nodeId, pathwayId: null };
}

function isProteinNode(nodeId) {
    return nodeId.includes('__pw_');
}
```

**Step 3: Commit design notes**

This is a design step - no code changes yet.

---

## Task 12: Add Helper Functions to Visualizer

**Files:**
- Modify: `static/visualizer.js` (add near top, around line 60)

**Step 1: Add compound ID helper functions**

Add after the global variable declarations (around line 60):

```javascript
// === COMPOUND NODE ID HELPERS ===
// Proteins can appear in multiple pathways, so we use compound IDs: "PROTEIN__pw_123"

function makeProteinNodeId(proteinSymbol, pathwayId) {
    return `${proteinSymbol}__pw_${pathwayId}`;
}

function parseProteinNodeId(nodeId) {
    const match = nodeId.match(/^(.+)__pw_(\d+)$/);
    if (match) {
        return { proteinId: match[1], pathwayId: parseInt(match[2]) };
    }
    // Legacy format or pathway node
    return { proteinId: nodeId, pathwayId: null };
}

function isCompoundProteinNode(nodeId) {
    return nodeId.includes('__pw_');
}

function getProteinIdFromNode(nodeId) {
    return parseProteinNodeId(nodeId).proteinId;
}

function getPathwayIdFromNode(nodeId) {
    return parseProteinNodeId(nodeId).pathwayId;
}

// Count how many pathways a protein appears in
function countPathwaysForProtein(proteinSymbol) {
    let count = 0;
    if (window.pathwayToInteractors) {
        window.pathwayToInteractors.forEach((proteins, pathwayId) => {
            if (proteins.has(proteinSymbol)) count++;
        });
    }
    return count;
}
```

**Step 2: Commit**

```bash
git add static/visualizer.js
git commit -m "feat: add compound node ID helpers for multi-pathway proteins"
```

---

## Task 13: Update Modal to Accept Pathway Context

**Files:**
- Modify: `static/visualizer.js` - find `showAggregatedInteractionsModal`

**Step 1: Find the modal function**

Search for `function showAggregatedInteractionsModal`

**Step 2: Add contextPathwayId parameter**

Update the function signature:

```javascript
function showAggregatedInteractionsModal(proteinId, contextPathwayId = null) {
    // ... existing code ...

    // If we have a pathway context, highlight matching interactions
    if (contextPathwayId !== null) {
        // Group interactions by pathway and highlight the context pathway
        // This will be implemented in Task 14
    }
}
```

**Step 3: Update callers to pass pathway context**

Find where this function is called (e.g., in node click handlers) and update to pass context:

```javascript
// When clicking a protein node in a pathway
const parsed = parseProteinNodeId(node.id);
showAggregatedInteractionsModal(parsed.proteinId, parsed.pathwayId);
```

**Step 4: Commit**

```bash
git add static/visualizer.js
git commit -m "feat: modal accepts pathway context for highlighting"
```

---

## Task 14: Implement Modal Pathway Grouping

**Files:**
- Modify: `static/visualizer.js` - inside `showAggregatedInteractionsModal`

**Step 1: Add grouping logic**

Inside the modal function, add interaction grouping:

```javascript
function groupInteractionsByPathway(interactions) {
    const groups = new Map();

    interactions.forEach(interaction => {
        // Get pathway from step3_finalized_pathway or functions
        const pathwayName = interaction.step3_finalized_pathway ||
                           interaction.data?.step3_finalized_pathway ||
                           'Unassigned';

        if (!groups.has(pathwayName)) {
            groups.set(pathwayName, []);
        }
        groups.get(pathwayName).push(interaction);
    });

    return groups;
}
```

**Step 2: Render grouped interactions with highlighting**

```javascript
function renderGroupedInteractions(groups, contextPathwayName) {
    const container = document.createElement('div');
    container.className = 'grouped-interactions';

    // Sort: context pathway first, then alphabetically
    const sortedKeys = Array.from(groups.keys()).sort((a, b) => {
        if (a === contextPathwayName) return -1;
        if (b === contextPathwayName) return 1;
        return a.localeCompare(b);
    });

    sortedKeys.forEach(pathwayName => {
        const interactions = groups.get(pathwayName);
        const isContext = (pathwayName === contextPathwayName);

        const groupDiv = document.createElement('div');
        groupDiv.className = `interaction-group ${isContext ? 'highlighted' : 'dimmed'}`;

        // Header
        const header = document.createElement('div');
        header.className = 'group-header';
        header.style.background = isContext ? '#3b82f6' : '#6b7280';
        header.style.color = 'white';
        header.style.padding = '8px 12px';
        header.style.cursor = 'pointer';
        header.style.borderRadius = '4px';
        header.style.marginBottom = '4px';
        header.innerHTML = `${pathwayName} (${interactions.length})`;

        // Content (collapsed if not context)
        const content = document.createElement('div');
        content.className = 'group-content';
        content.style.display = isContext ? 'block' : 'none';

        // ... render individual interactions into content ...

        // Toggle on header click
        header.onclick = () => {
            content.style.display = content.style.display === 'none' ? 'block' : 'none';
        };

        groupDiv.appendChild(header);
        groupDiv.appendChild(content);
        container.appendChild(groupDiv);
    });

    return container;
}
```

**Step 3: Commit**

```bash
git add static/visualizer.js
git commit -m "feat: group modal interactions by pathway with highlighting"
```

---

## Task 15: Add Multi-Pathway Badge to Protein Nodes

**Files:**
- Modify: `static/visualizer.js` - find protein node rendering

**Step 1: Find where protein nodes are rendered**

Search for where nodes are appended to SVG with protein styling

**Step 2: Add badge for multi-pathway proteins**

After rendering the main node circle and label:

```javascript
// Add multi-pathway badge if protein appears in multiple pathways
const pathwayCount = countPathwaysForProtein(node.proteinId || node.id);
if (pathwayCount > 1) {
    nodeGroup.append('circle')
        .attr('class', 'multi-pathway-badge-bg')
        .attr('cx', nodeRadius - 5)
        .attr('cy', -nodeRadius + 5)
        .attr('r', 10)
        .attr('fill', '#7c3aed');

    nodeGroup.append('text')
        .attr('class', 'multi-pathway-badge')
        .attr('x', nodeRadius - 5)
        .attr('y', -nodeRadius + 9)
        .attr('text-anchor', 'middle')
        .attr('fill', 'white')
        .attr('font-size', '10px')
        .attr('font-weight', 'bold')
        .text(`${pathwayCount}`);
}
```

**Step 3: Add CSS styles**

In `static/viz-styles.css`:

```css
.multi-pathway-badge-bg {
    pointer-events: none;
}

.multi-pathway-badge {
    pointer-events: none;
    user-select: none;
}
```

**Step 4: Commit**

```bash
git add static/visualizer.js static/viz-styles.css
git commit -m "feat: add multi-pathway badge to protein nodes"
```

---

## Task 16: Update Card View for Multi-Pathway Proteins

**Files:**
- Modify: `static/card_view.js`

**Step 1: Find card rendering function**

Search for where cards are created/rendered

**Step 2: Add "Also in" indicator**

When rendering a protein card:

```javascript
function renderProteinCard(protein, currentPathway) {
    // Get all pathways this protein appears in
    const allPathways = getPathwaysForProtein(protein.id);
    const otherPathways = allPathways.filter(p => p.id !== currentPathway.id);

    const card = document.createElement('div');
    card.className = 'protein-card';

    // ... existing card content ...

    // Add "Also in" indicator
    if (otherPathways.length > 0) {
        const alsoIn = document.createElement('div');
        alsoIn.className = 'card-also-in';
        alsoIn.textContent = `Also in: ${otherPathways.map(p => p.name).join(', ')}`;
        card.appendChild(alsoIn);
    }

    return card;
}
```

**Step 3: Add helper function**

```javascript
function getPathwaysForProtein(proteinId) {
    const pathways = [];
    if (window.pathwayToInteractors) {
        window.pathwayToInteractors.forEach((proteins, pathwayId) => {
            if (proteins.has(proteinId)) {
                const pathway = window.allPathwaysData?.find(p => p.id === pathwayId);
                if (pathway) pathways.push(pathway);
            }
        });
    }
    return pathways;
}
```

**Step 4: Add CSS styles**

```css
.card-also-in {
    font-size: 11px;
    color: #6b7280;
    font-style: italic;
    margin-top: 4px;
    padding-top: 4px;
    border-top: 1px solid #e5e7eb;
}
```

**Step 5: Commit**

```bash
git add static/card_view.js static/viz-styles.css
git commit -m "feat: add 'Also in' indicator to protein cards"
```

---

## Task 17: Integration Testing

**Step 1: Start server**

```bash
python app.py
```

**Step 2: Test Case A - Query-specific pipeline**

1. Open browser to http://127.0.0.1:5000
2. Open pipeline controls
3. Verify query dropdown shows available queries
4. Select a specific query
5. Run pipeline
6. Verify logs show filtering message

**Step 3: Test Case B - Multi-pathway protein**

1. Find a protein that appears in multiple pathways (check database)
2. Expand both pathways in graph view
3. Verify protein appears in both with badge
4. Click protein under Pathway A
5. Verify modal shows Pathway A highlighted
6. Verify other pathways are collapsed/dimmed

**Step 4: Test Case C - Card view**

1. Switch to card view
2. Find protein in multiple pathways
3. Verify "Also in" text appears

**Step 5: Commit test notes**

```bash
git add -A
git commit -m "test: complete integration testing for Session 4"
```

---

## Task 18: Final Commit and Documentation

**Step 1: Update SESSION4_COMPLETE.md**

Create documentation file summarizing changes.

**Step 2: Final commit**

```bash
git add -A
git commit -m "docs: add Session 4 completion notes"
```

---

## Summary

| Task | Description | Files |
|------|-------------|-------|
| 1-2 | /api/queries endpoint | app.py |
| 3-5 | Pipeline query filter (Steps 2-4) | step2, step3, step4 |
| 6-7 | Frontend query dropdown | pipeline_controls.js, HTML |
| 8-10 | Testing & status tracking | app.py |
| 11-13 | Compound node ID helpers | visualizer.js |
| 14 | Modal pathway grouping | visualizer.js |
| 15 | Multi-pathway badge | visualizer.js, CSS |
| 16 | Card view "Also in" | card_view.js, CSS |
| 17-18 | Integration testing | all |

**Total: 18 tasks, ~650 lines of changes**
