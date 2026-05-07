/* ===== shared_utils.js — Pure helpers shared by visualizer.js and card_view.js ===== */

/**
 * Helper: Get node ID from link endpoint (handles both string IDs and node objects)
 * D3 force simulation converts link source/target to node objects after initialization
 */
function getLinkNodeId(endpoint) {
  return typeof endpoint === 'object' ? endpoint.id : endpoint;
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

// Get all pathways a protein appears in
function getPathwaysForProtein(proteinSymbol) {
    const pathways = [];
    if (window.pathwayToInteractors) {
        window.pathwayToInteractors.forEach((proteins, pathwayId) => {
            if (proteins.has(proteinSymbol)) {
                const pathway = window.allPathwaysData?.find(p => p.id === pathwayId);
                if (pathway) pathways.push(pathway);
            }
        });
    }
    return pathways;
}

/**
 * Classify proteins by their directional relationship to the query protein.
 *
 * S1: bidirectional is DEAD. Every interaction now has an asymmetric
 * direction. The return shape still includes a `bidirectional` Set for
 * backward compat with callers that iterate all three keys, but it is
 * always empty. Legacy `direction='bidirectional'` rows are treated as
 * downstream (main_to_primary) — the pipeline's query-centric default.
 *
 * @param {Array} interactions
 * @param {string} queryProtein
 * @returns {Object} - { upstream: Set, downstream: Set, bidirectional: Set (always empty) }
 */
function getProteinsByRole(interactions, queryProtein) {
  const upstream = new Set();      // direction = 'primary_to_main' (interactor acts ON query)
  const downstream = new Set();    // direction = 'main_to_primary' (query acts ON interactor)
  const bidirectional = new Set(); // S1: always empty — kept for shape compat

  interactions.forEach(inter => {
    const src = inter.source;
    const tgt = inter.target;

    let other = null;
    if (src === queryProtein) {
      other = tgt;
    } else if (tgt === queryProtein) {
      other = src;
    } else {
      return;
    }

    if (!other || other === queryProtein) return;

    const dir = inter.direction || 'main_to_primary';
    if (dir === 'primary_to_main') {
      upstream.add(other);
    } else {
      // S1: main_to_primary, bidirectional, or anything else → downstream
      downstream.add(other);
    }
  });

  return { upstream, downstream, bidirectional };
}

function escapeHtml(text) {
  if (text == null) return '';
  const div = document.createElement('div');
  div.textContent = String(text);
  return div.innerHTML;
}

function escapeCsv(text) {
  if (text == null) return '';
  const str = String(text);
  // Escape quotes and wrap in quotes if contains comma, quote, or newline
  if (str.includes(',') || str.includes('"') || str.includes('\n')) {
    return '"' + str.replace(/"/g, '""') + '"';
  }
  return str;
}
