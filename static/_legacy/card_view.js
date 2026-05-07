/**
 * Card View / Horizontal List View Visualization
 * INDEPENDENT IMPLEMENTATION
 * 
 * Features:
 * - Independent State Management (Selection, Expansion)
 * - Independent Sidebar Controls
 * - Horizontal Tree Layout (D3)
 * - Reuses Global Data (SNAP) but builds its own hierarchy
 * - Rich Card Context (Upstream/Downstream info)
 */

// --- Configuration ---
const CV_CONFIG = {
    CARD_WIDTH: 280,
    CARD_HEIGHT: 80,
    LEVEL_SPACING: 380,
    NODE_VERTICAL_SPACING: 120,
    SATELLITE_GAP: 60,
    SATELLITE_V_SPACING: 85,
    ANIMATION_DURATION: 400
};

// --- Chain Visualization ---
// Color palette for chain lanes (subtle hues, high lightness for background tints)
const CHAIN_LANE_PALETTE = [
    { h: 210, label: 'blue' },    // Blue
    { h: 150, label: 'green' },   // Green
    { h: 30,  label: 'amber' },   // Amber
    { h: 280, label: 'purple' },  // Purple
    { h: 0,   label: 'red' },     // Red
    { h: 180, label: 'teal' },    // Teal
    { h: 330, label: 'pink' },    // Pink
    { h: 60,  label: 'yellow' },  // Yellow
];
let _chainColorIndex = 0;
const _chainColorMap = new Map(); // chain_id -> palette entry

function getChainColor(chainId) {
    if (!_chainColorMap.has(chainId)) {
        _chainColorMap.set(chainId, CHAIN_LANE_PALETTE[_chainColorIndex % CHAIN_LANE_PALETTE.length]);
        _chainColorIndex++;
    }
    return _chainColorMap.get(chainId);
}

// Per-pathway merge mode toggle state
const _pathwayMergeMode = new Map(); // pathwayNodeId -> boolean
let _globalChainMerge = false; // Global chain merge toggle

// --- Header Toggle Functions (called from visualize.html buttons) ---
window.toggleCrossQuery = function() {
    cvState.showCrossQuery = !cvState.showCrossQuery;
    if (!cvState.showCrossQuery) cvState.mergeCrossQuery = false;
    _syncHeaderToggleButtons();
    if (typeof renderCardView === 'function') renderCardView();
};

window.toggleMergeCrossQuery = function() {
    if (!cvState.showCrossQuery) return;
    cvState.mergeCrossQuery = !cvState.mergeCrossQuery;
    _syncHeaderToggleButtons();
    if (typeof renderCardView === 'function') renderCardView();
};

window.toggleChainMode = function() {
    _globalChainMerge = !_globalChainMerge;
    // Apply to all pathways
    _pathwayMergeMode.clear();
    _syncHeaderToggleButtons();
    if (typeof renderCardView === 'function') renderCardView();
};

// L5.6 — filter chip state. Persists to sessionStorage so the user's
// preference survives navigation within the visualizer.
const _CV_FILTER_MODES = ['all', 'direct', 'indirect', 'chain'];
function _readCvFilterPrefs() {
    try {
        const raw = sessionStorage.getItem('cv_filter_prefs');
        if (!raw) return { showPseudo: true, mode: 'all' };
        const obj = JSON.parse(raw);
        return {
            showPseudo: obj.showPseudo !== false,
            mode: _CV_FILTER_MODES.includes(obj.mode) ? obj.mode : 'all',
        };
    } catch (e) {
        return { showPseudo: true, mode: 'all' };
    }
}
function _writeCvFilterPrefs(p) {
    try { sessionStorage.setItem('cv_filter_prefs', JSON.stringify(p)); } catch (e) {}
}
window._cvFilterPrefs = _readCvFilterPrefs();

function getCvInteractionLocus(interaction) {
    if (!interaction || typeof interaction !== 'object') return 'direct_claim';
    if (typeof window.getInteractionLocus === 'function') return window.getInteractionLocus(interaction);
    const explicit = (interaction.locus || '').toString().toLowerCase();
    if (explicit === 'direct_claim' || explicit === 'chain_hop_claim' || explicit === 'net_effect_claim') return explicit;
    const ctx = (interaction.function_context || '').toString().toLowerCase();
    if (ctx === 'net' || interaction.is_net_effect || interaction._net_effect || interaction._display_badge === 'NET EFFECT') {
        return 'net_effect_claim';
    }
    if (interaction._is_chain_link) return 'chain_hop_claim';
    return 'direct_claim';
}

window.toggleShowPseudo = function() {
    window._cvFilterPrefs.showPseudo = !window._cvFilterPrefs.showPseudo;
    _writeCvFilterPrefs(window._cvFilterPrefs);
    _syncHeaderToggleButtons();
    if (typeof renderCardView === 'function') renderCardView();
};

window.cycleCvFilter = function() {
    const cur = window._cvFilterPrefs.mode || 'all';
    const idx = _CV_FILTER_MODES.indexOf(cur);
    const next = _CV_FILTER_MODES[(idx + 1) % _CV_FILTER_MODES.length];
    window._cvFilterPrefs.mode = next;
    _writeCvFilterPrefs(window._cvFilterPrefs);
    _syncHeaderToggleButtons();
    if (typeof renderCardView === 'function') renderCardView();
};

// True if a SNAP interaction matches the current filter mode.
window.cvInteractionPasses = function(interaction) {
    if (!interaction || typeof interaction !== 'object') return true;
    const p = window._cvFilterPrefs || { showPseudo: true, mode: 'all' };

    // Pseudo gate: if pseudo display is OFF, drop any interaction whose
    // source/target/primary is a pseudo entity.
    if (!p.showPseudo) {
        if (interaction._source_is_pseudo || interaction._target_is_pseudo ||
            interaction._partner_is_pseudo) return false;
        // Client-side fallback for older payloads
        const _PFB = new Set(['RNA','mRNA','tRNA','rRNA','lncRNA','miRNA','DNA',
            'Ubiquitin','SUMO','NEDD8','Proteasome','Ribosome','Spliceosome',
            'Actin','Tubulin']);
        if (_PFB.has(interaction.source) || _PFB.has(interaction.target) ||
            _PFB.has(interaction.primary)) return false;
    }

    const locus = getCvInteractionLocus(interaction);
    if (p.mode === 'all') return true;
    if (p.mode === 'direct') return locus === 'direct_claim';
    if (p.mode === 'indirect') return locus === 'net_effect_claim' || (interaction.interaction_type || interaction.type) === 'indirect';
    if (p.mode === 'chain') return locus === 'chain_hop_claim';
    return true;
};

function _syncHeaderToggleButtons() {
    const btnCQ = document.getElementById('btn-cross-query');
    const btnMerge = document.getElementById('btn-merge-cq');
    const btnChain = document.getElementById('btn-chain-mode');
    const btnPseudo = document.getElementById('btn-show-pseudo');
    const btnFilter = document.getElementById('btn-cv-filter');
    if (btnCQ) {
        btnCQ.classList.toggle('cv-toggle-active', cvState.showCrossQuery);
    }
    if (btnMerge) {
        btnMerge.classList.toggle('cv-toggle-active', cvState.mergeCrossQuery);
        btnMerge.disabled = !cvState.showCrossQuery;
        btnMerge.style.opacity = cvState.showCrossQuery ? '1' : '0.4';
    }
    if (btnChain) {
        btnChain.classList.toggle('cv-toggle-active', _globalChainMerge);
        btnChain.textContent = _globalChainMerge ? 'Chains: Merged' : 'Chains: Split';
    }
    // L5.6 — filter chips
    const prefs = window._cvFilterPrefs || { showPseudo: true, mode: 'all' };
    if (btnPseudo) {
        btnPseudo.classList.toggle('cv-toggle-active', prefs.showPseudo);
        btnPseudo.textContent = prefs.showPseudo ? 'Pseudo: ON' : 'Pseudo: OFF';
    }
    if (btnFilter) {
        const labelMap = { all: 'Filter: All', direct: 'Filter: Direct', indirect: 'Filter: Indirect', chain: 'Filter: Chain' };
        btnFilter.classList.toggle('cv-toggle-active', prefs.mode !== 'all');
        btnFilter.textContent = labelMap[prefs.mode] || 'Filter: All';
    }
}

/**
 * Group chain link interactions by chain_id.
 * Returns Map<chainId, { proteins: string[], arrows: object[], interactions: object[] }>
 */
function groupChainsByChainId(pathwayInteractorIds, pathwayName) {
    if (!SNAP || !SNAP.interactions) return new Map();

    const chains = new Map();
    const pwSet = new Set(pathwayInteractorIds);
    const pathwayNameNorm = (pathwayName || '').trim().toLowerCase();

    for (const inter of SNAP.interactions) {
        if (!inter._is_chain_link) continue;
        // L5.6 — apply current filter (Pseudo on/off + filter mode).
        if (window.cvInteractionPasses && !window.cvInteractionPasses(inter)) continue;

        // P3.3 leak-fix: a chain link must EARN inclusion under this
        // pathway. Drop the old escape hatch where containing the main
        // protein (SNAP.main) was sufficient — every ATXN3 chain
        // includes ATXN3, so that rule made every chain show up under
        // every pathway ATXN3 was explored in.
        //
        // New rule: include the chain link if EITHER
        //   (a) BOTH endpoints belong to this pathway's interactor set
        //       (true intra-pathway hop), OR
        //   (b) the chain entity's own assigned pathway matches the
        //       pathway being expanded (the chain explicitly belongs
        //       here per quick_assign).
        const src = inter.source;
        const tgt = inter.target;
        const entity = inter._chain_entity || {};
        const chainPathwayNorm = (entity.pathway_name || '').trim().toLowerCase();

        const bothEndpointsInPathway = pwSet.has(src) && pwSet.has(tgt);
        const chainAssignedHere = !!pathwayNameNorm && chainPathwayNorm === pathwayNameNorm;
        // Layer 2 of CLAUDE_DOCS/11_CHAIN_TOPOLOGY.md — admit the chain
        // when ANY of its claims landed in this pathway, even if
        // `chain.pathway_name` points elsewhere. Backend emits
        // `inter.chain_pathways` (union over all this row's chain
        // memberships) plus per-chain pathways inside `all_chains[i]`
        // for the per-instance gate below.
        const interChainPathwaysNorm = Array.isArray(inter.chain_pathways)
            ? inter.chain_pathways.map(p => (p || '').trim().toLowerCase())
            : [];
        const claimAssignedHere = !!pathwayNameNorm && interChainPathwaysNorm.includes(pathwayNameNorm);
        if (!bothEndpointsInPathway && !chainAssignedHere && !claimAssignedHere) continue;

        // R3: prefer the multi-chain `all_chains` payload when present
        // so the same hop participating in N chains lands in all N
        // groups. Falls back to the legacy scalar `chain_id` /
        // `_chain_entity` shape for backends that haven't shipped the
        // membership emitter yet.
        const chainProteinsPrimary = entity.chain_proteins || [];
        const allChains = Array.isArray(inter.all_chains) ? inter.all_chains : null;
        let chainInstances;
        if (allChains && allChains.length) {
            chainInstances = allChains.map(c => ({
                cid: c.chain_id != null ? c.chain_id : (Array.isArray(c.chain_proteins) ? c.chain_proteins.join('->') : null),
                proteins: Array.isArray(c.chain_proteins) ? c.chain_proteins : chainProteinsPrimary,
                arrows: Array.isArray(c.chain_with_arrows) ? c.chain_with_arrows : (entity.chain_with_arrows || []),
                pathwayName: c.pathway_name || entity.pathway_name || '',
                chainPathways: Array.isArray(c.chain_pathways) ? c.chain_pathways : [],
            })).filter(c => c.cid != null);
        } else {
            const cid = inter.chain_id != null
                ? inter.chain_id
                : (chainProteinsPrimary.length >= 2 ? chainProteinsPrimary.join('->') : null);
            if (cid == null) continue;
            chainInstances = [{
                cid,
                proteins: chainProteinsPrimary,
                arrows: entity.chain_with_arrows || [],
                pathwayName: entity.pathway_name || '',
                chainPathways: Array.isArray(inter.chain_pathways) ? inter.chain_pathways : [],
            }];
        }

        for (const inst of chainInstances) {
            // Per-instance pathway gate: when chainAssignedHere logic
            // depended on `entity.pathway_name`, re-evaluate against
            // THIS instance's own pathway so a hop that's in chain A
            // (Autophagy) and chain B (PQC) only joins each group when
            // the expansion matches that chain's own pathway.
            const instPwNorm = (inst.pathwayName || '').trim().toLowerCase();
            const instAssignedHere = !!pathwayNameNorm && instPwNorm === pathwayNameNorm;
            // Layer 2 widening at the per-instance level: admit when
            // any of THIS instance's claims landed in this pathway.
            const instChainPathwaysNorm = Array.isArray(inst.chainPathways)
                ? inst.chainPathways.map(p => (p || '').trim().toLowerCase())
                : [];
            const instClaimHere = !!pathwayNameNorm && instChainPathwaysNorm.includes(pathwayNameNorm);
            if (!bothEndpointsInPathway && !instAssignedHere && !instClaimHere) continue;

            if (!chains.has(inst.cid)) {
                chains.set(inst.cid, {
                    proteins: inst.proteins,
                    arrows: inst.arrows,
                    interactions: [],
                    pathwayName: inst.pathwayName,
                });
            }
            chains.get(inst.cid).interactions.push(inter);
        }
    }

    // Filter out chains that couldn't resolve their protein list
    for (const [cid, chain] of chains) {
        if (chain.proteins.length < 2) {
            chains.delete(cid);
        }
    }

    return chains;
}

// Interaction colors derived from CSS custom properties (theme-aware).
// Read lazily on first access so the DOM is ready. Invalidated when the theme
// changes (prefers-color-scheme or a data-theme attribute swap on <html>).
let _cvColorsCache = null;
function _invalidateCVColorsCache() { _cvColorsCache = null; }

if (typeof window !== 'undefined' && typeof document !== 'undefined') {
    // Wire up theme-change listeners exactly once.
    if (!window._cvColorsListenersAttached) {
        window._cvColorsListenersAttached = true;
        try {
            const mq = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)');
            if (mq && mq.addEventListener) {
                mq.addEventListener('change', _invalidateCVColorsCache);
            } else if (mq && mq.addListener) {
                mq.addListener(_invalidateCVColorsCache);
            }
        } catch (e) { /* ignore — matchMedia unavailable in some test envs */ }
        // Watch <html> for data-theme / class changes (custom theme toggles).
        try {
            const mo = new MutationObserver(() => _invalidateCVColorsCache());
            mo.observe(document.documentElement, {
                attributes: true,
                attributeFilter: ['data-theme', 'class'],
            });
        } catch (e) { /* ignore */ }
    }
}

function getCVColors() {
    if (_cvColorsCache) return _cvColorsCache;
    const s = getComputedStyle(document.documentElement);
    const v = (name, fallback) => (s.getPropertyValue(name) || '').trim() || fallback;
    _cvColorsCache = {
        activates: {
            stroke: v('--color-activation', '#059669'),
            bg:     '#064e3b',
            subgroupBg: '#042f24',
            badge:  v('--color-activation', '#10b981'),
            aura:   'rgba(16, 185, 129, 0.4)',
        },
        inhibits: {
            stroke: v('--color-inhibition', '#dc2626'),
            bg:     '#7f1d1d',
            subgroupBg: '#3a0e0e',
            badge:  v('--color-inhibition', '#ef4444'),
            aura:   'rgba(239, 68, 68, 0.4)',
        },
        binds: {
            stroke: v('--color-binding', '#a78bfa'),
            bg:     '#3b0764',
            subgroupBg: '#1f0545',
            badge:  v('--color-binding', '#a78bfa'),
            aura:   'rgba(167, 139, 250, 0.4)',
        },
        regulates: {
            stroke: v('--color-regulation', '#f59e0b'),
            bg:     '#713f12',
            subgroupBg: '#352a09',
            badge:  v('--color-regulation', '#f59e0b'),
            aura:   'rgba(245, 158, 11, 0.4)',
        },
    };
    return _cvColorsCache;
}

// ===========================================================================
// UNIFIED STATE MANAGER - PathwayState (Single Source of Truth)
// ===========================================================================

const PathwayState = (function() {
    'use strict';

    // --- Core State (Private) ---
    const core = {
        selectedPathways: new Set(),    // ANY level pathways (no L0 filter!)
        hiddenPathways: new Set(),      // Hidden from BOTH Explorer AND Card View
        expandedBranches: new Set(),    // Explorer tree expansion
        expandedCards: new Set(),       // Card View expansion
        interactionMetadata: new Map(), // {pathwayId -> {activates, inhibits, binds, regulates, total}}
        searchQuery: '',
        syncInProgress: false,          // Prevent infinite loops

        // Visual state (animations)
        syncPulseActive: false,
        recentlyChanged: new Set(),     // For visual feedback (fade after 2s)
    };

    // --- Observers (Imperative Shell) ---
    const observers = {
        explorer: [],
        cardView: [],
        sidebar: [],
        syncIndicator: []
    };

    // --- Public API ---
    return {
        // Getters
        getSelectedPathways: () => new Set(core.selectedPathways),
        getHiddenPathways: () => new Set(core.hiddenPathways),
        getExpandedBranches: () => new Set(core.expandedBranches),
        getExpandedCards: () => new Set(core.expandedCards),
        getInteractionMetadata: () => new Map(core.interactionMetadata),
        isSyncPulseActive: () => core.syncPulseActive,
        getRecentlyChanged: () => new Set(core.recentlyChanged),

        // Mutations (notify observers)
        toggleSelection(pathwayId, source = 'unknown') {
            if (core.syncInProgress) return;

            core.syncInProgress = true;
            try {
                const wasSelected = core.selectedPathways.has(pathwayId);

                const hierarchyMap = window.getPathwayHierarchy?.() || new Map();
                const childrenMap = window.getPathwayChildrenMap?.() || new Map();

                let cascadedIds = [];

                if (wasSelected) {
                    // DESELECTING: Remove this pathway AND all descendants
                    core.selectedPathways.delete(pathwayId);

                    const toDeselect = calculateCascadeDeselectDown(
                        pathwayId,
                        core.selectedPathways,
                        childrenMap
                    );

                    toDeselect.forEach(id => {
                        core.selectedPathways.delete(id);
                        core.recentlyChanged.add(id);
                        setTimeout(() => core.recentlyChanged.delete(id), 2000);
                    });

                    cascadedIds = Array.from(toDeselect);

                } else {
                    // SELECTING: Add this pathway AND all ancestors
                    core.selectedPathways.add(pathwayId);

                    const toSelect = calculateCascadeSelectUp(
                        pathwayId,
                        core.selectedPathways,
                        hierarchyMap
                    );

                    toSelect.forEach(id => {
                        core.selectedPathways.add(id);
                        core.recentlyChanged.add(id);
                        setTimeout(() => core.recentlyChanged.delete(id), 2000);
                    });

                    cascadedIds = Array.from(toSelect);
                }

                core.recentlyChanged.add(pathwayId);
                setTimeout(() => core.recentlyChanged.delete(pathwayId), 2000);

                this.notifyAll('selection', {
                    pathwayId,
                    selected: !wasSelected,
                    source,
                    cascadedIds
                });
            } finally {
                core.syncInProgress = false;
            }
        },

        toggleVisibility(pathwayId, source = 'unknown') {
            if (core.syncInProgress) return;

            core.syncInProgress = true;
            try {
                const wasHidden = core.hiddenPathways.has(pathwayId);

                if (wasHidden) {
                    core.hiddenPathways.delete(pathwayId);
                } else {
                    core.hiddenPathways.add(pathwayId);
                }

                core.recentlyChanged.add(pathwayId);
                setTimeout(() => core.recentlyChanged.delete(pathwayId), 2000);

                this.notifyAll('visibility', { pathwayId, hidden: !wasHidden, source });
            } finally {
                core.syncInProgress = false;
            }
        },

        toggleExpansion(pathwayId, component = 'both', source = 'unknown') {
            if (core.syncInProgress) return;
            core.syncInProgress = true;

            try {
                const hierarchyMap = window.getPathwayHierarchy?.() || new Map();
                const childrenMap = window.getPathwayChildrenMap?.() || new Map();

                // Check if EITHER set has it expanded (unified expansion state)
                const wasExpanded = core.expandedBranches.has(pathwayId) || core.expandedCards.has(pathwayId);

                let cascadedIds = [];

                if (wasExpanded) {
                    // COLLAPSING: Collapse this node AND all descendants
                    core.expandedBranches.delete(pathwayId);
                    core.expandedCards.delete(pathwayId);
                    cascadedIds.push(pathwayId);

                    // CASCADE COLLAPSE: Also collapse all descendants
                    const descendants = calculateDescendants(pathwayId, childrenMap);
                    descendants.forEach(descendantId => {
                        if (core.expandedBranches.has(descendantId) || core.expandedCards.has(descendantId)) {
                            core.expandedBranches.delete(descendantId);
                            core.expandedCards.delete(descendantId);
                            cascadedIds.push(descendantId);
                        }
                    });

                    // Track for visual feedback
                    cascadedIds.forEach(id => {
                        core.recentlyChanged.add(id);
                        setTimeout(() => core.recentlyChanged.delete(id), 2000);
                    });
                } else {
                    // EXPANDING: Also expand all ancestors for visibility
                    const ancestors = calculateAncestors(pathwayId, hierarchyMap);

                    // Add the target and all ancestors to BOTH sets (unified)
                    [pathwayId, ...ancestors].forEach(id => {
                        core.expandedBranches.add(id);
                        core.expandedCards.add(id);
                        cascadedIds.push(id);
                    });

                    // Track for visual feedback
                    cascadedIds.forEach(id => {
                        core.recentlyChanged.add(id);
                        setTimeout(() => core.recentlyChanged.delete(id), 2000);
                    });
                }

                this.notifyAll('expansion', {
                    pathwayId,
                    expanded: !wasExpanded,
                    component,
                    source,
                    cascadedIds
                });
            } finally {
                core.syncInProgress = false;
            }
        },

        setInteractionMetadata(pathwayId, metadata) {
            core.interactionMetadata.set(pathwayId, metadata);
        },

        clearSelections() {
            core.selectedPathways.clear();
            this.notifyAll('selection', { pathwayId: null, selected: false, source: 'clearAll' });
        },

        selectAll(pathwayIds) {
            pathwayIds.forEach(id => core.selectedPathways.add(id));
            this.notifyAll('selection', { pathwayId: null, selected: true, source: 'selectAll' });
        },

        showAll() {
            core.hiddenPathways.clear();
            this.notifyAll('visibility', { pathwayId: null, hidden: false, source: 'showAll' });
        },

        // Observer pattern
        observe(component, callback) {
            if (!observers[component]) observers[component] = [];
            observers[component].push(callback);
        },

        notifyAll(eventType, data) {
            // Trigger sync pulse animation
            core.syncPulseActive = true;
            setTimeout(() => core.syncPulseActive = false, 800);

            // Trigger visual indicator
            const indicator = document.getElementById('pe-sync-indicator');
            const line = document.getElementById('pe-sync-line');
            if (indicator) {
                indicator.classList.remove('active');
                void indicator.offsetWidth; // Force reflow
                indicator.classList.add('active');
            }
            if (line) {
                line.classList.remove('active');
                void line.offsetWidth; // Force reflow
                line.classList.add('active');
            }

            // Notify all components
            Object.values(observers).flat().forEach(cb => {
                try {
                    cb(eventType, data);
                } catch (e) {
                    console.error('Observer error:', e);
                }
            });
        },

        // Helpers
        isSelected(pathwayId) {
            return core.selectedPathways.has(pathwayId);
        },

        isHidden(pathwayId) {
            return core.hiddenPathways.has(pathwayId);
        },

        isExpanded(pathwayId, component = 'explorer') {
            const set = component === 'explorer' ? core.expandedBranches : core.expandedCards;
            return set.has(pathwayId);
        }
    };
})();

// ===========================================================================
// EXPANSION WITH AUTO-SELECTION (Issue 2b Fix)
// ===========================================================================

/**
 * Expand a pathway and auto-select its visible children in the navigator.
 * When expanding a card node, we want the children to appear in BOTH card view
 * AND be selected in the pathway navigator for sync.
 *
 * @param {string} pathwayId - Pathway to expand
 * @param {string} source - Event source for tracking ('cardView', 'click', etc.)
 */
function expandAndSelectChildren(pathwayId, source = 'cardView') {
    const childrenMap = window.getPathwayChildrenMap?.() || new Map();

    // Check current expansion state BEFORE toggling
    const wasExpanded = PathwayState.isExpanded(pathwayId, 'cardView');

    // Toggle expansion
    PathwayState.toggleExpansion(pathwayId, 'cardView', source);

    if (!wasExpanded) {
        // EXPANDING: Auto-select visible children (those with interactors in their subtree)
        const childIds = childrenMap.get(pathwayId) || [];
        const hasInteractorsMap = window.getHasInteractorsInSubtree?.() || new Map();

        childIds.forEach(childId => {
            const hasContent = hasInteractorsMap.get(childId) === true;
            if (hasContent && !PathwayState.isSelected(childId)) {
                PathwayState.toggleSelection(childId, source);
            }
        });
    } else {
        // COLLAPSING: Deselect all descendants to hide them from card view
        // This matches the behavior of navigator checkbox deselection
        const descendants = calculateDescendants(pathwayId, childrenMap);
        descendants.forEach(descendantId => {
            if (PathwayState.isSelected(descendantId)) {
                PathwayState.toggleSelection(descendantId, source);
            }
        });
    }
}

// ===========================================================================
// PURE CASCADE LOGIC (Functional Core - No Side Effects)
// ===========================================================================

/**
 * Calculate all ancestor pathway IDs (parents, grandparents, etc.)
 * @param {string} pathwayId - Starting pathway
 * @param {Map} hierarchyMap - window.getPathwayHierarchy() result
 * @returns {Set<string>} All ancestor IDs
 */
function calculateAncestors(pathwayId, hierarchyMap) {
    const ancestors = new Set();
    const visited = new Set();
    const queue = [pathwayId];

    while (queue.length > 0) {
        const currentId = queue.shift();
        if (visited.has(currentId)) continue;
        visited.add(currentId);

        const hier = hierarchyMap.get(currentId);
        if (!hier || !hier.parent_ids) continue;

        hier.parent_ids.forEach(parentId => {
            if (!ancestors.has(parentId) && !visited.has(parentId)) {
                ancestors.add(parentId);
                queue.push(parentId);
            }
        });
    }

    return ancestors;
}

/**
 * Calculate all descendant pathway IDs (children, grandchildren, etc.)
 * @param {string} pathwayId - Starting pathway
 * @param {Map} childrenMap - window.getPathwayChildrenMap() result
 * @returns {Set<string>} All descendant IDs
 */
function calculateDescendants(pathwayId, childrenMap) {
    const descendants = new Set();
    const visited = new Set();
    const queue = [pathwayId];

    while (queue.length > 0) {
        const currentId = queue.shift();
        if (visited.has(currentId)) continue;
        visited.add(currentId);

        const children = childrenMap.get(currentId);
        if (!children) continue;

        children.forEach(childId => {
            if (!descendants.has(childId) && !visited.has(childId)) {
                descendants.add(childId);
                queue.push(childId);
            }
        });
    }

    return descendants;
}

/**
 * Calculate pathways to select when cascading up (for selection)
 * @param {string} pathwayId - Pathway being selected
 * @param {Set} currentlySelected - Current selection state
 * @param {Map} hierarchyMap - Hierarchy data
 * @returns {Set<string>} IDs that need to be selected
 */
function calculateCascadeSelectUp(pathwayId, currentlySelected, hierarchyMap) {
    const toSelect = new Set();
    const ancestors = calculateAncestors(pathwayId, hierarchyMap);

    ancestors.forEach(ancestorId => {
        if (!currentlySelected.has(ancestorId)) {
            toSelect.add(ancestorId);
        }
    });

    return toSelect;
}

/**
 * Calculate pathways to deselect when cascading down (for deselection)
 * @param {string} pathwayId - Pathway being deselected
 * @param {Set} currentlySelected - Current selection state
 * @param {Map} childrenMap - Children mapping
 * @returns {Set<string>} IDs that need to be deselected
 */
function calculateCascadeDeselectDown(pathwayId, currentlySelected, childrenMap) {
    const toDeselect = new Set();
    const descendants = calculateDescendants(pathwayId, childrenMap);

    descendants.forEach(descendantId => {
        if (currentlySelected.has(descendantId)) {
            toDeselect.add(descendantId);
        }
    });

    return toDeselect;
}

/**
 * Validate hierarchy data integrity
 * @param {Map} hierarchyMap - Hierarchy data
 * @param {Map} childrenMap - Children mapping
 * @returns {Object} {valid: boolean, errors: string[]}
 */
function validateHierarchyData(hierarchyMap, childrenMap) {
    const errors = [];
    const allPathways = new Set([...hierarchyMap.keys(), ...childrenMap.keys()]);

    allPathways.forEach(pathwayId => {
        const ancestors = calculateAncestors(pathwayId, hierarchyMap);
        if (ancestors.has(pathwayId)) {
            errors.push(`Self-ancestry cycle: ${pathwayId}`);
        }
    });

    return { valid: errors.length === 0, errors };
}

// --- State (Legacy - will be replaced by PathwayState) ---
const cvState = {
    expandedNodes: new Set(), // IDs of expanded nodes
    selectedRoots: new Set(), // IDs of selected root pathways
    hiddenCards: new Set(),   // IDs of hidden pathway cards (NEW for v2)
    rootPathways: [],         // List of all root pathways
    initialized: false,
    sidebarCollapsed: false,
    filterByInteractorDescendants: true,  // Only show pathways leading to interactors by default
    showCrossQuery: false,   // Show non-query interactions under pathways
    mergeCrossQuery: false,  // Merge cross-query into main tree (vs separate branches)
    // --- Interactor Mode State ---
    cardViewMode: 'pathway',  // 'pathway' | 'interactor'
    interactorExpandedGroups: new Set(),  // Keys like 'upstream', 'downstream_activates', etc.
    interactorDirectionFilter: new Set(['upstream', 'downstream', 'bidirectional']),
    interactorArrowFilter: new Set(['activates', 'inhibits', 'binds', 'regulates']),
    interactorSearchQuery: ''
};

// --- D3 Objects ---
let cvSvg, cvG, cvZoom;

// ============================================================================
// INITIALIZATION
// ============================================================================

function initCardView() {
    if (cvState.initialized) return;

    // 1. Setup D3 - Scrollable mode (no zoom/pan)
    const container = document.getElementById('card-view');
    if (!container) return;

    cvSvg = d3.select('#card-svg');
    cvG = cvSvg.append('g').attr('class', 'card-view-group');

    // No zoom behavior - we use native scroll instead
    cvZoom = null;


    // 2. Initialize Data
    const rawPathways = window.getRawPathwayData ? window.getRawPathwayData() : [];

    // ✅ FIXED: Store ALL pathways (not just L0) to support any-level selection
    cvState.rootPathways = rawPathways;

    // ✅ FIX 2a: Do NOT auto-select all L0 pathways
    // Let PathwayExplorer.init() handle selection based on filterByInteractorDescendants
    // cvState.selectedRoots starts empty

    // 3. Render Sidebar
    renderCardSidebar();

    // 4. Validate hierarchy data integrity
    runHierarchyValidation();

    // 5. Register expansion observer for bidirectional sync
    PathwayState.observe('cardView', (eventType, data) => {
        if (eventType === 'expansion') {
            // Sync expansion state from PathwayState to cvState
            cvState.expandedNodes = new Set([
                ...PathwayState.getExpandedBranches(),
                ...PathwayState.getExpandedCards()
            ]);
            renderCardView();
        }
    });

    // 6. Pre-compute interaction type counts per pathway for badge display
    _populatePathwayBadgeCounts();

    cvState.initialized = true;
    renderCardView();
}

/**
 * Count interaction types (activates/inhibits/binds/regulates) per pathway
 * and store via PathwayState.setInteractionMetadata so badges render correctly.
 * Also ensures window.pathwayToInteractors is populated for getPathwaysForProtein().
 *
 * Called both during init and on every render. A cheap signature check skips
 * redundant work when SNAP data hasn't changed.
 */
let _lastBadgeSignature = null;
function _populatePathwayBadgeCounts(force = false) {
    if (typeof SNAP === 'undefined' || !SNAP.interactions) return;
    const rawPathways = window.getRawPathwayData ? window.getRawPathwayData() : [];
    const interactions = SNAP.interactions;

    // Skip work if the underlying data hasn't changed since last computation.
    // Signature = (# interactions, # pathways, a cheap last-entry fingerprint).
    const sig = `${interactions.length}|${rawPathways.length}|` +
        (interactions.length ? JSON.stringify([
            interactions[interactions.length - 1].source,
            interactions[interactions.length - 1].target,
            interactions[interactions.length - 1].arrow,
        ]) : '0');
    if (!force && sig === _lastBadgeSignature) return;
    _lastBadgeSignature = sig;

    // Ensure pathwayToInteractors is available for getPathwaysForProtein()
    // (normally set by visualizer.js, but card view may init before graph
    // view). Refresh it every time because chain-link endpoints can be
    // injected after the initial pathway payload is assembled.
    if (!window.pathwayToInteractors) window.pathwayToInteractors = new Map();
    rawPathways.forEach(pw => {
        const pwId = pw.id || ('pathway_' + (pw.name || '').replace(/\s+/g, '_'));
        const ids = new Set();
        const add = (proteinId) => {
            if (!proteinId || proteinId === SNAP.main) return;
            ids.add(proteinId);
        };
        (pw.interactor_ids || []).forEach(add);
        (pw.cross_query_interactor_ids || []).forEach(add);
        (pw.interactions || []).forEach(ix => {
            add(ix?.source);
            add(ix?.target);
        });
        (pw.cross_query_interactions || []).forEach(ix => {
            add(ix?.source);
            add(ix?.target);
        });
        window.pathwayToInteractors.set(pwId, ids);
    });
    // Also ensure allPathwaysData is set for getPathwaysForProtein lookups
    if (!window.allPathwaysData) {
        window.allPathwaysData = rawPathways;
    }

    // Build a lookup: protein → SET of arrow types from its interactions with main.
    // A protein can have multiple interaction types (e.g., both activates AND binds)
    // so we track each type separately rather than letting one overwrite another.
    const arrowsByProtein = new Map();
    const addArrow = (proteinId, arrow) => {
        if (!proteinId || proteinId === SNAP.main) return;
        let set = arrowsByProtein.get(proteinId);
        if (!set) {
            set = new Set();
            arrowsByProtein.set(proteinId, set);
        }
        const normalised = (arrow === 'complex') ? 'binds' : (arrow || 'binds');
        set.add(normalised);
    };
    interactions.forEach(inter => {
        addArrow(inter.target, inter.arrow);
        addArrow(inter.source, inter.arrow);
    });

    rawPathways.forEach(pw => {
        const pwId = pw.id || ('pathway_' + (pw.name || '').replace(/\s+/g, '_'));
        const ids = pw.interactor_ids || [];
        const counts = { activates: 0, inhibits: 0, binds: 0, regulates: 0, total: 0 };

        ids.forEach(intId => {
            const set = arrowsByProtein.get(intId);
            if (!set || set.size === 0) {
                counts.binds++;
                counts.total++;
                return;
            }
            // Count each interaction type once per protein (protein may appear in
            // multiple arrow buckets if it has both activates and inhibits, etc.)
            set.forEach(arrowType => {
                if (counts.hasOwnProperty(arrowType)) counts[arrowType]++;
                else counts.binds++;
                counts.total++;
            });
        });

        PathwayState.setInteractionMetadata(pwId, counts);
    });
}

/**
 * Run hierarchy validation and report any issues
 */
function runHierarchyValidation() {
    const hierarchyMap = window.getPathwayHierarchy?.() || new Map();
    const childrenMap = window.getPathwayChildrenMap?.() || new Map();

    const result = validateHierarchyData(hierarchyMap, childrenMap);

    if (!result.valid) {
        console.group('⚠️ Hierarchy Validation Errors');
        result.errors.forEach(err => console.error(err));
        console.error('Run: python scripts/pathway_v2/verify_pipeline.py --auto-fix');
        console.groupEnd();
    }

    return result.valid;
}

// ============================================================================
// DATA & HIERARCHY
// ============================================================================

/**
 * Build the Tree Data Structure dynamically based on State
 */
function buildCardHierarchy() {
    const mainId = window.getMainProteinId ? window.getMainProteinId() : "Main";
    const hierarchyMap = window.getPathwayHierarchy ? window.getPathwayHierarchy() : new Map();
    const childrenMap = window.getPathwayChildrenMap ? window.getPathwayChildrenMap() : new Map();

    const rootNode = {
        id: mainId,
        type: 'main',
        children: []
    };

    // Track created nodes to avoid duplicates
    const nodeMap = new Map(); // pathwayId -> node

    /**
     * Get full ancestry chain from L0 root down to pathwayId
     * Returns: [L0_id, L1_id, L2_id, ..., pathwayId]
     */
    function getAncestryChain(pathwayId) {
        const chain = [];
        let currentId = pathwayId;
        const visited = new Set(); // Prevent infinite loops
        
        while (currentId && !visited.has(currentId)) {
            visited.add(currentId);
            chain.unshift(currentId); // Add to front
            
            const hier = hierarchyMap.get(currentId);
            const parentIds = hier?.parent_ids || [];
            
            // Take first parent (pathways can have multiple parents, but we show one chain)
            currentId = parentIds.length > 0 ? parentIds[0] : null;
        }
        
        return chain;
    }

    /**
     * Get or create a pathway node (cached to avoid duplicates)
     */
    function getOrCreateNode(pathwayId) {
        if (nodeMap.has(pathwayId)) {
            return nodeMap.get(pathwayId);
        }

        const raw = cvState.rootPathways.find(pw => (pw.id || `pathway_${(pw.name || 'unknown').replace(/\s+/g, '_')}`) === pathwayId);
        if (!raw) {
            return null;
        }

        const node = createPathwayNode(pathwayId, raw, hierarchyMap);
        nodeMap.set(pathwayId, node);
        return node;
    }

    // 1. Build ancestry chains for all selected pathways
    // ✅ Shows full L0→L1→L2→L3 chain for context
    // ✅ FIX: Only show pathways if all their ancestors are expanded
    cvState.selectedRoots.forEach(pathwayId => {
        if (cvState.hiddenCards.has(pathwayId)) return;

        const chain = getAncestryChain(pathwayId);

        // Check if all ancestors (except the pathway itself) are expanded
        // This ensures collapsed parents hide their children
        const ancestorsExceptSelf = chain.slice(0, -1); // All except the last (target pathway)
        const allAncestorsExpanded = ancestorsExceptSelf.every(ancestorId =>
            cvState.expandedNodes.has(ancestorId)
        );

        // Skip this pathway if any ancestor is collapsed (unless it's an L0 root with no ancestors)
        if (ancestorsExceptSelf.length > 0 && !allAncestorsExpanded) {
            return;
        }

        // Build tree from L0 root down to selected pathway
        let parentNode = rootNode;

        chain.forEach((id) => {
            const node = getOrCreateNode(id);
            if (!node) return;

            // Check if node already exists in parent's children
            if (!parentNode.children) parentNode.children = [];
            const existingChild = parentNode.children.find(c => c.id === id);

            if (!existingChild) {
                // Add new node as child
                parentNode.children.push(node);
                parentNode = node; // Move down to this node
            } else {
                // Reuse existing node
                parentNode = existingChild;
            }
        });
    });

    // 2. Recursively add children if expanded
    // DFS traversal to add children
    const processedNodes = new Set(); // ✅ Track processed nodes to prevent infinite loops
    
    const processChildren = (parentNode) => {
        if (!cvState.expandedNodes.has(parentNode.id)) return;
        // Skip if hidden
        if (cvState.hiddenCards.has(parentNode.id)) return;

        // A. Add Child Pathways
        const childIds = childrenMap.get(parentNode.id);
        if (childIds) {
            childIds.forEach(childId => {
                // Skip hidden child pathways
                if (cvState.hiddenCards.has(childId)) return;

                // ✅ NEW: Filter by interactor descendants (default behavior)
                // Only show pathways that eventually lead to interactors
                if (cvState.filterByInteractorDescendants) {
                    const hasInteractorsMap = window.getHasInteractorsInSubtree?.();
                    if (hasInteractorsMap && hasInteractorsMap.get(childId) === false) {
                        // Skip pathways without interactor descendants
                        // UNLESS it's manually selected (via cvState.selectedRoots)
                        if (!cvState.selectedRoots.has(childId)) {
                            return;
                        }
                    }
                }

                // ✅ FIX: Check if child already exists (from ancestry chain)
                if (!parentNode.children) parentNode.children = [];
                const existingChild = parentNode.children.find(c => c.id === childId);
                if (existingChild) {
                    // Child already in tree from ancestry chain, skip adding duplicate
                    return;
                }

                const raw = cvState.rootPathways.find(pw => (pw.id || `pathway_${(pw.name || 'unknown').replace(/\s+/g, '_')}`) === childId);

                if (raw) {
                    const childNode = createPathwayNode(childId, raw, hierarchyMap);
                    parentNode.children.push(childNode);
                    // Recursively process this new child's descendants if expanded
                    processChildren(childNode);
                }
            });
        }

        // B. Add Leaf Interactors (Iterative Hierarchical Assignment)
        if (parentNode.raw && parentNode.raw.interactor_ids) {
            // Base interactors (query-related, always shown)
            let pathwayInteractors = [...parentNode.raw.interactor_ids];

            // Cross-query interactors (shown when checkbox is ON)
            const crossQueryIds = parentNode.raw.cross_query_interactor_ids || [];
            const crossQueryInteractions = parentNode.raw.cross_query_interactions || [];
            if (cvState.showCrossQuery && crossQueryIds.length > 0) {
                if (cvState.mergeCrossQuery) {
                    // Merge mode: add cross-query proteins to main interactor list
                    pathwayInteractors = [...pathwayInteractors, ...crossQueryIds];
                }
                // Separate mode is handled after the main tree is built
            }

            // Build pathway-scoped edge set from this pathway's own interactions.
            const pwInteractions = [
                ...(parentNode.raw.interactions || []),
                // Include cross-query edges when checkbox ON
                ...(cvState.showCrossQuery ? crossQueryInteractions : []),
            ];
            const pathwayInteractorSet = new Set(pathwayInteractors);
            const addPathwayEndpoint = (proteinId) => {
                if (!proteinId || proteinId === SNAP.main) return;
                pathwayInteractorSet.add(proteinId);
            };
            const pwEdgeSet = new Set();
            pwInteractions.forEach(ix => {
                if (ix.source && ix.target) {
                    pwEdgeSet.add(`${ix.source}|${ix.target}`);
                    pwEdgeSet.add(`${ix.target}|${ix.source}`);
                    addPathwayEndpoint(ix.source);
                    addPathwayEndpoint(ix.target);
                }
            });
            pathwayInteractors = Array.from(pathwayInteractorSet);

            // Pathway-scoped parent finder: only returns parents connected via pathway interactions
            function findPathwayParent(childId, candidates) {
                for (const candidateId of candidates) {
                    if (pwEdgeSet.has(`${candidateId}|${childId}`)) {
                        return candidateId;
                    }
                }
                return null;
            }

            // Track all created nodes for lookup
            const nodesById = new Map();
            const unassignedIds = new Set(pathwayInteractors);
            const assignedIds = new Set(); // Nodes successfully placed in the tree

            // --- SETUP: Identify Anchors and Split by Direction ---
            // Pass 1 now respects the flow relative to the Main Protein (e.g., ATXN3).
            // - Downstream (ATXN3 -> Int): Group under a single ATXN3 parent node.
            // - Upstream (Int -> ATXN3): Int becomes the root, ATXN3 becomes its child.

            // Use pathway edges to find query-adjacent anchors in THIS pathway.
            // Chain-backed net effects are deliberately not direct anchors:
            // the chain pre-pass renders the terminal through its hop path
            // (e.g. TDP43 -> GLE1 -> DDX3X). Rendering the terminal as a
            // direct child of the query creates a fake direct-looking card.
            const directInteractors = pathwayInteractors.filter(intId => {
                const pairInteractions = pwInteractions.filter(i =>
                    (i.source === SNAP.main && i.target === intId) ||
                    (i.source === intId && i.target === SNAP.main)
                );
                if (pairInteractions.length === 0) return false;
                if (pairInteractions.some(i => getCvInteractionLocus(i) !== 'net_effect_claim')) {
                    return true;
                }
                return !pairInteractions.some(i =>
                    getCvInteractionLocus(i) === 'net_effect_claim' &&
                    i.chain_id &&
                    Array.isArray(i.chain_members) &&
                    i.chain_members.length > 2
                );
            });
            const downstreamAnchors = [];
            const upstreamAnchors = [];

            directInteractors.forEach(intId => {
                if (!SNAP || !SNAP.interactions) return;

                // Look up direction from pathway interactions first, fall back to global
                const interaction = pwInteractions.find(i =>
                    (i.source === SNAP.main && i.target === intId) ||
                    (i.source === intId && i.target === SNAP.main)
                ) || SNAP.interactions.find(i =>
                    (i.source === SNAP.main && i.target === intId) ||
                    (i.source === intId && i.target === SNAP.main)
                );

                let isUpstream = false;
                if (interaction) {
                    // Primary check: explicit direction field
                    if (interaction.direction === 'primary_to_main') {
                        isUpstream = true;
                    }
                    // Secondary check: source/target fields (ground truth set by backend)
                    // Backend sets source=interactor, target=query for primary_to_main (app.py:495-498)
                    else if (interaction.source === intId && interaction.target === SNAP.main) {
                        isUpstream = true;
                    }
                    // If bi-directional or undefined, default to Downstream
                }

                if (isUpstream) {
                    upstreamAnchors.push(intId);
                } else {
                    downstreamAnchors.push(intId);
                }
            });

            // === UNIFIED LAYOUT: Upstream → ATXN3 → Downstream ===
            // All anchors (upstream AND downstream) share ONE central ATXN3 node.
            // Layout: [Pathway] → [Upstream1] → [ATXN3] → [Downstream1]
            //         [Pathway] → [Upstream2] ↗          ↘ [Downstream2]
            // In tree form: upstream anchors are children of pathway,
            // ATXN3 is child of LAST upstream anchor (or pathway if no upstream),
            // downstream anchors are children of ATXN3.

            let centralMainNode = null;

            // === UPSTREAM → ATXN3 → DOWNSTREAM LAYOUT ===
            // Layout: [Pathway] → [RAD23A] ↘
            //         [Pathway] → [RAD23B] → [ATXN3] → [E2F1, KLF4, ...]
            //         [Pathway] → [UBE4B]  ↗
            //
            // In tree structure: ATXN3 is child of FIRST upstream anchor.
            // Other upstream anchors are siblings (children of pathway).
            // Extra links from other upstream anchors → ATXN3 are added post-layout.

            if (upstreamAnchors.length > 0) {
                // Create all upstream anchor nodes as children of the pathway
                upstreamAnchors.forEach((intId, idx) => {
                    const anchorNode = createInteractorNode(intId, parentNode.id);
                    nodesById.set(intId, anchorNode);
                    addChildIfUnique(parentNode, anchorNode);
                    assignedIds.add(intId);
                    unassignedIds.delete(intId);

                    if (idx === 0) {
                        // FIRST upstream anchor: ATXN3 is its child (natural tree link)
                        centralMainNode = createInteractorNode(SNAP.main, anchorNode.id);
                        centralMainNode.isQueryProtein = true;
                        centralMainNode._uid = SNAP.main + '::' + parentNode.id;
                        nodesById.set(SNAP.main, centralMainNode);
                        assignedIds.add(SNAP.main);
                        addChildIfUnique(anchorNode, centralMainNode);
                    } else {
                        // OTHER upstream anchors: store for extra links later
                        // Tag them so we can find them and draw extra links to ATXN3
                        anchorNode._extraLinkToMain = true;
                        anchorNode._extraLinkTargetUid = centralMainNode._uid;
                    }
                });

                // Attach downstream anchors as children of the shared ATXN3 node
                downstreamAnchors.forEach(intId => {
                    const node = createInteractorNode(intId, centralMainNode.id);
                    nodesById.set(intId, node);
                    addChildIfUnique(centralMainNode, node);
                    assignedIds.add(intId);
                    unassignedIds.delete(intId);
                });

            } else if (downstreamAnchors.length > 0) {
                // No upstream: ATXN3 is child of pathway directly
                centralMainNode = createInteractorNode(SNAP.main, parentNode.id);
                centralMainNode.isQueryProtein = true;
                centralMainNode._uid = SNAP.main + '::' + parentNode.id;
                nodesById.set(SNAP.main, centralMainNode);
                assignedIds.add(SNAP.main);
                addChildIfUnique(parentNode, centralMainNode);

                downstreamAnchors.forEach(intId => {
                    const node = createInteractorNode(intId, centralMainNode.id);
                    nodesById.set(intId, node);
                    addChildIfUnique(centralMainNode, node);
                    assignedIds.add(intId);
                    unassignedIds.delete(intId);
                });
            } else if (pathwayInteractors.length > 0) {
                // Edge case: No direct anchors at all (only indirect).
                centralMainNode = createInteractorNode(SNAP.main, parentNode.id);
                centralMainNode.isQueryProtein = true;
                centralMainNode._uid = SNAP.main + '::' + parentNode.id;
                nodesById.set(SNAP.main, centralMainNode);
                assignedIds.add(SNAP.main);
                addChildIfUnique(parentNode, centralMainNode);
            }

            // Note: If centralMainNode acts as a catch-all, ensure it exists for Extensions/Islands
            if (!centralMainNode && nodesById.has(SNAP.main)) {
                centralMainNode = nodesById.get(SNAP.main);
            }

            // --- CHAIN PRE-PASS: Render chains as linear sequences ---
            // Groups chain links by chain_id and builds parent→child chains
            // BEFORE the extension pass, so chains render linearly instead of as fans.
            // Preserve complete chain order. Pathway-scoped endpoints above
            // decide whether a chain belongs here; once it does, render every
            // protein in the stored chain so non-query hops do not collapse
            // back onto the query node.
            //
            // P3.3: pass the pathway name so groupChainsByChainId can require
            // either both endpoints in this pathway OR an explicit chain
            // pathway match — the "contains main protein" rule that used to
            // gate this pre-pass is removed below for the same reason.
            const _pathwayNameForChains = (parentNode.raw && parentNode.raw.name) || '';
            const chainGroups = groupChainsByChainId(pathwayInteractors, _pathwayNameForChains);

            for (const [chainId, chainGroup] of chainGroups) {
                const chainColor = getChainColor(chainId);
                const chainProteins = [...chainGroup.proteins].filter(Boolean);
                const chainArrows = Array.isArray(chainGroup.arrows) ? chainGroup.arrows : [];
                if (chainProteins.length < 2) continue; // Need at least 2 for a chain
                // Layer 3 of CLAUDE_DOCS/11_CHAIN_TOPOLOGY.md: per-edge
                // arrow verb so the renderer can label each chain link
                // with the biological direction. Indexed by destination
                // chain position k (k=0 root has no inbound arrow).
                const inboundArrowFor = (k) => {
                    if (k <= 0) return null;
                    const entry = chainArrows[k - 1];
                    if (!entry || typeof entry !== 'object') return null;
                    const arrow = entry.arrow;
                    return (typeof arrow === 'string' && arrow.trim()) ? arrow.trim().toLowerCase() : null;
                };
                // P3.3: a chain renders here only when it earned membership
                // via pathway-endpoint overlap or explicit chain pathway
                // assignment (see groupChainsByChainId). The old fallback
                // "chainIncludesMain" is gone — every ATXN3 chain trivially
                // contained ATXN3, which is exactly why the screenshot
                // showed 6 unrelated cascades all under PQC.
                const chainTouchesPathway = chainProteins.some(p => pathwayInteractorSet.has(p));
                if (!chainTouchesPathway) continue;

                // Find the anchor point for this chain.
                let anchorNodeId = null;
                let startIdx = 0;
                for (let k = 0; k < chainProteins.length; k++) {
                    if (assignedIds.has(chainProteins[k])) {
                        anchorNodeId = chainProteins[k];
                        startIdx = k;
                        break;
                    }
                }

                // If the first assigned protein is in the middle or tail,
                // render an independent chain sequence. Otherwise a
                // TBK1→OPTN→Ubiquitin→TDP43 chain becomes TDP43→TBK1→OPTN,
                // which is exactly the query-centric distortion users see.
                if (anchorNodeId && startIdx > 0) {
                    anchorNodeId = null;
                    startIdx = 0;
                }

                // F2/F7: removed the old "attach query-led chain to central
                // main node" branch. That branch (a) referenced the now-
                // undefined `chainIncludesMain` (runtime bug), and (b) was
                // the reason every chain involving the query collapsed
                // into a single visual blob under the pathway. Without it,
                // each chain instance gets its own root via the
                // independentChainRoot path below — so the same protein
                // (e.g. ATXN3, PERK) appears once per chain, which is
                // exactly the user's "show every chain separately" ask.

                let prevNode = null;
                let independentChainRoot = false;
                if (!anchorNodeId) {
                    const firstId = chainProteins[0];
                    const rootNode = createInteractorNode(firstId, parentNode.id);
                    rootNode._uid = `${firstId}::chain::${chainId}::0`;
                    rootNode._chainId = chainId;
                    rootNode._chainPosition = 0;
                    rootNode._chainLength = chainProteins.length;
                    rootNode._chainColor = chainColor;
                    rootNode._chainProteins = chainProteins;
                    rootNode._isIndependentChainRoot = true;
                    addChildIfUnique(parentNode, rootNode);
                    prevNode = rootNode;
                    independentChainRoot = true;
                    startIdx = 0;
                    assignedIds.add(firstId);
                    unassignedIds.delete(firstId);
                } else {
                    prevNode = (startIdx >= 0) ? nodesById.get(anchorNodeId) : centralMainNode;
                }

                if (!prevNode) continue;

                for (let k = 0; k < chainProteins.length; k++) {
                    const protId = chainProteins[k];

                    if (k === startIdx && assignedIds.has(protId)) {
                        prevNode = nodesById.get(protId) || prevNode;
                        continue;
                    }

                    // Query-led chains share the central query node. Query-tail
                    // or non-query chains are independent sequences, so the
                    // query may appear as a terminal chain participant instead
                    // of becoming the parent of unrelated upstream hops.
                    if (protId === SNAP.main && nodesById.has(SNAP.main) && !independentChainRoot) {
                        prevNode = nodesById.get(SNAP.main);
                        continue;
                    }

                    // When a chain protein already appears as a direct
                    // interactor under this pathway, emit a SECOND node tagged
                    // as a chain participant so the same HGNC symbol can appear
                    // twice in one card — once in the direct subtree, once in
                    // the chain subtree. The user asked for this explicitly so
                    // that chain participation is visible even when the same
                    // protein also has a direct claim. Keyed by a separate uid
                    // so addChildIfUnique treats them as distinct.
                    if (assignedIds.has(protId)) {
                        const duplicate = createInteractorNode(protId, prevNode.id);
                        duplicate._uid = `${protId}::chain::${chainId}::${k}`;
                        duplicate._chainId = chainId;
                        duplicate._chainPosition = k;
                        duplicate._chainLength = chainProteins.length;
                        duplicate._chainColor = chainColor;
                        duplicate._chainProteins = chainProteins;
                        duplicate._isChainDuplicate = true;
                        duplicate._duplicateOf = protId;
                        duplicate._inboundChainArrow = inboundArrowFor(k);
                        addChildIfUnique(prevNode, duplicate);
                        prevNode = duplicate;
                        continue;
                    }

                    const node = createInteractorNode(protId, prevNode.id);
                    node._chainId = chainId;
                    node._chainPosition = k;
                    node._chainLength = chainProteins.length;
                    node._chainColor = chainColor;
                    node._chainProteins = chainProteins;
                    node._inboundChainArrow = inboundArrowFor(k);

                    nodesById.set(protId, node);
                    addChildIfUnique(prevNode, node);
                    assignedIds.add(protId);
                    unassignedIds.delete(protId);
                    prevNode = node;
                }
            }

            // --- PASS 2: Extensions (Iteratively attach to Assigned Nodes) ---
            // Uses pathway-scoped edges so only claim-backed connections are created.
            let progress = true;
            while (progress && unassignedIds.size > 0) {
                progress = false;
                const currentUnassigned = Array.from(unassignedIds);

                currentUnassigned.forEach(childId => {
                    const candidates = Array.from(assignedIds);
                    // Use pathway-scoped parent finder (not global findUpstreamParent)
                    const parentId = findPathwayParent(childId, candidates);

                    if (parentId && nodesById.has(parentId)) {
                        const parentNodeForIndirect = nodesById.get(parentId);

                        // Create Node
                        const node = createInteractorNode(childId, parentNodeForIndirect.id);
                        nodesById.set(childId, node);

                        // Add to Parent's children
                        addChildIfUnique(parentNodeForIndirect, node);

                        // Mark assigned
                        assignedIds.add(childId);
                        unassignedIds.delete(childId);
                        progress = true; // Continue to next iteration
                    }
                });
            }

            // --- PASS 3: Island Resolution (Disconnected Sub-trees) ---
            // Islands usually imply missing intermediates.
            // We attach them to the Central Main Node (ATXN3) if it exists, as the most logical parent.
            if (unassignedIds.size > 0) {
                // Ensure we have a central anchor for islands
                if (!centralMainNode) {
                    centralMainNode = createInteractorNode(SNAP.main, parentNode.id);
                    centralMainNode.isQueryProtein = true;
                    centralMainNode._uid = SNAP.main + '::' + parentNode.id;
                    nodesById.set(SNAP.main, centralMainNode);
                    assignedIds.add(SNAP.main);
                    addChildIfUnique(parentNode, centralMainNode);
                }
            }

            while (unassignedIds.size > 0) {
                const currentUnassigned = Array.from(unassignedIds);
                let madeAssignment = false;

                // A. Try to find Local Roots (using pathway-scoped edges)
                const localRoots = currentUnassigned.filter(nodeId => {
                    const internalParent = findPathwayParent(nodeId, currentUnassigned.filter(id => id !== nodeId));
                    return !internalParent;
                });

                if (localRoots.length > 0) {
                    localRoots.forEach(rootId => {
                        // Attach to Central Main Node (ATXN3)
                        const node = createInteractorNode(rootId, centralMainNode.id);
                        nodesById.set(rootId, node);

                        addChildIfUnique(centralMainNode, node);

                        assignedIds.add(rootId);
                        unassignedIds.delete(rootId);
                    });
                    madeAssignment = true;
                } else {
                    // B. Cycle Detection
                    const cycleBreaker = currentUnassigned[0];
                    const node = createInteractorNode(cycleBreaker, centralMainNode.id);
                    nodesById.set(cycleBreaker, node);

                    addChildIfUnique(centralMainNode, node);

                    assignedIds.add(cycleBreaker);
                    unassignedIds.delete(cycleBreaker);
                    madeAssignment = true;
                }

                // C. Re-run Extensions (Pass 2 Logic)
                if (madeAssignment && unassignedIds.size > 0) {
                    let extensionProgress = true;
                    while (extensionProgress && unassignedIds.size > 0) {
                        extensionProgress = false;
                        const validParents = Array.from(assignedIds);
                        const leftovers = Array.from(unassignedIds);

                        leftovers.forEach(childId => {
                            const parentId = findPathwayParent(childId, validParents);
                            if (parentId && nodesById.has(parentId)) {
                                const parentNode = nodesById.get(parentId);
                                const node = createInteractorNode(childId, parentNode.id);
                                nodesById.set(childId, node);

                                addChildIfUnique(parentNode, node);

                                assignedIds.add(childId);
                                unassignedIds.delete(childId);
                                extensionProgress = true;
                            }
                        });
                    }
                }
            }
            // --- CROSS-QUERY SEPARATE BRANCHES ---
            // When showCrossQuery is ON but mergeCrossQuery is OFF, render
            // cross-query proteins as separate sub-trees under the pathway.
            if (cvState.showCrossQuery && !cvState.mergeCrossQuery && crossQueryIds.length > 0) {
                // Group cross-query interactions by hub protein (the protein
                // that connects query-world to cross-query-world)
                const cqHubs = new Map(); // hubProtein -> Set of connected cross-query proteins
                for (const ix of crossQueryInteractions) {
                    const src = ix.source;
                    const tgt = ix.target;
                    // The "hub" is whichever protein is already assigned (in the main tree)
                    if (assignedIds.has(src) && !assignedIds.has(tgt)) {
                        if (!cqHubs.has(src)) cqHubs.set(src, new Set());
                        cqHubs.get(src).add(tgt);
                    } else if (assignedIds.has(tgt) && !assignedIds.has(src)) {
                        if (!cqHubs.has(tgt)) cqHubs.set(tgt, new Set());
                        cqHubs.get(tgt).add(src);
                    } else if (!assignedIds.has(src) && !assignedIds.has(tgt)) {
                        // Neither assigned — pick one as hub
                        if (!cqHubs.has(src)) cqHubs.set(src, new Set());
                        cqHubs.get(src).add(tgt);
                    }
                }

                // For each hub, create a sub-tree branch
                for (const [hubId, children] of cqHubs) {
                    let hubNode;
                    if (nodesById.has(hubId)) {
                        hubNode = nodesById.get(hubId);
                    } else {
                        // Hub is a cross-query protein — create as child of pathway
                        hubNode = createInteractorNode(hubId, parentNode.id);
                        hubNode._isCrossQuery = true;
                        nodesById.set(hubId, hubNode);
                        addChildIfUnique(parentNode, hubNode);
                        assignedIds.add(hubId);
                    }
                    for (const childId of children) {
                        if (assignedIds.has(childId)) continue;
                        const node = createInteractorNode(childId, hubNode.id);
                        node._isCrossQuery = true;
                        nodesById.set(childId, node);
                        addChildIfUnique(hubNode, node);
                        assignedIds.add(childId);
                    }
                }
            }
        }
    };

    // ✅ FIX: Traverse ALL nodes in tree (including those from ancestry chains)
    // Then add children/interactions for expanded nodes
    function traverseAndProcess(node) {
        if (!node) return;

        // First, process this node (add children if expanded)
        processChildren(node);

        // Then traverse existing children (from ancestry chains)
        if (node.children) {
            node.children.forEach(child => {
                // Only traverse pathway nodes, not interactor nodes
                if (child.type === 'pathway') {
                    traverseAndProcess(child);
                }
            });
        }
    }

    // Start traversal from root's immediate children
    if (rootNode.children) {
        rootNode.children.forEach(traverseAndProcess);
    }

    return rootNode;
}

// Helper: Add child to parent only if not already present.
// The uniqueness key prefers ``_uid`` (set by the chain-duplicate
// construction, e.g. ``TP53::chain::42::1``) and falls back to ``id``
// for ordinary nodes. Before this fix the check compared ``id`` only,
// so two DIFFERENT chain branches that both routed through the same
// protein (e.g. TDP43→TP53→BAX and TDP43→TP53→CASP3 under one pathway)
// collapsed into a single TP53 node because their _uid differed but
// ``id`` was the same symbol. D3's render-time key function already
// respects ``_uid || id``, so aligning the dedup makes the tree match
// what the renderer is already prepared for.
function addChildIfUnique(parent, child) {
    if (!parent.children) parent.children = [];
    if (child && child.type === 'interactor' && !child.pathwayId) {
        const inheritedContext = parent?._pathwayContext ||
            (parent?.type === 'pathway'
                ? { id: parent.id, name: parent.raw?.name || parent.label || parent.id }
                : null);
        if (inheritedContext?.id) {
            child.pathwayId = inheritedContext.id;
            child._pathwayContext = inheritedContext;
        }
    }
    const childKey = child._uid || child.id;
    const exists = parent.children.some(c => (c._uid || c.id) === childKey);
    if (!exists) {
        parent.children.push(child);
        return true;
    }
    return false;
}

// Helper: Check direct connection to Main
function isDirectlyConnectedToMain(nodeId) {
    if (!SNAP || !SNAP.interactions) return false;
    // Check both directions
    return SNAP.interactions.some(i =>
        (
            (i.source === SNAP.main && i.target === nodeId) ||
            (i.source === nodeId && i.target === SNAP.main)
        ) &&
        // EXCLUDE 'indirect' types to force them into Pass 2
        // unless they are explicitly masquerading as direct (should be handled by source switch in app.py)
        // Ideally, if app.py switched source to upstream, this strict check isn't needed,
        // but adding it ensures safety against data artifacts.
        i.type !== 'indirect'
    );
}

// Helper: Find upstream parent for an indirect node within a candidate set
function findUpstreamParent(childId, candidates) {
    if (!SNAP || !SNAP.interactions) return null;

    // Strategy 1: Look for explicit 'upstream_interactor' metadata
    // This is specific to our V2 pipeline's indirect interaction structure
    // We look for any interaction involving the child where the upstream_interactor is a candidate
    const indirectInteraction = SNAP.interactions.find(i =>
        (i.target === childId || i.source === childId) &&
        i.upstream_interactor &&
        candidates.includes(i.upstream_interactor)
    );

    if (indirectInteraction) {
        return indirectInteraction.upstream_interactor;
    }

    // Strategy 1.5: Use mediator_chain ordering to find the nearest assigned ancestor.
    // For a chain [MAIN, A, B, C], when processing C, walk the chain in reverse to
    // find the highest-depth candidate — this ensures correct topology (C→B, not C→MAIN).
    const chainInter = SNAP.interactions.find(i =>
        (i.target === childId || i.source === childId) &&
        i.mediator_chain && Array.isArray(i.mediator_chain) && i.mediator_chain.length > 0
    );
    if (chainInter) {
        const chain = chainInter.mediator_chain;
        for (let k = chain.length - 1; k >= 0; k--) {
            if (candidates.includes(chain[k])) {
                return chain[k];
            }
        }
    }

    // Strategy 2: Topology - Look for direct link from Candidate -> Child (or swapped)
    const parent = candidates.find(candidateId => {
        return SNAP.interactions.some(i =>
            (i.source === candidateId && i.target === childId) ||
            (i.source === childId && i.target === candidateId)
        );
    });

    return parent || null;
}

function createInteractorNode(intId, parentId) {
    let rel = null;
    if (window.getNodeRelationship) {
        const isPathway = parentId.startsWith('pathway_');
        if (isPathway) {
            rel = window.getNodeRelationship(intId);
        } else {
            rel = getLocalRelationship(parentId, intId);
        }
    }

    // S3: Detect chain-link status from SNAP interaction metadata.
    // The S2 backend stamps _is_chain_link, _chain_position, _chain_length
    // on every chain segment in the payload.
    let isChainLink = false;
    let chainPosition = -1;
    let chainLength = 0;
    if (SNAP && SNAP.interactions) {
        const chainInter = SNAP.interactions.find(i =>
            (i.source === intId || i.target === intId) && i._is_chain_link
        );
        if (chainInter) {
            isChainLink = true;
            chainPosition = chainInter._chain_position ?? -1;
            chainLength = chainInter._chain_length ?? 0;
        }
    }

    // L5.1 — Detect pseudo entity (RNA, Ubiquitin, Proteasome, ...). The
    // backend stamps _source_is_pseudo / _target_is_pseudo on chain-link
    // rows and _partner_is_pseudo on direct interactor rows. We also fall
    // back to a static client-side whitelist for older payloads.
    let isPseudo = false;
    const _PSEUDO_FALLBACK = new Set([
        'RNA','mRNA','pre-mRNA','tRNA','rRNA','lncRNA','miRNA','snRNA','snoRNA',
        'DNA','ssDNA','dsDNA','Ubiquitin','SUMO','NEDD8','Proteasome','Ribosome',
        'Spliceosome','Actin','Tubulin','Stress Granules','P-bodies'
    ]);
    if (SNAP && SNAP.interactions) {
        for (const i of SNAP.interactions) {
            if (i.source === intId || i.target === intId || i.primary === intId) {
                if ((i.source === intId && i._source_is_pseudo) ||
                    (i.target === intId && i._target_is_pseudo) ||
                    (i.primary === intId && i._partner_is_pseudo)) {
                    isPseudo = true;
                    break;
                }
            }
        }
    }
    if (!isPseudo && _PSEUDO_FALLBACK.has(intId)) isPseudo = true;
    if (!isPseudo && (intId || '').endsWith('mRNA')) isPseudo = true;

    return {
        id: intId,
        _uid: intId + '::' + parentId,
        type: 'interactor',
        label: intId,
        parentId: parentId,
        contextText: rel ? rel.text : '',
        arrowType: rel ? rel.arrow : 'binds',
        isDownstream: rel ? (rel.direction === 'downstream') : false,
        _isChainLink: isChainLink,
        _chainPosition: chainPosition,
        _chainLength: chainLength,
        _isPseudo: isPseudo,
        children: []
    };
}

function getLocalRelationship(parentId, childId) {
    if (!SNAP || !SNAP.interactions) return null;
    const interaction = SNAP.interactions.find(i =>
        (i.source === parentId && i.target === childId) ||
        (i.source === childId && i.target === parentId)
    );

    if (!interaction) return { text: 'Associated', arrow: 'binds' };

    const isDownstream = interaction.source === parentId;
    const arrow = interaction.arrow || 'binds';
    const locus = getCvInteractionLocus(interaction);

    const action = arrow === 'activates' ? 'activates' :
        arrow === 'inhibits' ? 'inhibits' :
            arrow === 'regulates' ? 'regulates' : 'binds';

    const sourceId = isDownstream ? parentId : childId;
    const targetId = isDownstream ? childId : parentId;
    let text = `${sourceId} ${action} ${targetId}`;
    if (locus === 'net_effect_claim') {
        const via = Array.isArray(interaction.via) ? interaction.via.filter(Boolean).join(' -> ') : '';
        text = via
            ? `Net via ${via}: ${sourceId} ${action} ${targetId}`
            : `Net effect: ${sourceId} ${action} ${targetId}`;
    } else if (locus === 'chain_hop_claim') {
        text = `Hop: ${sourceId} ${action} ${targetId}`;
    }

    if (isDownstream) {
        return {
            direction: 'downstream',
            arrow: arrow,
            text: text,
            locus: locus
        };
    } else {
        return {
            direction: 'upstream',
            arrow: arrow,
            text: text,
            locus: locus
        };
    }
}

function createPathwayNode(id, raw, hierarchyMap) {
    const hier = hierarchyMap.get(id);
    return {
        id: id,
        label: raw.name,
        type: 'pathway',
        level: raw.hierarchy_level || 0,
        isLeaf: hier?.is_leaf,
        raw: raw, // Keep ref to raw for interactor lookup
        _childrenCount: (hier?.child_ids?.length || 0) + (raw.interactor_ids?.length || 0)
    };
}


// ============================================================================
// INTERACTOR HIERARCHY (for Interactors Mode)
// ============================================================================

// ============================================================================
// INTERACTORS DAG MODE — Network graph with directional arrow-tipped edges
// ============================================================================

function isDatabasedInteraction(interaction) {
    if (!interaction || typeof interaction !== 'object') return false;
    return interaction._isDatabased === true ||
        interaction.isDatabased === true ||
        interaction.origin === 'databased';
}

function getInteractorModeInteractions() {
    const fallback = (typeof SNAP !== 'undefined' && Array.isArray(SNAP.interactions)) ? SNAP.interactions : [];
    if (typeof window === 'undefined' || typeof window.getAugmentedNetworkInteractions !== 'function') {
        return fallback;
    }

    try {
        const augmented = window.getAugmentedNetworkInteractions();
        if (!Array.isArray(augmented)) return fallback;
        if (augmented.length === 0 && fallback.length > 0) return fallback;
        return augmented;
    } catch (err) {
        console.warn('Card interactor mode fallback to SNAP.interactions:', err);
        return fallback;
    }
}

/**
 * Build a full interaction graph from SNAP data for DAG layout.
 * Returns { nodes: Map<id, nodeData>, edges: Array<edgeData> }
 */
function buildInteractorGraph() {
    const mainId = (typeof SNAP !== 'undefined' && SNAP.main) ? SNAP.main : 'Unknown';
    const interactions = getInteractorModeInteractions();
    const pathways = (typeof SNAP !== 'undefined' && SNAP.pathways) ? SNAP.pathways : [];
    const queryInteractions = interactions.filter(inter => !isDatabasedInteraction(inter));

    const nodes = new Map();
    const edges = [];
    const edgeSet = new Set(); // Dedup edges by "src->tgt"

    // Ensure main protein node exists
    nodes.set(mainId, {
        id: mainId, type: 'main', label: mainId,
        arrowType: null, layer: -1, pathwayMemberships: [],
        origin: 'query',
        isQueryDerived: true,
        _isDatabased: false
    });

    function ensureNode(nodeId, originHint = 'query') {
        if (!nodes.has(nodeId)) {
            const memberships = pathways.filter(pw => (pw.interactor_ids || []).includes(nodeId));
            nodes.set(nodeId, {
                id: nodeId,
                type: 'interactor',
                label: nodeId,
                arrowType: 'binds',
                layer: -1,
                pathwayMemberships: memberships.map(pw => pw.name || pw.id),
                origin: originHint,
                isQueryDerived: originHint !== 'databased',
                _isDatabased: originHint === 'databased'
            });
        } else if (originHint === 'query') {
            // Query source always wins over databased source metadata.
            const existing = nodes.get(nodeId);
            existing.origin = 'query';
            existing.isQueryDerived = true;
            existing._isDatabased = false;
        }
        return nodes.get(nodeId);
    }

    // Track direct arrows per protein (for node coloring priority — direct > indirect)
    const directArrows = new Map(); // proteinId -> arrow from direct interaction

    // CRITICAL: Sort interactions so direct interactions are processed FIRST
    // This ensures chain links (like RHEB→MTOR activates) override indirect net-effects (inhibits)
    const sortedInteractions = [...interactions].sort((a, b) => {
        const aType = a.interaction_type || a.type || 'direct';
        const bType = b.interaction_type || b.type || 'direct';
        if (aType === 'direct' && bType !== 'direct') return -1;
        if (aType !== 'direct' && bType === 'direct') return 1;
        return 0;
    });

    sortedInteractions.forEach(inter => {
        const src = inter.source;
        const tgt = inter.target;
        if (!src || !tgt) return;

        const arrow = inter.arrow || 'binds';
        const interType = inter.interaction_type || inter.type || 'direct';
        const direction = inter.direction || 'main_to_primary';
        const origin = isDatabasedInteraction(inter) ? 'databased' : 'query';

        // Apply arrow type filter
        if (!cvState.interactorArrowFilter.has(arrow)) return;

        // Apply search filter
        if (cvState.interactorSearchQuery) {
            const q = cvState.interactorSearchQuery.toLowerCase();
            if (!src.toLowerCase().includes(q) && !tgt.toLowerCase().includes(q)) return;
        }

        // Add nodes and carry source origin metadata.
        ensureNode(src, origin);
        ensureNode(tgt, origin);

        // Track direct arrows for node coloring (direct interactions are ALWAYS authoritative)
        if (interType === 'direct' && origin !== 'databased') {
            // Arrow type for tgt when src is main (downstream)
            if (src === mainId) directArrows.set(tgt, arrow);
            // Arrow type for src when tgt is main (upstream)
            if (tgt === mainId) directArrows.set(src, arrow);
            // Chain links (between non-main proteins): ALWAYS overwrite
            // This is critical for RHEB→MTOR where direct=activates but indirect=inhibits
            if (src !== mainId && tgt !== mainId) {
                directArrows.set(tgt, arrow); // Always set, don't check if exists
            }
        }

        // Add edge (dedup)
        const edgeKey = `${src}->${tgt}::${interType}::${arrow}::${origin}`;
        if (!edgeSet.has(edgeKey)) {
            edgeSet.add(edgeKey);
            edges.push({
                source: src, target: tgt,
                arrow: arrow, direction: direction,
                interaction_type: interType,
                interactionType: interType,
                origin: origin,
                _isDatabased: origin === 'databased',
                data: inter
            });
        }
    });

    // Set node arrowType — prefer direct interaction arrow, fall back to any interaction
    nodes.forEach((node, id) => {
        if (id === mainId) return;
        if (directArrows.has(id)) {
            node.arrowType = directArrows.get(id);
        } else {
            // Fallback: find any interaction involving this node
            const anyInter = interactions.find(i =>
                (i.source === id || i.target === id) && i.arrow
            );
            if (anyInter) node.arrowType = anyInter.arrow;
        }
    });

    // Classify proteins by role (upstream/downstream/bidirectional)
    const roles = (typeof getProteinsByRole === 'function')
        ? getProteinsByRole(queryInteractions, mainId)
        : { upstream: new Set(), downstream: new Set(), bidirectional: new Set() };

    // Apply direction filter: remove nodes that don't match direction filter
    const validProteins = new Set([mainId]);
    if (cvState.interactorDirectionFilter.has('upstream')) roles.upstream.forEach(p => validProteins.add(p));
    if (cvState.interactorDirectionFilter.has('downstream')) roles.downstream.forEach(p => validProteins.add(p));
    if (cvState.interactorDirectionFilter.has('bidirectional')) roles.bidirectional.forEach(p => validProteins.add(p));

    // Iterative expansion: include chain proteins reachable from validProteins.
    // A single pass misses chains of 3+ proteins (MAIN→A→B→C) because B may
    // not be in validProteins when C's edge is evaluated. Loop until stable.
    let _expansionChanged = true;
    while (_expansionChanged) {
        _expansionChanged = false;
        edges.forEach(e => {
            if (validProteins.has(e.source) && !validProteins.has(e.target)) {
                validProteins.add(e.target);
                _expansionChanged = true;
            }
            if (validProteins.has(e.target) && !validProteins.has(e.source)) {
                validProteins.add(e.source);
                _expansionChanged = true;
            }
        });
    }

    // Remove filtered nodes and their edges
    nodes.forEach((_, id) => {
        if (!validProteins.has(id)) nodes.delete(id);
    });

    // Annotate each node with its semantic role for layout
    // Pre-compute which proteins mediate indirect interactions (for bidirectional placement).
    //
    // Chain-mediator promotion: a node that appears as an interior
    // position of ANY chain (chain_proteins[k] where 0 < k < length-1)
    // — i.e. not the query endpoint and not the indirect target — gets
    // role='chain' so the DAG topology pass assigns its layer relative
    // to its upstream neighbor. Before this, mediators inherited the
    // query's upstream/downstream classification and got pinned to
    // layer 0 or 2, collapsing every 4+ protein chain into a 3-shell
    // Upstream/Query/Downstream display. The topology pass places
    // chain nodes linearly: A(1) → B(2) → C(3) → D(4).
    const mediatorSet = new Set();
    const chainInteriorSet = new Set();
    queryInteractions.forEach(i => {
        if (i.interaction_type === 'indirect' && i.upstream_interactor) {
            mediatorSet.add(i.upstream_interactor);
        }
        const fullChain = (i._chain_entity && i._chain_entity.chain_proteins)
            || (i.chain_context && i.chain_context.full_chain)
            || [];
        if (Array.isArray(fullChain) && fullChain.length >= 3) {
            for (let k = 1; k < fullChain.length - 1; k++) {
                const sym = fullChain[k];
                if (sym && sym !== mainId) chainInteriorSet.add(sym);
            }
        }
    });

    nodes.forEach((node, id) => {
        if (id === mainId) { node.role = 'main'; return; }
        const isUp = roles.upstream.has(id);
        const isDown = roles.downstream.has(id);
        const isBidir = roles.bidirectional.has(id);
        const isChainMediator = chainInteriorSet.has(id);

        if (isChainMediator) {
            // Chain mediator wins — topology pass assigns layer from
            // its upstream neighbor. Keeps 4+ protein chains linear.
            node.role = 'chain';
        } else if (isUp && !isDown && !isBidir) {
            // Exclusively upstream — only these go to layer 0
            node.role = 'upstream';
        } else if (isDown || (isUp && isDown) || isBidir) {
            // Any downstream interaction, or bidirectional binding → downstream side
            node.role = 'downstream';
        } else if (isUp) {
            node.role = 'upstream';
        } else {
            node.role = 'chain'; // interactor-to-interactor only
        }
    });

    return { nodes, edges: edges.filter(e => nodes.has(e.source) && nodes.has(e.target)) };
}

/**
 * Classify nodes into "core" (participate in main layer structure) and
 * "satellite" (only indirect edges — placed as extensions of their mediator).
 * A satellite is any non-main node where EVERY edge involving it is indirect.
 */
function classifyNodesForLayout(nodes, edges, mainId) {
    // Collect the set of edge types each node participates in
    const nodeEdgeTypes = new Map(); // nodeId -> Set('direct'|'indirect')
    edges.forEach(e => {
        const t = e.interaction_type === 'indirect' ? 'indirect' : 'direct';
        [e.source, e.target].forEach(id => {
            if (!nodeEdgeTypes.has(id)) nodeEdgeTypes.set(id, new Set());
            nodeEdgeTypes.get(id).add(t);
        });
    });

    const coreNodes = new Map();
    const satelliteNodes = new Map();

    nodes.forEach((node, id) => {
        if (id === mainId) {
            coreNodes.set(id, node);
            return;
        }
        const types = nodeEdgeTypes.get(id);
        // Satellite: exists AND only has indirect edges
        if (types && !types.has('direct')) {
            satelliteNodes.set(id, node);
        } else {
            coreNodes.set(id, node);
        }
    });

    // Validate: each satellite must have an incoming edge from a core source.
    // When arrow filters remove a protein's direct edge (e.g. inhibits filtered),
    // that protein loses its "core" status. Promote it back so its satellites work.
    let promoted = true;
    while (promoted) {
        promoted = false;
        for (const [id, node] of satelliteNodes) {
            const hasCoreIncoming = edges.some(e =>
                e.target === id && coreNodes.has(e.source)
            );
            if (!hasCoreIncoming) {
                coreNodes.set(id, node);
                satelliteNodes.delete(id);
                promoted = true;
                break; // restart after each promotion
            }
        }
    }

    // Partition edges
    const coreEdges = edges.filter(e => coreNodes.has(e.source) && coreNodes.has(e.target));
    const satelliteEdges = edges.filter(e => satelliteNodes.has(e.target) || satelliteNodes.has(e.source));

    return { coreNodes, satelliteNodes, coreEdges, satelliteEdges };
}

/**
 * Compute DAG layers using semantic role classification.
 * Layer 0: upstream proteins (act ON query)
 * Layer 1: query protein (center)
 * Layer 2: direct downstream (query acts ON them)
 * Layer 3+: chain nodes (interactor-to-interactor, e.g. indirect targets)
 * Assigns layer property to each node. Returns maxLayer.
 */
function computeDAGLayers(nodes, edges, mainId) {
    const mainNode = nodes.get(mainId);
    if (!mainNode) return 0;

    // --- Step 1: Assign base layers from node.role (set by buildInteractorGraph) ---
    nodes.forEach((node, id) => {
        switch (node.role) {
            case 'main':
                node.layer = 1;
                break;
            case 'upstream':
                node.layer = 0;
                break;
            case 'downstream':
                node.layer = 2;
                break;
            case 'bidirectional':
                // Fallback: bidirectional proteins default to downstream side
                node.layer = 2;
                break;
            case 'chain':
                node.layer = -1; // Mark for topology pass
                break;
            default:
                node.layer = -1; // Mark for topology pass
        }
    });

    // --- Step 2: Topology pass for chain nodes (place relative to source) ---
    let changed = true;
    let iterations = 0;
    while (changed && iterations < 10) {
        changed = false;
        iterations++;
        nodes.forEach((node, id) => {
            if (node.layer !== -1) return;
            // Find an incoming edge from a node that already has a layer
            for (const e of edges) {
                if (e.target === id && nodes.has(e.source) && nodes.get(e.source).layer !== -1) {
                    node.layer = nodes.get(e.source).layer + 1;
                    changed = true;
                    return;
                }
            }
        });
    }

    // --- Step 3: Fallback for any still-unplaced nodes ---
    nodes.forEach((node) => {
        if (node.layer === -1) node.layer = 2;
    });

    // --- Step 4: Compute maxLayer ---
    let maxLayer = 0;
    nodes.forEach(n => { maxLayer = Math.max(maxLayer, n.layer); });

    return maxLayer;
}

/**
 * Assign x,y positions to nodes based on layers using barycenter heuristic.
 * x = horizontal (layer), y = vertical (within layer).
 * Barycenter ordering minimizes edge crossings by sorting each layer's nodes
 * by the average y-position of their neighbors in adjacent layers.
 */
function layoutDAGNodes(nodes, edges, maxLayer) {
    // Build adjacency map for barycenter calculation
    const neighbors = new Map(); // nodeId -> Set of neighbor nodeIds
    edges.forEach(e => {
        if (!neighbors.has(e.source)) neighbors.set(e.source, new Set());
        if (!neighbors.has(e.target)) neighbors.set(e.target, new Set());
        neighbors.get(e.source).add(e.target);
        neighbors.get(e.target).add(e.source);
    });

    // Group by layer
    const layers = new Map();
    nodes.forEach((node, id) => {
        if (!layers.has(node.layer)) layers.set(node.layer, []);
        layers.get(node.layer).push(node);
    });

    // Initial ordering: group by arrow type for visual clustering, then alphabetical
    const arrowOrder = { activates: 0, regulates: 1, binds: 2, inhibits: 3 };
    layers.forEach(layerNodes => {
        layerNodes.sort((a, b) => {
            const aOrd = arrowOrder[a.arrowType] ?? 2;
            const bOrd = arrowOrder[b.arrowType] ?? 2;
            if (aOrd !== bOrd) return aOrd - bOrd;
            return a.id.localeCompare(b.id);
        });
    });

    const xSpacing = CV_CONFIG.LEVEL_SPACING;
    const baseYSpacing = CV_CONFIG.NODE_VERTICAL_SPACING;
    // Always use full spacing — let columns grow to fit all cards without overlap
    function getLayerSpacing(count) {
        return baseYSpacing;
    }

    // Helper: assign y positions based on current ordering within each layer
    function assignYPositions() {
        layers.forEach((layerNodes, layerIdx) => {
            const spacing = getLayerSpacing(layerNodes.length);
            const totalHeight = (layerNodes.length - 1) * spacing;
            const startY = -totalHeight / 2;
            layerNodes.forEach((node, i) => {
                node.x = layerIdx * xSpacing;
                node.y = startY + i * spacing;
            });
        });
    }

    // Seed initial positions for barycenter reference
    assignYPositions();

    // Barycenter sweep: 3 passes (forward, backward, forward) for convergence
    const sortedLayerKeys = Array.from(layers.keys()).sort((a, b) => a - b);

    for (let pass = 0; pass < 3; pass++) {
        const order = pass % 2 === 0 ? sortedLayerKeys : [...sortedLayerKeys].reverse();

        for (const layerIdx of order) {
            const layerNodes = layers.get(layerIdx);
            if (!layerNodes || layerNodes.length <= 1) continue;

            // Compute barycenter for each node (avg y of neighbors in other layers)
            layerNodes.forEach(node => {
                const nbrs = neighbors.get(node.id);
                if (!nbrs || nbrs.size === 0) {
                    node._barycenter = node.y; // Keep current position
                    return;
                }
                let sumY = 0;
                let count = 0;
                nbrs.forEach(nbrId => {
                    const nbrNode = nodes.get(nbrId);
                    if (nbrNode && nbrNode.layer !== layerIdx) {
                        sumY += nbrNode.y;
                        count++;
                    }
                });
                node._barycenter = count > 0 ? sumY / count : node.y;
            });

            // Sort by barycenter (stable: preserves alphabetical for ties)
            layerNodes.sort((a, b) => a._barycenter - b._barycenter);

            // Reassign y positions after sorting with adaptive spacing
            const spacing = getLayerSpacing(layerNodes.length);
            const totalHeight = (layerNodes.length - 1) * spacing;
            const startY = -totalHeight / 2;
            layerNodes.forEach((node, i) => {
                node.y = startY + i * spacing;
            });
        }
    }

    // Final position assignment
    assignYPositions();
}

/**
 * Position satellite nodes directly to the right of their mediator (core) protein.
 * Satellites are grouped by mediator; multiple satellites per mediator are spread
 * vertically, centered on the mediator's Y position.
 */
function placeSatellites(coreNodes, satelliteNodes, satelliteEdges) {
    // Group satellites by mediator (the source of the indirect edge)
    const mediatorGroups = new Map(); // mediatorId -> [satelliteNode, ...]
    satelliteEdges.forEach(e => {
        if (!satelliteNodes.has(e.target)) return;
        const mediator = e.source;
        if (!mediatorGroups.has(mediator)) mediatorGroups.set(mediator, []);
        const satNode = satelliteNodes.get(e.target);
        // Avoid adding the same satellite twice (if multiple edges point to it)
        if (!mediatorGroups.get(mediator).includes(satNode)) {
            mediatorGroups.get(mediator).push(satNode);
        }
    });

    mediatorGroups.forEach((satellites, mediatorId) => {
        const mediator = coreNodes.get(mediatorId);
        if (!mediator) {
            console.warn(`placeSatellites: mediator "${mediatorId}" not in coreNodes, skipping satellites`);
            return;
        }

        // Grid-align satellite x to the next layer column
        const satLayer = mediator.layer + 1;
        const satX = satLayer * CV_CONFIG.LEVEL_SPACING;

        // Center satellite group around mediator's Y so each satellite
        // lines up straight-right from its upstream mediator node
        satellites.sort((a, b) => a.id.localeCompare(b.id));
        const groupHeight = (satellites.length - 1) * CV_CONFIG.SATELLITE_V_SPACING;
        const startY = mediator.y - groupHeight / 2;
        satellites.forEach((sat, i) => {
            sat.x = satX;
            sat.y = startY + i * CV_CONFIG.SATELLITE_V_SPACING;
            sat.layer = satLayer;
            sat._isSatellite = true;
            sat._mediatorId = mediatorId;
        });
    });
}

/**
 * Post-placement collision detection and resolution for card view.
 * Cards are CARD_WIDTH x CARD_HEIGHT rectangles. Pushes overlapping cards apart
 * vertically. Runs iteratively until no overlaps remain (max 30 iterations).
 */
function resolveCardOverlaps(allNodes) {
    const CARD_W = CV_CONFIG.CARD_WIDTH;
    const CARD_H = CV_CONFIG.CARD_HEIGHT;
    const PAD_Y = 14;  // Vertical gap between cards

    const nodeArr = Array.from(allNodes.values())
        .filter(n => n.x != null && n.y != null);

    // Group nodes into columns by x-range (nodes within CARD_W of each other)
    const columns = new Map(); // layerKey -> [nodes]
    nodeArr.forEach(n => {
        // Round x to nearest LEVEL_SPACING grid to group by column
        const col = Math.round(n.x / CV_CONFIG.LEVEL_SPACING);
        if (!columns.has(col)) columns.set(col, []);
        columns.get(col).push(n);
    });

    // For each column, sort by Y and push overlapping cards apart
    columns.forEach(colNodes => {
        colNodes.sort((a, b) => a.y - b.y);

        for (let i = 1; i < colNodes.length; i++) {
            const prev = colNodes[i - 1];
            const curr = colNodes[i];

            // Check horizontal overlap (cards in same column should overlap in x)
            const prevRight = prev.x + CARD_W;
            const currLeft = curr.x;
            const xOverlap = prevRight > currLeft && prev.x < curr.x + CARD_W;

            if (!xOverlap) continue;

            // Required minimum Y gap: previous card bottom + padding
            const prevBottom = prev.y + CARD_H / 2;
            const currTop = curr.y - CARD_H / 2;
            const gap = currTop - prevBottom;

            if (gap < PAD_Y) {
                // Push current card down so it clears the previous card
                curr.y = prevBottom + PAD_Y + CARD_H / 2;
            }
        }
    });
}

/**
 * Render the Interactors Mode as a DAG network graph with arrow-tipped edges.
 */
function renderInteractorDAG() {
    const { nodes, edges } = buildInteractorGraph();
    const mainId = (typeof SNAP !== 'undefined' && SNAP.main) ? SNAP.main : 'Unknown';

    if (nodes.size === 0) return;

    // Classify into core (layered) and satellite (attached to mediators)
    const { coreNodes, satelliteNodes, coreEdges, satelliteEdges } =
        classifyNodesForLayout(nodes, edges, mainId);

    // Layer assignment + position for core nodes only
    const maxLayer = computeDAGLayers(coreNodes, coreEdges, mainId);
    layoutDAGNodes(coreNodes, coreEdges, maxLayer);

    // Position satellites relative to their mediator proteins
    placeSatellites(coreNodes, satelliteNodes, satelliteEdges);

    // Safety net: remove any satellite that still has no valid position
    satelliteNodes.forEach((node, id) => {
        if (node.x == null || node.y == null || isNaN(node.x) || isNaN(node.y)) {
            satelliteNodes.delete(id);
        }
    });

    // Merge for rendering
    const allNodes = new Map([...coreNodes, ...satelliteNodes]);
    const allEdges = [...coreEdges, ...satelliteEdges];

    // Resolve overlapping cards (satellites vs core nodes in same column)
    resolveCardOverlaps(allNodes);

    // Nuclear cleanup: remove ALL children to eliminate stale D3 data bindings
    // from pathway mode that can conflict with DAG mode's key functions
    cvG.selectAll('*').remove();

    // Layer band indicators (subtle vertical bands behind each layer column)
    const layerSet = new Set();
    allNodes.forEach(n => { if (n.layer != null) layerSet.add(n.layer); });
    const sortedLayers = Array.from(layerSet).sort((a, b) => a - b);
    const shellLabels = { 0: 'Upstream', 1: 'Query', 2: 'Downstream' };
    // Dynamically add chain labels for any depth beyond layer 2
    sortedLayers.forEach(l => {
        if (l >= 3 && !shellLabels[l]) shellLabels[l] = `Chain (${l - 1})`;
    });
    const bandColors = [
      'rgba(239,68,68,0.04)', 'rgba(99,102,241,0.06)',
      'rgba(16,185,129,0.04)', 'rgba(245,158,11,0.03)', 'rgba(168,85,247,0.03)'
    ];

    // Compute vertical extent from all nodes
    let minNodeY = Infinity, maxNodeY = -Infinity;
    allNodes.forEach(n => {
      if (n.y != null) {
        minNodeY = Math.min(minNodeY, n.y - CV_CONFIG.CARD_HEIGHT / 2);
        maxNodeY = Math.max(maxNodeY, n.y + CV_CONFIG.CARD_HEIGHT / 2);
      }
    });
    const bandTop = minNodeY - 60;
    const bandHeight = (maxNodeY - minNodeY) + 120;

    const bandGroup = cvG.append('g').attr('class', 'cv-layer-bands');
    sortedLayers.forEach((layerIdx) => {
      const x = layerIdx * CV_CONFIG.LEVEL_SPACING - 20;
      bandGroup.append('rect')
        .attr('x', x)
        .attr('y', bandTop)
        .attr('width', CV_CONFIG.CARD_WIDTH + 40)
        .attr('height', bandHeight)
        .attr('rx', 8)
        .style('fill', bandColors[Math.min(layerIdx, bandColors.length - 1)])
        .style('pointer-events', 'none');
      bandGroup.append('text')
        .attr('x', x + (CV_CONFIG.CARD_WIDTH + 40) / 2)
        .attr('y', bandTop - 8)
        .attr('text-anchor', 'middle')
        .style('fill', 'rgba(148, 140, 180, 0.4)')
        .style('font-size', '10px')
        .style('font-weight', '600')
        .style('letter-spacing', '1px')
        .text(shellLabels[layerIdx] || `Depth ${layerIdx}`);
    });

    // --- Arrow marker definitions ---
    const defs = cvSvg.select('defs').empty() ? cvSvg.append('defs') : cvSvg.select('defs');

    const arrowColors = {
        activates: '#10b981', inhibits: '#ef4444',
        binds: '#a78bfa', regulates: '#f59e0b'
    };

    // Define arrow markers for each type
    Object.entries(arrowColors).forEach(([type, color]) => {
        const markerId = `arrowhead-${type}`;
        // Remove existing marker if present
        defs.select(`#${markerId}`).remove();
        defs.append('marker')
            .attr('id', markerId)
            .attr('viewBox', '0 0 10 10')
            .attr('refX', 10)
            .attr('refY', 5)
            .attr('markerWidth', 8)
            .attr('markerHeight', 8)
            .attr('orient', 'auto-start-reverse')
            .append('polygon')
            .attr('points', '0,0 10,5 0,10')
            .attr('fill', color);
    });

    // Also define an inhibition flat-bar marker
    const inhibMarkerId = 'inhibit-bar';
    defs.select(`#${inhibMarkerId}`).remove();
    defs.append('marker')
        .attr('id', inhibMarkerId)
        .attr('viewBox', '0 0 6 10')
        .attr('refX', 6)
        .attr('refY', 5)
        .attr('markerWidth', 6)
        .attr('markerHeight', 10)
        .attr('orient', 'auto')
        .append('line')
        .attr('x1', 5).attr('y1', 0)
        .attr('x2', 5).attr('y2', 10)
        .attr('stroke', arrowColors.inhibits)
        .attr('stroke-width', 2);

    // --- Build node data array for D3 ---
    const nodeArray = Array.from(allNodes.values());

    // --- Render edges first (behind nodes) ---
    // Sort: databased/indirect edges first (rendered behind), query/direct edges on top
    const sortedEdges = [...allEdges].sort((a, b) => {
        const aWeight = (a.origin === 'databased' ? 0 : 2) + (a.interaction_type === 'indirect' ? 0 : 1);
        const bWeight = (b.origin === 'databased' ? 0 : 2) + (b.interaction_type === 'indirect' ? 0 : 1);
        return aWeight - bWeight;
    });

    const edgeGroup = cvG.selectAll('.cv-dag-edge')
        .data(sortedEdges, d => `${d.source}->${d.target}::${d.interaction_type}::${d.origin || 'query'}`)
        .join(
            enter => {
                const g = enter.append('g')
                    .attr('class', d => `cv-dag-edge ${d.origin === 'databased' ? 'origin-databased' : 'origin-query'}`);
                g.append('path').attr('class', 'dag-edge-path');
                return g;
            },
            update => update,
            exit => exit.remove()
        );

    edgeGroup.select('path')
        .attr('class', 'dag-edge-path')
        .attr('d', d => {
            const srcNode = allNodes.get(d.source);
            const tgtNode = allNodes.get(d.target);
            if (!srcNode || !tgtNode) return '';
            const sx = srcNode.x + CV_CONFIG.CARD_WIDTH;
            const sy = srcNode.y;
            const tx = tgtNode.x;
            const ty = tgtNode.y;

            // Satellite edges: always straight lines (satellites are centered
            // around their mediator's Y, so lines are nearly horizontal)
            if (satelliteNodes.has(d.target)) {
                return `M${sx},${sy} L${tx},${ty}`;
            }

            // Handle same-layer or backward edges: arc above/below nodes
            if (tx <= sx + 20) {
                const arcDir = sy <= ty ? -1 : 1; // arc away from target direction
                const arcH = 60 + Math.abs(sy - ty) * 0.3;
                const loopOut = Math.max(80, (sx - tx) + 80);
                return `M${sx},${sy} C${sx + loopOut},${sy + arcDir * arcH} ${tx - loopOut},${ty + arcDir * arcH} ${tx},${ty}`;
            }
            const midX = (sx + tx) / 2;
            return `M${sx},${sy} C${midX},${sy} ${midX},${ty} ${tx},${ty}`;
        })
        .style('fill', 'none')
        .style('stroke', d => arrowColors[d.arrow] || '#64748b')
        .style('stroke-width', d => {
            if (d.origin === 'databased') return '1.4px';
            return d.interaction_type === 'indirect' ? '1.4px' : '2.2px';
        })
        .style('stroke-dasharray', d => {
            if (d.origin === 'databased') return '6,4';
            return d.interaction_type === 'indirect' ? '5,4' : 'none';
        })
        .style('opacity', d => {
            if (d.origin === 'databased') return 0.55;
            return d.interaction_type === 'indirect' ? 0.65 : 0.9;
        })
        .attr('marker-end', d => `url(#arrowhead-${d.arrow || 'binds'})`);

    // --- Render chain lane backgrounds (behind nodes) ---
    // Group chain nodes by _chainId and draw a subtle colored lane behind each chain
    const chainLanes = new Map();
    nodeArray.forEach(d => {
        if (d._chainId && d._chainColor) {
            if (!chainLanes.has(d._chainId)) {
                chainLanes.set(d._chainId, { color: d._chainColor, nodes: [] });
            }
            chainLanes.get(d._chainId).nodes.push(d);
        }
    });

    cvG.selectAll('.cv-chain-lane').remove();
    for (const [chainId, lane] of chainLanes) {
        if (lane.nodes.length < 2) continue;
        const xs = lane.nodes.map(n => n.x);
        const ys = lane.nodes.map(n => n.y);
        const minX = Math.min(...xs) - 10;
        const maxX = Math.max(...xs) + CV_CONFIG.CARD_WIDTH + 10;
        const minY = Math.min(...ys) - CV_CONFIG.CARD_HEIGHT / 2 - 6;
        const maxY = Math.max(...ys) + CV_CONFIG.CARD_HEIGHT / 2 + 6;
        const hue = lane.color.h;

        cvG.insert('rect', '.cv-node')
            .attr('class', 'cv-chain-lane')
            .attr('x', minX)
            .attr('y', minY)
            .attr('width', maxX - minX)
            .attr('height', maxY - minY)
            .attr('rx', 8)
            .style('fill', `hsla(${hue}, 60%, 50%, 0.06)`)
            .style('stroke', `hsla(${hue}, 60%, 50%, 0.15)`)
            .style('stroke-width', '1px')
            .style('pointer-events', 'none');
    }

    // --- Render nodes ---
    // L5.1: append "pseudo" class when the node represents a generic
    // biomolecule entity (RNA, Ubiquitin, ...) and "chain-link" when it
    // is a mid-chain hop, so the CSS rules in static/styles.css apply.
    const _cvClass = d => 'cv-node'
        + (d._isPseudo ? ' pseudo' : '')
        + (d._isChainLink ? ' chain-link' : '');
    const nodeEnter = cvG.selectAll('.cv-node')
        .data(nodeArray, d => d.id)
        .join(
            enter => enter.append('g')
                .attr('class', _cvClass)
                .attr('transform', d => `translate(${d.x},${d.y})`)
                .style('cursor', 'pointer')
                .on('click', (event, d) => {
                    // Wrap in hierarchy-like structure for handleCardClick compatibility
                    handleCardClick(event, { data: d });
                }),
            update => update.attr('class', _cvClass),
            exit => exit.remove()
        );

    // Card rectangle
    nodeEnter.append('rect')
        .attr('width', CV_CONFIG.CARD_WIDTH)
        .attr('height', CV_CONFIG.CARD_HEIGHT)
        .attr('y', -CV_CONFIG.CARD_HEIGHT / 2)
        .attr('rx', 8)
        .style('fill', d => getCVColor(d))
        .style('stroke', d => d._isDatabased ? 'rgba(148, 163, 184, 0.6)' : getCVStroke(d))
        .style('stroke-width', d => d.type === 'main' ? '2px' : '1px')
        .style('stroke-dasharray', d => d._isDatabased ? '4,3' : 'none');

    const roleCode = (d) => {
        if (d.type === 'main') return `Q${d.layer ?? 1}`;
        // S3: chain-link nodes get a chain-specific role code
        if (d._isChainLink) return `C${d._chainPosition + 1}`;
        const role = d.role || 'downstream';
        const roleLetter = role === 'upstream' ? 'U' : 'D';
        const shell = Number.isFinite(d.layer) ? d.layer : 1;
        return `${roleLetter}${shell}`;
    };

    const roleColor = (d) => {
        // S3: chain-link nodes get the amber accent
        if (d._isChainLink) return '#f59e0b';
        const role = d.role || 'downstream';
        if (role === 'upstream') return '#2563eb';
        return '#059669';
    };

    nodeEnter.filter(d => d.type === 'interactor').append('rect')
        .attr('class', 'cv-role-badge-bg')
        .attr('width', 30)
        .attr('height', 14)
        .attr('x', 8)
        .attr('y', -CV_CONFIG.CARD_HEIGHT / 2 + 4)
        .attr('rx', 3)
        .style('fill', d => roleColor(d))
        .style('opacity', 0.9)
        .style('pointer-events', 'none');
    nodeEnter.filter(d => d.type === 'interactor').append('text')
        .attr('class', 'cv-role-badge-text')
        .attr('x', 23)
        .attr('y', -CV_CONFIG.CARD_HEIGHT / 2 + 14)
        .attr('text-anchor', 'middle')
        .style('fill', 'rgba(255,255,255,0.95)')
        .style('font-size', '8px')
        .style('font-weight', '700')
        .style('letter-spacing', '0.2px')
        .style('pointer-events', 'none')
        .text(d => roleCode(d));

    // Databased indicator strip (subtle "DB" badge on card)
    nodeEnter.filter(d => d._isDatabased).append('rect')
        .attr('width', 28)
        .attr('height', 14)
        .attr('x', CV_CONFIG.CARD_WIDTH - 34)
        .attr('y', -CV_CONFIG.CARD_HEIGHT / 2 + 4)
        .attr('rx', 3)
        .style('fill', 'rgba(100, 116, 139, 0.6)')
        .style('pointer-events', 'none');
    nodeEnter.filter(d => d._isDatabased).append('text')
        .attr('x', CV_CONFIG.CARD_WIDTH - 20)
        .attr('y', -CV_CONFIG.CARD_HEIGHT / 2 + 14)
        .attr('text-anchor', 'middle')
        .style('fill', 'rgba(255,255,255,0.8)')
        .style('font-size', '8px')
        .style('font-weight', 'bold')
        .style('pointer-events', 'none')
        .text('DB');

    // S3: Chain-link visual differentiation
    // Left accent border for chain-link nodes (golden amber)
    nodeEnter.filter(d => d._isChainLink).append('rect')
        .attr('width', 3)
        .attr('height', CV_CONFIG.CARD_HEIGHT - 8)
        .attr('x', 0)
        .attr('y', -CV_CONFIG.CARD_HEIGHT / 2 + 4)
        .attr('rx', 2)
        .style('fill', '#f59e0b')
        .style('pointer-events', 'none');

    // Chain position badge (e.g., "C2" for chain hop 2)
    nodeEnter.filter(d => d._isChainLink && !d._isDatabased).append('rect')
        .attr('width', 28)
        .attr('height', 14)
        .attr('x', CV_CONFIG.CARD_WIDTH - 34)
        .attr('y', -CV_CONFIG.CARD_HEIGHT / 2 + 4)
        .attr('rx', 3)
        .style('fill', 'rgba(245, 158, 11, 0.7)')
        .style('pointer-events', 'none');
    nodeEnter.filter(d => d._isChainLink && !d._isDatabased).append('text')
        .attr('x', CV_CONFIG.CARD_WIDTH - 20)
        .attr('y', -CV_CONFIG.CARD_HEIGHT / 2 + 14)
        .attr('text-anchor', 'middle')
        .style('fill', 'rgba(255,255,255,0.95)')
        .style('font-size', '8px')
        .style('font-weight', 'bold')
        .style('pointer-events', 'none')
        .text(d => `C${d._chainPosition + 1}`);

    // Chain link icon badge on first node of each chain
    nodeEnter.filter(d => d._chainId && d._chainPosition === 0)
        .append('text')
        .attr('class', 'cv-chain-badge')
        .attr('x', CV_CONFIG.CARD_WIDTH - 14)
        .attr('y', CV_CONFIG.CARD_HEIGHT / 2 - 6)
        .style('font-size', '11px')
        .style('pointer-events', 'all')
        .style('cursor', 'default')
        .text('\u{1F517}') // 🔗
        .append('title')
        .text(d => {
            const proteins = d._chainProteins || [];
            return `Chain: ${proteins.join(' \u2192 ')}`;
        });

    // Main label
    nodeEnter.append('text')
        .attr('class', 'cv-label')
        .attr('x', 15)
        .attr('dy', d => d.type === 'interactor' ? '-0.4em' : '0.1em')
        .style('fill', 'white')
        .style('font-size', '15px')
        .style('font-weight', '600')
        .style('font-family', 'Inter, sans-serif')
        .style('pointer-events', 'none')
        .text(d => truncateCVText(_cvDisplayLabel(d), 28));

    nodeEnter.each(function(d) {
        const fullText = String(_cvDisplayLabel(d));
        if (fullText.length > 28) {
            d3.select(this).append('title').text(fullText);
        }
    });

    // Context text (relationship description)
    nodeEnter.each(function(d) {
        if (d.type === 'interactor') {
            let ctxText = '';

            // 1) Direct edge to/from mainId
            const directEdge = allEdges.find(e =>
                (e.source === mainId && e.target === d.id) ||
                (e.source === d.id && e.target === mainId)
            );

            if (directEdge) {
                const verb = directEdge.arrow === 'activates' ? 'activates' :
                    directEdge.arrow === 'inhibits' ? 'inhibits' :
                    directEdge.arrow === 'regulates' ? 'regulates' : 'binds';
                ctxText = directEdge.source === mainId
                    ? `${mainId} ${verb} ${d.id}`
                    : `${d.id} ${verb} ${mainId}`;
            } else {
                // 2) Indirect edge: find any edge targeting this node from a mediator
                const indirectEdge = allEdges.find(e =>
                    e.target === d.id && e.interaction_type === 'indirect'
                );
                if (indirectEdge) {
                    const mediator = indirectEdge.source;
                    const verb = indirectEdge.arrow === 'activates' ? 'activates' :
                        indirectEdge.arrow === 'inhibits' ? 'inhibits' :
                        indirectEdge.arrow === 'regulates' ? 'regulates' : 'binds';
                    ctxText = `${mainId} \u2192 ${mediator} ${verb} ${d.id}`;
                } else {
                    // 3) Any edge involving this node (fallback)
                    const anyEdge = allEdges.find(e =>
                        e.source === d.id || e.target === d.id
                    );
                    if (anyEdge) {
                        const verb = anyEdge.arrow === 'activates' ? 'activates' :
                            anyEdge.arrow === 'inhibits' ? 'inhibits' :
                            anyEdge.arrow === 'regulates' ? 'regulates' : 'binds';
                        ctxText = anyEdge.source === d.id
                            ? `${d.id} ${verb} ${anyEdge.target}`
                            : `${anyEdge.source} ${verb} ${d.id}`;
                    }
                }
            }

            if (ctxText) {
                d3.select(this).append('text')
                    .attr('class', 'cv-subtitle')
                    .attr('x', 15)
                    .attr('dy', '1.1em')
                    .style('fill', '#94a3b8')
                    .style('font-size', '11px')
                    .style('font-family', 'Inter, sans-serif')
                    .style('pointer-events', 'none')
                    .text(truncateCVText(ctxText, 40));
            }

            // "Also in" pathway memberships
            if (d.pathwayMemberships && d.pathwayMemberships.length > 0) {
                const pwText = 'Also in: ' + d.pathwayMemberships.slice(0, 2).join(', ');
                d3.select(this).append('text')
                    .attr('class', 'cv-pathway-tag')
                    .attr('x', 15)
                    .attr('dy', '2.3em')
                    .style('fill', 'rgba(139, 92, 246, 0.7)')
                    .style('font-size', '9px')
                    .style('font-style', 'italic')
                    .style('font-family', 'Inter, sans-serif')
                    .style('pointer-events', 'none')
                    .text(truncateCVText(pwText, 45));
            }
        } else if (d.type === 'main') {
            const directCount = allEdges.filter(e =>
                e.source === mainId || e.target === mainId
            ).length;
            const ctxText = `Query protein \u00b7 ${directCount} interactions`;
            d3.select(this).append('text')
                .attr('class', 'cv-subtitle')
                .attr('x', 15)
                .attr('dy', '1.1em')
                .style('fill', '#94a3b8')
                .style('font-size', '11px')
                .style('font-family', 'Inter, sans-serif')
                .style('pointer-events', 'none')
                .text(ctxText);
        }
    });

    // --- Resize SVG to fit ---
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    nodeArray.forEach(d => {
        minY = Math.min(minY, d.y - CV_CONFIG.CARD_HEIGHT / 2);
        maxY = Math.max(maxY, d.y + CV_CONFIG.CARD_HEIGHT / 2);
        minX = Math.min(minX, d.x);
        maxX = Math.max(maxX, d.x + CV_CONFIG.CARD_WIDTH);
    });

    const padding = 100;
    const contentWidth = (maxX - minX) + padding * 2;
    const contentHeight = (maxY - minY) + padding * 2;

    cvSvg
        .attr('width', contentWidth)
        .attr('height', contentHeight);

    cvG.attr('transform', `translate(${-minX + padding}, ${-minY + padding})`);
}

// ============================================================================
// LEGACY INTERACTORS HIERARCHY (kept for reference, replaced by DAG)
// ============================================================================

/**
 * Build a D3-compatible hierarchy tree for Interactors Mode.
 * Structure: QueryProtein → Direction Groups → Arrow Subgroups → Interactor cards
 * Uses getProteinsByRole() from visualizer.js to classify by direction.
 */
function buildInteractorHierarchy() {
    const mainId = (typeof SNAP !== 'undefined' && SNAP.main) ? SNAP.main : 'Unknown';
    const interactions = (typeof SNAP !== 'undefined' && SNAP.interactions) ? SNAP.interactions : [];
    const pathways = (typeof SNAP !== 'undefined' && SNAP.pathways) ? SNAP.pathways : [];

    // 1. Classify all proteins by direction using global helper
    const roles = (typeof getProteinsByRole === 'function')
        ? getProteinsByRole(interactions, mainId)
        : { upstream: new Set(), downstream: new Set(), bidirectional: new Set() };

    // 2. For each protein, find ALL its interactions with the main protein and pick the best arrow type
    function getInteractionDetails(proteinId) {
        const matching = interactions.filter(i =>
            (i.source === mainId && i.target === proteinId) ||
            (i.source === proteinId && i.target === mainId)
        );
        if (matching.length === 0) return { arrow: 'binds', interaction: null };
        // Prefer the first interaction's arrow type
        const best = matching[0];
        return { arrow: best.arrow || 'binds', interaction: best };
    }

    // 3. Sub-classify each direction group by arrow type
    function classifyByArrow(proteinSet) {
        const groups = { activates: [], inhibits: [], binds: [], regulates: [] };
        proteinSet.forEach(proteinId => {
            const { arrow } = getInteractionDetails(proteinId);
            const bucket = groups[arrow] ? arrow : 'binds';
            groups[bucket].push(proteinId);
        });
        return groups;
    }

    const upstreamByArrow = classifyByArrow(roles.upstream);
    const downstreamByArrow = classifyByArrow(roles.downstream);
    const bidirectionalByArrow = classifyByArrow(roles.bidirectional);

    // 4. Build the hierarchy tree
    const totalCount = roles.upstream.size + roles.downstream.size + roles.bidirectional.size;
    const rootNode = {
        id: mainId,
        type: 'main',
        label: mainId,
        count: totalCount,
        children: []
    };

    // Helper: build one direction group with arrow subgroups
    function buildDirectionGroup(directionName, arrowGroups, directionSet) {
        if (directionSet.size === 0) return null;
        if (!cvState.interactorDirectionFilter.has(directionName)) return null;

        // S1b: user explicitly rejected bidirectional — every direction
        // picks ONE rational arrow from the claim. The legacy
        // "bidirectional" bucket is relabeled "Undirected" and uses the
        // forward (→) glyph so the UI never renders ↔.
        const dirIcons = { upstream: '\u2191', downstream: '\u2193', bidirectional: '\u2192' };
        const dirLabels = { upstream: 'Upstream', downstream: 'Downstream', bidirectional: 'Undirected' };

        const groupNode = {
            id: `dir_${directionName}`,
            type: 'direction_group',
            label: `${dirIcons[directionName]} ${dirLabels[directionName]}`,
            direction: directionName,
            count: directionSet.size,
            children: []
        };

        const isGroupExpanded = cvState.interactorExpandedGroups.has(directionName);
        if (!isGroupExpanded) return groupNode; // collapsed — no children

        // Build arrow subgroups
        ['activates', 'inhibits', 'binds', 'regulates'].forEach(arrowType => {
            let proteins = arrowGroups[arrowType];
            if (!proteins || proteins.length === 0) return;
            if (!cvState.interactorArrowFilter.has(arrowType)) return;

            // Apply search filter
            if (cvState.interactorSearchQuery) {
                const q = cvState.interactorSearchQuery.toLowerCase();
                proteins = proteins.filter(p => p.toLowerCase().includes(q));
            }
            if (proteins.length === 0) return;

            const subgroupKey = `${directionName}_${arrowType}`;
            const isSubExpanded = cvState.interactorExpandedGroups.has(subgroupKey);

            const subgroupNode = {
                id: `sub_${subgroupKey}`,
                type: 'arrow_subgroup',
                label: `${arrowType.charAt(0).toUpperCase() + arrowType.slice(1)} (${proteins.length})`,
                arrowType: arrowType,
                direction: directionName,
                count: proteins.length,
                children: isSubExpanded ? proteins.map(pid =>
                    createInteractorNodeForMode(pid, mainId, directionName, arrowType, interactions, pathways)
                ) : []
            };

            groupNode.children.push(subgroupNode);
        });

        // Update group count to reflect filtered children
        const filteredTotal = groupNode.children.reduce((sum, sg) => sum + sg.count, 0);
        groupNode.count = filteredTotal;

        return groupNode.children.length > 0 ? groupNode : null;
    }

    const upGroup = buildDirectionGroup('upstream', upstreamByArrow, roles.upstream);
    const downGroup = buildDirectionGroup('downstream', downstreamByArrow, roles.downstream);
    const biGroup = buildDirectionGroup('bidirectional', bidirectionalByArrow, roles.bidirectional);

    if (upGroup) rootNode.children.push(upGroup);
    if (downGroup) rootNode.children.push(downGroup);
    if (biGroup) rootNode.children.push(biGroup);

    return rootNode;
}

/**
 * Create an interactor node for the Interactors Mode hierarchy.
 * Includes direction-aware context text and pathway membership info.
 */
function createInteractorNodeForMode(proteinId, mainId, direction, arrowType, interactions, pathways) {
    // Find the specific interaction for context
    const interaction = interactions.find(i =>
        ((i.source === mainId && i.target === proteinId) ||
         (i.source === proteinId && i.target === mainId)) &&
        (i.arrow || 'binds') === arrowType
    ) || interactions.find(i =>
        (i.source === mainId && i.target === proteinId) ||
        (i.source === proteinId && i.target === mainId)
    );

    // Build human-readable context
    const verb = arrowType === 'activates' ? 'activates' :
        arrowType === 'inhibits' ? 'inhibits' :
        arrowType === 'regulates' ? 'regulates' : 'binds to';

    // S1b: the legacy bidirectional branch formerly rendered as
    // `A \u2194 B (verb)` (A ↔ B). Per user intent, the pipeline
    // produces ONE rational direction per claim; any residual
    // "bidirectional" role falls back to the forward-arrow rendering
    // with a parenthetical hint so the \u2194 glyph is never emitted.
    const contextText = direction === 'upstream'
        ? `${proteinId} ${verb} ${mainId}`
        : direction === 'downstream'
            ? `${mainId} ${verb} ${proteinId}`
            : `${proteinId} \u2192 ${mainId} (${verb})`;

    // Find pathways containing this protein
    const containingPathways = pathways.filter(pw =>
        (pw.interactor_ids || []).includes(proteinId)
    );

    return {
        id: proteinId,
        type: 'interactor',
        label: proteinId,
        arrowType: arrowType,
        direction: direction,
        contextText: contextText,
        isDownstream: direction === 'downstream',
        confidence: interaction?.confidence || null,
        interactionType: interaction?.interaction_type || interaction?.type || 'direct',
        pathwayMemberships: containingPathways.map(pw => pw.name || pw.id),
        children: []
    };
}


// ============================================================================
// RENDERING
// ============================================================================

/**
 * Compute interaction-type counts for a pathway node from SNAP data.
 * Centralized so enter/update/sidebar paths all produce the same numbers.
 */
function _getPathwayInteractionCounts(data) {
    let interactions = null;
    if (typeof PathwayState !== 'undefined' && data && data.id) {
        const metadata = PathwayState.getInteractionMetadata();
        interactions = metadata.get(data.id);
    }
    if (!interactions && data && data.raw && data.raw.interactor_ids) {
        interactions = { activates: 0, inhibits: 0, binds: 0, regulates: 0, total: 0 };
        const ids = new Set(data.raw.interactor_ids);
        if (typeof SNAP !== 'undefined' && SNAP.interactions) {
            SNAP.interactions.forEach(inter => {
                const target = (inter.target !== SNAP.main) ? inter.target : inter.source;
                if (ids.has(target)) {
                    const arrow = inter.arrow || 'binds';
                    const key = (arrow === 'complex') ? 'binds' : arrow;
                    if (interactions.hasOwnProperty(key)) interactions[key]++;
                    else interactions.binds++;
                    interactions.total++;
                }
            });
        }
        if (interactions.total === 0) {
            interactions.total = data.raw.interactor_ids.length;
            interactions.binds = interactions.total;
        }
    }
    return interactions;
}

/**
 * Render interaction badges onto a card node group. Safe to call repeatedly;
 * clears any existing badges before appending new ones.
 */
function _renderCardBadges(nodeSel, data) {
    // Clear any stale badge group first — lets update path re-render fresh
    nodeSel.selectAll('.cv-card-badges').remove();
    if (!data || data.type !== 'pathway') return;

    const interactions = _getPathwayInteractionCounts(data);
    if (!interactions || !interactions.total) return;

    const badgeGroup = nodeSel.append('g')
        .attr('class', 'cv-card-badges')
        .attr('transform', `translate(${CV_CONFIG.CARD_WIDTH - 15}, ${-CV_CONFIG.CARD_HEIGHT / 2 + 15})`);

    const _c = getCVColors();
    const badgeTypes = ['activates', 'inhibits', 'binds', 'regulates'];
    const colors = {
        activates: { core: _c.activates.badge, aura: _c.activates.aura },
        inhibits:  { core: _c.inhibits.badge,  aura: _c.inhibits.aura  },
        binds:     { core: _c.binds.badge,     aura: _c.binds.aura     },
        regulates: { core: _c.regulates.badge, aura: _c.regulates.aura },
    };

    let badgeX = 0;
    badgeTypes.forEach(type => {
        if (interactions[type] > 0) {
            const badge = badgeGroup.append('g')
                .attr('class', `cv-badge-organism ${type}`)
                .attr('transform', `translate(${badgeX}, 0)`);

            badge.append('circle')
                .attr('class', 'badge-aura')
                .attr('r', 10)
                .style('fill', colors[type].aura)
                .style('opacity', 0.4);

            badge.append('circle')
                .attr('class', 'badge-core')
                .attr('r', 6)
                .style('fill', colors[type].core);

            badge.append('text')
                .attr('class', 'badge-count')
                .attr('text-anchor', 'middle')
                .attr('dy', '0.35em')
                .style('fill', 'white')
                .style('font-family', 'JetBrains Mono, monospace')
                .style('font-size', '9px')
                .style('font-weight', '700')
                .style('pointer-events', 'none')
                .text(interactions[type]);

            badgeX -= 22;
        }
    });
}

// P4.2: rAF-coalesced render scheduler. Card-view callers fire
// `renderCardView()` 4-8× per user click (toggle + hierarchy rebuild +
// selection update + post-state-change), and each call did a full SVG
// wipe + 5000-node rebuild. Coalescing collapses N synchronous calls
// in the same task into ONE render the next frame. The actual
// implementation lives in `_renderCardViewImpl`; the public
// `renderCardView()` is the scheduler.
let _cardViewRenderPending = false;
let _cardViewRenderRequestedAt = 0;
function renderCardView() {
    // First-call path: ensure init runs synchronously so `cvState`
    // exists for any subsequent state reads in the same tick.
    if (!cvState || !cvState.initialized) {
        try { initCardView(); } catch (_e) { /* defensive */ }
    }
    if (_cardViewRenderPending) return;
    _cardViewRenderPending = true;
    _cardViewRenderRequestedAt = (typeof performance !== 'undefined' && performance.now)
        ? performance.now() : Date.now();
    const _runRender = () => {
        _cardViewRenderPending = false;
        try {
            _renderCardViewImpl();
        } catch (e) {
            // Surface the error in console but never let render exceptions
            // wedge the scheduler in a "pending forever" state.
            console.error('[card-view] render failed', e);
        }
    };
    if (typeof requestAnimationFrame === 'function') {
        requestAnimationFrame(_runRender);
    } else {
        // Fallback for non-browser tests (jsdom etc.).
        setTimeout(_runRender, 0);
    }
}

// Force a synchronous render — used by code paths that genuinely need
// the DOM up-to-date before continuing (e.g. printing, snapshotting).
function renderCardViewSync() {
    _cardViewRenderPending = false;
    _renderCardViewImpl();
}

function _renderCardViewImpl() {
    if (!cvState.initialized) initCardView();

    // Refresh badge counts in case SNAP data changed since last render
    _populatePathwayBadgeCounts();

    // Surface pipeline diagnostics: pass_rate banner above the card view,
    // depth-issue badges per node, partial-chain badges per indirect.
    // No-op if SNAP._diagnostics is absent (e.g. pre-Phase-C1 cached data).
    try {
        if (window.cvDiagnostics && typeof SNAP !== 'undefined' && SNAP) {
            window.cvDiagnostics.renderBanner(SNAP);
            // Defer badge application until after D3 has finished
            // re-rendering nodes for this pass.
            setTimeout(() => {
                try {
                    const root = document.getElementById('card-svg') || document;
                    window.cvDiagnostics.applyDepthBadges(root, SNAP.interactions || []);
                    window.cvDiagnostics.applyPartialChainBadges(root, SNAP._diagnostics);
                    // P3.1 surface: write-time pathway drift correction.
                    // The backend (scripts/pathway_v2/quick_assign.py) now
                    // emits a ``pathway_drifts`` array when claim
                    // assignments disagree with prose keywords; this badge
                    // makes the rewrite visible in the card view so the
                    // user knows the pathway they see is corrected (or
                    // flagged for review) rather than the raw LLM output.
                    if (typeof window.cvDiagnostics.applyPathwayDriftBadges === 'function') {
                        window.cvDiagnostics.applyPathwayDriftBadges(root, SNAP._diagnostics);
                    }
                } catch (_e) { /* defensive: never break render on diagnostics */ }
            }, 0);
        }
    } catch (_e) { /* defensive */ }

    // --- Empty state handling (mode-aware) ---
    if (cvState.cardViewMode === 'pathway' && cvState.selectedRoots.size === 0) {
        cvG.selectAll('.cv-node').remove();
        cvG.selectAll('.cv-link').remove();
        cvG.selectAll('.cv-empty-state').remove();
        cvSvg.attr('width', 600).attr('height', 200);
        cvG.attr('transform', 'translate(300, 100)');
        const emptyGroup = cvG.append('g').attr('class', 'cv-empty-state');
        emptyGroup.append('text')
            .attr('text-anchor', 'middle').attr('dy', '-0.5em')
            .style('fill', '#64748b').style('font-size', '16px').style('font-family', 'Inter, sans-serif')
            .text('No pathways selected');
        emptyGroup.append('text')
            .attr('text-anchor', 'middle').attr('dy', '1.2em')
            .style('fill', '#475569').style('font-size', '12px').style('font-family', 'Inter, sans-serif')
            .text('Use the Pathway Navigator to select pathways');
        return;
    }

    if (cvState.cardViewMode === 'interactor') {
        const hasInteractions = getInteractorModeInteractions().length > 0;
        if (!hasInteractions) {
            cvG.selectAll('.cv-node').remove();
            cvG.selectAll('.cv-link').remove();
            cvG.selectAll('.cv-empty-state').remove();
            cvSvg.attr('width', 600).attr('height', 200);
            cvG.attr('transform', 'translate(300, 100)');
            const emptyGroup = cvG.append('g').attr('class', 'cv-empty-state');
            emptyGroup.append('text')
                .attr('text-anchor', 'middle').attr('dy', '-0.5em')
                .style('fill', '#64748b').style('font-size', '16px').style('font-family', 'Inter, sans-serif')
                .text('No interaction data available');
            emptyGroup.append('text')
                .attr('text-anchor', 'middle').attr('dy', '1.2em')
                .style('fill', '#475569').style('font-size', '12px').style('font-family', 'Inter, sans-serif')
                .text('Query a protein to see its interactors');
            return;
        }
    }

    // Clear any existing empty state
    cvG.selectAll('.cv-empty-state').remove();

    // --- Mode-aware rendering ---
    if (cvState.cardViewMode === 'interactor') {
        renderInteractorDAG();
        return;
    }
    const data = buildCardHierarchy();
    const root = d3.hierarchy(data);

    // Helper: Count descendants for a node (for separation calculation)
    function countDescendants(node) {
        if (!node.children || node.children.length === 0) return 1;
        return node.children.reduce((sum, child) => sum + countDescendants(child), 0);
    }

    // D3 Tree Layout with dynamic separation based on subtree size
    const treeLayout = d3.tree()
        .nodeSize([CV_CONFIG.NODE_VERTICAL_SPACING, CV_CONFIG.LEVEL_SPACING])
        .separation((a, b) => {
            // Base separation for siblings vs cousins
            const baseSep = a.parent === b.parent ? 1.1 : 1.5;

            // Calculate subtree sizes - expanded nodes need more space
            const aDescendants = countDescendants(a);
            const bDescendants = countDescendants(b);
            const maxDescendants = Math.max(aDescendants, bDescendants);

            // Scale separation based on largest subtree
            // More descendants = more space needed
            if (maxDescendants > 1) {
                // Add extra space proportional to subtree size
                return baseSep + (maxDescendants - 1) * 0.3;
            }
            return baseSep;
        });

    treeLayout(root);

    // --- Nodes ---
    const nodes = root.descendants();
    const links = root.links();

    // Build lookup of ATXN3 nodes by scoped UID
    const mainNodesByUid = new Map();
    nodes.forEach(n => {
        if (n.data._uid) mainNodesByUid.set(n.data._uid, n);
    });

    // Add extra links from non-first upstream anchors to their CORRECT ATXN3
    nodes.forEach(n => {
        if (n.data._extraLinkToMain && n.data._extraLinkTargetUid) {
            const targetNode = mainNodesByUid.get(n.data._extraLinkTargetUid);
            if (targetNode) {
                links.push({ source: n, target: targetNode });
            }
        }
    });

    // Bind data — old-style pattern for proper enter/update separation
    const nodeSel = cvG.selectAll('.cv-node')
        .data(nodes, d => d.data._uid || d.data.id);

    // EXIT
    nodeSel.exit().transition().duration(CV_CONFIG.ANIMATION_DURATION)
        .style('opacity', 0)
        .remove();

    // ENTER
    // L5.1: same pseudo / chain-link class wiring as the bag layout.
    const _treeCvClass = d => 'cv-node'
        + (d.data?._isPseudo ? ' pseudo' : '')
        + (d.data?._isChainLink ? ' chain-link' : '');
    const nodeEnter = nodeSel.enter()
        .append('g')
        .attr('class', _treeCvClass)
        .attr('transform', d => `translate(${d.y},${d.x})`)
        .style('opacity', 0)
        .on('click', handleCardClick);
    // Also apply to existing nodes on update so a flip in pseudo state
    // (rare, but possible after a refresh) is reflected.
    nodeSel.attr('class', _treeCvClass);

    // Definitions for gradients (re-defined here to ensure availability if needed, or rely on main)
    // We'll rely on global defs, but add card specific drop shadow
    const defs = cvSvg.select('defs').empty() ? cvSvg.append('defs') : cvSvg.select('defs');

    // Card Rect
    nodeEnter.append('rect')
        .attr('width', CV_CONFIG.CARD_WIDTH)
        .attr('height', CV_CONFIG.CARD_HEIGHT)
        .attr('y', -CV_CONFIG.CARD_HEIGHT / 2)
        .attr('rx', 8)
        .style('fill', d => getCVColor(d.data))
        .style('stroke', d => getCVStroke(d.data))
        .style('stroke-width', '1px')
    //.style('filter', 'drop-shadow(0px 4px 6px rgba(0,0,0,0.3))'); // Performance hit?

    // 1. Main Label (Protein or Pathway Name) with tooltip for truncated text
    nodeEnter.append('text')
        .attr('class', 'cv-label')
        .attr('x', 15)
        .attr('dy', d => d.data.type === 'interactor' ? '-0.4em' : '0.2em')
        .style('fill', 'white')
        .style('font-size', '15px')
        .style('font-weight', '600')
        .style('font-family', 'Inter, sans-serif')
        .style('pointer-events', 'none')
        .text(d => truncateCVText(d.data.id === data.id ? d.data.id : _cvDisplayLabel(d.data), 26));

    // Add tooltip (title) for truncated labels
    nodeEnter.each(function(d) {
        const fullText = String(d.data.id === data.id ? d.data.id : _cvDisplayLabel(d.data));
        if (fullText.length > 26) {
            d3.select(this).append('title').text(fullText);
        }
    });

    // 2. Subtitle / Context (Relationship info)
    nodeEnter.each(function (d) {
        if (d.data.type === 'interactor' && d.data.contextText) {
            d3.select(this).append('text')
                .attr('class', 'cv-subtitle')
                .attr('x', 15)
                .attr('dy', '1.1em')
                .style('fill', '#94a3b8')
                .style('font-size', '11px')
                .style('font-family', 'Inter, sans-serif')
                .style('pointer-events', 'none')
                .text(truncateCVText(d.data.contextText, 40));
        } else if (d.data.type === 'main') {
            d3.select(this).append('text')
                .attr('class', 'cv-subtitle')
                .attr('x', 15)
                .attr('dy', '1.1em')
                .style('fill', '#94a3b8')
                .style('font-size', '11px')
                .style('font-family', 'Inter, sans-serif')
                .style('pointer-events', 'none')
                .text('Query protein');
        } else if (d.data.type === 'pathway') {
            d3.select(this).append('text')
                .attr('class', 'cv-badge')
                .attr('x', 15)
                .attr('dy', '1.8em')
                .style('fill', 'rgba(255,255,255,0.5)')
                .style('font-size', '10px')
                .style('pointer-events', 'none')
                .text(`Pathway Level ${d.data.level}`);

            // Chain merge toggle icon (only on pathways that have chains)
            const pwId = d.data.id;
            const pwInteractors = d.data.interactor_ids || [];
            const pwChains = groupChainsByChainId(pwInteractors);
            if (pwChains.size > 0) {
                const isMerged = _pathwayMergeMode.get(pwId) || false;
                d3.select(this).append('text')
                    .attr('class', `cv-chain-toggle ${isMerged ? 'active' : ''}`)
                    .attr('x', CV_CONFIG.CARD_WIDTH - 25)
                    .attr('dy', '1.6em')
                    .attr('text-anchor', 'middle')
                    .style('fill', isMerged ? '#3b82f6' : 'rgba(255,255,255,0.35)')
                    .style('font-size', '14px')
                    .style('cursor', 'pointer')
                    .style('pointer-events', 'all')
                    .text('\u{1F517}') // 🔗
                    .on('click', (event) => {
                        event.stopPropagation();
                        _pathwayMergeMode.set(pwId, !isMerged);
                        // Re-render the card view to apply mode change
                        if (typeof renderCardView === 'function') renderCardView();
                    })
                    .append('title')
                    .text(isMerged ? 'Switch to separated chains' : 'Switch to merged chains');
            }
        } else if (d.data.type === 'direction_group') {
            // Direction group subtitle: count of interactors
            d3.select(this).append('text')
                .attr('class', 'cv-subtitle')
                .attr('x', 15)
                .attr('dy', '1.1em')
                .style('fill', '#94a3b8')
                .style('font-size', '11px')
                .style('font-family', 'Inter, sans-serif')
                .style('pointer-events', 'none')
                .text(`${d.data.count} interactor${d.data.count !== 1 ? 's' : ''}`);
            // Expand/collapse indicator
            const hasChildren = d.data.children && d.data.children.length > 0;
            const isExpanded = cvState.interactorExpandedGroups.has(d.data.direction);
            d3.select(this).append('text')
                .attr('x', CV_CONFIG.CARD_WIDTH - 25)
                .attr('dy', '0.2em')
                .attr('text-anchor', 'middle')
                .style('fill', '#94a3b8')
                .style('font-size', '16px')
                .style('pointer-events', 'none')
                .text(isExpanded ? '\u25BE' : '\u25B8');
        } else if (d.data.type === 'arrow_subgroup') {
            // Arrow subgroup: colored dot + expand indicator
            const _cc = getCVColors();
            const colors = { activates: _cc.activates.badge, inhibits: _cc.inhibits.badge, binds: _cc.binds.badge, regulates: _cc.regulates.badge };
            const color = colors[d.data.arrowType] || '#94a3b8';
            // Colored dot before label
            d3.select(this).append('circle')
                .attr('cx', CV_CONFIG.CARD_WIDTH - 40)
                .attr('cy', 0)
                .attr('r', 5)
                .style('fill', color)
                .style('pointer-events', 'none');
            // Expand indicator
            const subKey = `${d.data.direction}_${d.data.arrowType}`;
            const isSubExpanded = cvState.interactorExpandedGroups.has(subKey);
            d3.select(this).append('text')
                .attr('x', CV_CONFIG.CARD_WIDTH - 25)
                .attr('dy', '0.2em')
                .attr('text-anchor', 'middle')
                .style('fill', '#94a3b8')
                .style('font-size', '16px')
                .style('pointer-events', 'none')
                .text(isSubExpanded ? '\u25BE' : '\u25B8');
        }
    });

    // 3. "Also in" / Pathway membership indicator
    nodeEnter.each(function (d) {
        if (d.data.type === 'interactor') {
            let pathwayNames = [];

            if (cvState.cardViewMode === 'interactor' && d.data.pathwayMemberships) {
                // Interactor mode: show pathway memberships from node data
                pathwayNames = d.data.pathwayMemberships;
            } else {
                // Pathway mode: show other pathways via helper
                const proteinId = d.data.label || d.data.id;
                const otherPathways = typeof getPathwaysForProtein === 'function'
                    ? getPathwaysForProtein(proteinId).filter(p => p.id !== d.data.pathwayId)
                    : [];
                pathwayNames = otherPathways.map(p => p.name);
            }

            if (pathwayNames.length > 0) {
                const prefix = cvState.cardViewMode === 'interactor' ? 'In: ' : 'Also in: ';
                const displayText = `${prefix}${pathwayNames.slice(0, 2).join(', ')}`;
                const suffix = pathwayNames.length > 2 ? ` +${pathwayNames.length - 2}` : '';

                d3.select(this).append('text')
                    .attr('class', 'cv-also-in')
                    .attr('x', 15)
                    .attr('dy', '2.3em')
                    .style('fill', '#64748b')
                    .style('font-size', '10px')
                    .style('font-style', 'italic')
                    .style('font-family', 'Inter, sans-serif')
                    .style('pointer-events', 'none')
                    .text(truncateCVText(displayText + suffix, 38));
            }
        }
    });

    // 4. Interaction Organism Badges (top-right corner) — rendered via helper
    //    that both enter and update paths call so badge counts stay fresh.
    nodeEnter.each(function (d) {
        _renderCardBadges(d3.select(this), d.data);
    });

    // Expand/Collapse Indicator
    nodeEnter.each(function (d) {
        if (d.data.type === 'pathway') {
            const hasChildren = d.data._childrenCount > 0;
            if (hasChildren) {
                d3.select(this).append('circle')
                    .attr('cx', CV_CONFIG.CARD_WIDTH - 25)
                    .attr('cy', 0)
                    .attr('r', 10)
                    .style('fill', 'rgba(255,255,255,0.1)')
                    .style('stroke', 'rgba(255,255,255,0.3)');

                d3.select(this).append('text')
                    .attr('class', 'cv-expander')
                    .attr('x', CV_CONFIG.CARD_WIDTH - 25)
                    .attr('dy', '0.35em')
                    .attr('text-anchor', 'middle')
                    .style('fill', 'white')
                    .style('font-size', '12px')
                    .style('cursor', 'pointer')
                    .text(cvState.expandedNodes.has(d.data.id) ? '−' : '+');
            }
        }
    });

    // UPDATE
    const nodeUpdate = nodeSel.merge(nodeEnter);

    nodeUpdate.transition().duration(CV_CONFIG.ANIMATION_DURATION)
        .attr('transform', d => `translate(${d.y},${d.x})`)
        .style('opacity', 1);

    nodeUpdate.select('rect')
        .style('fill', d => getCVColor(d.data))
        .style('stroke', d => getCVStroke(d.data));

    nodeUpdate.select('.cv-expander')
        .text(d => cvState.expandedNodes.has(d.data.id) ? '−' : '+');

    nodeUpdate.select('.cv-label')
        .text(d => truncateCVText(d.data.id === data.id ? d.data.id : _cvDisplayLabel(d.data), 26));

    // Update subtitle text on existing nodes (context may change on re-render)
    nodeUpdate.select('.cv-subtitle')
        .text(d => {
            if (d.data.type === 'interactor' && d.data.contextText) {
                return truncateCVText(d.data.contextText, 38);
            }
            if (d.data.type === 'main') {
                return 'Query protein';
            }
            if (d.data.type === 'direction_group') {
                return `${d.data.count} interactor${d.data.count !== 1 ? 's' : ''}`;
            }
            return '';
        });

    // Re-render badges on existing nodes so pathway counts stay fresh
    nodeUpdate.each(function (d) {
        _renderCardBadges(d3.select(this), d.data);
    });

    // --- Links ---
    const linkSel = cvG.selectAll('.cv-link')
        .data(links, d => (d.source.data._uid || d.source.data.id) + '->' + (d.target.data._uid || d.target.data.id));

    linkSel.exit().transition().duration(CV_CONFIG.ANIMATION_DURATION)
        .style('opacity', 0)
        .remove();

    const linkEnter = linkSel.enter()
        .insert('path', '.cv-node')
        .attr('class', 'cv-link')
        .style('fill', 'none')
        .style('stroke', '#475569') // Slate-600
        .style('stroke-width', '1.5px')
        .attr('d', d => {
            const o = { x: d.source.x, y: d.source.y };
            return d3.linkHorizontal()
                .x(d => d.y)
                .y(d => d.x)
                ({ source: o, target: o });
        });

    linkSel.merge(linkEnter).transition().duration(CV_CONFIG.ANIMATION_DURATION)
        .attr('d', d3.linkHorizontal()
            .x(d => d.y)
            .y(d => d.x)
        );

    // --- Layer 3 of CLAUDE_DOCS/11_CHAIN_TOPOLOGY.md ---
    // (a) Direction-aware chain edge labels — render the biological
    //     verb on each chain edge so direction is unambiguous even
    //     when spatial layout is constrained by the strict tree.
    //     Load-bearing for chains stored in inverted query-centric
    //     order (the going-forward-only canonicalization in db_sync
    //     doesn't backfill existing rows — see Phase B.1).
    // (b) Cross-links between duplicates of the same protein — when
    //     a protein appears as both a direct interactor and a chain
    //     participant under the same pathway, draw a faint dashed
    //     path between the two cards so the user sees they're the
    //     same entity. Hover on any instance highlights all.
    _renderChainEdgeLabels(links);
    _renderDuplicateCrossLinks(nodes);

    // --- Resize SVG to fit content (for scrollable view) ---
    resizeCardViewToFit(nodes);
}

function _renderChainEdgeLabels(links) {
    cvG.selectAll('.cv-chain-edge-label').remove();
    if (!links || !links.length) return;
    const chainLinks = links.filter(d => {
        const td = d && d.target && d.target.data;
        return td && td._chainId && typeof td._inboundChainArrow === 'string' && td._inboundChainArrow;
    });
    if (!chainLinks.length) return;
    cvG.selectAll('.cv-chain-edge-label')
        .data(chainLinks, d => (d.source.data._uid || d.source.data.id) + '->' + (d.target.data._uid || d.target.data.id))
        .enter()
        .append('text')
        .attr('class', d => `cv-chain-edge-label arrow-${d.target.data._inboundChainArrow}`)
        .attr('x', d => (d.source.y + d.target.y) / 2)
        .attr('y', d => (d.source.x + d.target.x) / 2 - 4)
        .attr('text-anchor', 'middle')
        .style('pointer-events', 'none')
        .text(d => d.target.data._inboundChainArrow);
}

function _renderDuplicateCrossLinks(nodes) {
    cvG.selectAll('.cv-duplicate-crosslink').remove();
    if (!nodes || nodes.length < 2) return;

    const _baseProtein = (d) => {
        const data = d.data || {};
        if (data.type !== 'interactor') return null;
        // _duplicateOf is the original direct-protein id; if absent,
        // fall back to data.id which already equals the symbol for
        // protein nodes.
        return data._duplicateOf || data.id || null;
    };

    const byProtein = new Map();
    nodes.forEach(d => {
        const protein = _baseProtein(d);
        if (!protein) return;
        if (!byProtein.has(protein)) byProtein.set(protein, []);
        byProtein.get(protein).push(d);
    });

    const pairs = [];
    for (const [protein, instances] of byProtein) {
        if (instances.length < 2) continue;
        // Cap at 5 cross-links per protein to keep the visual readable
        // for densely-duplicated cases.
        const cap = Math.min(instances.length, 5);
        for (let i = 0; i < cap; i++) {
            for (let j = i + 1; j < cap; j++) {
                pairs.push({ a: instances[i], b: instances[j], protein });
            }
        }
    }
    if (!pairs.length) return;

    // Insert below nodes so the dashed lines don't paint over cards.
    cvG.selectAll('.cv-duplicate-crosslink')
        .data(pairs, p => `${p.protein}::${p.a.data._uid || p.a.data.id}::${p.b.data._uid || p.b.data.id}`)
        .enter()
        .insert('path', '.cv-node')
        .attr('class', 'cv-duplicate-crosslink')
        .attr('data-protein', p => p.protein)
        .attr('d', p => {
            // Cubic Bezier between the two card centers (d.y is horizontal
            // in the rotated tree layout, d.x is vertical).
            const x1 = p.a.y, y1 = p.a.x;
            const x2 = p.b.y, y2 = p.b.x;
            const cx1 = (x1 + x2) / 2;
            const cy1 = y1;
            const cx2 = (x1 + x2) / 2;
            const cy2 = y2;
            return `M${x1},${y1} C${cx1},${cy1} ${cx2},${cy2} ${x2},${y2}`;
        });

    // Hover any instance of a multi-rendered protein highlights all
    // its instances and the cross-links between them. Idempotent —
    // re-bind on every render so newly-added nodes get the handler.
    cvG.selectAll('.cv-node')
        .on('mouseenter.cv-duplicate', function(event, d) {
            const protein = _baseProtein(d);
            if (!protein || !byProtein.has(protein)) return;
            const instances = byProtein.get(protein);
            if (instances.length < 2) return;
            cvG.selectAll('.cv-node').classed('cv-protein-active', false);
            instances.forEach(inst => {
                cvG.selectAll('.cv-node')
                    .filter(n => n === inst)
                    .classed('cv-protein-active', true);
            });
            cvG.selectAll('.cv-duplicate-crosslink')
                .classed('highlighted', function() {
                    return this.getAttribute('data-protein') === protein;
                });
        })
        .on('mouseleave.cv-duplicate', function() {
            cvG.selectAll('.cv-node').classed('cv-protein-active', false);
            cvG.selectAll('.cv-duplicate-crosslink').classed('highlighted', false);
        });
}

/**
 * Resize the SVG to fit all nodes, enabling native scroll
 */
// Rough monospace-char-width approximation for 15px Inter at font-weight 600.
// Used to extend the bounding box for cards with labels longer than CARD_WIDTH.
const _CV_APPROX_CHAR_PX = 8.2;
const _CV_LABEL_PADDING = 30; // left/right padding inside the card rect

function _estimateLabelWidth(data) {
    const label = _cvDisplayLabel(data);
    // Account for label + trailing context text on interactor nodes
    const contextExtra = data && data.type === 'interactor' && data.contextText
        ? Math.min(String(data.contextText).length, 38)
        : 0;
    const chars = Math.max(Math.min(String(label).length, 30), contextExtra);
    return Math.max(CV_CONFIG.CARD_WIDTH, chars * _CV_APPROX_CHAR_PX + _CV_LABEL_PADDING);
}

function resizeCardViewToFit(nodes) {
    if (!nodes || nodes.length === 0) return;

    // Calculate bounding box of all nodes — widen horizontally for cards whose
    // text labels exceed the default CARD_WIDTH (otherwise long pathway names
    // overflow into neighboring cards).
    let minX = Infinity, maxX = -Infinity;
    let minY = Infinity, maxY = -Infinity;

    nodes.forEach(d => {
        const widthForThisNode = _estimateLabelWidth(d.data);
        // d.x is vertical position, d.y is horizontal position in tree layout
        minX = Math.min(minX, d.x - CV_CONFIG.CARD_HEIGHT / 2);
        maxX = Math.max(maxX, d.x + CV_CONFIG.CARD_HEIGHT / 2);
        minY = Math.min(minY, d.y);
        maxY = Math.max(maxY, d.y + widthForThisNode);
    });

    // Add padding
    const padding = 60;
    const contentWidth = (maxY - minY) + padding * 2;
    const contentHeight = (maxX - minX) + padding * 2;

    // Set SVG size to fit content
    cvSvg
        .attr('width', contentWidth)
        .attr('height', contentHeight);

    // Translate the group to account for negative positions + padding
    cvG.attr('transform', `translate(${-minY + padding}, ${-minX + padding})`);
}

// ============================================================================
// INTERACTIONS
// ============================================================================

function makeCardModalContext(d, pathwayContext = null) {
    const data = d?.data || {};
    const parentData = d?.parent?.data || null;
    const inheritedPathwayContext = pathwayContext || data._pathwayContext || (
        data.pathwayId
            ? { id: data.pathwayId, name: data.pathwayId }
            : null
    );

    return {
        id: data.id,
        label: data.label || data.id,
        originalId: data.originalId || data._duplicateOf || data.id,
        uid: data._uid || null,
        nodeType: data.type || null,
        parentId: parentData?._uid || parentData?.id || data.parentId || null,
        pathwayId: inheritedPathwayContext?.id || data.pathwayId || null,
        pathwayContext: inheritedPathwayContext,
        _pathwayContext: inheritedPathwayContext,
        _chainId: data._chainId ?? null,
        _chainPosition: data._chainPosition ?? null,
        _chainLength: data._chainLength ?? null,
        _chainProteins: Array.isArray(data._chainProteins) ? data._chainProteins.slice() : null,
        relationshipText: data.contextText || '',
        relationshipArrow: data.arrowType || data._inboundChainArrow || null,
        _inboundChainArrow: data._inboundChainArrow || null,
        duplicateOf: data._duplicateOf || null,
        isChainDuplicate: !!data._isChainDuplicate,
    };
}

function handleCardClick(event, d) {
    // --- Interactor Mode: direction_group / arrow_subgroup click = toggle expand ---
    if (d.data.type === 'direction_group') {
        const key = d.data.direction;
        if (cvState.interactorExpandedGroups.has(key)) {
            // Collapse: remove this key and all its arrow subgroup children
            cvState.interactorExpandedGroups.delete(key);
            ['activates', 'inhibits', 'binds', 'regulates'].forEach(at => {
                cvState.interactorExpandedGroups.delete(`${key}_${at}`);
            });
        } else {
            // Expand: add this key and auto-expand all arrow subgroups
            cvState.interactorExpandedGroups.add(key);
            ['activates', 'inhibits', 'binds', 'regulates'].forEach(at => {
                cvState.interactorExpandedGroups.add(`${key}_${at}`);
            });
        }
        renderCardView();
        return;
    }

    if (d.data.type === 'arrow_subgroup') {
        const key = `${d.data.direction}_${d.data.arrowType}`;
        if (cvState.interactorExpandedGroups.has(key)) {
            cvState.interactorExpandedGroups.delete(key);
        } else {
            cvState.interactorExpandedGroups.add(key);
        }
        renderCardView();
        return;
    }

    // --- Interactor card click (also handle main protein click in DAG mode) ---
    if (d.data.type === 'interactor' || (d.data.type === 'main' && cvState.cardViewMode === 'interactor')) {
        // Find pathway context (only meaningful in pathway mode)
        let pathwayContext = null;
        if (cvState.cardViewMode === 'pathway') {
            let current = d;
            while (current.parent && !pathwayContext) {
                if (current.parent.data.type === 'pathway') {
                    pathwayContext = {
                        id: current.parent.data.id,
                        name: current.parent.data.label,
                        level: current.parent.data.level
                    };
                    break;
                }
                current = current.parent;
            }
        }

        if (window.openModalForCard) {
            const cardContext = makeCardModalContext(d, pathwayContext);
            window.openModalForCard(d.data.id, pathwayContext, cardContext);
        } else if (window.handleNodeClick) {
            window.handleNodeClick({
                id: d.data.id,
                type: 'interactor',
                label: d.data.label,
                pathwayContext: pathwayContext,
                cardContext: makeCardModalContext(d, pathwayContext)
            });
        }
    } else {
        // Pathway mode: Toggle Expansion via PathwayState
        expandAndSelectChildren(d.data.id, 'click');
    }
}

// ============================================================================
// SIDEBAR (Replicated Logic)
// ============================================================================

function renderCardSidebar() {
    const tree = document.getElementById('pathway-tree-card');
    if (!tree) return;

    tree.innerHTML = '';

    // ✅ REDESIGNED: Show ALL selected pathways (any level), grouped by hierarchy level
    const allPathways = window.getRawPathwayData ? window.getRawPathwayData() : [];
    const hierarchyMap = window.getPathwayHierarchy ? window.getPathwayHierarchy() : new Map();

    // Get all selected pathways (not just L0!)
    const selectedIds = new Set(cvState.selectedRoots);

    if (selectedIds.size === 0) {
        tree.innerHTML = '<div style="padding: 20px; text-align: center; color: #64748b; font-size: 13px;">No pathways selected.<br>Use the Explorer to select pathways.</div>';
        return;
    }

    // Group pathways by hierarchy level
    const byLevel = new Map();
    selectedIds.forEach(id => {
        const hier = hierarchyMap.get(id);
        const level = hier?.level || 0;
        if (!byLevel.has(level)) byLevel.set(level, []);
        byLevel.get(level).push(id);
    });

    // Sort levels (L0, L1, L2, ...)
    const sortedLevels = [...byLevel.keys()].sort((a, b) => a - b);

    // Render each level
    sortedLevels.forEach(level => {
        const ids = byLevel.get(level);

        // Level header
        const levelSection = document.createElement('div');
        levelSection.className = 'cv-sidebar-level';
        levelSection.innerHTML = `
            <div class="cv-sidebar-level-header">
                <span class="cv-sidebar-level-badge">L${level}</span>
                <span class="cv-sidebar-level-count">${ids.length} pathway${ids.length > 1 ? 's' : ''}</span>
            </div>
        `;

        // Items container
        const itemsContainer = document.createElement('div');
        itemsContainer.className = 'cv-sidebar-items';

        // Render each pathway in this level
        ids.forEach(id => {
            const pw = allPathways.find(p => (p.id || `pathway_${p.name.replace(/\s+/g, '_')}`) === id);
            if (!pw) return;

            const isHidden = cvState.hiddenCards.has(id);
            const isRecent = typeof PathwayState !== 'undefined' && PathwayState.getRecentlyChanged().has(id);

            // Get interaction metadata
            let interactions = null;
            if (typeof PathwayState !== 'undefined') {
                const metadata = PathwayState.getInteractionMetadata();
                interactions = metadata.get(id);
            }
            // Fallback: count actual arrow types from SNAP
            if (!interactions && pw.interactor_ids) {
                interactions = { activates: 0, inhibits: 0, binds: 0, regulates: 0, total: 0 };
                const ids = new Set(pw.interactor_ids);
                if (typeof SNAP !== 'undefined' && SNAP.interactions) {
                    SNAP.interactions.forEach(inter => {
                        const target = (inter.target !== SNAP.main) ? inter.target : inter.source;
                        if (ids.has(target)) {
                            const arrow = inter.arrow || 'binds';
                            const key = (arrow === 'complex') ? 'binds' : arrow;
                            if (interactions.hasOwnProperty(key)) interactions[key]++;
                            else interactions.binds++;
                            interactions.total++;
                        }
                    });
                }
                if (interactions.total === 0) {
                    interactions.total = pw.interactor_ids.length;
                    interactions.binds = interactions.total;
                }
            }

            const item = document.createElement('div');
            item.className = `cv-sidebar-item ${isHidden ? 'hidden' : ''} ${isRecent ? 'recent-change' : ''}`;
            item.dataset.pathwayId = id;

            // Build interaction badges HTML
            let badgesHtml = '';
            if (interactions) {
                const types = ['activates', 'inhibits', 'binds', 'regulates'];
                types.forEach(type => {
                    if (interactions[type] > 0) {
                        badgesHtml += `
                            <div class="cv-interaction-organism ${type}" title="${interactions[type]} ${type}">
                                <div class="organism-core"></div>
                                <div class="organism-aura"></div>
                                <span class="organism-count">${interactions[type]}</span>
                            </div>
                        `;
                    }
                });
            }

            // Eye icon SVG
            const eyeIcon = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>`;
            const eyeOffIcon = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17.94 17.94A10.07 10.07 0 0112 20c-7 0-11-8-11-8a18.45 18.45 0 015.06-5.94M9.9 4.24A9.12 9.12 0 0112 4c7 0 11 8 11 8a18.5 18.5 0 01-2.16 3.19m-6.72-1.07a3 3 0 11-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>`;

            item.innerHTML = `
                <div class="cv-sidebar-item-main">
                    <span class="cv-sidebar-item-name" title="${_cvHtml(pw.name)}">${_cvHtml(pw.name)}</span>
                    <div class="cv-sidebar-item-actions">
                        ${badgesHtml}
                        <button class="cv-visibility-btn ${isHidden ? 'is-hidden' : ''}"
                                type="button"
                                title="${isHidden ? 'Show' : 'Hide'} pathway">
                            ${isHidden ? eyeOffIcon : eyeIcon}
                        </button>
                    </div>
                </div>
            `;

            const visibilityBtn = item.querySelector('.cv-visibility-btn');
            if (visibilityBtn) {
                visibilityBtn.addEventListener('click', (event) => {
                    event.stopPropagation();
                    toggleCardPathwayVisibility(id);
                });
            }

            itemsContainer.appendChild(item);
        });

        levelSection.appendChild(itemsContainer);
        tree.appendChild(levelSection);
    });

}

// ✅ NEW: Toggle pathway visibility (hide/show)
window.toggleCardPathwayVisibility = (id) => {
    const isHidden = cvState.hiddenCards.has(id);

    if (isHidden) {
        cvState.hiddenCards.delete(id);
    } else {
        cvState.hiddenCards.add(id);
    }

    // Sync to PathwayState if available
    if (typeof PathwayState !== 'undefined') {
        PathwayState.toggleVisibility(id, 'sidebar');
    }

    renderCardSidebar(); // Update sidebar UI
    renderCardView();    // Update visualization
};

// Global handlers for Sidebar
window.toggleCardRoot = (id) => {
    if (cvState.selectedRoots.has(id)) {
        cvState.selectedRoots.delete(id);
    } else {
        cvState.selectedRoots.add(id);
    }
    renderCardSidebar(); // Update UI highlight
    renderCardView();    // Update Visualization
};

window.selectAllCardRoots = () => {
    // Only select L0 pathways for "Select All"
    cvState.rootPathways
        .filter(pw => (pw.hierarchy_level || 0) === 0)
        .forEach(pw => {
            const id = pw.id || `pathway_${(pw.name || 'unknown').replace(/\s+/g, '_')}`;
            cvState.selectedRoots.add(id);
        });
    renderCardSidebar();
    renderCardView();
};

window.clearAllCardRoots = () => {
    cvState.selectedRoots.clear();
    renderCardSidebar();
    renderCardView();
};

window.filterCardSidebar = (query) => {
    // Hide/Show items based on name
    const items = document.querySelectorAll('#pathway-tree-card .pathway-item');
    const q = query.toLowerCase();
    items.forEach(item => {
        const name = item.querySelector('.pathway-name').innerText.toLowerCase();
        item.style.display = name.includes(q) ? 'block' : 'none';
    });
};

window.toggleCardSidebar = () => {
    const sidebar = document.getElementById('pathway-sidebar-card');
    const tab = document.getElementById('pathway-sidebar-tab-card');
    const cardView = document.getElementById('card-view');
    if (!sidebar || !tab || !cardView) return;
    cvState.sidebarCollapsed = !cvState.sidebarCollapsed;

    if (cvState.sidebarCollapsed) {
        sidebar.style.display = 'none';
        tab.style.display = 'flex';
        cardView.classList.add('sidebar-collapsed');
    } else {
        sidebar.style.display = 'flex';
        tab.style.display = 'none';
        cardView.classList.remove('sidebar-collapsed');
    }
};

// ============================================================================
// UTILS
// ============================================================================

function getCVColor(data) {
    const c = getCVColors();
    if (data.type === 'main') return 'url(#mainGradient)';
    if (data.type === 'pathway') return '#2e1065';
    // Direction group headers (Interactors Mode)
    if (data.type === 'direction_group') {
        if (data.direction === 'upstream') return '#0c1929';
        if (data.direction === 'downstream') return '#0c2912';
        if (data._isChainLink) return '#29200c';
        return '#1a1a2e';
    }
    // Arrow subgroup headers
    if (data.type === 'arrow_subgroup') {
        return (c[data.arrowType] || {}).subgroupBg || '#1e293b';
    }
    // Interactor bg dependent on arrow type
    if (data.type === 'interactor') {
        return (c[data.arrowType] || {}).bg || '#1e293b';
    }
    return '#1e293b';
}

function getCVStroke(data) {
    const c = getCVColors();
    if (data.type === 'main') return '#818cf8';
    if (data.type === 'pathway') return '#7c3aed';
    // Direction group strokes
    if (data.type === 'direction_group') {
        if (data.direction === 'upstream') return '#3b82f6';
        if (data.direction === 'downstream') return '#22c55e';
        if (data._isChainLink) return '#f59e0b';
        return '#64748b';
    }
    // Arrow subgroup / interactor strokes — use CSS-derived color
    if (data.type === 'arrow_subgroup' || data.type === 'interactor') {
        return (c[data.arrowType] || {}).stroke || '#94a3b8';
    }
    return '#334155';
}


function truncateCVText(text, len) {
    const value = text == null ? '' : String(text);
    if (!len || value.length <= len) return value;
    return `${value.slice(0, Math.max(0, len - 3)).trimEnd()}...`;
}

function _cvHtml(value) {
    return typeof escapeHtml === 'function' ? escapeHtml(value) : String(value ?? '');
}

function _cvDisplayLabel(data) {
    return (data && (data.label || data.id)) || '';
}


// ============================================================================
// PATHWAY EXPLORER V2 - Neural Command Interface
// ============================================================================

const PathwayExplorer = (function() {
    'use strict';

    // --- State ---
    const state = {
        selectedPathways: new Set(),      // ANY level pathway IDs
        hiddenCards: new Set(),           // Hidden from card view
        expandedBranches: new Set(),      // Explorer tree expansion
        interactionsByPathway: new Map(), // {activates, inhibits, binds, regulates}
        hasInteractions: new Map(),       // Propagated flags
        hasInteractorsInSubtree: new Map(), // Which pathways eventually lead to interactors
        searchQuery: '',
        hoveredPathway: null,
        breadcrumbPath: [],
        keyboardFocusIndex: -1,
        flattenedItems: [],
        isCollapsed: false,
        initialized: false
    };

    // --- DOM References ---
    let elements = {
        explorer: null,
        tree: null,
        breadcrumb: null,
        searchInput: null,
        collapsedTab: null,
        svgContainer: null
    };

    // =========================================================================
    // INITIALIZATION
    // =========================================================================

    function init() {
        if (state.initialized) return;

        cacheElements();

        // Wait for data to be available
        const rawPathways = window.getRawPathwayData ? window.getRawPathwayData() : [];
        if (rawPathways.length === 0) {
            setTimeout(init, 500);
            return;
        }

        computeInteractionMetadata();

        // ✅ FIX 2a: Do NOT auto-select anything on init
        // User explicitly selects pathways via navigator - start with empty selection
        cvState.selectedRoots.clear();
        state.selectedPathways.clear();

        // Sync hidden state (if any persisted)
        cvState.hiddenCards.forEach(id => state.hiddenCards.add(id));

        renderTree();
        updateStatsBar();

        // Observe PathwayState for cascade updates
        if (typeof PathwayState !== 'undefined') {
            PathwayState.observe('explorer', (eventType, data) => {
                if (eventType === 'selection') {
                    // Sync local state with PathwayState
                    state.selectedPathways.clear();
                    const selected = PathwayState.getSelectedPathways();
                    selected.forEach(id => state.selectedPathways.add(id));

                    // ✅ NEW: Auto-expand ancestors when selecting a pathway
                    if (data.selected && data.pathwayId) {
                        expandAncestors(data.pathwayId);
                    }

                    // Update UI for primary pathway
                    if (data.pathwayId) {
                        updateSelectionUI(data.pathwayId, data.selected);
                    }

                    // Update UI for cascaded pathways
                    if (data.cascadedIds && data.cascadedIds.length > 0) {
                        data.cascadedIds.forEach(id => {
                            const isSelected = state.selectedPathways.has(id);
                            updateSelectionUI(id, isSelected);
                            // Also expand ancestors for cascaded selections
                            if (isSelected) {
                                expandAncestors(id);
                            }
                        });
                    }

                    // BUG B FIX: Ensure all selected pathways have correct UI state
                    // This handles cases where selection comes from card view expansion
                    PathwayState.getSelectedPathways().forEach(id => {
                        updateSelectionUI(id, true);
                    });

                    updateStatsBar();

                    // ✅ FIX 2b: Sync to card view so it re-renders with new selections
                    // This is critical for bidirectional sync when expanding card nodes
                    syncToCardView();
                }

                // ✅ Handle expansion events for bidirectional sync
                if (eventType === 'expansion') {
                    // Sync local state from PathwayState
                    state.expandedBranches.clear();
                    PathwayState.getExpandedBranches().forEach(id => state.expandedBranches.add(id));

                    // Update DOM for all cascaded items (including ancestors)
                    const allIds = data.cascadedIds || [data.pathwayId];
                    allIds.forEach(id => {
                        const item = document.querySelector(`[data-pathway-id="${id}"]`);
                        if (item) {
                            const children = item.querySelector('.pe-children');
                            if (data.expanded) {
                                item.classList.add('expanded');
                                // BUG B FIX: Also update children container
                                if (children) {
                                    children.classList.remove('collapsed');
                                    children.classList.add('expanded');
                                }
                            } else {
                                item.classList.remove('expanded');
                                if (children) {
                                    children.classList.add('collapsed');
                                    children.classList.remove('expanded');
                                }
                            }
                        }
                    });

                    // BUG B FIX: Sync all selection states after expansion
                    // This ensures checkboxes are updated for auto-selected children
                    PathwayState.getSelectedPathways().forEach(id => {
                        updateSelectionUI(id, true);
                    });

                    // Scroll the primary pathway into view
                    if (data.expanded && data.pathwayId) {
                        const targetItem = document.querySelector(`[data-pathway-id="${data.pathwayId}"]`);
                        if (targetItem) {
                            targetItem.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
                        }
                    }
                }
            });
        }

        // Setup dragging
        setupDragging();

        state.initialized = true;
    }

    function setupDragging() {
        const explorer = elements.explorer;
        const header = explorer?.querySelector('.pe-header');
        if (!explorer || !header) return;

        let isDragging = false;
        let startX, startY, startLeft, startTop;

        header.addEventListener('mousedown', (e) => {
            // Don't drag if clicking on buttons or interactive elements
            if (e.target.closest('button') || e.target.closest('input') || e.target.closest('.pe-collapse-btn')) {
                return;
            }

            isDragging = true;
            startX = e.clientX;
            startY = e.clientY;

            const rect = explorer.getBoundingClientRect();
            startLeft = rect.left;
            startTop = rect.top;

            explorer.style.transition = 'none';
            document.body.style.cursor = 'grabbing';
            document.body.style.userSelect = 'none';

            e.preventDefault();
        });

        document.addEventListener('mousemove', (e) => {
            if (!isDragging) return;

            const deltaX = e.clientX - startX;
            const deltaY = e.clientY - startY;

            let newLeft = startLeft + deltaX;
            let newTop = startTop + deltaY;

            // Constrain to viewport
            const maxLeft = window.innerWidth - 100;
            const maxTop = window.innerHeight - 100;
            newLeft = Math.max(0, Math.min(newLeft, maxLeft));
            newTop = Math.max(0, Math.min(newTop, maxTop));

            explorer.style.left = newLeft + 'px';
            explorer.style.top = newTop + 'px';
        });

        document.addEventListener('mouseup', () => {
            if (isDragging) {
                isDragging = false;
                explorer.style.transition = '';
                document.body.style.cursor = '';
                document.body.style.userSelect = '';
            }
        });

        // Double-click to reset position
        header.addEventListener('dblclick', (e) => {
            if (e.target.closest('button') || e.target.closest('input')) return;
            explorer.style.left = '0';
            explorer.style.top = '60px';
        });
    }

    function cacheElements() {
        elements.explorer = document.getElementById('pathway-explorer-v2');
        elements.tree = document.getElementById('pe-tree');
        elements.breadcrumb = document.getElementById('pe-breadcrumb');
        elements.searchInput = document.getElementById('pe-search-input');
        elements.collapsedTab = document.getElementById('pe-collapsed-tab');
        elements.svgContainer = document.getElementById('card-svg-container');
    }

    // =========================================================================
    // INTERACTION METADATA
    // =========================================================================

    function computeInteractionMetadata() {
        const allPathways = window.getRawPathwayData?.() || [];
        const interactions = (typeof SNAP !== 'undefined' ? SNAP?.interactions : null) || [];

        state.interactionsByPathway.clear();
        state.hasInteractions.clear();

        allPathways.forEach(pw => {
            const pathwayId = pw.id || `pathway_${(pw.name || 'unknown').replace(/\s+/g, '_')}`;
            const counts = { activates: 0, inhibits: 0, binds: 0, regulates: 0, total: 0 };

            // Count by interaction type from interactor_ids
            (pw.interactor_ids || []).forEach(intId => {
                const interaction = interactions.find(i =>
                    i.source === intId || i.target === intId ||
                    i.source === SNAP?.main && i.target === intId ||
                    i.target === SNAP?.main && i.source === intId
                );
                if (interaction) {
                    const arrow = interaction.arrow || 'binds';
                    if (counts[arrow] !== undefined) {
                        counts[arrow]++;
                    }
                    counts.total++;
                }
            });

            state.interactionsByPathway.set(pathwayId, counts);
            state.hasInteractions.set(pathwayId, counts.total > 0);
        });

        propagateInteractionFlags();
        computeInteractorDescendantFlags();  // Compute which pathways lead to interactors

        // ✅ FIX 2b: Expose hasInteractorsInSubtree for use by expandAndSelectChildren()
        window.getHasInteractorsInSubtree = () => state.hasInteractorsInSubtree;
    }

    function propagateInteractionFlags() {
        const hierarchyMap = window.getPathwayHierarchy?.() || new Map();
        const allPathways = window.getRawPathwayData?.() || [];

        // ✅ IMPROVED: Bubble interactions up through ALL ancestors
        // Process from leaves up (higher levels first)
        const sorted = [...allPathways].sort((a, b) =>
            (b.hierarchy_level || 0) - (a.hierarchy_level || 0)
        );

        sorted.forEach(pw => {
            const pathwayId = pw.id || `pathway_${(pw.name || 'unknown').replace(/\s+/g, '_')}`;

            // If this pathway has interactions, propagate to ALL ancestors
            if (state.hasInteractions.get(pathwayId)) {
                const hier = hierarchyMap.get(pathwayId);
                const parentIds = hier?.parent_ids || [];

                // Recursively mark all ancestors as having interactions
                const visited = new Set();
                function markAncestors(currentId) {
                    if (visited.has(currentId)) return;
                    visited.add(currentId);

                    const h = hierarchyMap.get(currentId);
                    const parents = h?.parent_ids || [];

                    parents.forEach(parentId => {
                        state.hasInteractions.set(parentId, true);
                        markAncestors(parentId); // Recurse to grandparents
                    });
                }

                parentIds.forEach(parentId => {
                    state.hasInteractions.set(parentId, true);
                    markAncestors(parentId);
                });
            }
        });
    }

    /**
     * Compute hasInteractorsInSubtree for each pathway
     * A pathway has interactors in subtree if it or ANY of its descendants have interactors
     */
    function computeInteractorDescendantFlags() {
        const childrenMap = window.getPathwayChildrenMap?.() || new Map();
        const allPathways = window.getRawPathwayData?.() || [];
        const pathwayToInteractors = window.pathwayToInteractors || new Map();

        state.hasInteractorsInSubtree.clear();

        // Recursive function with memoization
        function hasInteractorsRecursive(pathwayId, visited = new Set()) {
            // Cycle detection
            if (visited.has(pathwayId)) return false;
            visited.add(pathwayId);

            // Check memo
            if (state.hasInteractorsInSubtree.has(pathwayId)) {
                return state.hasInteractorsInSubtree.get(pathwayId);
            }

            // Check 1: Direct interactors via hasInteractions (set by computeInteractionMetadata)
            if (state.hasInteractions.get(pathwayId)) {
                state.hasInteractorsInSubtree.set(pathwayId, true);
                return true;
            }

            // Check 2: Direct interactors via pathwayToInteractors map
            const directInteractors = pathwayToInteractors.get(pathwayId);
            if (directInteractors && directInteractors.size > 0) {
                state.hasInteractorsInSubtree.set(pathwayId, true);
                state.hasInteractions.set(pathwayId, true); // Also update hasInteractions
                return true;
            }

            // Check 3: Check children recursively
            // Try both childrenMap (from pathwayToChildren) and hierarchy child_ids
            let childIds = childrenMap.get(pathwayId);

            // Convert Set to Array if needed
            if (childIds instanceof Set) {
                childIds = [...childIds];
            } else if (!childIds) {
                // Fallback: check hierarchy for child_ids
                const hierarchy = window.getPathwayHierarchy?.();
                const hier = hierarchy?.get(pathwayId);
                childIds = hier?.child_ids || [];
            }

            for (const childId of childIds) {
                if (hasInteractorsRecursive(childId, new Set(visited))) {
                    state.hasInteractorsInSubtree.set(pathwayId, true);
                    return true;
                }
            }

            state.hasInteractorsInSubtree.set(pathwayId, false);
            return false;
        }

        // Compute for all pathways
        allPathways.forEach(pw => {
            const pathwayId = pw.id || `pathway_${(pw.name || 'unknown').replace(/\s+/g, '_')}`;
            hasInteractorsRecursive(pathwayId);
        });

    }

    function updateStatsBar() {
        let totals = { activates: 0, inhibits: 0, binds: 0, regulates: 0 };

        state.selectedPathways.forEach(pathwayId => {
            const counts = state.interactionsByPathway.get(pathwayId);
            if (counts) {
                totals.activates += counts.activates;
                totals.inhibits += counts.inhibits;
                totals.binds += counts.binds;
                totals.regulates += counts.regulates;
            }
        });

        const activatesEl = document.getElementById('pe-stat-activates');
        const inhibitsEl = document.getElementById('pe-stat-inhibits');
        const bindsEl = document.getElementById('pe-stat-binds');
        const regulatesEl = document.getElementById('pe-stat-regulates');

        if (activatesEl) activatesEl.textContent = totals.activates;
        if (inhibitsEl) inhibitsEl.textContent = totals.inhibits;
        if (bindsEl) bindsEl.textContent = totals.binds;
        if (regulatesEl) regulatesEl.textContent = totals.regulates;
    }

    // =========================================================================
    // TREE RENDERING
    // =========================================================================

    function renderTree() {
        if (!elements.tree) {
            cacheElements();
            if (!elements.tree) return;
        }

        const allPathways = window.getRawPathwayData?.() || [];
        const childrenMap = window.getPathwayChildrenMap?.() || new Map();

        // Get root pathways (level 0)
        // ✅ FIX 2d: Sort by hasInteractorsInSubtree first, then by interaction count
        const rootPathways = allPathways
            .filter(pw => (pw.hierarchy_level || 0) === 0)
            .sort((a, b) => {
                const aId = a.id || `pathway_${a.name.replace(/\s+/g, '_')}`;
                const bId = b.id || `pathway_${b.name.replace(/\s+/g, '_')}`;
                // 1. Has interactors in subtree first (strict check - undefined = no content)
                const aHasContent = state.hasInteractorsInSubtree.get(aId) === true;
                const bHasContent = state.hasInteractorsInSubtree.get(bId) === true;
                if (aHasContent !== bHasContent) return bHasContent ? 1 : -1;
                // 2. Then by interaction count
                return (b.interaction_count || 0) - (a.interaction_count || 0);
            });

        elements.tree.innerHTML = '';
        state.flattenedItems = [];

        if (rootPathways.length === 0) {
            elements.tree.innerHTML = `
                <div class="pe-empty-state">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                        <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2z"/>
                        <path d="M12 8v4M12 16h.01"/>
                    </svg>
                    <div class="pe-empty-state-text">No pathways available</div>
                </div>
            `;
            return;
        }

        rootPathways.forEach(pw => {
            const pathwayId = pw.id || `pathway_${(pw.name || 'unknown').replace(/\s+/g, '_')}`;
            const visited = new Set(); // Track visited pathways to prevent cycles
            const itemEl = buildTreeItem(pw, pathwayId, 0, childrenMap, allPathways, visited);
            elements.tree.appendChild(itemEl);
        });

    }

    function buildTreeItem(pw, pathwayId, level, childrenMap, allPathways, visited = new Set()) {
        // ✅ CYCLE DETECTION: Prevent infinite recursion from circular references
        if (visited.has(pathwayId)) {
            console.warn(`⚠️ Cycle detected in hierarchy data: ${pathwayId}`);
            console.warn(`   → This indicates a data integrity issue in PathwayParent table`);
            console.warn(`   → Run: python scripts/pathway_v2/fix_cycle.py --auto`);
            return document.createElement('div'); // Return empty element
        }
        
        // Add to visited set for this branch
        visited.add(pathwayId);
        
        const hierarchy = window.getPathwayHierarchy?.() || new Map();
        const hier = hierarchy.get(pathwayId);
        const childIds = hier?.child_ids || [];
        const hasChildren = childIds.length > 0;
        const counts = state.interactionsByPathway.get(pathwayId) || {};
        const hasInteractions = state.hasInteractions.get(pathwayId);
        const hasInteractorsInSubtree = state.hasInteractorsInSubtree.get(pathwayId);
        const isSelected = state.selectedPathways.has(pathwayId);
        const isHidden = state.hiddenCards.has(pathwayId);
        const isExpanded = state.expandedBranches.has(pathwayId);
        const isGreyed = hasInteractorsInSubtree === false;  // Pathway leads nowhere with interactors

        // Create item element
        const item = document.createElement('div');
        item.className = `pe-item${isExpanded ? ' expanded' : ''}${hasChildren ? ' has-children' : ''}${isGreyed ? ' no-interactors' : ''}`;
        item.dataset.pathwayId = pathwayId;
        item.dataset.level = level;

        // Track for keyboard navigation
        state.flattenedItems.push({ pathwayId, level, element: item });

        // Create content
        const content = document.createElement('div');
        content.className = `pe-item-content${isSelected ? ' selected' : ''}${isHidden ? ' hidden-card' : ''}`;

        // Build indicators HTML
        let indicatorsHtml = '<div class="pe-indicators">';
        if (hasInteractions) {
            if (counts.activates > 0) {
                indicatorsHtml += `<span class="pe-indicator activates${counts.activates > 2 ? ' active' : ''}" title="${counts.activates} activating"></span>`;
            }
            if (counts.inhibits > 0) {
                indicatorsHtml += `<span class="pe-indicator inhibits${counts.inhibits > 2 ? ' active' : ''}" title="${counts.inhibits} inhibiting"></span>`;
            }
            if (counts.binds > 0) {
                indicatorsHtml += `<span class="pe-indicator binds${counts.binds > 2 ? ' active' : ''}" title="${counts.binds} binding"></span>`;
            }
            if (counts.regulates > 0) {
                indicatorsHtml += `<span class="pe-indicator regulates${counts.regulates > 2 ? ' active' : ''}" title="${counts.regulates} regulating"></span>`;
            }
        }
        indicatorsHtml += '</div>';

        // BUG C FIX: Build content indicator with better visual distinction
        let indicatorClass = 'pe-content-indicator';
        let indicatorTitle = 'No interactions';
        let indicatorRadius = 3;

        if (hasInteractions) {
            indicatorClass += ' has-content has-direct';
            indicatorTitle = `${counts.total || 0} direct interactions`;
            indicatorRadius = 5;
        } else if (hasInteractorsInSubtree === true) {
            indicatorClass += ' has-subtree-content';
            indicatorTitle = 'Contains sub-pathways with interactions';
            indicatorRadius = 4;
        }

        content.innerHTML = `
            <button class="pe-expander${hasChildren ? '' : ' no-children'}">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M9 5l7 7-7 7"/>
                </svg>
            </button>

            <span class="${indicatorClass}" title="${indicatorTitle}">
                <svg viewBox="0 0 16 16" fill="currentColor">
                    <circle cx="8" cy="8" r="${indicatorRadius}"/>
                </svg>
            </span>

            <div class="pe-checkbox-wrapper">
                <input type="checkbox"
                       class="pe-checkbox"
                       id="pe-cb-${_cvHtml(pathwayId)}"
                       ${isSelected ? 'checked' : ''}>
                <div class="pe-checkbox-visual">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3">
                        <path d="M5 12l4 4 10-10"/>
                    </svg>
                </div>
            </div>

            <div class="pe-label-group">
                <span class="pe-label" title="${_cvHtml(pw.name)}">${highlightSearchTerm(pw.name)}</span>
                <span class="pe-level-badge">L${level}</span>
            </div>

            ${indicatorsHtml}

            <button class="pe-visibility-btn${isHidden ? ' is-hidden' : ''}" title="${isHidden ? 'Show in Card View' : 'Hide from Card View'}">
                <svg class="pe-eye-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                    ${isHidden ?
                        '<path d="M17.94 17.94A10.07 10.07 0 0112 20c-7 0-11-8-11-8a18.45 18.45 0 015.06-5.94M9.9 4.24A9.12 9.12 0 0112 4c7 0 11 8 11 8a18.5 18.5 0 01-2.16 3.19m-6.72-1.07a3 3 0 11-4.24-4.24M1 1l22 22"/>' :
                        '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>'
                    }
                </svg>
            </button>
        `;

        // Add event listeners
        const expander = content.querySelector('.pe-expander');
        expander.addEventListener('click', (e) => {
            e.stopPropagation();
            toggleBranch(pathwayId);
        });

        const checkbox = content.querySelector('.pe-checkbox');
        checkbox.addEventListener('change', (e) => {
            e.stopPropagation();
            toggleSelection(pathwayId, e.target.checked);
        });

        const labelGroup = content.querySelector('.pe-label-group');
        labelGroup.addEventListener('click', () => {
            toggleSelection(pathwayId, !state.selectedPathways.has(pathwayId));
            const cb = content.querySelector('.pe-checkbox');
            if (cb) cb.checked = state.selectedPathways.has(pathwayId);
        });

        const visibilityBtn = content.querySelector('.pe-visibility-btn');
        visibilityBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            toggleVisibility(pathwayId);
        });

        // Hover events for breadcrumb
        content.addEventListener('mouseenter', () => {
            state.hoveredPathway = pathwayId;
            state.breadcrumbPath = hier?.ancestry || [pw.name];
            updateBreadcrumb();
        });

        content.addEventListener('mouseleave', () => {
            state.hoveredPathway = null;
            state.breadcrumbPath = [];
            updateBreadcrumb();
        });

        item.appendChild(content);

        // Children container (NO depth limit!)
        if (hasChildren) {
            const childrenContainer = document.createElement('div');
            childrenContainer.className = `pe-children${isExpanded ? ' expanded' : ' collapsed'}`;

            // NEW FEATURE: Separate children into content-containing and empty groups
            const allChildren = childIds
                .map(childId => ({
                    id: childId,
                    pw: allPathways.find(p =>
                        (p.id || `pathway_${p.name.replace(/\s+/g, '_')}`) === childId
                    ),
                    hasContent: state.hasInteractorsInSubtree.get(childId) === true
                }))
                .filter(c => c.pw);

            // Content-containing children: sorted by interaction count
            const contentChildren = allChildren
                .filter(c => c.hasContent)
                .sort((a, b) => (b.pw.interaction_count || 0) - (a.pw.interaction_count || 0));

            // Empty children: sorted alphabetically
            const emptyChildren = allChildren
                .filter(c => !c.hasContent)
                .sort((a, b) => a.pw.name.localeCompare(b.pw.name));

            // Render content-containing children normally
            contentChildren.forEach(({ id: childId, pw: childPw }) => {
                const childVisited = new Set(visited);
                const childItem = buildTreeItem(childPw, childId, level + 1, childrenMap, allPathways, childVisited);
                childrenContainer.appendChild(childItem);
            });

            // Render empty children in collapsible group (if any)
            if (emptyChildren.length > 0) {
                const emptyGroup = document.createElement('div');
                emptyGroup.className = 'pe-empty-group collapsed';
                emptyGroup.dataset.level = level + 1;

                emptyGroup.innerHTML = `
                    <div class="pe-empty-group-header">
                        <svg class="pe-empty-group-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M9 5l7 7-7 7"/>
                        </svg>
                        <span class="pe-empty-group-label">L${level + 1} Empty Pathways</span>
                        <span class="pe-empty-group-count">${emptyChildren.length}</span>
                    </div>
                    <div class="pe-empty-group-children"></div>
                `;

                // Toggle handler for empty group
                const header = emptyGroup.querySelector('.pe-empty-group-header');
                header.addEventListener('click', (e) => {
                    e.stopPropagation();
                    emptyGroup.classList.toggle('collapsed');
                    emptyGroup.classList.toggle('expanded');
                });

                // Add empty children to the group
                const emptyContainer = emptyGroup.querySelector('.pe-empty-group-children');
                emptyChildren.forEach(({ id: childId, pw: childPw }) => {
                    const childVisited = new Set(visited);
                    const childItem = buildTreeItem(childPw, childId, level + 1, childrenMap, allPathways, childVisited);
                    emptyContainer.appendChild(childItem);
                });

                childrenContainer.appendChild(emptyGroup);
            }

            item.appendChild(childrenContainer);
        }

        return item;
    }

    function highlightSearchTerm(text) {
        const rawText = text == null ? '' : String(text);
        if (!state.searchQuery) return _cvHtml(rawText);
        const regex = new RegExp(`(${escapeRegex(state.searchQuery)})`, 'gi');
        return rawText.split(regex).map(part => {
            if (part.toLowerCase() === state.searchQuery) {
                return `<span class="search-match">${_cvHtml(part)}</span>`;
            }
            return _cvHtml(part);
        }).join('');
    }

    function escapeRegex(str) {
        return str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    }

    // =========================================================================
    // INTERACTION HANDLERS
    // =========================================================================

    function toggleBranch(pathwayId) {
        // Use PathwayState for bidirectional sync with card view
        PathwayState.toggleExpansion(pathwayId, 'explorer', 'toggle');

        // Also update local DOM immediately for responsiveness
        // (PathwayState observer will handle this too, but this is faster)
        const isExpanded = state.expandedBranches.has(pathwayId);
        const item = document.querySelector(`.pe-item[data-pathway-id="${pathwayId}"]`);
        if (item) {
            const children = item.querySelector('.pe-children');
            if (children) {
                if (isExpanded) {
                    item.classList.remove('expanded');
                    children.classList.remove('expanded');
                    children.classList.add('collapsed');
                } else {
                    item.classList.add('expanded');
                    children.classList.remove('collapsed');
                    children.classList.add('expanded');
                }
            }
        }
    }

    function updateSelectionUI(pathwayId, isSelected) {
        const item = document.querySelector(`.pe-item[data-pathway-id="${pathwayId}"]`);
        if (!item) return;

        const content = item.querySelector('.pe-item-content');
        const checkbox = item.querySelector('.pe-checkbox');

        if (content) content.classList.toggle('selected', isSelected);
        if (checkbox) checkbox.checked = isSelected;
    }

    function toggleSelection(pathwayId, checked) {
        // Delegate to PathwayState (which handles cascading)
        if (typeof PathwayState !== 'undefined') {
            const isCurrentlySelected = PathwayState.isSelected(pathwayId);
            if (checked !== isCurrentlySelected) {
                PathwayState.toggleSelection(pathwayId, 'explorer');
            }
        } else {
            // Fallback for when PathwayState not available
            if (checked) {
                state.selectedPathways.add(pathwayId);
            } else {
                state.selectedPathways.delete(pathwayId);
            }
            updateSelectionUI(pathwayId, checked);
        }

        updateStatsBar();
        syncToCardView();
    }

    function toggleVisibility(pathwayId) {
        const isHidden = state.hiddenCards.has(pathwayId);

        if (isHidden) {
            state.hiddenCards.delete(pathwayId);
        } else {
            state.hiddenCards.add(pathwayId);
        }

        // ✅ CRITICAL FIX: Sync with PathwayState
        if (typeof PathwayState !== 'undefined') {
            const isInPathwayState = PathwayState.isHidden(pathwayId);
            // Only toggle if states differ
            if (!isHidden && !isInPathwayState) {
                PathwayState.toggleVisibility(pathwayId, 'explorer');
            } else if (isHidden && isInPathwayState) {
                PathwayState.toggleVisibility(pathwayId, 'explorer');
            }
        }

        // Update visual
        const item = document.querySelector(`.pe-item[data-pathway-id="${pathwayId}"]`);
        if (item) {
            const content = item.querySelector('.pe-item-content');
            const btn = item.querySelector('.pe-visibility-btn');

            if (content) content.classList.toggle('hidden-card', !isHidden);
            if (btn) {
                btn.classList.toggle('is-hidden', !isHidden);
                const svg = btn.querySelector('svg');
                if (svg) {
                    svg.innerHTML = isHidden ?
                        '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>' :
                        '<path d="M17.94 17.94A10.07 10.07 0 0112 20c-7 0-11-8-11-8a18.45 18.45 0 015.06-5.94M9.9 4.24A9.12 9.12 0 0112 4c7 0 11 8 11 8a18.5 18.5 0 01-2.16 3.19m-6.72-1.07a3 3 0 11-4.24-4.24M1 1l22 22"/>';
                }
            }
        }

        syncToCardView();
    }

    // =========================================================================
    // BREADCRUMB
    // =========================================================================

    function updateBreadcrumb() {
        if (!elements.breadcrumb) return;

        if (state.breadcrumbPath.length === 0) {
            elements.breadcrumb.innerHTML = '<span class="pe-breadcrumb-root">Navigate pathways below</span>';
            return;
        }

        let html = '';
        state.breadcrumbPath.forEach((name, idx) => {
            const isLast = idx === state.breadcrumbPath.length - 1;
            html += `<span class="pe-breadcrumb-item${isLast ? ' active' : ''}">${_cvHtml(name)}</span>`;
            if (!isLast) {
                html += '<span class="pe-breadcrumb-divider">›</span>';
            }
        });

        elements.breadcrumb.innerHTML = html;
    }

    // =========================================================================
    // SEARCH
    // =========================================================================

    function handleSearch(query) {
        state.searchQuery = query.trim().toLowerCase();

        const items = document.querySelectorAll('.pe-item');
        const allPathways = window.getRawPathwayData?.() || [];
        const hierarchyMap = window.getPathwayHierarchy?.() || new Map();
        let matchCount = 0;

        // First pass: Find all matching items and their ancestors
        const matchingIds = new Set();
        const ancestorsToShow = new Set();

        items.forEach(item => {
            const pathwayId = item.dataset.pathwayId;
            const pw = allPathways.find(p =>
                (p.id || `pathway_${p.name.replace(/\s+/g, '_')}`) === pathwayId
            );

            const name = pw?.name?.toLowerCase() || '';
            const matches = state.searchQuery === '' || name.includes(state.searchQuery);

            if (matches && state.searchQuery) {
                matchingIds.add(pathwayId);
                matchCount++;

                // Collect all ancestors of this match
                const collectAncestors = (id, visited = new Set()) => {
                    if (visited.has(id)) return;
                    visited.add(id);
                    const hier = hierarchyMap.get(id);
                    if (hier?.parent_ids) {
                        hier.parent_ids.forEach(parentId => {
                            ancestorsToShow.add(parentId);
                            collectAncestors(parentId, visited);
                        });
                    }
                };
                collectAncestors(pathwayId);
            }
        });

        // Second pass: Show/hide items and update highlights
        items.forEach(item => {
            const pathwayId = item.dataset.pathwayId;
            const pw = allPathways.find(p =>
                (p.id || `pathway_${p.name.replace(/\s+/g, '_')}`) === pathwayId
            );

            const isMatch = matchingIds.has(pathwayId);
            const isAncestor = ancestorsToShow.has(pathwayId);
            const shouldShow = state.searchQuery === '' || isMatch || isAncestor;

            item.style.display = shouldShow ? '' : 'none';

            // Expand ancestors to show matches
            if (isAncestor && state.searchQuery) {
                item.classList.add('expanded');
                const children = item.querySelector('.pe-children');
                if (children) {
                    children.classList.remove('collapsed');
                    children.classList.add('expanded');
                }
                state.expandedBranches.add(pathwayId);
            }

            // Highlight matching text
            const label = item.querySelector('.pe-label');
            if (label && pw) {
                label.innerHTML = highlightSearchTerm(pw.name);
            }

            // Add visual distinction for direct matches vs ancestors
            const content = item.querySelector('.pe-item-content');
            if (content) {
                content.classList.toggle('search-match', isMatch && state.searchQuery !== '');
                content.classList.toggle('search-ancestor', isAncestor && !isMatch && state.searchQuery !== '');
            }
        });

        // Update results count
        const resultsEl = document.getElementById('pe-search-results');
        if (resultsEl) {
            resultsEl.textContent = state.searchQuery ?
                `${matchCount} match${matchCount !== 1 ? 'es' : ''}` : '';
        }
    }

    function expandParentBranches(pathwayId, visited = new Set()) {
        // ✅ CYCLE DETECTION: Prevent infinite recursion from circular references
        if (visited.has(pathwayId)) {
            console.warn(`⚠️ Cycle during search expansion: ${pathwayId}`);
            console.warn(`   → Run: python scripts/pathway_v2/verify_pipeline.py --auto-fix`);
            return;
        }
        visited.add(pathwayId);

        const hierarchy = window.getPathwayHierarchy?.() || new Map();
        const hier = hierarchy.get(pathwayId);

        if (hier?.parent_ids) {
            hier.parent_ids.forEach(parentId => {
                if (!state.expandedBranches.has(parentId)) {
                    state.expandedBranches.add(parentId);

                    const parentItem = document.querySelector(`.pe-item[data-pathway-id="${parentId}"]`);
                    if (parentItem) {
                        parentItem.classList.add('expanded');
                        const children = parentItem.querySelector('.pe-children');
                        if (children) {
                            children.classList.remove('collapsed');
                            children.classList.add('expanded');
                        }
                    }
                }
                expandParentBranches(parentId, visited); // Pass visited set
            });
        }
    }

    function expandAncestors(pathwayId) {
        const hierarchy = window.getPathwayHierarchy?.() || new Map();
        const visited = new Set();

        function expandRecursive(id) {
            if (visited.has(id)) return;
            visited.add(id);

            const hier = hierarchy.get(id);
            const parentIds = hier?.parent_ids || [];

            parentIds.forEach(parentId => {
                // Expand in explorer tree
                if (!state.expandedBranches.has(parentId)) {
                    state.expandedBranches.add(parentId);
                }

                // Expand in card view
                if (typeof cvState !== 'undefined' && !cvState.expandedNodes.has(parentId)) {
                    cvState.expandedNodes.add(parentId);
                }

                // Recurse to grandparents
                expandRecursive(parentId);
            });
        }

        expandRecursive(pathwayId);

        // Re-render both views
        renderTree();
        if (typeof renderCardView === 'function') {
            renderCardView();
        }
    }

    function clearSearch() {
        if (elements.searchInput) {
            elements.searchInput.value = '';
        }
        handleSearch('');
    }

    // =========================================================================
    // KEYBOARD NAVIGATION
    // =========================================================================

    function handleKeyNav(event) {
        const { key } = event;

        switch (key) {
            case 'ArrowDown':
                event.preventDefault();
                navigateItems(1);
                break;
            case 'ArrowUp':
                event.preventDefault();
                navigateItems(-1);
                break;
            case 'ArrowRight':
                event.preventDefault();
                expandFocusedItem();
                break;
            case 'ArrowLeft':
                event.preventDefault();
                collapseFocusedItem();
                break;
            case 'Enter':
            case ' ':
                event.preventDefault();
                toggleFocusedItem();
                break;
            case 'Escape':
                clearSearch();
                break;
        }
    }

    function navigateItems(direction) {
        const visibleItems = state.flattenedItems.filter(item =>
            item.element.style.display !== 'none'
        );

        if (visibleItems.length === 0) return;

        document.querySelector('.pe-item.keyboard-focus')?.classList.remove('keyboard-focus');

        state.keyboardFocusIndex += direction;

        if (state.keyboardFocusIndex < 0) state.keyboardFocusIndex = visibleItems.length - 1;
        if (state.keyboardFocusIndex >= visibleItems.length) state.keyboardFocusIndex = 0;

        const focused = visibleItems[state.keyboardFocusIndex];
        if (focused) {
            focused.element.classList.add('keyboard-focus');
            focused.element.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }
    }

    function expandFocusedItem() {
        const focused = document.querySelector('.pe-item.keyboard-focus');
        if (focused) {
            const pathwayId = focused.dataset.pathwayId;
            if (!state.expandedBranches.has(pathwayId)) {
                toggleBranch(pathwayId);
            }
        }
    }

    function collapseFocusedItem() {
        const focused = document.querySelector('.pe-item.keyboard-focus');
        if (focused) {
            const pathwayId = focused.dataset.pathwayId;
            if (state.expandedBranches.has(pathwayId)) {
                toggleBranch(pathwayId);
            }
        }
    }

    function toggleFocusedItem() {
        const focused = document.querySelector('.pe-item.keyboard-focus');
        if (focused) {
            const pathwayId = focused.dataset.pathwayId;
            const checkbox = focused.querySelector('.pe-checkbox');
            if (checkbox) {
                checkbox.checked = !checkbox.checked;
                toggleSelection(pathwayId, checkbox.checked);
            }
        }
    }

    // =========================================================================
    // BULK ACTIONS
    // =========================================================================

    function selectAllVisible() {
        const items = document.querySelectorAll('.pe-item:not([style*="display: none"])');
        const pathwayIds = [];
        items.forEach(item => {
            const pathwayId = item.dataset.pathwayId;
            state.selectedPathways.add(pathwayId);
            pathwayIds.push(pathwayId);

            const checkbox = item.querySelector('.pe-checkbox');
            if (checkbox) checkbox.checked = true;

            const content = item.querySelector('.pe-item-content');
            if (content) content.classList.add('selected');
        });

        // ✅ Sync with PathwayState
        if (typeof PathwayState !== 'undefined') {
            PathwayState.selectAll(pathwayIds);
        }

        updateStatsBar();
        syncToCardView();
    }

    function clearAllSelections() {
        state.selectedPathways.clear();

        document.querySelectorAll('.pe-checkbox').forEach(cb => cb.checked = false);
        document.querySelectorAll('.pe-item-content.selected').forEach(el => el.classList.remove('selected'));

        // ✅ Sync with PathwayState
        if (typeof PathwayState !== 'undefined') {
            PathwayState.clearSelections();
        }

        updateStatsBar();
        syncToCardView();
    }

    function showAllCards() {
        state.hiddenCards.clear();

        document.querySelectorAll('.pe-item-content.hidden-card').forEach(el => el.classList.remove('hidden-card'));
        document.querySelectorAll('.pe-visibility-btn.is-hidden').forEach(btn => {
            btn.classList.remove('is-hidden');
            const svg = btn.querySelector('svg');
            if (svg) {
                svg.innerHTML = '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>';
            }
        });

        // ✅ Sync with PathwayState
        if (typeof PathwayState !== 'undefined') {
            PathwayState.showAll();
        }

        syncToCardView();
    }

    // =========================================================================
    // EXPLORER COLLAPSE/EXPAND
    // =========================================================================

    function toggleExplorer() {
        state.isCollapsed = !state.isCollapsed;

        if (elements.explorer) {
            elements.explorer.style.display = state.isCollapsed ? 'none' : 'flex';
        }
        if (elements.collapsedTab) {
            elements.collapsedTab.style.display = state.isCollapsed ? 'flex' : 'none';
        }
        if (elements.svgContainer) {
            elements.svgContainer.style.left = state.isCollapsed ? '50px' : '400px';
        }
    }

    // =========================================================================
    // CARD VIEW SYNC
    // =========================================================================

    function syncToCardView() {
        // Update cvState from PathwayState (or local state for backward compatibility)
        cvState.selectedRoots.clear();
        cvState.hiddenCards.clear();

        // ✅ FIXED: Accept pathways at ANY level (not just L0!)
        // If PathwayState exists, use it; otherwise fall back to local state
        const selectedSet = typeof PathwayState !== 'undefined'
            ? PathwayState.getSelectedPathways()
            : state.selectedPathways;
        const hiddenSet = typeof PathwayState !== 'undefined'
            ? PathwayState.getHiddenPathways()
            : state.hiddenCards;

        // Add ALL selected pathways (no level filter!)
        selectedSet.forEach(id => {
            cvState.selectedRoots.add(id);
        });

        hiddenSet.forEach(id => {
            cvState.hiddenCards.add(id);
        });

        // Trigger re-render
        if (typeof renderCardView === 'function') {
            renderCardView();
        }

        // Dispatch event
        window.dispatchEvent(new CustomEvent('pathwayExplorerUpdated', {
            detail: {
                selectedPathways: [...selectedSet],
                hiddenCards: [...hiddenSet]
            }
        }));
    }

    // =========================================================================
    // PUBLIC API
    // =========================================================================

    return {
        init,
        toggleBranch,
        toggleSelection,
        toggleVisibility,
        handleSearch,
        handleKeyNav,
        clearSearch,
        selectAllVisible,
        clearAllSelections,
        showAllCards,
        toggleExplorer,
        renderTree,

        // State access
        getState: () => ({ ...state }),
        getSelectedPathways: () => new Set(state.selectedPathways),
        getHiddenCards: () => new Set(state.hiddenCards),
        getHasInteractorsInSubtree: () => new Map(state.hasInteractorsInSubtree)
    };
})();

// Global accessor for hasInteractorsInSubtree (used by card view filtering)
window.getHasInteractorsInSubtree = () => PathwayExplorer.getHasInteractorsInSubtree();

// Global reference
window.PathwayExplorer = PathwayExplorer;

// Initialize when data is ready
document.addEventListener('DOMContentLoaded', () => {
    // Delay to ensure data is loaded
    setTimeout(() => {
        PathwayExplorer.init();
    }, 800);
});

// Also initialize when card view is shown
window.addEventListener('cardViewShown', () => {
    PathwayExplorer.init();
});


// ============================================================================
// CARD VIEW MODE SWITCHING
// ============================================================================

/**
 * Switch between Pathway and Interactor modes in Card View.
 * Toggles sidebars, updates button states, and re-renders.
 */
function setCardViewMode(mode) {
    if (mode !== 'pathway' && mode !== 'interactor') return;
    cvState.cardViewMode = mode;

    // Comprehensive SVG cleanup on mode switch to prevent stale D3 data bindings
    if (cvG) {
        cvG.selectAll('*').remove();
        cvG.attr('transform', 'translate(0,0)');
    }
    if (cvSvg) {
        cvSvg.select('defs').selectAll('*').remove();
    }

    // Toggle sidebars
    const peExplorer = document.getElementById('pathway-explorer-v2');
    const intExplorer = document.getElementById('interactor-explorer');
    const peCollapsed = document.getElementById('pe-collapsed-tab');
    const intCollapsed = document.getElementById('int-collapsed-tab');

    if (peExplorer) peExplorer.style.display = mode === 'pathway' ? 'flex' : 'none';
    if (intExplorer) intExplorer.style.display = mode === 'interactor' ? 'flex' : 'none';
    if (peCollapsed) peCollapsed.style.display = (mode === 'pathway' && cvState.sidebarCollapsed) ? 'flex' : 'none';
    if (intCollapsed) intCollapsed.style.display = 'none'; // Will be shown by toggleExplorer if collapsed

    // Toggle mode buttons
    const pathwayBtn = document.getElementById('card-mode-pathway');
    const interactorBtn = document.getElementById('card-mode-interactor');
    if (pathwayBtn) pathwayBtn.classList.toggle('active', mode === 'pathway');
    if (interactorBtn) interactorBtn.classList.toggle('active', mode === 'interactor');

    // Initialize interactor explorer on first switch
    if (mode === 'interactor') {
        InteractorExplorer.init();
    }

    // Re-render
    renderCardView();
}

window.setCardViewMode = setCardViewMode;


// ============================================================================
// INTERACTOR EXPLORER MODULE - Sidebar for Interactors Mode
// ============================================================================

const InteractorExplorer = (function() {
    'use strict';

    let initialized = false;
    let isCollapsed = false;

    // --- Initialization ---
    function init() {
        const interactions = getInteractorModeInteractions();
        if (!SNAP || interactions.length === 0) return;

        const mainId = SNAP.main || 'Unknown';
        const queryInteractions = interactions.filter(inter => !isDatabasedInteraction(inter));
        const roles = (typeof getProteinsByRole === 'function')
            ? getProteinsByRole(queryInteractions, mainId)
            : { upstream: new Set(), downstream: new Set(), bidirectional: new Set() };

        // Update direction counts
        updateCount('upstream', roles.upstream.size);
        updateCount('downstream', roles.downstream.size);
        updateCount('bidirectional', roles.bidirectional.size);

        // Update stats bar counts
        const arrowCounts = { activates: 0, inhibits: 0, binds: 0, regulates: 0 };
        interactions.forEach(inter => {
            const arrow = inter.arrow || 'binds';
            if (arrowCounts[arrow] !== undefined) arrowCounts[arrow]++;
        });
        ['activates', 'inhibits', 'binds', 'regulates'].forEach(type => {
            const el = document.getElementById(`int-stat-${type}`);
            if (el) el.textContent = arrowCounts[type];
        });

        // Auto-expand all groups on first init
        if (!initialized) {
            cvState.interactorExpandedGroups = new Set([
                'upstream', 'downstream', 'bidirectional',
                'upstream_activates', 'upstream_inhibits', 'upstream_binds', 'upstream_regulates',
                'downstream_activates', 'downstream_inhibits', 'downstream_binds', 'downstream_regulates',
                'bidirectional_activates', 'bidirectional_inhibits', 'bidirectional_binds', 'bidirectional_regulates'
            ]);
        }

        renderInteractorList(roles, interactions);
        initialized = true;
    }

    function updateCount(dir, count) {
        const el = document.getElementById(`int-count-${dir}`);
        if (el) el.textContent = count;
    }

    // --- Direction filter toggle ---
    function toggleDirection(dir) {
        const filter = cvState.interactorDirectionFilter;
        if (filter.has(dir)) {
            filter.delete(dir);
        } else {
            filter.add(dir);
        }

        // Update button state
        const btns = document.querySelectorAll(`.int-filter-btn[data-dir="${dir}"]`);
        btns.forEach(btn => btn.classList.toggle('active', filter.has(dir)));

        renderCardView();
    }

    // --- Arrow type filter toggle ---
    function toggleArrowType(type) {
        const filter = cvState.interactorArrowFilter;
        if (filter.has(type)) {
            filter.delete(type);
        } else {
            filter.add(type);
        }

        // Update button state
        const btns = document.querySelectorAll(`.int-arrow-btn.${type}`);
        btns.forEach(btn => btn.classList.toggle('active', filter.has(type)));

        renderCardView();
    }

    // --- Search ---
    function handleSearch(query) {
        cvState.interactorSearchQuery = query.trim();
        renderCardView();
    }

    // --- Render interactor list in sidebar ---
    function renderInteractorList(roles, interactions = getInteractorModeInteractions()) {
        const container = document.getElementById('int-list');
        if (!container) return;

        if (!roles) {
            const mainId = SNAP.main || 'Unknown';
            const queryInteractions = interactions.filter(inter => !isDatabasedInteraction(inter));
            roles = (typeof getProteinsByRole === 'function')
                ? getProteinsByRole(queryInteractions, mainId)
                : { upstream: new Set(), downstream: new Set(), bidirectional: new Set() };
        }

        const directionConfig = [
            { key: 'upstream', label: 'Upstream', icon: '\u2191', color: '#3b82f6', set: roles.upstream },
            { key: 'downstream', label: 'Downstream', icon: '\u2193', color: '#22c55e', set: roles.downstream },
            // S1b: no more ↔. Relabel the residual bucket Undirected with →.
            { key: 'bidirectional', label: 'Undirected', icon: '\u2192', color: '#f59e0b', set: roles.bidirectional }
        ];

        const arrowColors = {
            activates: '#10b981',
            inhibits: '#ef4444',
            binds: '#a78bfa',
            regulates: '#f59e0b'
        };

        let html = '';

        directionConfig.forEach(dir => {
            if (dir.set.size === 0) return;

            html += `<div class="int-list-group">
                <div class="int-list-group-header" style="border-left: 3px solid ${dir.color};">
                    <span class="int-list-group-icon">${dir.icon}</span>
                    <span class="int-list-group-label">${dir.label}</span>
                    <span class="int-list-group-count">${dir.set.size}</span>
                </div>`;

            // Group proteins by arrow type
            const byArrow = { activates: [], inhibits: [], binds: [], regulates: [] };
            dir.set.forEach(pid => {
                const inter = interactions.find(i =>
                    (i.source === SNAP.main && i.target === pid) ||
                    (i.source === pid && i.target === SNAP.main)
                );
                const arrow = inter?.arrow || 'binds';
                const bucket = byArrow[arrow] ? arrow : 'binds';
                byArrow[bucket].push(pid);
            });

            Object.entries(byArrow).forEach(([arrow, proteins]) => {
                if (proteins.length === 0) return;
                const color = arrowColors[arrow] || '#94a3b8';

                proteins.sort().forEach(pid => {
                    html += `<div class="int-list-item" data-protein="${_cvHtml(pid)}" title="${_cvHtml(pid)}">
                        <span class="int-list-dot" style="background:${color};"></span>
                        <span class="int-list-name">${_cvHtml(pid)}</span>
                        <span class="int-list-arrow">${_cvHtml(arrow)}</span>
                    </div>`;
                });
            });

            html += `</div>`;
        });

        container.innerHTML = html || '<div style="padding: 20px; text-align: center; color: #64748b; font-size: 13px;">No interactors found</div>';
        container.querySelectorAll('.int-list-item[data-protein]').forEach(item => {
            item.addEventListener('click', () => InteractorExplorer.highlightProtein(item.dataset.protein));
        });
    }

    // --- Highlight a protein in the card view ---
    function highlightProtein(proteinId) {
        // Find and flash the matching card in the SVG
        if (cvG) {
            cvG.selectAll('.cv-node')
                .each(function(d) {
                    if (d.data.id === proteinId) {
                        const node = d3.select(this);
                        const rect = node.select('rect');
                        const origStroke = rect.style('stroke');
                        rect.transition().duration(200)
                            .style('stroke', '#ffffff')
                            .style('stroke-width', '3px')
                          .transition().duration(800)
                            .style('stroke', origStroke)
                            .style('stroke-width', '1px');
                    }
                });
        }
    }

    // --- Toggle sidebar collapse ---
    function toggleExplorer() {
        const explorer = document.getElementById('interactor-explorer');
        const collapsedTab = document.getElementById('int-collapsed-tab');
        const svgContainer = document.getElementById('card-svg-container');

        isCollapsed = !isCollapsed;

        if (isCollapsed) {
            if (explorer) explorer.style.display = 'none';
            if (collapsedTab) collapsedTab.style.display = 'flex';
            if (svgContainer) svgContainer.style.left = '48px';
        } else {
            if (explorer) explorer.style.display = 'flex';
            if (collapsedTab) collapsedTab.style.display = 'none';
            if (svgContainer) svgContainer.style.left = '400px';
        }
    }

    return {
        init,
        toggleDirection,
        toggleArrowType,
        handleSearch,
        highlightProtein,
        toggleExplorer,
        renderInteractorList
    };
})();

window.InteractorExplorer = InteractorExplorer;
