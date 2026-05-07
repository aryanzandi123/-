# Session 4 Design: Query-Specific Pipeline + UI Integration

> **Date:** 2025-01-01
> **Goal:** Enable query-specific pathway pipeline runs + update visualization for multi-pathway proteins

---

## Overview

**Problem:** After Session 1's per-function pathway assignment, the same protein can appear in multiple pathways. The UI must handle this, and users need the ability to run the pipeline for specific queries.

**Solution:**
1. Add query filter to pipeline endpoint
2. Update graph to show proteins in multiple pathway locations
3. Update card view with multi-pathway grouping
4. Update modal with context-aware pathway highlighting

---

## Task 1: Query-Specific Pipeline Runs

### Backend Changes

**New Endpoint: `GET /api/queries`**
```python
@app.route('/api/queries', methods=['GET'])
def get_queries():
    """Return list of unique queries with interaction counts."""
    results = db.session.query(
        Interaction.discovered_in_query,
        func.count(Interaction.id)
    ).group_by(Interaction.discovered_in_query).all()

    return jsonify({
        "queries": [
            {"name": q, "interaction_count": c}
            for q, c in results if q
        ]
    })
```

**Modified Endpoint: `POST /api/pipeline/run`**
```python
@app.route('/api/pipeline/run', methods=['POST'])
def run_pipeline():
    data = request.json or {}
    mode = data.get('mode', 'full')
    step = data.get('step', 1)
    query = data.get('query', None)  # NEW: None = all queries

    # Pass query filter to pipeline
    thread = threading.Thread(
        target=run_pathway_pipeline,
        args=(mode, step),
        kwargs={'query_filter': query}
    )
    thread.start()

    return jsonify({
        "message": "Pipeline started",
        "mode": mode,
        "step": step,
        "query": query or "all"
    })
```

**File: `scripts/pathway_v2/run_all_v2.py`**
```python
def run_pipeline(start_step=1, query_filter=None):
    """Run pathway pipeline, optionally filtered to specific query."""

    # Get interactions to process
    if query_filter:
        interactions = Interaction.query.filter_by(
            discovered_in_query=query_filter
        ).all()
        interaction_ids = [i.id for i in interactions]
        print(f"[Pipeline] Processing {len(interactions)} interactions from query: {query_filter}")
    else:
        interactions = Interaction.query.all()
        interaction_ids = None  # None = process all
        print(f"[Pipeline] Processing ALL {len(interactions)} interactions")

    # Pass to each step
    if start_step <= 2:
        step2_assign_initial_terms.run(interaction_ids=interaction_ids)
    if start_step <= 3:
        step3_refine_pathways.run(interaction_ids=interaction_ids)
    # ... etc
```

### Frontend Changes

**File: `static/pipeline_controls.js`**
```javascript
// Add query dropdown
async function initQueryDropdown() {
    const response = await fetch('/api/queries');
    const data = await response.json();

    const select = document.getElementById('pipeline-query');
    select.innerHTML = '<option value="">All queries</option>';

    data.queries.forEach(q => {
        const option = document.createElement('option');
        option.value = q.name;
        option.textContent = `${q.name} (${q.interaction_count} interactions)`;
        select.appendChild(option);
    });
}

// Modified runPipeline()
async function runPipeline() {
    const mode = document.getElementById('pipeline-mode').value;
    const step = document.getElementById('pipeline-step').value;
    const query = document.getElementById('pipeline-query').value || null;

    const response = await fetch('/api/pipeline/run', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ mode, step, query })
    });

    // ... rest of function
}
```

---

## Task 2: Query Flow Consistency Audit

### Issues to Fix

| Issue | Location | Fix |
|-------|----------|-----|
| Pipeline runs on ALL interactions after query | `runner.py` stage 11 | Pass `query_filter=protein_symbol` |
| No pipeline lock per query | `app.py` | Add mutex dict |
| Status format inconsistent | Various | Standardize format |

### Pipeline Lock

```python
# app.py
pipeline_locks = {}

def get_pipeline_lock(query_name):
    if query_name not in pipeline_locks:
        pipeline_locks[query_name] = threading.Lock()
    return pipeline_locks[query_name]

# In run_pathway_pipeline:
def run_pathway_pipeline(mode, step, query_filter=None):
    lock_key = query_filter or '__all__'
    with get_pipeline_lock(lock_key):
        # Run pipeline - prevents concurrent runs for same query
        _run_pipeline_internal(mode, step, query_filter)
```

### Status Standardization

All status endpoints return:
```json
{
    "status": "processing|complete|error|cancelled",
    "progress": {
        "current": 5,
        "total": 10,
        "text": "Step 4: Building hierarchy..."
    }
}
```

---

## Task 3: Visualizer Updates (Graph View)

### Multi-Instance Protein Nodes

**Compound Node IDs:**
```javascript
// Instead of: { id: 'TBP' }
// Now: { id: 'TBP__pw_123', proteinId: 'TBP', pathwayId: 123 }

function createProteinNode(proteinId, pathwayId, pathwayName) {
    return {
        id: `${proteinId}__pw_${pathwayId}`,
        proteinId: proteinId,
        pathwayId: pathwayId,
        pathwayName: pathwayName,
        type: 'protein',
        instanceCount: countPathwaysForProtein(proteinId)
    };
}
```

**Multi-Pathway Badge:**
```javascript
function renderProteinNode(node) {
    // ... existing node rendering ...

    // Add badge if protein appears in multiple pathways
    if (node.instanceCount > 1) {
        nodeGroup.append('text')
            .attr('class', 'multi-pathway-badge')
            .attr('x', nodeRadius - 5)
            .attr('y', -nodeRadius + 10)
            .text(`+${node.instanceCount - 1}`);
    }
}
```

**Pathway Expansion:**
```javascript
function expandPathway(pathwayId) {
    // Get interactions for this specific pathway
    const pathwayInteractions = getInteractionsForPathway(pathwayId);

    pathwayInteractions.forEach(interaction => {
        const targetProtein = interaction.target;
        const nodeId = `${targetProtein}__pw_${pathwayId}`;

        if (!nodeExists(nodeId)) {
            const node = createProteinNode(targetProtein, pathwayId, getPathwayName(pathwayId));
            addNodeToGraph(node);
            addLinkToGraph(pathwayId, nodeId, interaction);
        }
    });
}
```

---

## Task 4: Card View Updates

### Card Data Structure

```javascript
function buildCardData(proteinId, pathwayId) {
    const allPathways = getPathwaysForProtein(proteinId);
    const interactionsHere = getInteractionsForProteinInPathway(proteinId, pathwayId);

    return {
        proteinId: proteinId,
        pathwayId: pathwayId,
        pathwayName: getPathwayName(pathwayId),
        interactionsInThisPathway: interactionsHere.length,
        totalInteractions: getTotalInteractionsForProtein(proteinId),
        otherPathways: allPathways
            .filter(p => p.id !== pathwayId)
            .map(p => p.name)
    };
}
```

### Card Rendering

```javascript
function renderCard(cardData) {
    const card = d3.create('div').attr('class', 'protein-card');

    // Protein name
    card.append('div')
        .attr('class', 'card-title')
        .text(cardData.proteinId);

    // Pathway context
    card.append('div')
        .attr('class', 'card-pathway')
        .text(cardData.pathwayName);

    // Interaction count
    card.append('div')
        .attr('class', 'card-count')
        .text(`${cardData.interactionsInThisPathway} interaction(s)`);

    // Multi-pathway indicator
    if (cardData.otherPathways.length > 0) {
        card.append('div')
            .attr('class', 'card-multi-pathway')
            .text(`Also in: ${cardData.otherPathways.join(', ')}`);
    }

    return card;
}
```

---

## Task 5: Modal Updates (Blue/Grey Pathway Labels)

### Context-Aware Modal

```javascript
function showAggregatedInteractionsModal(proteinId, contextPathwayId = null) {
    const allInteractions = getInteractionsForProtein(proteinId);
    const grouped = groupInteractionsByPathway(allInteractions);

    const modalContent = document.getElementById('modal-content');
    modalContent.innerHTML = '';

    // Sort: current context first, then alphabetically
    grouped.sort((a, b) => {
        if (a.pathwayId === contextPathwayId) return -1;
        if (b.pathwayId === contextPathwayId) return 1;
        return a.pathwayName.localeCompare(b.pathwayName);
    });

    grouped.forEach(group => {
        const isCurrentContext = (group.pathwayId === contextPathwayId);
        const groupDiv = renderInteractionGroup(group, isCurrentContext, contextPathwayId !== null);
        modalContent.appendChild(groupDiv);
    });
}

function renderInteractionGroup(group, highlighted, hasContext) {
    const div = document.createElement('div');
    div.className = `interaction-group ${highlighted ? 'highlighted' : (hasContext ? 'dimmed' : '')}`;

    // Header
    const header = document.createElement('div');
    header.className = 'group-header';
    header.style.background = highlighted ? '#3b82f6' : '#6b7280';
    header.style.color = 'white';
    header.innerHTML = `
        ${group.pathwayName} (${group.interactions.length} interaction${group.interactions.length > 1 ? 's' : ''})
        ${!highlighted && hasContext ? '<span class="navigate-link">→ View in pathway</span>' : ''}
    `;

    // Click to navigate
    if (!highlighted && hasContext) {
        header.querySelector('.navigate-link').onclick = () => {
            navigateToPathway(group.pathwayId);
            closeModal();
        };
    }

    div.appendChild(header);

    // Interactions (collapsed if dimmed)
    const interactionsDiv = document.createElement('div');
    interactionsDiv.className = 'group-interactions';
    if (!highlighted && hasContext) {
        interactionsDiv.style.display = 'none';
    }

    group.interactions.forEach(interaction => {
        interactionsDiv.appendChild(renderInteraction(interaction));
    });

    div.appendChild(interactionsDiv);

    // Toggle expand/collapse
    header.onclick = () => {
        const isCollapsed = interactionsDiv.style.display === 'none';
        interactionsDiv.style.display = isCollapsed ? 'block' : 'none';
    };

    return div;
}
```

---

## Task 6: Integration Test Plan

### Test Case A: Fresh Query + Query-Specific Pipeline

1. Clear existing data for test protein
2. Run query for "TEST_PROTEIN"
3. Verify: `discovered_in_query = "TEST_PROTEIN"` for all interactions
4. Open pipeline controls
5. Verify: Dropdown shows "TEST_PROTEIN (N interactions)"
6. Select "TEST_PROTEIN" and run pipeline
7. Verify: Only that query's interactions processed
8. Verify: PathwayInteraction records created
9. Verify: Visualization shows correct pathways

### Test Case B: Multi-Pathway Protein

1. Find protein in 2+ pathways (e.g., TBP)
2. Graph View:
   - Expand both pathways
   - Verify: TBP appears in each
   - Verify: Badge shows "2 pathways"
3. Click TBP under Pathway A:
   - Verify: Modal shows Pathway A highlighted (blue)
   - Verify: Pathway B dimmed (grey, collapsed)
   - Click grey label → navigates to Pathway B

### Test Case C: Pipeline Re-run Idempotency

1. Run pipeline for "ATXN3"
2. Note pathway counts
3. Run again
4. Verify: No duplicates
5. Verify: Step 7 passes

### Verification SQL

```sql
-- Orphaned interactions (should be 0)
SELECT COUNT(*) FROM interactions i
WHERE NOT EXISTS (
    SELECT 1 FROM pathway_interactions pi WHERE pi.interaction_id = i.id
);

-- Duplicate PathwayInteraction (should be 0)
SELECT pathway_id, interaction_id, COUNT(*)
FROM pathway_interactions
GROUP BY pathway_id, interaction_id
HAVING COUNT(*) > 1;
```

---

## Files to Modify

| File | Changes |
|------|---------|
| `app.py` | Add `/api/queries`, add `query` param to pipeline endpoint, add locks |
| `scripts/pathway_v2/run_all_v2.py` | Accept `query_filter`, pass to steps |
| `scripts/pathway_v2/step2_assign_initial_terms.py` | Filter by interaction IDs |
| `scripts/pathway_v2/step3_refine_pathways.py` | Filter by interaction IDs |
| `scripts/pathway_v2/step4_build_hierarchy_backwards.py` | Filter by interaction IDs |
| `runner.py` | Pass query filter after db_sync |
| `static/pipeline_controls.js` | Query dropdown, fetch `/api/queries` |
| `static/visualizer.js` | Compound node IDs, badges, modal context |
| `static/card_view.js` | Multi-pathway grouping, "Also in" |
| `static/viz-styles.css` | Badge styles, highlighted/dimmed groups |

---

## Implementation Order

1. **Backend** (Tasks 1-2): Query filtering, endpoints, locks
2. **Graph** (Task 3): Multi-instance nodes, expansion
3. **Cards** (Task 4): Grouped cards, indicators
4. **Modal** (Task 5): Context highlighting, navigation
5. **Testing** (Task 6): Full integration tests

---

*Design complete. Ready for implementation.*
