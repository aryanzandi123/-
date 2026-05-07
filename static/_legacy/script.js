// ============================================================================
// SECURITY UTILITIES
// ============================================================================

/**
 * Escape HTML special characters to prevent XSS
 * @pure
 */
function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

// ============================================================================
// THEME (dark mode)
// ============================================================================
//
// Three-state toggle: 'auto' (follow prefers-color-scheme), 'light', 'dark'.
// Persisted in localStorage under 'propaths-theme'. If absent, defaults to
// 'auto' which means CSS's @media (prefers-color-scheme: dark) decides.
// When 'light' or 'dark' is set, we write data-theme on <html> which the
// CSS overrides read with higher specificity than the @media block.
const THEME_STORAGE_KEY = 'propaths-theme';

/**
 * Apply a theme mode ('auto' | 'light' | 'dark'). Returns the applied mode.
 */
function applyTheme(mode) {
  const normalized = ['auto', 'light', 'dark'].includes(mode) ? mode : 'auto';
  const html = document.documentElement;
  if (normalized === 'auto') {
    html.removeAttribute('data-theme');
  } else {
    html.setAttribute('data-theme', normalized);
  }
  try { localStorage.setItem(THEME_STORAGE_KEY, normalized); } catch {}
  return normalized;
}

/** Cycle through auto → light → dark → auto. */
function cycleTheme() {
  const current = localStorage.getItem(THEME_STORAGE_KEY) || 'auto';
  const next = current === 'auto' ? 'light' : current === 'light' ? 'dark' : 'auto';
  return applyTheme(next);
}

// Apply persisted theme as early as possible to avoid flash-of-wrong-theme.
(function initTheme() {
  try {
    const stored = localStorage.getItem(THEME_STORAGE_KEY);
    if (stored && stored !== 'auto') {
      document.documentElement.setAttribute('data-theme', stored);
    }
  } catch {}
})();

// Expose a simple global for any UI button that wants to toggle.
window.ProPathsTheme = { apply: applyTheme, cycle: cycleTheme };

// ============================================================================
// UTILITY FUNCTIONS - Fetch with timeout and retry
// ============================================================================

/**
 * Fetch with timeout to prevent hanging requests
 * FIXED: Added 30s timeout for all HTTP requests
 */
async function fetchWithTimeout(url, options = {}, timeout = 30000) {
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), timeout);

  try {
    const response = await fetch(url, {
      ...options,
      signal: controller.signal
    });
    clearTimeout(id);
    return response;
  } catch (error) {
    clearTimeout(id);
    if (error.name === 'AbortError') {
      throw new Error('Request timeout');
    }
    throw error;
  }
}

/**
 * Fetch with exponential backoff retry
 * FIXED: Added retry logic for failed status checks
 */
async function fetchWithRetry(url, options = {}, maxRetries = 3) {
  for (let i = 0; i < maxRetries; i++) {
    try {
      const response = await fetchWithTimeout(url, options);
      return response;
    } catch (error) {
      if (i === maxRetries - 1) throw error;

      // Exponential backoff: 1s, 2s, 4s
      const delay = 1000 * Math.pow(2, i);
      console.log(`[Fetch] Retry ${i + 1}/${maxRetries} after ${delay}ms for ${url}`);
      await new Promise(resolve => setTimeout(resolve, delay));
    }
  }
}

// ============================================================================
// FUNCTIONAL CORE - Pure State Management (No Side Effects)
// ============================================================================

/**
 * Calculate percentage from current/total progress
 * @pure
 */
function calculatePercent(current, total) {
  if (typeof current !== 'number' || typeof total !== 'number') return 0;
  if (total <= 0) return 0;
  if (current >= total) return 100;
  return Math.round((current / total) * 100);
}

/**
 * Format job status into display metadata
 * @pure
 */
function formatJobStatus(status) {
  const statusMap = {
    processing: { color: '#3b82f6', icon: '⏳', text: 'Running' },
    complete: { color: '#10b981', icon: '✓', text: 'Complete' },
    error: { color: '#ef4444', icon: '✕', text: 'Failed' },
    cancelled: { color: '#6b7280', icon: '⊘', text: 'Cancelled' }
  };
  return statusMap[status] || statusMap.processing;
}

/**
 * Create new job state object
 * @pure
 */
function createJobState(protein, config) {
  return {
    protein,
    status: 'processing',
    progress: {
      current: 0,
      total: 100,
      text: 'Initializing...'
    },
    config,
    startTime: Date.now()
  };
}

/**
 * Update job progress (returns new object)
 * @pure
 */
function updateJobProgress(job, progressData) {
  return {
    ...job,
    progress: {
      current: progressData.current || job.progress.current,
      total: progressData.total || job.progress.total,
      text: progressData.text || job.progress.text
    }
  };
}

/**
 * Mark job as complete (returns new object)
 * @pure
 */
function markJobComplete(job) {
  return {
    ...job,
    status: 'complete',
    progress: {
      current: 100,
      total: 100,
      text: 'Complete!'
    }
  };
}

/**
 * Mark job as error (returns new object)
 * @pure
 */
function markJobError(job, errorText) {
  return {
    ...job,
    status: 'error',
    progress: {
      ...job.progress,
      text: errorText || 'Error occurred'
    }
  };
}

/**
 * Mark job as cancelled (returns new object)
 * @pure
 */
function markJobCancelled(job) {
  return {
    ...job,
    status: 'cancelled',
    progress: {
      ...job.progress,
      text: 'Cancelled by user'
    }
  };
}

/**
 * Extract config from form inputs
 * @pure (reads DOM but doesn't mutate)
 */
function readConfigFromInputs() {
  // Minimal settings — only mode, preset values, and iterations are in the form.
  // Everything else uses server-side defaults from environment variables.
  const pipelineModeSelect = document.getElementById('pipeline-mode-select');
  const discoveryIterationsInput = document.getElementById('discovery-iterations');
  const interactorRoundsInput = document.getElementById('interactor-rounds');
  const functionRoundsInput = document.getElementById('function-rounds');

  const config = {
    pipeline_mode: pipelineModeSelect ? pipelineModeSelect.value : 'iterative',
    discovery_iterations: discoveryIterationsInput ? parseInt(discoveryIterationsInput.value) || 5 : 5,
    interactor_rounds: interactorRoundsInput ? parseInt(interactorRoundsInput.value) || 3 : 3,
    function_rounds: functionRoundsInput ? parseInt(functionRoundsInput.value) || 3 : 3,
  };

  // Skip-step checkboxes
  const skipFlags = [
    ['skip_arrow_validation', 'skip-arrow-validation'],
    ['skip_direct_links', 'skip-direct-links'],
    ['skip_validation', 'skip-validation'],
    ['skip_deduplicator', 'skip-deduplicator'],
    ['skip_interaction_metadata', 'skip-interaction-metadata'],
    ['skip_arrow_determination', 'skip-arrow-determination'],
    ['skip_schema_validation', 'skip-schema-validation'],

    ['skip_citation_verification', 'skip-citation-verification'],
    ['skip_finalize_metadata', 'skip-finalize-metadata'],
  ];
  for (const [key, id] of skipFlags) {
    const el = document.getElementById(id);
    if (el && el.checked) config[key] = true;
  }

  // Quick pathway assignment toggle (P3.4).
  //
  // Quick assign is the default mode (DEFAULT_QUICK_PATHWAY_ASSIGNMENT=true
  // in the backend env). Previously this code only sent the field when
  // the checkbox was checked, so unchecking the box was indistinguishable
  // from "default" — backend fell back to the env default (true) and
  // ran quick assign even when the user explicitly opted out. Now we
  // ALWAYS send the field as a real boolean so the user's UI state
  // wins over the env default.
  //
  // If the checkbox doesn't exist yet (legacy template), default to
  // true to match the backend env default.
  const qpa = document.getElementById('quick-pathway-assignment');
  if (qpa) {
    config.quick_pathway_assignment = !!qpa.checked;
  } else {
    config.quick_pathway_assignment = true;
  }

  return config;
}

/**
 * Save config to localStorage
 * @impure (writes to localStorage)
 */
function saveConfigToLocalStorage(config) {
  localStorage.setItem('pipeline_mode', config.pipeline_mode);
  localStorage.setItem('discovery_iterations', config.discovery_iterations);
  localStorage.setItem('interactor_rounds', config.interactor_rounds);
  localStorage.setItem('function_rounds', config.function_rounds);

  // Persist skip flags and quick pathway
  const boolKeys = [
    'skip_arrow_validation', 'skip_direct_links', 'skip_validation',
    'skip_deduplicator', 'skip_interaction_metadata', 'skip_arrow_determination',
    'skip_schema_validation', 'skip_citation_verification',
    'skip_finalize_metadata',
    'quick_pathway_assignment',
  ];
  for (const key of boolKeys) {
    localStorage.setItem(key, config[key] ? 'true' : 'false');
  }
}

// ============================================================================
// IMPERATIVE SHELL - DOM Manipulation (Thin I/O Layer)
// ============================================================================

/**
 * Create a job card DOM element
 * @returns {Object} { container, bar, text, percent, removeBtn, cancelBtn }
 */
function createJobCard(protein) {
  const container = document.createElement('div');
  container.className = 'job-card';
  container.id = `job-${protein}`;

  container.innerHTML = `
    <div class="job-header">
      <span class="job-protein">${escapeHtml(protein)}</span>
      <div class="job-actions">
        <button class="job-btn job-remove" title="Remove from tracker (job continues in background)" aria-label="Remove from tracker">
          <span class="job-btn-icon">−</span>
        </button>
        <button class="job-btn job-cancel" title="Cancel job" aria-label="Cancel job">
          <span class="job-btn-icon">✕</span>
        </button>
      </div>
    </div>
    <div class="job-progress-container">
      <div class="job-progress-text">Initializing...</div>
      <div class="job-progress-percent">0%</div>
    </div>
    <div class="job-progress-bar-outer">
      <div class="job-progress-bar-inner" style="transition: width 0.3s ease;"></div>
    </div>
  `;

  return {
    container,
    bar: container.querySelector('.job-progress-bar-inner'),
    text: container.querySelector('.job-progress-text'),
    percent: container.querySelector('.job-progress-percent'),
    removeBtn: container.querySelector('.job-remove'),
    cancelBtn: container.querySelector('.job-cancel')
  };
}

/**
 * Update job card UI with current job state
 */
function updateJobCard(elements, job) {
  if (!elements || !job) return;

  const { bar, text, percent, container } = elements;
  const progressPercent = calculatePercent(job.progress.current, job.progress.total);
  const statusInfo = formatJobStatus(job.status);

  // Update progress bar
  if (bar) {
    bar.style.width = `${progressPercent}%`;
    bar.style.backgroundColor = statusInfo.color;
  }

  // Update text
  if (text) {
    if (job.progress.current && job.progress.total) {
      text.textContent = `Step ${job.progress.current}/${job.progress.total}: ${job.progress.text}`;
    } else {
      text.textContent = job.progress.text;
    }
  }

  // Update percent
  if (percent) {
    percent.textContent = `${progressPercent}%`;
  }

  // Update container color/state
  if (container) {
    container.setAttribute('data-status', job.status);
  }
}

/**
 * Remove job card from DOM with fade animation
 */
function removeJobCard(container, callback) {
  if (!container) {
    if (callback) callback();
    return;
  }

  container.style.opacity = '0';
  container.style.transform = 'translateX(-10px)';

  setTimeout(() => {
    if (container.parentNode) {
      container.parentNode.removeChild(container);
    }
    if (callback) callback();
  }, 300);
}

/**
 * Show status message (for non-job updates)
 */
function showStatusMessage(text) {
  const statusMessage = document.getElementById('status-message');
  if (statusMessage) {
    statusMessage.style.display = 'block';
    statusMessage.textContent = '';
    const p = document.createElement('p');
    p.textContent = text;
    statusMessage.appendChild(p);
  }
}

/**
 * Hide status message
 */
function hideStatusMessage() {
  const statusMessage = document.getElementById('status-message');
  if (statusMessage) {
    statusMessage.style.display = 'none';
  }
}

// ============================================================================
// JOB TRACKER - Multi-Job Orchestration (Composition Layer)
// ============================================================================

class JobTracker {
  constructor(containerId) {
    this.jobs = new Map();           // protein -> job state
    this.intervals = new Map();      // protein -> intervalId
    this.uiElements = new Map();     // protein -> DOM elements
    this.container = document.getElementById(containerId);

    if (!this.container) {
      console.warn(`[JobTracker] Container #${containerId} not found. Creating fallback.`);
      this._createFallbackContainer();
    }
  }

  /**
   * Create fallback container if none exists
   */
  _createFallbackContainer() {
    const statusDisplay = document.getElementById('status-display');
    if (statusDisplay) {
      const container = document.createElement('div');
      container.id = 'job-container';
      container.className = 'job-container';
      statusDisplay.insertBefore(container, statusDisplay.firstChild);
      this.container = container;
    }
  }

  /**
   * Add a new job to tracker and start polling
   */
  addJob(protein, config) {
    // Guard: prevent duplicate jobs
    if (this.jobs.has(protein)) {
      const existingJob = this.jobs.get(protein);
      if (existingJob.status === 'processing') {
        console.warn(`[JobTracker] Job for ${protein} already running`);

        // Show user-friendly warning
        const confirmed = confirm(
          `A query for ${protein} is already running.\n\nCancel the existing job and start a new one?`
        );

        if (confirmed) {
          this.cancelJob(protein);
          // Wait a moment for cleanup
          setTimeout(() => this._addJobInternal(protein, config), 500);
        }
        return;
      }
    }

    this._addJobInternal(protein, config);
  }

  /**
   * Internal method to add job (separated for recursion after cancel)
   */
  _addJobInternal(protein, config) {
    // Create job state
    const job = createJobState(protein, config);
    this.jobs.set(protein, job);

    // Render UI
    this._renderJob(protein);

    // Start polling
    this._startPolling(protein);

    console.log(`[JobTracker] Added job for ${protein}`);
  }

  /**
   * Remove job from tracker (UI only, job continues in background)
   */
  removeFromTracker(protein) {
    console.log(`[JobTracker] Removing ${protein} from tracker (job continues in background)`);

    // Stop polling
    this._stopPolling(protein);

    // Remove UI
    const elements = this.uiElements.get(protein);
    if (elements) {
      removeJobCard(elements.container, () => {
        this.uiElements.delete(protein);
      });
    }

    // Remove from state
    this.jobs.delete(protein);
  }

  /**
   * Cancel job (stops backend job + removes from tracker)
   * FIXED: Stop polling BEFORE cancel request to prevent race condition
   */
  async cancelJob(protein) {
    console.log(`[JobTracker] Cancelling job for ${protein}`);

    const job = this.jobs.get(protein);
    if (!job) {
      console.warn(`[JobTracker] No job found for ${protein}`);
      return;
    }

    // FIXED: Stop polling FIRST to prevent race with completion
    this._stopPolling(protein);

    // Disable cancel button to prevent double-clicks
    const elements = this.uiElements.get(protein);
    if (elements && elements.cancelBtn) {
      elements.cancelBtn.disabled = true;
    }

    try {
      // Send cancel request to backend
      const response = await fetch(`/api/cancel/${encodeURIComponent(protein)}`, {
        method: 'POST'
      });

      if (!response.ok) {
        throw new Error('Cancel request failed');
      }

      // Update state
      const cancelledJob = markJobCancelled(job);
      this.jobs.set(protein, cancelledJob);

      // Update UI
      this._updateJobUI(protein);

      // Remove after delay
      setTimeout(() => {
        this.removeFromTracker(protein);
      }, 2000);

    } catch (error) {
      console.error(`[JobTracker] Failed to cancel ${protein}:`, error);

      // Re-enable cancel button on error
      if (elements && elements.cancelBtn) {
        elements.cancelBtn.disabled = false;
      }

      // Show error in UI
      const errorJob = markJobError(job, 'Failed to cancel job');
      this.jobs.set(protein, errorJob);
      this._updateJobUI(protein);

      // Restart polling on error (cancel failed, job still running)
      this._startPolling(protein);
    }
  }

  /**
   * Update job progress
   */
  updateJob(protein, progressData) {
    const job = this.jobs.get(protein);
    if (!job) return;

    const updatedJob = updateJobProgress(job, progressData);
    this.jobs.set(protein, updatedJob);
    this._updateJobUI(protein);
  }

  /**
   * Mark job as complete
   */
  completeJob(protein) {
    const job = this.jobs.get(protein);
    if (!job) return;

    const completedJob = markJobComplete(job);
    this.jobs.set(protein, completedJob);
    this._updateJobUI(protein);
    this._stopPolling(protein);

    // Navigate to visualization after brief delay
    setTimeout(() => {
      localStorage.setItem('lastQueriedProtein', protein.toUpperCase());
      window.location.href = `/api/visualize/${encodeURIComponent(protein)}?t=${Date.now()}`;
    }, 1000);
  }

  /**
   * Mark job as error
   */
  errorJob(protein, errorText) {
    const job = this.jobs.get(protein);
    if (!job) return;

    const errorJob = markJobError(job, errorText);
    this.jobs.set(protein, errorJob);
    this._updateJobUI(protein);
    this._stopPolling(protein);

    // Auto-remove after delay
    setTimeout(() => {
      this.removeFromTracker(protein);
    }, 5000);
  }

  /**
   * Render job card in UI
   */
  _renderJob(protein) {
    if (!this.container) return;

    const job = this.jobs.get(protein);
    if (!job) return;

    // Create job card
    const elements = createJobCard(protein);
    this.uiElements.set(protein, elements);

    // Wire up event listeners
    elements.removeBtn.onclick = () => this.removeFromTracker(protein);
    elements.cancelBtn.onclick = () => this.cancelJob(protein);

    // Add to DOM
    this.container.appendChild(elements.container);

    // Initial render
    this._updateJobUI(protein);

    // Trigger animation
    setTimeout(() => {
      elements.container.style.opacity = '1';
    }, 10);
  }

  /**
   * Update job UI from state
   */
  _updateJobUI(protein) {
    const job = this.jobs.get(protein);
    const elements = this.uiElements.get(protein);

    if (!job || !elements) return;

    updateJobCard(elements, job);
  }

  /**
   * Start listening for job status via SSE, with polling fallback.
   */
  _startPolling(protein) {
    // Try SSE first
    if (typeof EventSource !== 'undefined') {
      try {
        const es = new EventSource(`/api/stream/${encodeURIComponent(protein)}`);
        // Store so _stopPolling can close it
        this.intervals.set(protein, { type: 'sse', source: es });

        es.onmessage = (event) => {
          const data = JSON.parse(event.data);
          this._handleStatusData(protein, data);
        };

        es.addEventListener('done', (event) => {
          const data = JSON.parse(event.data);
          this._handleStatusData(protein, data);
          es.close();
          this.intervals.delete(protein);
        });

        es.onerror = () => {
          console.warn(`[JobTracker] SSE error for ${protein}, falling back to polling`);
          es.close();
          this.intervals.delete(protein);
          // Only fall back to polling if job is still active
          const job = this.jobs.get(protein);
          if (job && job.status === 'processing') {
            this._startLegacyPolling(protein);
          }
        };
        return;
      } catch (e) {
        console.warn(`[JobTracker] SSE init failed for ${protein}, using polling`, e);
      }
    }
    this._startLegacyPolling(protein);
  }

  /**
   * Handle a status data object from either SSE or polling.
   */
  _handleStatusData(protein, data) {
    const job = this.jobs.get(protein);
    if (!job) {
      this._stopPolling(protein);
      return;
    }

    // PR-4: surface pipeline events (locus_router, arrow_drift, chain
    // pathway drift, merge collisions, etc.) into a per-protein drawer.
    // Stored on the job object so UI code can render them lazily.
    if (Array.isArray(data.events)) {
      job.pipelineEvents = data.events;
      if (typeof this._renderPipelineEvents === 'function') {
        this._renderPipelineEvents(protein, data.events);
      }
    }

    if (data.status === 'complete') {
      this.completeJob(protein);
    } else if (data.status === 'cancelled' || data.status === 'cancelling') {
      const cancelledJob = markJobCancelled(job);
      this.jobs.set(protein, cancelledJob);
      this._updateJobUI(protein);
      this._stopPolling(protein);
      setTimeout(() => this.removeFromTracker(protein), 2000);
    } else if (data.status === 'error') {
      const errorText = typeof data.progress === 'object' ? data.progress.text : data.progress;
      this.errorJob(protein, errorText || 'Unknown error');
    } else if (data.progress) {
      this.updateJob(protein, data.progress);
    }
  }

  /**
   * Render the pipeline-events drawer for a protein job. Writes into the
   * element with id `pipeline-events-${protein}` if it exists; silently
   * no-op otherwise. Kept simple; styling lives in CSS.
   */
  _renderPipelineEvents(protein, events) {
    const host = document.getElementById(`pipeline-events-${protein}`);
    if (!host) return;
    const rows = (events || []).map(e => {
      const lvl = (e.level || 'info').toLowerCase();
      const tag = (e.tag || e.event || '').toString();
      const fields = Object.entries(e)
        .filter(([k]) => !['t', 'event', 'level', 'tag'].includes(k))
        .map(([k, v]) => `<span class="pevt-field"><span class="pevt-k">${k}</span>=<span class="pevt-v">${String(v)}</span></span>`)
        .join(' ');
      return `
        <div class="pevt pevt-${lvl}">
          <span class="pevt-tag">[${tag}]</span>
          <span class="pevt-event">${e.event}</span>
          ${fields}
        </div>
      `;
    }).join('');
    host.innerHTML = rows || '<div class="pevt-empty">No pipeline events yet.</div>';
  }

  /**
   * Legacy polling fallback (setInterval every 5s).
   */
  _startLegacyPolling(protein) {
    let consecutiveFailures = 0;
    let totalPolls = 0;
    const MAX_CONSECUTIVE_FAILURES = 5;
    const MAX_TOTAL_POLLS = 360;  // 30 min at 5s interval

    const intervalId = setInterval(async () => {
      if (document.hidden) return;  // Skip polls while tab is backgrounded
      totalPolls++;

      if (totalPolls > MAX_TOTAL_POLLS) {
        console.warn(`[JobTracker] Max poll limit reached for ${protein}, stopping.`);
        this.errorJob(protein, 'Job timed out — server may have restarted. Please retry.');
        return;
      }

      try {
        const response = await fetchWithRetry(`/api/status/${encodeURIComponent(protein)}`);

        if (!response.ok) {
          consecutiveFailures++;
          console.warn(`[JobTracker] Status check failed for ${protein} (${consecutiveFailures}/${MAX_CONSECUTIVE_FAILURES})`);
          if (consecutiveFailures >= MAX_CONSECUTIVE_FAILURES) {
            this.errorJob(protein, 'Lost connection to server. Please check if the server is running and retry.');
          }
          return;
        }

        consecutiveFailures = 0;
        const data = await response.json();
        this._handleStatusData(protein, data);

      } catch (error) {
        consecutiveFailures++;
        console.error(`[JobTracker] Polling error for ${protein} (${consecutiveFailures}/${MAX_CONSECUTIVE_FAILURES}):`, error);
        if (consecutiveFailures >= MAX_CONSECUTIVE_FAILURES) {
          this.errorJob(protein, 'Lost connection to server. Please check if the server is running and retry.');
        }
      }
    }, 5000);

    this.intervals.set(protein, { type: 'poll', id: intervalId });
  }

  /**
   * Stop polling/SSE for job
   */
  _stopPolling(protein) {
    const entry = this.intervals.get(protein);
    if (entry) {
      if (entry.type === 'sse' && entry.source) {
        entry.source.close();
      } else if (entry.type === 'poll' && entry.id) {
        clearInterval(entry.id);
      }
      this.intervals.delete(protein);
    }
  }

}

// ============================================================================
// MAIN APPLICATION LOGIC
// ============================================================================

document.addEventListener('DOMContentLoaded', () => {
  // --- Determine Page Context ---
  const isIndexPage = !!document.getElementById('status-display');

  // --- Early exit on viz page ---
  if (!isIndexPage) {
    return;
  }

  // --- Initialize Job Tracker ---
  const jobTracker = new JobTracker('job-container');

  // --- Get DOM elements ---
  const queryButton = document.getElementById('query-button');
  const proteinInput = document.getElementById('protein-input');
  const statusMessage = document.getElementById('status-message');

  // --- Search protein in database ---
  const searchProtein = async (proteinName) => {
    showStatusMessage(`Searching for ${proteinName}...`);

    try {
      const response = await fetch(`/api/search/${encodeURIComponent(proteinName)}`);

      if (!response.ok) {
        try { const errorData = await response.json(); showStatusMessage(errorData.error || 'Search failed'); }
        catch { showStatusMessage('Search failed'); }
        return;
      }

      const data = await response.json();

      if (data.status === 'found') {
        // Protein exists - navigate immediately
        showStatusMessage(`Found! Loading visualization for ${proteinName}...`);
        localStorage.setItem('lastQueriedProtein', proteinName.toUpperCase());
        window.location.href = `/api/visualize/${encodeURIComponent(proteinName)}?t=${Date.now()}`;
      } else {
        // Not found - show query prompt
        showQueryPrompt(proteinName);
      }
    } catch (error) {
      console.error('[ERROR] Search failed:', error);
      showStatusMessage('Failed to search database.');
    }
  };

  // --- Show query prompt ---
  const showQueryPrompt = (proteinName) => {
    if (!statusMessage) return;
    statusMessage.style.display = 'block';
    statusMessage.textContent = '';
    const wrapper = document.createElement('div');
    wrapper.style.cssText = 'text-align: center; padding: 20px;';
    const msg = document.createElement('p');
    msg.style.cssText = 'font-size: 16px; color: #6b7280; margin-bottom: 16px;';
    msg.textContent = 'Protein ';
    const strong = document.createElement('strong');
    strong.textContent = proteinName;
    msg.appendChild(strong);
    msg.appendChild(document.createTextNode(' not found in database.'));
    const btn = document.createElement('button');
    btn.style.cssText = 'padding: 10px 20px; background: #3b82f6; color: white; border: none; border-radius: 6px; font-weight: 500; cursor: pointer; font-size: 14px;';
    btn.textContent = 'Start Research Query';
    btn.addEventListener('click', () => window.startQueryFromPrompt(proteinName));
    wrapper.appendChild(msg);
    wrapper.appendChild(btn);
    statusMessage.appendChild(wrapper);
  };

  // --- Start query ---
  const startQuery = async (proteinName) => {
    // Hide status message when starting job
    hideStatusMessage();

    // Get config from inputs
    const config = readConfigFromInputs();

    // Save to localStorage
    saveConfigToLocalStorage(config);

    try {
      const response = await fetch('/api/query', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          protein: proteinName,
          ...config
        })
      });

      if (!response.ok) throw new Error('Server error');

      const data = await response.json();

      if (data.status === 'complete') {
        // Cached result - navigate immediately
        showStatusMessage(`Cached result found! Loading visualization for ${proteinName}...`);
        localStorage.setItem('lastQueriedProtein', proteinName.toUpperCase());
        window.location.href = `/api/visualize/${encodeURIComponent(proteinName)}?t=${Date.now()}`;
      } else if (data.status === 'processing') {
        // Add to job tracker
        jobTracker.addJob(proteinName, config);

        // Clear input
        proteinInput.value = '';
      } else {
        showStatusMessage(`Error: ${data.message || 'Unknown error'}`);
      }
    } catch (error) {
      console.error('Failed to start query:', error);
      showStatusMessage('Failed to connect to the server.');
    }
  };

  // --- Make startQuery available globally ---
  window.startQueryFromPrompt = (proteinName) => {
    startQuery(proteinName);
  };

  // --- Event Listeners ---
  if (proteinInput) {
    proteinInput.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        if (queryButton) queryButton.click();
      }
    });
  }

  if (queryButton) {
    queryButton.addEventListener('click', () => {
      const proteinName = proteinInput.value.trim();
      const validProteinRegex = /^[a-zA-Z0-9_-]+$/;

      if (!proteinName) {
        showStatusMessage('Please enter a protein name.');
        return;
      }

      if (!validProteinRegex.test(proteinName)) {
        showStatusMessage('Invalid format. Please use only letters, numbers, hyphens, and underscores.');
        return;
      }

      searchProtein(proteinName);
    });
  }

  // --- CLEANUP ON PAGE UNLOAD ---
  // Stop all SSE connections and polling intervals
  window.addEventListener('beforeunload', () => {
    jobTracker.intervals.forEach((entry) => {
      if (entry && entry.type === 'sse' && entry.source) {
        entry.source.close();
      } else if (entry && entry.type === 'poll' && entry.id) {
        clearInterval(entry.id);
      } else {
        clearInterval(entry);
      }
    });
    jobTracker.intervals.clear();
    console.log('[JobTracker] Cleaned up all connections on unload');
  });

  // --- Restore saved config on page load ---
  const interactorRoundsInput = document.getElementById('interactor-rounds');
  const functionRoundsInput = document.getElementById('function-rounds');
  const skipValidationCheckbox = document.getElementById('skip-validation');
  const skipDeduplicatorCheckbox = document.getElementById('skip-deduplicator');
  const skipArrowCheckbox = document.getElementById('skip-arrow-determination');
  const quickPathwayCheckbox2 = document.getElementById('quick-pathway-assignment');
  const discoveryIterationsInput = document.getElementById('discovery-iterations');
  const pipelineModeSelect = document.getElementById('pipeline-mode-select');
  const iterCountDisplay = document.getElementById('iter-count-display');

  if (interactorRoundsInput && functionRoundsInput) {
    const savedInteractor = localStorage.getItem('interactor_rounds');
    const savedFunction = localStorage.getItem('function_rounds');
    const savedSkipValidation = localStorage.getItem('skip_validation');
    const savedSkipDeduplicator = localStorage.getItem('skip_deduplicator');
    const savedSkipArrow = localStorage.getItem('skip_arrow_determination');
    const savedQuickPathway = localStorage.getItem('quick_pathway_assignment');
    const savedDiscoveryIterations = localStorage.getItem('discovery_iterations');
    const savedPipelineMode = localStorage.getItem('pipeline_mode');

    if (savedInteractor) interactorRoundsInput.value = savedInteractor;
    if (savedFunction) functionRoundsInput.value = savedFunction;
    if (skipValidationCheckbox) skipValidationCheckbox.checked = (savedSkipValidation === 'true');
    if (skipDeduplicatorCheckbox) skipDeduplicatorCheckbox.checked = (savedSkipDeduplicator === 'true');
    if (skipArrowCheckbox) skipArrowCheckbox.checked = (savedSkipArrow === 'true');
    // P3.4: only override the HTML default (checked) when the user has
    // explicitly saved a preference. Otherwise null/missing localStorage
    // would silently uncheck the box and pretend quick-assign was opt-in,
    // which is the opposite of the backend's main-mode default.
    if (quickPathwayCheckbox2 && savedQuickPathway !== null) {
        quickPathwayCheckbox2.checked = (savedQuickPathway === 'true');
    }

    // Restore all skip-step checkboxes added in the new UI
    const restoreChecks = [
      ['skip_arrow_validation', 'skip-arrow-validation'],
      ['skip_direct_links', 'skip-direct-links'],
      ['skip_interaction_metadata', 'skip-interaction-metadata'],
      ['skip_schema_validation', 'skip-schema-validation'],
  
      ['skip_citation_verification', 'skip-citation-verification'],
    ];
    for (const [storageKey, elId] of restoreChecks) {
      const saved = localStorage.getItem(storageKey);
      const el = document.getElementById(elId);
      if (el && saved !== null) el.checked = (saved === 'true');
    }
    if (savedDiscoveryIterations && discoveryIterationsInput) {
      discoveryIterationsInput.value = savedDiscoveryIterations;
      if (iterCountDisplay) iterCountDisplay.textContent = savedDiscoveryIterations;
    }
    if (savedPipelineMode && pipelineModeSelect) {
      pipelineModeSelect.value = savedPipelineMode;
      // Trigger visibility update for iterative settings
      if (typeof onPipelineModeChange === 'function') onPipelineModeChange();
    }

    // Restore advanced settings
    const advancedSelects = [
      ['gemini_model_core', 'gemini-model-core'],
      ['gemini_model_evidence', 'gemini-model-evidence'],
      ['gemini_model_arrow', 'gemini-model-arrow'],
      ['gemini_model_flash', 'gemini-model-flash'],
      ['request_mode', 'request-mode'],
    ];
    for (const [key, id] of advancedSelects) {
      const saved = localStorage.getItem(key);
      const el = document.getElementById(id);
      if (saved && el) el.value = saved;
    }

    const advancedNumbers = [
      ['validation_max_workers', 'validation-max-workers'],
      ['validation_batch_size', 'validation-batch-size'],
      ['validation_batch_delay', 'validation-batch-delay'],
      ['thinking_budget', 'thinking-budget'],
      ['iterative_delay_seconds', 'iterative-delay'],
    ];
    for (const [key, id] of advancedNumbers) {
      const saved = localStorage.getItem(key);
      const el = document.getElementById(id);
      if (saved && el) el.value = saved;
    }

    const advancedCheckboxes = [
      ['allow_output_clamp', 'allow-output-clamp'],
      ['verbose_pipeline', 'verbose-pipeline'],
      ['enable_step_logging', 'enable-step-logging'],
      // Post-processing skip flags
      ['skip_schema_validation', 'skip-schema-validation'],
  
      ['skip_interaction_metadata', 'skip-interaction-metadata'],
      ['skip_pmid_update', 'skip-pmid-update'],
      ['skip_arrow_validation', 'skip-arrow-validation'],
      ['skip_clean_names', 'skip-clean-names'],
      ['skip_finalize_metadata', 'skip-finalize-metadata'],
    ];
    for (const [key, id] of advancedCheckboxes) {
      const el = document.getElementById(id);
      if (el) el.checked = localStorage.getItem(key) === 'true';
    }

    const savedMaxChainClaims = localStorage.getItem('max_chain_claims');
    if (savedMaxChainClaims) {
      const el = document.getElementById('max-chain-claims');
      if (el) el.value = savedMaxChainClaims;
    }
    const savedChainClaimStyle = localStorage.getItem('chain_claim_style');
    if (savedChainClaimStyle) {
      const el = document.getElementById('chain-claim-style');
      if (el) el.value = savedChainClaimStyle;
    }
  }
});

// --- Global Helper Functions (for inline onclick handlers) ---
function setPreset(interactorRounds, functionRounds, discoveryIterations) {
  const interactorInput = document.getElementById('interactor-rounds');
  const functionInput = document.getElementById('function-rounds');
  const iterInput = document.getElementById('discovery-iterations');
  const iterDisplay = document.getElementById('iter-count-display');

  if (interactorInput) interactorInput.value = interactorRounds;
  if (functionInput) functionInput.value = functionRounds;
  if (discoveryIterations !== undefined && iterInput) {
    iterInput.value = discoveryIterations;
    if (iterDisplay) iterDisplay.textContent = discoveryIterations;
  }

  // Quick preset: auto-skip expensive non-essential steps
  const skipIds = [
    'skip-arrow-validation', 'skip-direct-links', 'skip-interaction-metadata',
    'skip-validation', 'skip-deduplicator', 'skip-arrow-determination',
    'skip-schema-validation', 'skip-citation-verification'
  ];
  const qpaEl = document.getElementById('quick-pathway-assignment');
  if (interactorRounds <= 3) {
    // Quick: skip the heaviest post-processing + enable quick pathway
    ['skip-arrow-validation', 'skip-direct-links', 'skip-interaction-metadata'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.checked = true;
    });
    if (qpaEl) qpaEl.checked = true;
  } else {
    // Standard/Thorough: uncheck all
    skipIds.forEach(id => {
      const el = document.getElementById(id);
      if (el) el.checked = false;
    });
    if (qpaEl) qpaEl.checked = false;
  }
}

function onPipelineModeChange() {
  const modeSelect = document.getElementById('pipeline-mode-select');
  const iterativeSettings = document.getElementById('iterative-settings');
  if (!modeSelect || !iterativeSettings) return;
  iterativeSettings.style.display = modeSelect.value === 'iterative' ? 'block' : 'none';
}
