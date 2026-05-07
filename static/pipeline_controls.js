
// Pipeline Control Logic
function updatePipelineUI() {
    const mode = document.getElementById('pipeline-mode').value;
    const stepSelect = document.getElementById('pipeline-step');
    stepSelect.disabled = (mode === 'full');
}

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

async function pollPipelineStatus() {
    const statusDiv = document.getElementById('pipeline-status');

    const interval = setInterval(async () => {
        try {
            const res = await fetch('/api/pipeline/status');
            const status = await res.json();

            if (status.is_running) {
                statusDiv.innerText = `[RUNNING] ${status.current_step}\n\nLast Log: ${status.logs[status.logs.length - 1]}`;
            } else {
                clearInterval(interval);
                if (status.error) {
                    statusDiv.innerText = `[FAILED] Error: ${status.error}`;
                } else {
                    statusDiv.innerText = `[COMPLETE] Pipeline finished successfully.`;
                }
            }
        } catch (e) {
            console.error("Polling error", e);
        }
    }, 2000);
}

async function repairPathways() {
    const statusDiv = document.getElementById('pipeline-status');
    statusDiv.style.display = 'block';

    // Try multiple methods to detect the protein
    let protein = null;

    // Method 1: URL path (e.g., /api/visualize/ATXN3)
    const urlPath = window.location.pathname;
    const apiMatch = urlPath.match(/\/api\/visualize\/(\w+)/);
    if (apiMatch) protein = apiMatch[1];

    // Method 2: URL path without /api/ (e.g., /visualize/ATXN3)
    if (!protein) {
        const vizMatch = urlPath.match(/\/visualize\/(\w+)/);
        if (vizMatch) protein = vizMatch[1];
    }

    // Method 3: Query string parameter (e.g., ?protein=ATXN3)
    if (!protein) {
        const urlParams = new URLSearchParams(window.location.search);
        protein = urlParams.get('protein');
    }

    // Method 4: Get from SNAP global if available
    if (!protein && window.SNAP && window.SNAP.main) {
        protein = window.SNAP.main;
    }

    // Method 5: Try to get from page title
    if (!protein) {
        const titleMatch = document.title.match(/(\w+)\s*[-–]/);
        if (titleMatch) protein = titleMatch[1];
    }

    // Method 6: Check localStorage for last queried protein
    if (!protein) {
        protein = localStorage.getItem('lastQueriedProtein');
    }

    // If still no protein, prompt the user to enter one
    if (!protein) {
        protein = prompt(
            "Enter the protein name to repair pathways for:\n\n" +
            "(e.g., ATXN3, TP53, VCP)"
        );
        if (!protein || protein.trim() === '') {
            statusDiv.innerText = "Repair cancelled - no protein specified.";
            return;
        }
        protein = protein.trim().toUpperCase();
    }

    // Confirm with user
    const clearExisting = confirm(
        `Repair pathways for ${protein}?\n\n` +
        `Click OK to clear existing pathway data and re-run assignment.\n` +
        `Click Cancel to only fill in missing assignments.`
    );

    statusDiv.innerText = `Repairing pathways for ${protein}...\nThis may take a few minutes.`;

    try {
        const response = await fetch(`/api/repair-pathways/${protein}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ clear_existing: clearExisting })
        });

        const data = await response.json();

        if (response.ok && data.success) {
            statusDiv.innerHTML = `<span style="color: #4caf50;">✓ Repair Complete for ${protein}</span>\n\n` +
                `Total Interactions: ${data.total_interactions}\n` +
                `New Step 2 Assignments: ${data.new_assignments.step2}\n` +
                `New Step 3 Assignments: ${data.new_assignments.step3}\n` +
                `New Pathway Links: ${data.new_assignments.pathway_links}\n\n` +
                `<span style="color: #aaa;">Refresh the page to see updated pathways.</span>`;
        } else {
            statusDiv.innerHTML = `<span style="color: #f44336;">✗ Repair Failed</span>\n${data.error || 'Unknown error'}`;
        }
    } catch (e) {
        statusDiv.innerText = "Error: " + e.message;
    }
}

// Attach to window for HTML access
window.updatePipelineUI = updatePipelineUI;
window.runPipeline = runPipeline;
window.repairPathways = repairPathways;
window.initQueryDropdown = initQueryDropdown;
