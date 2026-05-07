/* ===== modal.js — Modal rendering system (extracted from visualizer.js) ===== */


let modalOpen = false;
let _modalLastFocused = null;

const _MODAL_FOCUSABLE_SELECTOR = [
  'a[href]',
  'area[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])'
].join(',');

// Placeholder-text catalog — module scope so every renderer (per-function
// section, interaction-level SUMMARY, cascade/specific-effect filters)
// consults the SAME list. Any claim field whose text contains one of
// these fragments is a pipeline stub; we hide it rather than render
// "Discovered via chain resolution" five times in a single claim card.
const _PLACEHOLDER_SNIPPETS = [
  'not fully characterized',
  'not specified',
  'discovered via chain resolution',
  'function data not generated',
  'data not generated',
  'uncharacterized interaction',
  'no mechanism documented',
];
function _isPlaceholder(s) {
  if (!s) return true;
  const lower = String(s).toLowerCase();
  return _PLACEHOLDER_SNIPPETS.some(frag => lower.includes(frag));
}

/**
 * Resolve a CSS custom property on :root, with a hex fallback.
 * Lazy + cached so reading the full badge palette isn't free-form
 * getComputedStyle churn on every template literal.
 */
let _modalCssVarCache = null;
function _cssVar(name, fallback) {
  if (!_modalCssVarCache) {
    _modalCssVarCache = getComputedStyle(document.documentElement);
  }
  const v = _modalCssVarCache.getPropertyValue(name);
  return (v && v.trim()) || fallback;
}

/**
 * Return true if `nodeId` appears anywhere in this interaction's chain.
 *
 * Prefers `chain_context.full_chain` (authoritative, query-position-
 * agnostic) and falls back to the legacy `mediator_chain` array for older
 * rows. Matches case-insensitively so symbol casing drift doesn't break
 * the check.
 *
 * @param {Object} interactionData - Either an Interaction row (via
 *   `l.data` in the graph links) or a SNAP.interactions entry. Must
 *   expose `chain_context.full_chain` or `mediator_chain`.
 * @param {string} nodeId - The protein symbol to look up.
 * @returns {boolean}
 */
function chainIncludesNode(interactionData, nodeId) {
  if (!interactionData || !nodeId) return false;
  const target = String(nodeId).toUpperCase();
  const ctx = interactionData.chain_context || null;
  if (ctx && Array.isArray(ctx.full_chain)) {
    if (ctx.full_chain.some((p) => (p || '').toUpperCase() === target)) {
      return true;
    }
  }
  const legacy = interactionData.mediator_chain || [];
  return legacy.some((p) => (p || '').toUpperCase() === target);
}

function getDisplayHopIndex(L) {
  const value = L && (L.hop_index ?? L._chain_position);
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function buildChainNavClickedNode(target, hopLink) {
  const previousNode = _lastModalArgs?.clickedNode || {};
  const previousCardContext = previousNode.cardContext || previousNode._cardContext || {};
  const hopIndex = getDisplayHopIndex(target);
  const chainProteins = Array.isArray(target.chain_members)
    ? target.chain_members.slice()
    : Array.isArray(previousCardContext._chainProteins)
      ? previousCardContext._chainProteins.slice()
      : null;
  const chainPosition = hopIndex != null ? hopIndex + 1 : (target._chain_position ?? previousCardContext._chainPosition ?? null);
  const pathwayContext = previousNode._pathwayContext || previousCardContext._pathwayContext || previousNode.pathwayContext || null;
  const pathwayId = previousNode.pathwayId || previousCardContext.pathwayId || pathwayContext?.id || null;
  const nextCardContext = {
    ...previousCardContext,
    id: target.target,
    label: target.target,
    originalId: target.target,
    pathwayId,
    pathwayContext,
    _pathwayContext: pathwayContext,
    _chainId: target.chain_id ?? previousCardContext._chainId ?? null,
    _chainPosition: chainPosition,
    _chainLength: chainProteins ? chainProteins.length : previousCardContext._chainLength ?? null,
    _chainProteins: chainProteins,
    relationshipText: `Hop: ${target.source || '-'} ${target.arrow || 'binds'} ${target.target || '-'}`,
    relationshipArrow: target.arrow || hopLink.arrow || previousCardContext.relationshipArrow || null,
    _inboundChainArrow: target.arrow || previousCardContext._inboundChainArrow || null,
  };

  return {
    ...previousNode,
    id: target.target,
    label: target.target,
    originalId: target.target,
    pathwayId,
    _pathwayContext: pathwayContext,
    cardContext: nextCardContext,
    _chainId: nextCardContext._chainId,
    _chainPosition: nextCardContext._chainPosition,
    _chainProteins: nextCardContext._chainProteins,
  };
}

function _getModalFocusableElements(modalEl) {
  if (!modalEl) return [];
  return Array.from(modalEl.querySelectorAll(_MODAL_FOCUSABLE_SELECTOR))
    .filter(el => {
      const style = window.getComputedStyle(el);
      return style.display !== 'none' && style.visibility !== 'hidden' && el.offsetParent !== null;
    });
}

function _focusModal(modalEl) {
  if (!modalEl) return;
  window.requestAnimationFrame(() => {
    const closeBtn = modalEl.querySelector('.close-btn');
    const firstFocusable = _getModalFocusableElements(modalEl)[0];
    const target = closeBtn || firstFocusable || modalEl.querySelector('.modal-content') || modalEl;
    if (target && typeof target.focus === 'function') {
      target.focus({ preventScroll: true });
    }
  });
}

function openModal(titleHTML, bodyHTML, accentColor) {
  const modalContent = document.querySelector('.modal-content');
  if (modalContent) {
    modalContent.style.borderLeftColor = accentColor || '';
  }
  const titleEl = document.getElementById('modalTitle');
  const bodyEl = document.getElementById('modalBody');
  const modalEl = document.getElementById('modal');
  if (!titleEl || !bodyEl || !modalEl) return;

  const activeElement = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  if (!modalOpen || !modalEl.contains(activeElement)) {
    _modalLastFocused = activeElement;
  }
  titleEl.innerHTML = titleHTML;
  bodyEl.innerHTML = bodyHTML;
  const scrollHost = modalEl.querySelector('.modal-body');
  if (scrollHost) scrollHost.scrollTop = 0;
  bodyEl.scrollTop = 0;
  modalEl.setAttribute('aria-hidden', 'false');
  modalEl.classList.add('active');
  document.body.classList.add('modal-open');
  modalOpen = true;
  document.removeEventListener('keydown', handleModalEscape);
  document.addEventListener('keydown', handleModalEscape);
  _focusModal(modalEl);
  // Event delegation handles expandable rows automatically - no setTimeout needed
}

function closeModal() {
  const el = document.getElementById('modal');
  const focusTarget = _modalLastFocused;
  const activeElement = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const focusIsInsideModal = !!(el && activeElement && el.contains(activeElement));
  if (focusIsInsideModal) {
    if (focusTarget && typeof focusTarget.focus === 'function' && document.contains(focusTarget)) {
      focusTarget.focus({ preventScroll: true });
    } else if (typeof activeElement.blur === 'function') {
      activeElement.blur();
    }
  }
  if (el) {
    el.classList.remove('active');
    el.setAttribute('aria-hidden', 'true');
  }
  document.body.classList.remove('modal-open');
  modalOpen = false;
  document.removeEventListener('keydown', handleModalEscape);
  if (!focusIsInsideModal && focusTarget && typeof focusTarget.focus === 'function' && document.contains(focusTarget)) {
    window.requestAnimationFrame(() => focusTarget.focus({ preventScroll: true }));
  }
  _modalLastFocused = null;
  // Clear modal-arg cache so the next open doesn't re-render a
  // stale pathway-filter state against a different node's links.
  _lastModalArgs = null;
}

function handleModalEscape(e) {
  if (e.key === 'Escape' && modalOpen) {
    closeModal();
    return;
  }

  if (e.key !== 'Tab' || !modalOpen) return;
  const modalEl = document.getElementById('modal');
  const focusable = _getModalFocusableElements(modalEl);
  if (focusable.length === 0) {
    e.preventDefault();
    modalEl?.querySelector('.modal-content')?.focus({ preventScroll: true });
    return;
  }

  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus({ preventScroll: true });
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus({ preventScroll: true });
  }
}

const _modalEl = document.getElementById('modal');
if (_modalEl) _modalEl.addEventListener('click', (e) => {
  if (e.target.id === 'modal') closeModal();
});

// Event delegation for modal expandable rows - handles all clicks via bubbling
// More robust than setTimeout-based listener attachment
const _modalBody = document.getElementById('modalBody');
if (_modalBody) _modalBody.addEventListener('click', (e) => {
  // Handle data-action buttons (query/expand/collapse) via delegation
  const actionBtn = e.target.closest('[data-action]');
  if (actionBtn) {
    const action = actionBtn.dataset.action;
    const protein = actionBtn.dataset.protein;
    if (action === 'query') handleQueryFromModal(protein);
    else if (action === 'expand') handleExpandFromModal(protein);
    else if (action === 'collapse') handleCollapseFromModal(protein);
    else if (action === 'switch-pathway') switchToPathway(actionBtn.dataset.pathwayId, actionBtn.dataset.currentPathwayId);
    else if (action === 'toggle-pathway-filter' && _lastModalArgs) {
      _lastModalArgs.options.showAll = !_lastModalArgs.options.showAll;
      showAggregatedInteractionsModal(
        _lastModalArgs.nodeLinks,
        _lastModalArgs.clickedNode,
        _lastModalArgs.options
      );
    }
    return;
  }

  // L5.4 — chain navigation. Clicking a chain protein chip or prev/next
  // button re-scopes the modal to the corresponding hop within the same
  // chain. Falls through silently if the chain has no other hops.
  //
  // Multi-chain (#12): a hop participating in N chains shows N banners,
  // each with its own ``data-chain-id``. We scope the chainHops search
  // to interactions whose ``chain_id`` matches OR whose ``chain_ids``
  // array contains the clicked banner's chain_id, so navigation stays
  // within the chosen chain even when the same hop appears in multiple.
  const navBtn = e.target.closest('[data-chain-nav]');
  if (navBtn) {
    e.preventDefault();
    e.stopPropagation();
    const banner = navBtn.closest('.chain-context-banner');
    const chainId = banner?.dataset.chainId || '';
    const SNAPref = (typeof SNAP !== 'undefined' ? SNAP : window.SNAP) || {};
    const allInter = SNAPref.interactions || [];
    if (!allInter.length) return;
    const matchesChain = (i) => {
      if (!i || !i._is_chain_link) return false;
      if (!chainId) return true;
      if (String(i.chain_id ?? '') === chainId) return true;
      if (Array.isArray(i.chain_ids) && i.chain_ids.some(cid => String(cid) === chainId)) return true;
      return false;
    };
    const chainHops = allInter
      .filter(matchesChain)
      .sort((a, b) => (getDisplayHopIndex(a) ?? 0) - (getDisplayHopIndex(b) ?? 0));
    if (!chainHops.length) return;

    let target = null;
    const navAction = navBtn.dataset.chainNav;
    if (navAction === 'prev' || navAction === 'next') {
      const curHop = getDisplayHopIndex(_lastModalArgs?.nodeLinks?.[0]?.data) ?? 0;
      const desired = navAction === 'prev' ? curHop - 1 : curHop + 1;
      target = chainHops.find(h => getDisplayHopIndex(h) === desired);
    } else if (navBtn.dataset.protein) {
      // Clicking a chain protein chip — find the hop where this protein is
      // the *target* (preferred) or the *source* (fallback).
      const prot = navBtn.dataset.protein;
      target = chainHops.find(h => h.target === prot) || chainHops.find(h => h.source === prot);
    }
    if (!target) return;
    // Re-open modal scoped to the chosen hop. Wrap as a single-link list.
    const hopLink = {
      data: target,
      source: { id: target.source, originalId: target.source },
      target: { id: target.target, originalId: target.target },
      arrow: target.arrow,
      direction: target.direction,
    };
    const navClickedNode = buildChainNavClickedNode(target, hopLink);
    showAggregatedInteractionsModal(
      [hopLink],
      navClickedNode,
      { ...(_lastModalArgs?.options || {}), showAll: true }
    );
    return;
  }

  // Handle function expandable rows
  const funcHeader = e.target.closest('.function-row-header');
  if (funcHeader) {
    const row = funcHeader.closest('.function-expandable-row');
    if (row) {
      row.classList.toggle('expanded');
      // Sync aria-expanded for screen readers + keyboard a11y.
      const isExpanded = row.classList.contains('expanded');
      funcHeader.setAttribute('aria-expanded', isExpanded ? 'true' : 'false');
    }
    return;
  }

  // Handle interaction expandable rows
  const interactionHeader = e.target.closest('.interaction-row-header');
  if (interactionHeader) {
    const row = interactionHeader.closest('.interaction-expandable-row');
    const content = row?.querySelector('.interaction-expanded-content');
    const icon = row?.querySelector('.interaction-expand-icon');

    if (row && content) {
      const isExpanded = row.classList.contains('expanded');
      if (isExpanded) {
        // COLLAPSING: restore fixed maxHeight for smooth transition, then collapse
        if (content.style.maxHeight === 'none') {
          content.style.maxHeight = content.scrollHeight + 'px';
        }
        requestAnimationFrame(() => {
          content.style.maxHeight = '0';
          content.style.opacity = '0';
          content.style.overflow = 'hidden';
        });
        row.classList.remove('expanded');
        if (icon) icon.style.transform = 'rotate(0deg)';
        interactionHeader.setAttribute('aria-expanded', 'false');
      } else {
        // EXPANDING
        row.classList.add('expanded');
        interactionHeader.setAttribute('aria-expanded', 'true');
        content.style.maxHeight = content.scrollHeight + 'px';
        content.style.opacity = '1';
        if (icon) icon.style.transform = 'rotate(180deg)';
        // After transition, switch to max-height:none + overflow:visible
        // so child function/claim rows can expand without being clipped
        content.addEventListener('transitionend', (evt) => {
          if (evt.propertyName !== 'max-height') return;
          if (row.classList.contains('expanded')) {
            content.style.maxHeight = 'none';
            content.style.overflow = 'visible';
          }
        }, { once: true });
      }
    }
  }
});

/* Helper: Check if pathway context matches function pathway (exact or hierarchy)
 *
 * P3.3 leak-fix: this used to return true whenever ANY pathway in
 * `interactionPathways` matched the context — so a claim assigned to
 * pathway A would still appear under pathway B if a sibling claim on
 * the same parent interaction was assigned to B. That violated the
 * "one claim = one pathway" invariant by stretching membership across
 * sibling claims.
 *
 * The fourth `interactionPathways` argument is kept (callers still
 * pass it) but is now ignored for membership decisions. Sibling
 * pathway info is preserved in the data layer for diagnostic use
 * (e.g. "this interaction also has claims in X, Y" badges) but does
 * NOT prove this specific claim belongs in the current pathway.
 */
function isPathwayInContext(fnPathway, fnHierarchy, pathwayContext, interactionPathways = null) {
  if (!pathwayContext?.name) return false;
  // Normalize: lowercase + collapse underscores/dashes/extra spaces
  const _norm = s => s.toLowerCase().replace(/[_-]/g, ' ').replace(/\s+/g, ' ').trim();
  const contextNorm = _norm(pathwayContext.name);
  // 1. Claim's own pathway
  if (fnPathway && _norm(fnPathway) === contextNorm) return true;
  // 2. Claim's pathway hierarchy (ancestor pathways the claim's own
  // pathway descends from — this is real claim-level membership).
  if (fnHierarchy && Array.isArray(fnHierarchy) && fnHierarchy.some(h => _norm(h) === contextNorm)) {
    return true;
  }
  // 3. (DELETED P3.3) — sibling-claim pathway leak. See header comment.
  return false;
}

function getInteractionLocus(interaction) {
  const L = interaction || {};
  const explicit = (L.locus || '').toString().toLowerCase();
  if (explicit === 'net_effect_claim' || explicit === 'chain_hop_claim' || explicit === 'direct_claim') {
    return explicit;
  }
  const functionContext = (L.function_context || L.data?.function_context || '').toString().toLowerCase();
  if (functionContext === 'net' || L.is_net_effect || L._net_effect || L._display_badge === 'NET EFFECT') {
    return 'net_effect_claim';
  }
  if (L._is_chain_link || (Array.isArray(L.all_chains) && L.all_chains.some(c => (c?.role || '').toLowerCase() === 'hop'))) {
    return 'chain_hop_claim';
  }
  return 'direct_claim';
}

function getInteractionSectionType(interaction) {
  const L = interaction || {};
  if (L._is_shared_link) return 'shared';
  const locus = getInteractionLocus(L);
  if (locus === 'net_effect_claim') return 'net';
  if (locus === 'chain_hop_claim') return 'chain';
  if ((L.interaction_type || L.type) === 'indirect') return 'indirect';
  return 'direct';
}

function normalizePathwayLabel(value) {
  return (value || '').toString().toLowerCase().replace(/[_-]/g, ' ').replace(/\s+/g, ' ').trim();
}

function collectInteractionPathwayLabels(interaction) {
  const L = interaction || {};
  const labels = [];
  const add = (value) => {
    if (typeof value === 'string' && value.trim()) labels.push(value.trim());
  };
  add(L._chain_pathway_name);
  add(L.chain_context_pathway);
  add(L.hop_local_pathway);
  add(L.step3_finalized_pathway);
  add(L._chain_entity?.pathway_name);
  (Array.isArray(L.chain_pathways) ? L.chain_pathways : []).forEach(add);
  (Array.isArray(L.all_chains) ? L.all_chains : []).forEach(c => {
    add(c?.pathway_name);
    (Array.isArray(c?.pathways) ? c.pathways : []).forEach(add);
  });
  (Array.isArray(L.claims) ? L.claims : []).forEach(c => {
    add(c?.pathway_name);
    (Array.isArray(c?._hierarchy) ? c._hierarchy : []).forEach(add);
  });
  (Array.isArray(L.functions) ? L.functions : []).forEach(fn => {
    const raw = fn?.pathway;
    if (typeof raw === 'string') add(raw);
    else {
      add(raw?.name);
      add(raw?.canonical_name);
      (Array.isArray(raw?.hierarchy) ? raw.hierarchy : []).forEach(add);
    }
  });
  return [...new Set(labels)];
}

function pathwayLabelsMatchContext(labels, pathwayLabel) {
  const target = normalizePathwayLabel(pathwayLabel);
  if (!target) return false;
  return (labels || []).some(label => normalizePathwayLabel(label) === target);
}

function renderCompactChainLabel(L) {
  if (!L) return '';
  const chainDisplay = buildFullChainPath(SNAP?.main, null, L);
  if (chainDisplay) return chainDisplay;
  const members = Array.isArray(L.chain_members) ? L.chain_members : [];
  if (members.length >= 2) return members.map(escapeHtml).join(' → ');
  const via = Array.isArray(L.via) ? L.via : (Array.isArray(L.mediators) ? L.mediators : []);
  if (getInteractionLocus(L) === 'net_effect_claim' && via.length) {
    return `${escapeHtml(L.source || SNAP?.main || 'query')} → ${via.map(escapeHtml).join(' → ')} → ${escapeHtml(L.target || L.primary || 'target')}`;
  }
  return '';
}

window.getInteractionLocus = getInteractionLocus;
window.getInteractionSectionType = getInteractionSectionType;

/* Helper: Render an expandable function row */
function renderExpandableFunction(fn, mainProtein, interactorProtein, defaultInteractionEffect, parentDirection, pathwayContext = null, interactionPathway = null) {
  // Wrap the whole render in an error boundary — one malformed claim
  // should not crash the entire modal. Delegate to an inner function so
  // we still benefit from early-return patterns below.
  try {
    return _renderExpandableFunctionInner(fn, mainProtein, interactorProtein,
      defaultInteractionEffect, parentDirection, pathwayContext, interactionPathway);
  } catch (err) {
    console.error('[modal] renderExpandableFunction failed on claim', fn, err);
    const safeName = escapeHtml(((fn || {}).function) || 'Function');
    return `
      <div class="function-expandable render-error" style="padding: 12px; border-left: 3px solid #dc2626; background: #fef2f2;">
        <div style="font-weight: 600; color: #991b1b;">Failed to render claim</div>
        <div style="font-size: 0.85rem; margin-top: 4px;">${safeName}</div>
        <div style="font-size: 0.75rem; color: #6b7280; margin-top: 6px; font-family: ui-monospace, monospace;">
          ${escapeHtml(err && err.message ? err.message : String(err))}
        </div>
      </div>
    `;
  }
}

function _renderExpandableFunctionInner(fn, mainProtein, interactorProtein, defaultInteractionEffect, parentDirection, pathwayContext = null, interactionPathway = null) {
  const _rawFnName = fn.function || '';
  const _isGarbageClaim = /^__fallback__$|^(activates?|inhibits?|binds?|regulates?|interacts?) interaction$/i.test(_rawFnName);
  const functionName = escapeHtml(_isGarbageClaim ? 'Function' : (_rawFnName || 'Function'));

  // PR-2 / C5: synthetic pathway-only stubs (backend fabricated to keep a
  // row in the modal when no real mechanism exists) render as a clear
  // placeholder card instead of a phony scientific claim. The flag is set
  // at services/data_builder.py synthesize path.
  if (fn && fn._synthetic) {
    const stubPathway = escapeHtml(fn.pathway || 'Unassigned');
    return `
      <div class="function-expandable synthetic-stub" style="opacity: 0.65; padding: 12px; border-left: 3px dashed var(--color-border, #9ca3af);">
        <div style="font-style: italic; color: var(--color-text-secondary, #6b7280);">
          No pipeline-generated mechanism for this interaction yet.
        </div>
        <div style="margin-top: 4px; font-size: 0.8rem;">
          Pathway: <strong>${stubPathway}</strong>
        </div>
      </div>
    `;
  }

  // Atom E — thin-claim stub emitted by the 2ax/2az LLM when the cascade-
  // context pair biology is genuinely undocumented. Emitted instead of
  // fabricating a mechanism. Render as a muted honest placeholder so the
  // hop row is never empty while making it visually obvious that the
  // biology is not characterized.
  if (fn && fn._thin_claim) {
    const thinTitle = escapeHtml(fn.function || 'Pair biology not characterized in the cascade context');
    const thinProse = escapeHtml(fn.cellular_process || '');
    return `
      <div class="function-expandable thin-claim-stub" style="opacity: 0.7; padding: 12px; border-left: 3px solid #94a3b8; background: rgba(148, 163, 184, 0.08); border-radius: 4px;">
        <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 6px;">
          <span style="color: #64748b; font-weight: 600; font-size: 0.9rem;">Thin claim</span>
          <span style="font-size: 10px; background: #cbd5e1; color: #1e293b; padding: 2px 6px; border-radius: 3px;">
            Pair biology not characterized
          </span>
        </div>
        <div style="font-weight: 500; color: var(--color-text, #374151);">
          ${thinTitle}
        </div>
        ${thinProse ? `<div style="margin-top: 6px; font-size: 0.85rem; color: var(--color-text-secondary, #6b7280);">${thinProse}</div>` : ''}
      </div>
    `;
  }

  // Atom E — synthetic router stub. Emitted when every LLM-generated
  // claim for a hop got rerouted off the hop (mentioned query) or
  // dropped (no hop mention). The stub exists so the hop row is never
  // empty; its prose describes the routing outcome, not biology.
  if (fn && fn._synthetic_from_router) {
    const routerTitle = escapeHtml(fn.function || 'Pair-specific biology pending manual curation');
    const routerOutcome = escapeHtml(fn._router_outcome_summary || fn.cellular_process || '');
    return `
      <div class="function-expandable synthetic-router-stub" style="opacity: 0.65; padding: 12px; border-left: 3px dashed #f59e0b; background: rgba(245, 158, 11, 0.06); border-radius: 4px;">
        <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 6px;">
          <span style="color: #b45309; font-weight: 600; font-size: 0.9rem;">Router placeholder</span>
          <span style="font-size: 10px; background: #fde68a; color: #78350f; padding: 2px 6px; border-radius: 3px;">
            Awaiting curation
          </span>
        </div>
        <div style="font-weight: 500; color: var(--color-text, #374151);">
          ${routerTitle}
        </div>
        ${routerOutcome ? `<div style="margin-top: 6px; font-size: 0.85rem; color: var(--color-text-secondary, #6b7280); font-style: italic;">${routerOutcome}</div>` : ''}
      </div>
    `;
  }

  // Pathway badge logic: Prioritize interaction-level assignment if no function-specific pathway
  let pathwayBadgeHTML = '';
  // FIX: Handle both object (legacy) and string (V2) formats for function pathway
  const fnPathwayRaw = fn.pathway;
  const fnPathway = (typeof fnPathwayRaw === 'string') ? fnPathwayRaw : (fnPathwayRaw?.canonical_name || fnPathwayRaw?.name);
  const fnHierarchy = fn._hierarchy || ((typeof fnPathwayRaw === 'object' && fnPathwayRaw?.hierarchy) ? fnPathwayRaw.hierarchy : []);

  if (fnPathway) {
    // 1. Function has explicit pathway data
    // Consider the claim in-context if its own pathway/hierarchy matches OR
    // if the parent interaction belongs to this pathway via a sibling claim.
    const interactionPathways = Array.isArray(fn._interaction_pathways) ? fn._interaction_pathways : null;
    const ownMatch = isPathwayInContext(fnPathway, fnHierarchy, pathwayContext, null);
    const matchesContext = ownMatch || isPathwayInContext(fnPathway, fnHierarchy, pathwayContext, interactionPathways);
    const viaInteractionOnly = matchesContext && !ownMatch;
    const baseTooltip = fnHierarchy.length > 1 ? fnHierarchy.join(' → ') : fnPathway;
    const tooltipText = viaInteractionOnly
      ? `${baseTooltip} (via interaction; primary pathway: ${fnPathway})`
      : baseTooltip;

    // Badge colors resolve through CSS custom properties (see
    // static/styles.css :root) so theme / dark-mode changes don't require
    // hunting through inline hex values. Fallbacks match the previous
    // hardcoded hex so rendering is unchanged when the vars are absent.
    const _txt = _cssVar('--color-badge-text', '#ffffff');
    if (matchesContext) {
      const _bg = _cssVar('--color-pathway-current', '#10b981');
      pathwayBadgeHTML = `<span class="pathway-badge current" style="font-size: 10px; padding: 2px 6px; border-radius: 4px; margin-right: 8px; background: ${_bg}; color: ${_txt}; cursor: help;" title="${escapeHtml(tooltipText)}">${escapeHtml(fnPathway)}</span>`;
    } else {
      const _bg = _cssVar('--color-pathway-other', '#6b7280');
      pathwayBadgeHTML = `<span class="pathway-badge other" style="font-size: 10px; padding: 2px 6px; border-radius: 4px; margin-right: 8px; background: ${_bg}; color: ${_txt}; opacity: 0.7; cursor: help;" title="${escapeHtml(tooltipText)}">${escapeHtml(fnPathway)}</span>`;
    }
  } else if (interactionPathway) {
    // 2. Fallback to Interaction-Level Assignment (V2 Pipeline)
    const _bg = _cssVar('--color-pathway-assigned', '#3b82f6');
    const _txt = _cssVar('--color-badge-text', '#ffffff');
    pathwayBadgeHTML = `<span class="pathway-badge assigned" style="font-size: 10px; padding: 2px 6px; border-radius: 4px; margin-right: 8px; background: ${_bg}; color: ${_txt}; cursor: help;" title="Assigned by Pipeline V2">${escapeHtml(interactionPathway)}</span>`;
  } else if (pathwayContext?.name) {
    // 3. Fallback to viewing context
    const _bg = _cssVar('--color-pathway-inherited', '#10b981');
    const _txt = _cssVar('--color-badge-text', '#ffffff');
    pathwayBadgeHTML = `<span class="pathway-badge inherited" style="font-size: 10px; padding: 2px 6px; border-radius: 4px; margin-right: 8px; background: ${_bg}; color: ${_txt}; opacity: 0.5; cursor: help;" title="Inherited from current view">${escapeHtml(pathwayContext.name)}</span>`;
  }

  // --- DATA PREPARATION (From Table View Logic) ---
  // S1: bidirectional is dead — all directions are asymmetric.
  // Legacy bidirectional values are treated as main_to_primary.
  const fnDirection = parentDirection || fn.interaction_direction || fn.direction || 'main_to_primary';
  let sourceProtein, targetProtein, arrowSymbol;
  if (fnDirection === 'primary_to_main') {
    sourceProtein = interactorProtein;
    targetProtein = mainProtein;
    arrowSymbol = '→';
  } else {
    // main_to_primary (or legacy bidirectional → same treatment)
    sourceProtein = mainProtein;
    targetProtein = interactorProtein;
    arrowSymbol = '→';
  }

  // Interaction Effect
  // FIX: Prioritize the passed defaultInteractionEffect (from Link) over fn.interaction_effect
  // This ensures consistency with Table View which relies on the Link's arrow.
  let interactionEffect = defaultInteractionEffect || fn.interaction_effect || 'binds';
  const interactionArrowClass = arrowKind(interactionEffect, fn.intent, fnDirection);
  const interactionEffectBadgeText = formatArrow(interactionEffect);
  const interactionEffectBadge = `<span class="effect-badge effect-${interactionArrowClass}">${interactionEffectBadgeText}</span>`;

  // Function Effect
  const fnArrow = fn.arrow || 'binds';
  // Context override logic
  if (interactionEffect === 'binds' && fn._context && fn._context.type === 'chain') {
    if (fnArrow === 'activates' || fnArrow === 'inhibits') {
      interactionEffect = fnArrow;
    }
  }
  const functionArrowClass = arrowKind(fnArrow, fn.intent, fnDirection);
  const functionEffectBadgeText = formatArrow(fnArrow);
  const functionEffectBadge = `<span class="effect-badge effect-${functionArrowClass}">${functionEffectBadgeText}</span>`;

  // Helper Data
  const fnLocus = (fn.locus || '').toString().toLowerCase();
  const fnContext = (fn.function_context || '').toString().toLowerCase();
  let contextBadge = '';
  if (fnLocus === 'net_effect_claim' || fnContext === 'net') {
    contextBadge = '<span class="context-badge net">NET EFFECT</span>';
  } else if (fnLocus === 'chain_hop_claim' || fnContext === 'chain_derived' || fnContext === 'chain_hop') {
    contextBadge = '<span class="context-badge chain">CHAIN HOP</span>';
  } else if (fn._context) {
    contextBadge = fn._context.type === 'chain'
      ? '<span class="context-badge chain">CHAIN CONTEXT</span>'
      : '<span class="context-badge direct">DIRECT PAIR</span>';
  }

  // Interaction Display (Header)
  const interactionDisplay = `
    <span class="detail-interaction">
      ${escapeHtml(sourceProtein)}
      <span class="detail-arrow">${arrowSymbol}</span>
      ${escapeHtml(targetProtein)}
    </span>
    ${interactionEffectBadge}
  `;

  // --- CONTENT CONSTRUCTION (Restoring "Pretty" Layout) ---
  let expandedSections = '';

  // 1. Effects Summary — only show when it adds unique info beyond the header
  const hasDualTrack = fn.net_effect && fn.direct_effect && fn.net_effect !== fn.direct_effect;
  const fnArrowDiffers = fnArrow !== (defaultInteractionEffect || 'binds') && fnArrow !== 'binds';

  if (_isGarbageClaim) {
    // Skip Effects Summary entirely for garbage/fallback claims
  } else if (hasDualTrack) {
    // Dual-track: show net vs direct effect comparison (unique info)
    expandedSections += `
      <div class="function-detail-section section-effects-summary section-highlighted" style="background: var(--color-bg-secondary); border-left: 3px solid var(--color-primary);">
        <div class="function-section-title">🎯 Effects Summary</div>
        <div class="function-section-content">
          <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px;">
            <div>
              <div style="font-size: 0.75rem; text-transform: uppercase; color: var(--color-text-secondary); margin-bottom: 4px;">Net Effect (via chain)</div>
              <span class="effect-badge effect-${arrowKind(fn.net_effect, fn.intent, fnDirection)}">${formatArrow(fn.net_effect)}</span>
            </div>
            <div>
              <div style="font-size: 0.75rem; text-transform: uppercase; color: var(--color-text-secondary); margin-bottom: 4px;">Direct Effect (pair)</div>
              <span class="effect-badge effect-${arrowKind(fn.direct_effect, fn.intent, fnDirection)}">${formatArrow(fn.direct_effect)}</span>
            </div>
          </div>
        </div>
      </div>
    `;
  } else if (fnArrowDiffers) {
    // Function arrow differs from interaction arrow — show the distinction
    expandedSections += `
      <div class="function-detail-section section-effects-summary section-highlighted" style="background: var(--color-bg-secondary); border-left: 3px solid var(--color-primary);">
        <div class="function-section-title">🎯 Effects Summary</div>
        <div class="function-section-content">
          <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px;">
            <div>
              <div style="font-size: 0.75rem; text-transform: uppercase; color: var(--color-text-secondary); margin-bottom: 4px;">Interaction</div>
              <div style="font-size: 0.9rem; margin-bottom: 4px;">
                ${escapeHtml(sourceProtein)} ${arrowSymbol} ${escapeHtml(targetProtein)}
              </div>
              ${interactionEffectBadge}
            </div>
            <div>
              <div style="font-size: 0.75rem; text-transform: uppercase; color: var(--color-text-secondary); margin-bottom: 4px;">Function</div>
              <div style="font-size: 0.9rem; margin-bottom: 4px;">
                ${escapeHtml(functionName)}
              </div>
              ${functionEffectBadge}
            </div>
          </div>
        </div>
      </div>
    `;
  }
  // else: effects are the same — skip Effects Summary (header already shows it)

  // 2. Mechanism (from cellular_process)
  // _isPlaceholder is defined at module scope (top of this file) so the
  // per-function renderer AND the interaction-section renderer share
  // one placeholder catalog — avoids the ReferenceError that hit when
  // the definition was local here but callers further down referenced it.
  if (fn.cellular_process && !_isPlaceholder(fn.cellular_process)) {
    expandedSections += `
      <div class="function-detail-section section-mechanism section-highlighted">
        <div class="function-section-title">⚙️ Mechanism</div>
        <div class="function-section-content">
          <div style="margin-bottom: 8px;">${escapeHtml(fn.cellular_process)}</div>
        </div>
      </div>
    `;
  }

  // 3. Effect Description
  if (fn.effect_description && !_isPlaceholder(fn.effect_description)) {
    expandedSections += `
      <div class="function-detail-section section-effect section-highlighted effect-${functionArrowClass}">
        <div class="function-section-title">💡 Effect</div>
        <div class="function-section-content">${escapeHtml(fn.effect_description)}</div>
      </div>
    `;
  }

  // 4. Biological Cascade
  if (Array.isArray(fn.biological_consequence) && fn.biological_consequence.length > 0) {
    const cascadesHTML = fn.biological_consequence.map((cascade, idx) => {
      const text = (cascade == null ? '' : cascade).toString().trim();
      if (!text) return '';
      // Gate: a cascade entry whose whole text is a placeholder stub
      // (e.g. "Discovered via chain resolution") renders as a single
      // meaningless "Scenario 1" box. Skip it so the Cascade section
      // collapses to empty for placeholder-only claims.
      if (_isPlaceholder(text)) return '';
      const steps = text.split('→').map(s => s.trim()).filter(s => s.length > 0);
      if (steps.length === 0) return '';
      return `
          <div class="cascade-scenario">
            <div class="cascade-scenario-label">Scenario ${idx + 1}</div>
            <div class="cascade-flow-container">
              ${steps.map(step => `<div class="cascade-flow-item">${escapeHtml(step)}</div>`).join('')}
            </div>
          </div>
        `;
    }).join('');

    if (cascadesHTML) {
      expandedSections += `
        <div class="function-detail-section">
          <div class="function-section-title">Biological Cascade</div>
          ${cascadesHTML}
        </div>
      `;
    }
  }

  // 5. Specific Effects
  // Filter out placeholder-only entries so chain stubs don't render
  // "Discovered via chain resolution" as a Specific Effect bullet.
  if (Array.isArray(fn.specific_effects) && fn.specific_effects.length > 0) {
    const _realEffects = fn.specific_effects.filter(eff => eff && !_isPlaceholder(String(eff)));
    if (_realEffects.length > 0) {
      expandedSections += `
        <div class="function-detail-section section-specific-effects section-highlighted">
          <div class="function-section-title">⚡ Specific Effects</div>
          <ul style="margin: 0; padding-left: 1.5em;">
            ${_realEffects.map(eff => `<li class="function-section-content">${escapeHtml(eff)}</li>`).join('')}
          </ul>
        </div>
      `;
    }
  }

  // 6. Evidence (Pretty Card Style)
  if (Array.isArray(fn.evidence) && fn.evidence.length > 0) {
    expandedSections += `
      <div class="function-detail-section">
        <div class="function-section-title">Evidence & Publications</div>
        ${fn.evidence.map(ev => {
      // Accept both canonical (paper_title) and legacy (title) field names.
      // Some pipeline paths emit one, some the other; fallback prevents
      // silent "Untitled" when the data is actually there under a different key.
      const title = ev.paper_title || ev.title || (ev.pmid ? `PMID: ${ev.pmid}` : 'Untitled');
      const metaParts = [];
      if (ev.journal) metaParts.push(escapeHtml(ev.journal));
      if (ev.year) metaParts.push(escapeHtml(ev.year));
      if (ev.assay) metaParts.push(escapeHtml(ev.assay));
      if (ev.species) metaParts.push(escapeHtml(ev.species));
      const meta = metaParts.join(' · ');

      let pmidLinks = '';
      if (ev.pmid) pmidLinks += `<a href="https://pubmed.ncbi.nlm.nih.gov/${escapeHtml(ev.pmid)}" target="_blank" class="pmid-badge" onclick="event.stopPropagation();">PMID: ${escapeHtml(ev.pmid)}</a>`;
      if (ev.doi) pmidLinks += `<a href="https://doi.org/${escapeHtml(ev.doi)}" target="_blank" class="pmid-badge" onclick="event.stopPropagation();">DOI</a>`;

      return `
            <div class="evidence-card">
              <div class="evidence-title">${escapeHtml(title)}</div>
              ${meta ? `<div class="evidence-meta">${meta}</div>` : ''}
              ${ev.key_finding ? `<div class="evidence-key-finding">${escapeHtml(ev.key_finding)}</div>` : ''}
              ${ev.relevant_quote ? `<div class="evidence-quote">"${escapeHtml(ev.relevant_quote)}"</div>` : ''}
              ${pmidLinks ? `<div style="margin-top: var(--space-2);">${pmidLinks}</div>` : ''}
            </div>
          `;
    }).join('')}
      </div>
    `;
  } else if (fn.pmids && fn.pmids.length > 0) {
    expandedSections += `
      <div class="function-detail-section">
        <div class="function-section-title">References</div>
        <div>
          ${fn.pmids.map(pmid => `<a href="https://pubmed.ncbi.nlm.nih.gov/${escapeHtml(pmid)}" target="_blank" class="pmid-badge">PMID: ${escapeHtml(pmid)}</a>`).join('')}
        </div>
      </div>
    `;
  }

  // Build Final Row HTML
  return `
    <div class="function-expandable-row">
      <div class="function-row-header" role="button" tabindex="0" aria-expanded="false">
        <div class="function-row-left">
          <div class="function-expand-icon">▼</div>
          ${pathwayBadgeHTML}
          <div class="function-name-with-effect">
            <div class="function-name-display">${functionName}</div>
            ${functionEffectBadge}
          </div>
          <span class="function-separator" style="margin: 0 8px; color: var(--color-text-secondary);">||</span>
          ${interactionDisplay}
          ${contextBadge}
        </div>
      </div>
      <div class="function-expanded-content">
        ${expandedSections || '<div class="function-section-content" style="color: var(--color-text-secondary); font-style: italic; padding: 8px 0;">Detailed mechanism data not yet available for this claim.</div>'}
      </div>
    </div>
  `;
}

function handleLinkClick(ev, d) {
  ev.stopPropagation();
  if (!d) return;
  if (d.type === 'function') {
    showFunctionModalFromLink(d);
  } else if (d.type === 'interaction' || d.type === 'interaction-edge') {
    // Both regular interactions and pathway-context interaction edges use the same modal
    showInteractionModal(d);
  }
}

/* ===============================================================
   Interaction Modal: NEW DESIGN with Expandable Functions
   =============================================================== */
function showInteractionModal(link, clickedNode = null) {
  const L = link.data || link;  // Link properties are directly on link object or in data
  const isSharedInteraction = L._is_shared_link || false;
  const interactionLocus = getInteractionLocus(L);
  const isNetEffectInteraction = interactionLocus === 'net_effect_claim';
  const isChainHopInteraction = interactionLocus === 'chain_hop_claim';
  const isIndirectInteraction = !isChainHopInteraction && (L.interaction_type === 'indirect' || isNetEffectInteraction);

  // Use semantic source/target (biological direction) instead of D3's geometric source/target
  // Semantic fields preserve the biological meaning, while link.source/target are D3 node references
  const srcName = L.semanticSource || ((link.source && link.source.id) ? link.source.id : link.source);
  const tgtName = L.semanticTarget || ((link.target && link.target.id) ? link.target.id : link.target);
  const safeSrc = escapeHtml(srcName || '-');
  const safeTgt = escapeHtml(tgtName || '-');

  // Determine which protein was clicked (if any)
  // If called from node click, use clickedNode; otherwise determine from link
  let clickedProteinId = null;
  if (clickedNode) {
    clickedProteinId = clickedNode.id;
  }

  // Determine arrow direction
  // IMPORTANT: Direction field has different semantics for direct vs indirect interactions
  // - Direct: direction is QUERY-RELATIVE (main_to_primary = query→interactor)
  // - Indirect: direction is LINK-ABSOLUTE (main_to_primary = source→target after transformation)
  const direction = L.direction || link.direction || 'main_to_primary';
  const isIndirect = !isChainHopInteraction && (L.interaction_type === 'indirect' || isNetEffectInteraction);
  const directionIsLinkAbsolute = L._direction_is_link_absolute || isIndirect;

  // S1: all directions are asymmetric. Arrow is always source → target.
  const arrowSymbol = '→';

  // === EXTRACT FUNCTIONS (claims preferred over raw JSONB functions) ===
  const claims = Array.isArray(L.claims) ? L.claims : [];
  const rawFunctions = Array.isArray(L.functions) ? L.functions : [];
  const functions = claims.length > 0
      ? claims.map(c => ({
          function: c.function_name,
          arrow: c.arrow,
          cellular_process: c.mechanism,
          effect_description: c.effect_description,
          biological_consequence: c.biological_consequences,
          specific_effects: c.specific_effects,
          evidence: c.evidence,
          pmids: c.pmids,
          pathway: c.pathway_name,
          _hierarchy: c._hierarchy,
          _interaction_pathways: c._interaction_pathways,
          interaction_effect: c.interaction_effect,
          function_effect: c.interaction_effect,
          interaction_direction: c.direction,
          direction: c.direction,
          locus: c.locus,
          _context: (c.context_data && typeof c.context_data === 'object' && !Array.isArray(c.context_data)) ? c.context_data : null,
          function_context: c.function_context,
          _claim_id: c.id,
      }))
      : (rawFunctions || []);

  // Filter out auto-generated garbage claim names (e.g., "activates interaction", "__fallback__")
  const _garbageClaimPattern = /^__fallback__$|^(activates?|inhibits?|binds?|regulates?|interacts?) interaction$|^\w+ (interacts?\s+with|activates?|inhibits?|binds?|regulates?) \w+$/i;
  const filteredFunctions = functions.filter(f => !_garbageClaimPattern.test(f.function || ''));
  const allGarbage = filteredFunctions.length === 0 && functions.length > 0;
  const displayFunctions = filteredFunctions;

  let functionsHTML = '';
  let interactionMetadataHTML = '';
  let chainBannerHTML = '';

  // === CHAIN CONTEXT BANNER (for chain link interactions) ===
  //
  // Multi-chain support (#12): the same Interaction can participate in
  // N IndirectChain rows (e.g. ATXN3\u2194MTOR via VCP\u2192RHEB AND via TSC2\u2192TSC1).
  // The data builder emits ``L.all_chains`` as a list of chain summaries
  // when N > 1; we render ONE banner per chain so the user sees every
  // cascade this hop is part of, not just the legacy "primary" chain
  // attached as ``L._chain_entity``.
  //
  // When ``all_chains`` is absent or empty, fall back to the single-
  // chain shape via ``L._chain_entity`` so older payloads still render.
  if (L._is_chain_link) {
    const currentSrc = srcName;
    const currentTgt = tgtName;

    // L5.1: client-side pseudo detection (matches the server-side whitelist).
    const _PSEUDO_FALLBACK = new Set([
      'RNA','mRNA','pre-mRNA','tRNA','rRNA','lncRNA','miRNA','snRNA','snoRNA',
      'DNA','ssDNA','dsDNA','Ubiquitin','SUMO','NEDD8','Proteasome','Ribosome',
      'Spliceosome','Actin','Tubulin','Stress Granules','P-bodies'
    ]);
    const _isPseudoProt = (p) =>
      _PSEUDO_FALLBACK.has(p) || (typeof p === 'string' && p.endsWith('mRNA'));

    // Resolve the list of chain entities to render. Prefer the multi-
    // chain ``all_chains[]`` payload when present.
    let chainEntities = [];
    if (Array.isArray(L.all_chains) && L.all_chains.length) {
      chainEntities = L.all_chains
        .filter(c => c && Array.isArray(c.chain_proteins) && c.chain_proteins.length >= 2)
        .map(c => ({
          chainId: c.chain_id != null ? c.chain_id : (c.chain_proteins.join('->')),
          chainProteins: c.chain_proteins,
          chainArrows: Array.isArray(c.chain_with_arrows) ? c.chain_with_arrows : [],
          pathwayName: c.pathway_name || '',
          role: c.role || '',
        }));
    }
    if (!chainEntities.length && L._chain_entity && L._chain_entity.chain_proteins) {
      chainEntities = [{
        chainId: L.chain_id != null ? L.chain_id : (L._chain_entity.chain_proteins.join('->')),
        chainProteins: L._chain_entity.chain_proteins,
        chainArrows: L._chain_entity.chain_with_arrows || [],
        pathwayName: L._chain_entity.pathway_name || '',
        role: '',
      }];
    }

    const renderBannerForChain = (entity, multiTag) => {
      const chainProteins = entity.chainProteins;
      const chainArrows = entity.chainArrows;
      let bannerParts = [];
      for (let i = 0; i < chainProteins.length; i++) {
        const prot = chainProteins[i];
        const isCurrent = (prot === currentSrc || prot === currentTgt);
        const isPseudo = _isPseudoProt(prot);
        const classes = ['chain-protein'];
        if (isCurrent) classes.push('current');
        if (isPseudo) classes.push('pseudo');
        // L5.4 \u2014 make chain protein chips clickable so the user can
        // re-scope the modal to a different hop within the SAME chain.
        // ``data-chain-id`` lets the click handler scope the chainHops
        // search to this specific chain instead of all chain links.
        const dataAttrs =
          ` data-chain-nav="1"` +
          ` data-protein="${escapeHtml(prot)}"` +
          ` data-chain-index="${i}"`;
        bannerParts.push(`<span class="${classes.join(' ')}"${dataAttrs} role="button" tabindex="0">${escapeHtml(prot)}</span>`);
        if (i < chainProteins.length - 1) {
          const arrowEntry = chainArrows[i];
          const arrowType = arrowEntry ? arrowEntry.arrow : 'binds';
          bannerParts.push(`<span class="chain-arrow ${arrowType}">\u2192 ${arrowType} \u2192</span>`);
        }
      }

      // Prev/next hop nav \u2014 only meaningful when chain has >1 hop.
      const totalHops = Math.max(1, chainProteins.length - 1);
      const curHop = getDisplayHopIndex(L) ?? 0;
      const navHTML = totalHops > 1
        ? `<div class="chain-hop-nav" style="font-size:11px;margin-top:6px;display:flex;gap:8px;">
             <button class="chain-hop-prev" data-chain-nav="prev" ${curHop <= 0 ? 'disabled' : ''}>\u2190 Prev hop</button>
             <button class="chain-hop-next" data-chain-nav="next" ${curHop >= totalHops - 1 ? 'disabled' : ''}>Next hop \u2192</button>
           </div>`
        : '';

      const tagHTML = multiTag
        ? `<span style="font-size:10px;background:rgba(99,102,241,0.18);color:#6366f1;padding:1px 6px;border-radius:8px;margin-left:6px;">${escapeHtml(multiTag)}</span>`
        : '';
      const pathwayHTML = entity.pathwayName
        ? `<span style="font-size:10px;color:#94a3b8;margin-left:6px;">in <strong>${escapeHtml(entity.pathwayName)}</strong></span>`
        : '';

      return `
        <div class="chain-context-banner" data-chain-id="${escapeHtml(String(entity.chainId ?? ''))}">
          <div style="font-size:10px;text-transform:uppercase;letter-spacing:0.05em;color:#64748b;margin-bottom:6px;">Chain Context${tagHTML}${pathwayHTML}</div>
          <div>${bannerParts.join(' ')}</div>
          <div style="font-size:11px;color:#64748b;margin-top:6px;">
            Viewing: ${escapeHtml(currentSrc)} \u2192 ${escapeHtml(currentTgt)} (hop ${curHop + 1} of ${totalHops})
          </div>
          ${navHTML}
        </div>
      `;
    };

    chainBannerHTML = chainEntities
      .map((entity, idx) => {
        const tag = chainEntities.length > 1
          ? `chain ${idx + 1} of ${chainEntities.length}`
          : '';
        return renderBannerForChain(entity, tag);
      })
      .join('\n');
  }

  // === BUILD INTERACTION METADATA SECTION ===

  let functionTypeBadge = '';
  if (isSharedInteraction) {
    functionTypeBadge = '<span class="mechanism-badge badge-shared"><svg width="10" height="10" viewBox="0 0 16 16" fill="none" style="vertical-align:middle;margin-right:3px;"><circle cx="8" cy="5" r="3" stroke="white" stroke-width="1.5" fill="none"/><circle cx="4" cy="12" r="2.5" stroke="white" stroke-width="1.5" fill="none"/><circle cx="12" cy="12" r="2.5" stroke="white" stroke-width="1.5" fill="none"/></svg>SHARED</span>';
  } else if (isNetEffectInteraction) {
    const via = Array.isArray(L.via) && L.via.length ? ` via ${L.via.map(escapeHtml).join(' → ')}` : '';
    functionTypeBadge = `<span class="mechanism-badge badge-net">NET EFFECT${via}</span>`;
  } else if (isChainHopInteraction) {
    const displayHopIndex = getDisplayHopIndex(L);
    const hopNumber = displayHopIndex != null ? ` HOP ${displayHopIndex + 1}` : ' HOP';
    functionTypeBadge = `<span class="mechanism-badge badge-chain">CHAIN${hopNumber}</span>`;
  } else if (isIndirectInteraction) {
    // Build full chain path display. Preference order inside
    // buildFullChainPath: chain_context.full_chain → _chain_entity.chain_proteins.
    // C2 cleanup: the old ``firstChainFunc._context.chain`` read was a
    // dead path — context_data was never populated as ``_context.chain``
    // in any modern writer. Removed. Now we pass null as chainArray and
    // let buildFullChainPath decide from the authoritative sources only.
    let chainDisplay = buildFullChainPath(SNAP.main, null, L);

    if (!chainDisplay && L.upstream_interactor) {
      if (L.upstream_interactor === L.primary) {
        chainDisplay = `${escapeHtml(SNAP.main)} → ${escapeHtml(L.primary)}`;
      } else {
        chainDisplay = `${escapeHtml(L.upstream_interactor)} → ${escapeHtml(L.primary)} (query position unknown)`;
      }
    }

    functionTypeBadge = chainDisplay
      ? `<span class="mechanism-badge badge-indirect"><svg width="10" height="10" viewBox="0 0 16 16" fill="none" style="vertical-align:middle;margin-right:3px;"><path d="M2 8h4M10 8h4M7 5l2 3-2 3" stroke="white" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>${chainDisplay}</span>`
      : `<span class="mechanism-badge badge-indirect"><svg width="10" height="10" viewBox="0 0 16 16" fill="none" style="vertical-align:middle;margin-right:3px;"><path d="M2 8h4M10 8h4M7 5l2 3-2 3" stroke="white" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>INDIRECT</span>`;
  } else {
    functionTypeBadge = '<span class="mechanism-badge badge-direct"><svg width="10" height="10" viewBox="0 0 16 16" fill="none" style="vertical-align:middle;margin-right:3px;"><path d="M2 8L6 12L14 4" stroke="white" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/></svg>DIRECT</span>';
  }

  // Chain-first display: For indirect interactions, show chain as primary
  // identifier. Prefer chain_context.full_chain (authoritative, query-
  // position-agnostic) so chains where the query sits anywhere in the
  // middle are rendered correctly.
  let chainTitleHTML = '';
  if (isIndirectInteraction) {
    let chainDisplay;

    // Resolve the authoritative chain: chain_context.full_chain first,
    // then chain_with_arrows (typed arrows), then the legacy
    // [main, ...mediator_chain, target] reconstruction.
    const ctx = L.chain_context || null;
    const fullChainFromCtx =
      ctx && Array.isArray(ctx.full_chain) && ctx.full_chain.length >= 2
        ? ctx.full_chain.filter((p) => p)
        : null;

    if (L._chain_entity && L._chain_entity.chain_with_arrows) {
      chainDisplay = L._chain_entity.chain_with_arrows.map((seg, i, arr) => {
        const arrowBadge = `<span style="display:inline-block;padding:1px 5px;border-radius:8px;font-size:9px;background:rgba(99,102,241,0.15);color:#6366f1;margin:0 2px;vertical-align:middle;">${escapeHtml(seg.arrow)}</span>`;
        return i === arr.length - 1
          ? `${escapeHtml(seg.from)} ${arrowBadge} ${escapeHtml(seg.to)}`
          : `${escapeHtml(seg.from)} ${arrowBadge}`;
      }).join(' ');
    } else if (fullChainFromCtx) {
      chainDisplay = fullChainFromCtx.map(escapeHtml).join(' → ');
    } else {
      const mediatorChainForTitle = L.mediator_chain || [];
      if (mediatorChainForTitle.length > 0) {
        chainDisplay = `${escapeHtml(SNAP.main)} → ${mediatorChainForTitle.map(escapeHtml).join(' → ')} → ${escapeHtml(tgtName)}`;
      } else {
        chainDisplay = `${escapeHtml(SNAP.main)} → ${escapeHtml(tgtName)}`;
      }
    }

    chainTitleHTML = `<div style="font-size:15px;font-weight:600;color:var(--color-text-primary,#e2e8f0);margin-bottom:8px;padding:8px 12px;background:rgba(99,102,241,0.08);border-radius:8px;border:1px solid rgba(99,102,241,0.2);">${chainDisplay}</div>`;
  }

  if (displayFunctions.length > 0) {
    if (isIndirectInteraction || isChainHopInteraction) {
      // Net-effect and chain-hop rows already carry biological source -> target
      // endpoints. Query-relative grouping makes those rows look direct.
      const arrows = L.arrows || {};
      const arrowCount = Object.values(arrows).flat().filter((v, i, a) => a.indexOf(v) === i).length;
      const functionsLabel = isChainHopInteraction ? 'Chain-Hop Claims' : (isNetEffectInteraction ? 'Net-Effect Claims' : 'Functions');
      const useAbsoluteFunctionEndpoints = isChainHopInteraction || isNetEffectInteraction;
      const functionSource = useAbsoluteFunctionEndpoints ? (srcName || SNAP.main) : SNAP.main;
      const functionTarget = useAbsoluteFunctionEndpoints ? (tgtName || L.primary || '') : L.primary;
      const functionDirection = useAbsoluteFunctionEndpoints ? 'main_to_primary' : direction;

      functionsHTML = `<div class="modal-functions-header">${functionsLabel} (${displayFunctions.length})${arrowCount > 1 ? ` <span style="background:#f59e0b;color:white;padding:2px 6px;border-radius:10px;font-size:10px;margin-left:8px;">${arrowCount} arrows</span>` : ''}</div>`;

      functionsHTML += `<div style="margin:16px 0;">
        ${displayFunctions.map(f => {
        return renderExpandableFunction(f, functionSource, functionTarget, L.arrow || 'binds', functionDirection, null, L.step3_finalized_pathway);
      }).join('')}
      </div>`;

    } else {
      // For direct interactions: Group by INTERACTION DIRECTION
      // Functions should be grouped by which protein acts on which, showing the directionality
      const grp = {
        main_to_primary: [],
        primary_to_main: [],
      };
      const _validDirs = new Set(['main_to_primary', 'primary_to_main']);
      displayFunctions.forEach(f => {
        const dir = f.interaction_direction || f.direction || direction || 'main_to_primary';
        grp[_validDirs.has(dir) ? dir : (direction || 'main_to_primary')].push(f);
      });

      const arrows = L.arrows || {};
      const arrowCount = Object.values(arrows).flat().filter((v, i, a) => a.indexOf(v) === i).length;

      // Determine protein names for direction labels
      const queryProtein = SNAP.main;
      const interactorProtein = safeSrc === queryProtein ? safeTgt : safeSrc;

      functionsHTML = `<div class="modal-functions-header">Functions (${displayFunctions.length})${arrowCount > 1 ? ` <span style="background:#f59e0b;color:white;padding:2px 6px;border-radius:10px;font-size:10px;margin-left:8px;">${arrowCount} arrows</span>` : ''}</div>`;

      // Direction labels with arrow symbols based on interaction type
      const directionConfig = {
        main_to_primary: {
          source: queryProtein,
          target: interactorProtein,
          arrowSymbol: '→',
          color: '#3b82f6',  // Blue
          bg: '#dbeafe'
        },
        primary_to_main: {
          source: interactorProtein,
          target: queryProtein,
          arrowSymbol: '→',
          color: '#9333ea',  // Purple
          bg: '#f3e8ff'
        },
      };

      ['main_to_primary', 'primary_to_main'].forEach(dir => {
        if (grp[dir].length) {
          const config = directionConfig[dir];
          functionsHTML += `<div style="">
            <div style="">
              <span class="detail-interaction">
                ${escapeHtml(config.source)}
                <span class="detail-arrow">${config.arrowSymbol}</span>
                ${escapeHtml(config.target)}
              </span> (${grp[dir].length})
            </div>
            ${grp[dir].map(f => {
            // Within each direction, show effect type badge
            const effectArrow = f.arrow || 'binds';
            // Pass SNAP.main and interactorName to ensure correct direction resolution
            // FIX: Pass interactionArrow as defaultInteractionEffect, NOT effectArrow
            return renderExpandableFunction(f, SNAP.main, interactorProtein, L.arrow || 'binds', dir, null, L.step3_finalized_pathway);
          }).join('')}
          </div>`;
        }
      });
    }
  } else {
    const emptyMessage = allGarbage
      ? 'Interaction confirmed but detailed function data is not yet available.'
      : isSharedInteraction
        ? 'Shared interactions may not include context-specific functions.'
        : 'No functions associated with this interaction.';
    functionsHTML = `
      <div class="modal-functions-header">Functions</div>
      <div style="padding: var(--space-4); color: var(--color-text-secondary); font-style: italic;">
        ${emptyMessage}
      </div>
    `;
  }

  // === BUILD EXPAND/COLLAPSE FOOTER (if called from node click) ===
  let footerHTML = '';
  if (clickedProteinId) {
    const proteinLabel = clickedProteinId;
    const isMainProtein = clickedProteinId === SNAP.main;
    const isExpanded = expanded.has(clickedProteinId);
    const canExpand = true;
    const hasInteractions = true; // Always true for showInteractionModal (single link exists)

    const safeClickedAttr = escapeHtml(clickedProteinId);
    const siBtnStyle = 'padding: 8px 20px; border: none; border-radius: 6px; font-weight: 500; cursor: pointer; font-size: 14px; font-family: var(--font-sans); transition: background 0.2s;';

    if (isMainProtein) {
      footerHTML = `
        <div class="modal-footer" style="border-top: 1px solid var(--color-border); padding: 16px; background: var(--color-bg-secondary);">
          <button data-action="query" data-protein="${safeClickedAttr}" class="btn-primary" style="${siBtnStyle} background: #10b981; color: white;">
            Find New Interactions
          </button>
        </div>
      `;
    } else {
      footerHTML = `
        <div class="modal-footer" style="border-top: 1px solid var(--color-border); padding: 16px; background: var(--color-bg-secondary);">
          <div style="display: flex; gap: 12px; align-items: center; flex-wrap: wrap;">
            ${!isExpanded && hasInteractions ? `
              <button data-action="expand" data-protein="${safeClickedAttr}" class="btn-primary" style="${siBtnStyle} background: #3b82f6; color: white;">
                Expand
              </button>
            ` : ''}
            ${isExpanded ? `
              <button data-action="collapse" data-protein="${safeClickedAttr}" class="btn-secondary" style="${siBtnStyle} background: #ef4444; color: white;">
                Collapse
              </button>
            ` : ''}
            <button data-action="query" data-protein="${safeClickedAttr}" class="btn-primary" style="${siBtnStyle} background: #10b981; color: white;">
              Query
            </button>
          </div>
          <div style="margin-top: 12px; font-size: 12px; color: var(--color-text-secondary); font-family: var(--font-sans);">
            Expand uses existing data &bull; Query finds new interactions
          </div>
        </div>
      `;
    }
  }

  // === BUILD MODAL TITLE WITH TYPE BADGE ===
  // Determine interaction type and create badge
  const isShared = L._is_shared_link || false;
  // isIndirect already declared at line 5518 - reuse that variable
  const mediatorChain = L.mediator_chain || [];
  const chainDepth = L.depth || 1;

  // Check if THIS interaction's target is a mediator for OTHER indirect interactions
  // (e.g., KEAP1 is mediator in p62→KEAP1→NRF2)
  const isMediator = (tgtName === L.upstream_interactor || srcName === L.upstream_interactor);

  let typeBadge = '';
  if (isShared) {
    typeBadge = '<span class="mechanism-badge" style="background: #9333ea; color: white; font-size: 10px; padding: 3px 8px; margin-left: 12px;">SHARED</span>';
  } else if (isNetEffectInteraction) {
    const chainDisplay = renderCompactChainLabel(L);
    const label = chainDisplay ? `NET EFFECT: ${chainDisplay}` : 'NET EFFECT';
    typeBadge = `<span class="mechanism-badge badge-net" style="font-size: 10px; padding: 3px 8px; margin-left: 12px;">${label}</span>`;
  } else if (isChainHopInteraction) {
    const localPw = L.hop_local_pathway ? ` · ${escapeHtml(L.hop_local_pathway)}` : '';
    const displayHopIndex = getDisplayHopIndex(L);
    const hopLabel = displayHopIndex != null ? `CHAIN HOP ${displayHopIndex + 1}` : 'CHAIN HOP';
    typeBadge = `<span class="mechanism-badge badge-chain" style="font-size: 10px; padding: 3px 8px; margin-left: 12px;">${hopLabel}${localPw}</span>`;
  } else if (isIndirect) {
    // See note in the earlier indirect branch — call buildFullChainPath
    // unconditionally so L.chain_context.full_chain and
    // L._chain_entity.chain_proteins (both arbitrary-length and query-
    // position-agnostic) are honored before the 3-protein fallback.
    const firstChainFunc = functions.find(f => f._context && f._context.type === 'chain' && f._context.chain);
    const chainArrayFromFn = firstChainFunc && firstChainFunc._context.chain ? firstChainFunc._context.chain : null;
    let chainDisplay = buildFullChainPath(SNAP.main, chainArrayFromFn, L);

    if (!chainDisplay && L.upstream_interactor) {
      if (L.upstream_interactor === L.primary) {
        chainDisplay = `${escapeHtml(SNAP.main)} → ${escapeHtml(L.primary)}`;
      } else {
        chainDisplay = `${escapeHtml(SNAP.main)} → ${escapeHtml(L.upstream_interactor)} → ${escapeHtml(L.primary)}`;
      }
    }

    typeBadge = chainDisplay
      ? `<span class="mechanism-badge" style="background: #f59e0b; color: white; font-size: 10px; padding: 3px 8px; margin-left: 12px;">${chainDisplay}</span>`
      : `<span class="mechanism-badge" style="background: #f59e0b; color: white; font-size: 10px; padding: 3px 8px; margin-left: 12px;">INDIRECT</span>`;
  } else if (isMediator) {
    // This protein is a mediator in indirect chains AND this link is direct
    typeBadge = `<span class="mechanism-badge" style="background: #10b981; color: white; font-size: 10px; padding: 3px 8px; margin-left: 12px;">DIRECT</span>
                 <span class="mechanism-badge" style="background: #6366f1; color: white; font-size: 10px; padding: 3px 8px; margin-left: 4px;">MEDIATOR</span>`;
  } else {
    typeBadge = '<span class="mechanism-badge" style="background: #10b981; color: white; font-size: 10px; padding: 3px 8px; margin-left: 12px;">DIRECT</span>';
  }

  // When the arrow was inferred (no recorded upstream arrow), surface a
  // small "inferred" indicator so users don't mistake a default "binds"
  // rendering for real literature evidence. The _arrow_inferred flag is
  // set by services/data_builder.py on shared-link and chain-link
  // fallback paths; absent means the arrow is backed by recorded data.
  const _inferA = _cssVar('--color-inferred-arrow-a', '#f59e0b');
  const _inferB = _cssVar('--color-inferred-arrow-b', '#fbbf24');
  const _badgeText = _cssVar('--color-badge-text', '#ffffff');
  const arrowInferredBadge = L._arrow_inferred
    ? `<span class="mechanism-badge" style="background: repeating-linear-gradient(45deg, ${_inferA}, ${_inferA} 4px, ${_inferB} 4px, ${_inferB} 8px); color: ${_badgeText}; font-size: 10px; padding: 3px 8px; margin-left: 4px;" title="Arrow was not recorded in the source data — displayed as 'binds' by convention. Treat as unverified.">INFERRED ARROW</span>`
    : '';

  let modalTitle = `
    <div style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap;">
      <span style="font-size: 18px; font-weight: 600;">${safeSrc} ${arrowSymbol} ${safeTgt}</span>
      ${typeBadge}
      ${arrowInferredBadge}
    </div>
  `;

  // Add full chain display for ALL indirect interactions
  if (isIndirect && !isChainHopInteraction) {
    let fullChainText = '';
    if (mediatorChain.length > 0) {
      // CRITICAL FIX (Issue #2): Use chain_with_arrows if available for typed arrows
      const chainWithArrows = L.chain_with_arrows || [];

      if (chainWithArrows.length > 0) {
        // CRITICAL FIX (Issue #1): For shared links, use correct protein perspective
        // Check if this is a shared link and reconstruct chain from shared interactor's perspective
        if (isShared && L._shared_between && L._shared_between.length >= 2) {
          // Find the shared interactor (not the main query protein)
          const sharedInteractor = L._shared_between.find(p => p !== SNAP.main);

          if (sharedInteractor) {
            // Filter chain segments to show only those starting from shared interactor
            const relevantSegments = chainWithArrows.filter(seg =>
              seg.from === sharedInteractor || chainWithArrows.indexOf(seg) > chainWithArrows.findIndex(s => s.from === sharedInteractor)
            );

            if (relevantSegments.length > 0) {
              const arrowSymbols = {
                'activates': ' <span style="color:#059669;font-weight:700;">--&gt;</span> ',
                'inhibits': ' <span style="color:#dc2626;font-weight:700;">--|</span> ',
                'binds': ' <span style="color:#7c3aed;font-weight:700;">---</span> ',
                'complex': ' <span style="color:#7c3aed;font-weight:700;">---</span> '
              };

              fullChainText = relevantSegments.map((segment, i) => {
                const arrow = arrowSymbols[segment.arrow] || ' → ';
                if (i === relevantSegments.length - 1) {
                  return escapeHtml(segment.from) + arrow + escapeHtml(segment.to);
                } else {
                  return escapeHtml(segment.from) + arrow;
                }
              }).join('');
            } else {
              // Fallback: shared interactor → target
              fullChainText = `${escapeHtml(sharedInteractor)} → ${escapeHtml(tgtName)}`;
            }
          } else {
            // Couldn't find shared interactor, use default
            fullChainText = chainWithArrows.map((segment, i) => {
              const arrow = arrowSymbols[segment.arrow] || ' → ';
              return i === chainWithArrows.length - 1
                ? escapeHtml(segment.from) + arrow + escapeHtml(segment.to)
                : escapeHtml(segment.from) + arrow;
            }).join('');
          }
        } else {
          // NOT a shared link: Display full chain with typed arrows
          const arrowSymbols = {
            'activates': ' <span style="color:#059669;font-weight:700;">--&gt;</span> ',
            'inhibits': ' <span style="color:#dc2626;font-weight:700;">--|</span> ',
            'binds': ' <span style="color:#7c3aed;font-weight:700;">---</span> ',
            'complex': ' <span style="color:#7c3aed;font-weight:700;">---</span> '
          };

          fullChainText = chainWithArrows.map((segment, i) => {
            const arrow = arrowSymbols[segment.arrow] || ' → ';
            if (i === chainWithArrows.length - 1) {
              // Last segment: show "from arrow to"
              return escapeHtml(segment.from) + arrow + escapeHtml(segment.to);
            } else {
              // Middle segments: only show "from arrow" (to avoid duplication)
              return escapeHtml(segment.from) + arrow;
            }
          }).join('');
        }
      } else {
        // FALLBACK: Generic arrows (old data or no chain_with_arrows)
        // CRITICAL FIX (Issue #1): For shared links, start chain from shared interactor
        let startProtein = SNAP.main;

        if (isShared && L._shared_between && L._shared_between.length >= 2) {
          const sharedInteractor = L._shared_between.find(p => p !== SNAP.main);
          if (sharedInteractor) {
            startProtein = sharedInteractor;
          }
        }

        const fullChain = [startProtein, ...mediatorChain, tgtName];
        fullChainText = fullChain.map(p => escapeHtml(p)).join(' → ');
      }
    } else if (L.upstream_interactor && L.upstream_interactor !== SNAP.main) {
      // Indirect with single upstream (no chain array but has upstream)
      // TODO: Could enhance to look up arrow types here too
      fullChainText = `${escapeHtml(SNAP.main)} → ${escapeHtml(L.upstream_interactor)} → ${escapeHtml(tgtName)}`;
    } else {
      // First-ring indirect: no mediator specified (pathway incomplete)
      fullChainText = `${escapeHtml(SNAP.main)} → ${escapeHtml(tgtName)} <span style="font-style: italic; color: #f59e0b;">(direct mediator unknown)</span>`;
    }

    modalTitle = `
      <div style="display: flex; flex-direction: column; gap: 8px;">
        <div style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap;">
          <span style="font-size: 18px; font-weight: 600;">${safeSrc} ${arrowSymbol} ${safeTgt}</span>
          ${typeBadge}
        </div>
        <div style="font-size: 13px; color: var(--color-text-secondary); font-weight: normal; padding: 4px 8px; background: var(--color-bg-tertiary); border-radius: 4px; border-left: 3px solid #f59e0b;">
          <strong>Full Chain:</strong> ${fullChainText}
        </div>
      </div>
    `;
  }

  // Prepend chain display for indirect interactions
  if (chainTitleHTML) {
    functionsHTML = chainTitleHTML + functionsHTML;
  }

  // Chain Link Detail Section for indirect interactions
  if (isIndirectInteraction && L._chain_entity && L._chain_entity.chain_proteins) {
    const chainProteins = L._chain_entity.chain_proteins;
    let chainLinksHTML = `
      <div style="margin-top:20px;border-top:2px solid var(--color-border,#334155);padding-top:16px;">
        <h4 style="color:var(--color-text-secondary,#94a3b8);font-size:12px;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:12px;">Chain Link Details</h4>
    `;

    for (let i = 0; i < chainProteins.length - 1; i++) {
      const src = chainProteins[i];
      const tgt = chainProteins[i + 1];
      const linkClaims = (claims || []).filter(c =>
        c.function_context === 'chain_derived' || c.function_context === 'direct'
      );

      chainLinksHTML += `
        <div style="margin:8px 0;padding:12px;background:var(--color-bg-secondary,rgba(30,41,59,0.5));border-radius:6px;border-left:3px solid #3b82f6;">
          <div style="font-weight:600;margin-bottom:4px;color:var(--color-text-primary,#e2e8f0);">${escapeHtml(src)} → ${escapeHtml(tgt)}</div>
          <div style="font-size:12px;color:var(--color-text-secondary,#94a3b8);">
            ${linkClaims.length > 0 ? linkClaims.length + ' chain-derived claim(s)' : 'No independent claims yet'}
          </div>
        </div>
      `;
    }
    chainLinksHTML += '</div>';
    functionsHTML += chainLinksHTML;
  }

  // === COMBINE SECTIONS AND DISPLAY ===
  const fullModalContent = chainBannerHTML + interactionMetadataHTML + functionsHTML + footerHTML;
  // Color-code the modal border by interaction type
  const _accentColors = { activates: '#10b981', inhibits: '#ef4444', binds: '#8b5cf6', regulates: '#f59e0b' };
  const _modalAccent = _accentColors[L.arrow || 'binds'] || '#4f46e5';
  openModal(modalTitle, fullModalContent, _modalAccent);
}

/* DEPRECATED: Old interactor modal - now using unified interaction modal for both arrows and nodes */
// showInteractorModal removed - nodes now use showInteractionModal with expand/collapse footer

/* Handle node click - show interaction modal with expand/collapse controls */
async function handleNodeClick(node) {
  try {
    // For pathway-expanded nodes, use originalId to find actual interaction data
    const lookupId = node.originalId || node.id;

    // Find ALL links involving this node (using originalId for pathway-expanded nodes)
    const nodeLinks = links.filter(l => {
      const src = (l.source && l.source.id) ? l.source.id : l.source;
      const tgt = (l.target && l.target.id) ? l.target.id : l.target;
      // Match either the full ID or the originalId
      const srcOriginal = l.source && l.source.originalId;
      const tgtOriginal = l.target && l.target.originalId;
      if (src === lookupId || tgt === lookupId ||
        srcOriginal === lookupId || tgtOriginal === lookupId ||
        src === node.id || tgt === node.id) return true;
      // Query protein owns ALL indirect interactions (discovered from its perspective)
      const ld = l.data || {};
      if (lookupId === SNAP.main && (ld.interaction_type === 'indirect' || ld.type === 'indirect')) return true;
      // Also include indirect interactions where this protein is anywhere in the chain
      if (ld.interaction_type === 'indirect') {
        if (ld.upstream_interactor === lookupId) return true;
        if (chainIncludesNode(ld, lookupId)) return true;
      }
      return false;
    });

    if (nodeLinks.length === 0) {
      // FALLBACK: Try finding interactions in raw SNAP data (Robustness for Card View / Desync)
      if (typeof SNAP !== 'undefined' && SNAP.interactions) {
        const rawInteractions = SNAP.interactions.filter(i => {
          if (i.source === lookupId || i.target === lookupId) return true;
          if (lookupId === SNAP.main && (i.interaction_type === 'indirect' || i.type === 'indirect')) return true;
          if (i.interaction_type === 'indirect') {
            if (i.upstream_interactor === lookupId) return true;
            if (chainIncludesNode(i, lookupId)) return true;
          }
          return false;
        });

        if (rawInteractions.length > 0) {
          const restoredLinks = rawInteractions.map(i => ({
            data: i,
            source: { id: i.source },
            target: { id: i.target },
            arrow: i.arrow,
            direction: i.direction
          }));
          showAggregatedInteractionsModal(restoredLinks, node);
          // Still fetch API data to enrich
          _fetchAndMergeDbInteractions(lookupId, restoredLinks, node);
          return;
        }
      }

      // No local data — show loading state while fetching from DB
      openModal(`Protein: ${escapeHtml(node.label || node.id)}`,
        '<div style="color:#6b7280; padding: 20px; text-align: center;">No interactions found for this protein.</div>');
      _fetchAndMergeDbInteractions(lookupId, [], node);
    } else {
      // Show modal immediately with local data
      showAggregatedInteractionsModal(nodeLinks, node, { loading: true });
      // Async fetch ALL DB interactions and re-render
      _fetchAndMergeDbInteractions(lookupId, nodeLinks, node);
    }
  } catch (err) {
    console.error('Error in handleNodeClick:', err);
    openModal('Error', `<div style="padding:20px;color:red;">Failed to open modal: ${escapeHtml(err.message)}</div>`);
  }
}

/** Fetch all DB interactions for a protein and merge with local links, then re-render modal. */
async function _fetchAndMergeDbInteractions(lookupId, localLinks, node) {
  try {
    const resp = await fetch(`/api/protein/${encodeURIComponent(lookupId)}/interactions`);
    if (!resp.ok) return; // Graceful degradation: keep showing local data

    const data = await resp.json();
    if (!data.interactions || data.interactions.length === 0) {
      // No additional DB data — remove loading spinner if present
      if (modalOpen) showAggregatedInteractionsModal(localLinks, node);
      return;
    }

    // Build set of current network protein IDs for _in_current_network flag
    const networkProteins = new Set();
    if (typeof nodeMap !== 'undefined') {
      for (const [id, n] of nodeMap) {
        networkProteins.add(n.originalId || id);
      }
    }

    // Convert API interactions to link-like objects
    const apiLinks = data.interactions.map(i => {
      const inNetwork = networkProteins.has(i.source) && networkProteins.has(i.target);
      i._in_current_network = inNetwork;
      i._from_db = true;
      return {
        data: i,
        source: { id: i.source, originalId: i.source },
        target: { id: i.target, originalId: i.target },
        arrow: i.arrow,
        direction: i.direction
      };
    });

    // Deduplicate: API data takes precedence (has claims)
    const seen = new Set();
    const merged = [];

    // Add API links first (they have claims)
    for (const link of apiLinks) {
      const d = link.data;
      const itype = (d.interaction_type || d.type || 'direct');
      const shared = d._is_shared_link ? '|shared' : '';
      const key = [d.source, d.target].sort().join('::') + '|' + itype + shared;
      if (!seen.has(key)) {
        seen.add(key);
        merged.push(link);
      }
    }

    // Add local links that aren't already covered
    for (const link of localLinks) {
      const d = link.data || {};
      const src = d.source || (link.source && (link.source.originalId || link.source.id)) || link.source;
      const tgt = d.target || (link.target && (link.target.originalId || link.target.id)) || link.target;
      const itype = (d.interaction_type || d.type || 'direct');
      const shared = d._is_shared_link ? '|shared' : '';
      const key = [src, tgt].sort().join('::') + '|' + itype + shared;
      if (!seen.has(key)) {
        seen.add(key);
        // Mark local-only links as in-network
        if (d) d._in_current_network = true;
        merged.push(link);
      }
    }

    // Re-render modal with merged data (only if modal is still open)
    if (modalOpen) {
      showAggregatedInteractionsModal(merged, node, { fromApi: true });
    }
  } catch (err) {
    console.error('[handleNodeClick] DB fetch failed (graceful degradation):', err);
    // On failure, remove loading spinner by re-rendering with local data
    if (modalOpen && localLinks.length > 0) {
      showAggregatedInteractionsModal(localLinks, node);
    }
  }
}

/**
 * Group interactions by their assigned pathway
 * @param {Array} interactions - Array of interaction objects
 * @returns {Map} Map of pathwayName -> Array of interactions
 */
function groupInteractionsByPathway(interactions) {
    const groups = new Map();

    interactions.forEach(interaction => {
        const data = interaction.data || interaction;
        // Get pathway from step3_finalized_pathway or functions
        const pathwayName = data.step3_finalized_pathway ||
                           data.data?.step3_finalized_pathway ||
                           'Unassigned';

        if (!groups.has(pathwayName)) {
            groups.set(pathwayName, []);
        }
        groups.get(pathwayName).push(interaction);
    });

    return groups;
}

/** Last arguments passed to showAggregatedInteractionsModal — used by the
 *  "Show All / Pathway Only" toggle to re-render the modal in-place. */
let _lastModalArgs = null;

/* Show aggregated modal for nodes with multiple interactions */
function showAggregatedInteractionsModal(nodeLinks, clickedNode, options = {}) {
  // Persist args so the pathway-filter toggle can re-invoke us
  _lastModalArgs = { nodeLinks, clickedNode, options: { ...options } };

  const nodeId = clickedNode.id;
  const nodeLabel = clickedNode.label || nodeId;
  // For pathway-expanded nodes, use originalId to look up actual interaction data
  const lookupId = clickedNode.originalId || nodeLabel;
  const clickedCardContext = clickedNode.cardContext || clickedNode._cardContext || clickedNode;

  // If this is a pathway-expanded node and nodeLinks is empty or only contains pathway links,
  // look up the actual interaction data from SNAP.interactions
  let actualLinks = nodeLinks;
  const hasChainScopedCardContext = clickedCardContext?._chainId != null;
  if (clickedNode.pathwayId && SNAP && SNAP.interactions && !hasChainScopedCardContext) {
    // Find interactions involving this protein (including as mediator in indirect chains)
    const interactionData = SNAP.interactions.filter(interaction => {
      const src = interaction.source || '';
      const tgt = interaction.target || '';
      if (src === lookupId || tgt === lookupId) return true;
      // Query protein owns ALL indirect interactions
      if (lookupId === SNAP.main && (interaction.interaction_type === 'indirect' || interaction.type === 'indirect')) return true;
      // Also include indirect interactions where this protein is anywhere in the chain
      if (interaction.interaction_type === 'indirect') {
        if (interaction.upstream_interactor === lookupId) return true;
        if (chainIncludesNode(interaction, lookupId)) return true;
      }
      return false;
    });

    // Convert SNAP.interactions to link-like objects for the modal
    if (interactionData.length > 0) {
      actualLinks = interactionData.map(interaction => ({
        data: interaction,
        source: { id: interaction.source, originalId: interaction.source },
        target: { id: interaction.target, originalId: interaction.target },
        arrow: interaction.arrow,
        direction: interaction.direction
      }));
    }
  }

  // ═══════════════════════════════════════════════════════════════
  // PATHWAY FILTERING: Filter interactions by pathway context
  // ═══════════════════════════════════════════════════════════════
  // ✅ FIXED: Use _pathwayContext from card view if available
  const currentPathwayId = clickedNode.pathwayId || clickedNode._pathwayContext?.id;
  const isPathwayExpanded = !!currentPathwayId;
  let pathwayFilterIndicatorHTML = '';
  let otherPathwaysHTML = '';
  let chainContextHTML = '';
  let pathwayLabel = '';  // Declare at outer scope for use in renderInteractionSection

  const showAll = !!options.showAll;  // "Show All" toggle overrides pathway filtering

  if (isPathwayExpanded && currentPathwayId) {
    // Get pathway's interactor set
    const pathwayNode = nodeMap.get(currentPathwayId);
    const pathwayInteractors = new Set(pathwayNode?.interactorIds || pathwayToInteractors.get(currentPathwayId) || []);
    // ✅ FIXED: Prioritize _pathwayContext.name from card view
    pathwayLabel = clickedNode._pathwayContext?.name || pathwayNode?.label || currentPathwayId.replace('pathway_', '').replace(/_/g, ' ');  // Assign (not redeclare)

    // Filter to pathway-relevant interactions (skipped when "Show All" is active)
    const unfilteredCount = actualLinks.length;
    if (!showAll && !hasChainScopedCardContext) {
      actualLinks = actualLinks.filter(link => {
        const L = link.data || {};
        const src = L.source || link.source?.originalId || link.source;
        const tgt = L.target || link.target?.originalId || link.target;
        const otherProtein = (src === lookupId) ? tgt : src;

        // P3.3 — chain-link hops must EARN inclusion under the current
        // pathway (was: blanket-kept). The old rule meant a Hippo/
        // Apoptosis/DNA-repair chain that happens to pass through
        // ATXN3 would render under "Protein Quality Control" just
        // because the user expanded PQC. New rule: keep the chain link
        // only if at least one of its hop's pathway labels matches the
        // current pathway, OR if both endpoints are pathway interactors.
        if (getInteractionLocus(L) === 'chain_hop_claim') {
          const chainAssignedHere = pathwayLabelsMatchContext(collectInteractionPathwayLabels(L), pathwayLabel);
          const bothInPathway = pathwayInteractors.has(src) && pathwayInteractors.has(tgt);
          if (chainAssignedHere || bothInPathway) return true;
          // Otherwise drop — this hop's biology belongs in another
          // pathway's expansion, not here.
          return false;
        }

        // Keep if: main protein OR in same pathway OR is shared between pathway interactors
        if (otherProtein === SNAP.main) return true;
        if (pathwayInteractors.has(otherProtein)) return true;
        // For shared links, check if BOTH proteins are in pathway
        if (L._is_shared_link) {
          return pathwayInteractors.has(src) && pathwayInteractors.has(tgt);
        }
        // For indirect interactions: ALWAYS apply pathway filtering when in pathway context
        // This ensures even the query protein's indirects are filtered to pathway-relevant ones
        if (getInteractionLocus(L) === 'net_effect_claim' || L.interaction_type === 'indirect') {
          const indirectTarget = L.primary || tgt;
          const chain = L.mediator_chain || [];

          if (pathwayLabelsMatchContext(collectInteractionPathwayLabels(L), pathwayLabel)) return true;

          // Keep if the indirect TARGET is in this pathway
          if (pathwayInteractors.has(indirectTarget)) return true;

          // Keep if the upstream_interactor (mediator) is in this pathway
          if (L.upstream_interactor && pathwayInteractors.has(L.upstream_interactor)) return true;

          // Keep if any chain member is in this pathway
          // This is CRITICAL for CYR61/CTGF: their chain includes YAP1, a Hippo member
          if (chain.some(m => pathwayInteractors.has(m))) return true;

          // If none of the above, this indirect is not relevant to the current pathway
          return false;
        }
        return false;
      });
    }

    console.log(`[Pathway Filter] ${lookupId}: ${actualLinks.length}/${unfilteredCount} interactions in "${pathwayLabel}"${showAll ? ' (Show All)' : ''}`);

    // Build pathway context indicator with toggle button
    const filterCount = (!showAll && actualLinks.length < unfilteredCount)
      ? `<span style="color: var(--color-text-secondary);">(${actualLinks.length} of ${unfilteredCount})</span>`
      : '';
    const toggleLabel = showAll ? 'Pathway Only' : 'Show All';
    pathwayFilterIndicatorHTML = `
      <div class="pathway-filter-indicator">
        <span>Pathway:</span>
        <span class="pathway-name">${escapeHtml(pathwayLabel)}</span>
        ${filterCount}
        <button class="pathway-filter-toggle" data-action="toggle-pathway-filter">${toggleLabel}</button>
      </div>
    `;

    // Find OTHER pathways this protein appears in (for cross-reference)
    const otherPathways = nodes
      .filter(n => n.type === 'pathway' && n.id !== currentPathwayId)
      .filter(n => {
        const pwInteractors = n.interactorIds || pathwayToInteractors.get(n.id) || new Set();
        return (Array.isArray(pwInteractors) ? pwInteractors.includes(lookupId) : pwInteractors.has(lookupId));
      })
      .map(n => ({ id: n.id, label: n.label }));

    if (otherPathways.length > 0) {
      otherPathwaysHTML = `
        <div class="other-pathways-section">
          <div class="other-pathways-label">Also appears in:</div>
          <div class="other-pathways-tags">
            ${otherPathways.map(p => `
              <button class="pathway-tag" data-action="switch-pathway" data-pathway-id="${escapeHtml(p.id)}" data-current-pathway-id="${escapeHtml(currentPathwayId)}">
                ${escapeHtml(p.label)}
              </button>
            `).join('')}
          </div>
        </div>
      `;
    }
  }

  // ═══════════════════════════════════════════════════════════════
  // CHAIN CONTEXT BANNER: Show full chain for indirect interactors
  // ═══════════════════════════════════════════════════════════════
  const nodeData = clickedNode.interactionData || clickedNode;
  const isIndirectNode = nodeData.interaction_type === 'indirect' || clickedNode.interaction_type === 'indirect';
  const mediator = nodeData.upstream_interactor || clickedNode.upstream_interactor;

  if (isIndirectNode && mediator) {
    const chainWithArrows = nodeData.chain_with_arrows || [];
    let chainDisplay = '';

    if (chainWithArrows.length > 0) {
      // Build typed chain: ATXN3 → PNKP ⊣ ATM
      chainDisplay = chainWithArrows.map((seg, i) => {
        const arrowSymbols = {
          'activates': ' → ',
          'inhibits': ' ⊣ ',
          'binds': ' — ',
          'regulates': ' → '
        };
        const arrow = arrowSymbols[seg.arrow] || ' → ';
        return i === chainWithArrows.length - 1
          ? `${escapeHtml(seg.from)}${arrow}${escapeHtml(seg.to)}`
          : `${escapeHtml(seg.from)}${arrow}`;
      }).join('');
    } else {
      // Fallback to simple chain
      chainDisplay = `${escapeHtml(SNAP.main)} → ${escapeHtml(mediator)} → ${escapeHtml(lookupId)}`;
    }

    chainContextHTML = `
      <div class="chain-context-banner">
        <span class="chain-label">Full Chain:</span>
        <code class="chain-path">${chainDisplay}</code>
      </div>
    `;
  }

  // Split links into "in current network" vs "other known" when API data is present
  const inNetworkLinks = [];
  const otherKnownLinks = [];

  if (options.fromApi) {
    actualLinks.forEach(link => {
      const L = link.data || {};
      if (L._in_current_network !== false) {
        inNetworkLinks.push(link);
      } else {
        otherKnownLinks.push(link);
      }
    });
  } else {
    inNetworkLinks.push(...actualLinks);
  }

  // Helper: group links by biological locus.
  function groupByType(linkList) {
    const direct = [], chain = [], net = [], indirect = [], shared = [];
    linkList.forEach(link => {
      const L = link.data || {};
      const sectionType = getInteractionSectionType(L);
      if (sectionType === 'shared') shared.push(link);
      else if (sectionType === 'chain') chain.push(link);
      else if (sectionType === 'net') net.push(link);
      else if (sectionType === 'indirect') indirect.push(link);
      else direct.push(link);
    });
    return { direct, chain, net, indirect, shared };
  }

  // Belt-and-suspenders dedup for link groups. Even after the backend
  // collapses chain-context entries per (pair, interaction.id) in
  // services/data_builder.py, the in-memory visualizer may reintroduce
  // paired directional D3 links for symmetric rendering. Key each link
  // by its canonical DB identity so the modal never shows the same
  // interaction twice (one A→B row and one B→A row with identical
  // claims). Fall back to a canonical-pair key when _db_id is absent.
  const _canonLinkKey = (link) => {
    const L = link?.data || {};
    // L5.5 — Chain-link rows: include chain_id so the same A↔B pair
    // appearing in two different parent indirects stays distinct. Without
    // this, two chain hops with the same canonical pair (e.g. RNA↔UNC13A
    // from the TDP43→FUS→RNA→UNC13A chain AND from a different chain that
    // also touches RNA→UNC13A) collapse into a single row in the modal,
    // hiding cross-chain biology.
    if (L._db_id != null && L.chain_id != null) {
      return `db:${L._db_id}|chain:${L.chain_id}`;
    }
    if (L._db_id != null) return `db:${L._db_id}`;
    const src = (L.source || link.source?.originalId || link.source || '').toString();
    const tgt = (L.target || link.target?.originalId || link.target || '').toString();
    const pair = [src, tgt].sort().join('|');
    const type = L.interaction_type || L.type || 'unknown';
    const chainSuffix = L.chain_id != null ? `|chain:${L.chain_id}` : '';
    return `pair:${pair}|${type}${chainSuffix}`;
  };
  const _dedupLinks = (arr) => {
    const seen = new Set();
    const out = [];
    for (const link of arr) {
      const k = _canonLinkKey(link);
      if (seen.has(k)) continue;
      seen.add(k);
      out.push(link);
    }
    return out;
  };
  let { direct: directLinks, chain: chainLinks, net: netLinks, indirect: indirectLinks, shared: sharedLinks } = groupByType(inNetworkLinks);
  directLinks = _dedupLinks(directLinks);
  chainLinks = _dedupLinks(chainLinks);
  netLinks = _dedupLinks(netLinks);
  indirectLinks = _dedupLinks(indirectLinks);
  sharedLinks = _dedupLinks(sharedLinks);

  // Build sections HTML
  let sectionsHTML = '';

  // Helper to render a single interaction section
  // Uses lookupId from outer scope to determine perspective
  /**
   * @param {object} link
   * @param {string} sectionType - 'direct'|'indirect'|'shared'
   * @param {string} [pathwayFilterMode='all'] - 'all'|'pathway_only'|'other_only'
   * @returns {{ html: string, totalClaims: number, shownClaims: number }}
   */
  function renderInteractionSection(link, sectionType, pathwayFilterMode) {
    if (!pathwayFilterMode) pathwayFilterMode = 'all';
    const L = link.data || link;  // Link properties are directly on link object or in data

    // Use semantic source/target (biological direction) instead of D3's geometric source/target
    // For pathway-expanded nodes, use originalId (the actual protein name) instead of the pathway-prefixed ID
    let srcName = L.semanticSource || ((link.source && link.source.id) ? link.source.id : link.source);
    let tgtName = L.semanticTarget || ((link.target && link.target.id) ? link.target.id : link.target);
    // Strip pathway prefix if present (e.g., "TUBULIN@pathway_..." -> "TUBULIN")
    if (link.source && link.source.originalId) srcName = link.source.originalId;
    if (link.target && link.target.originalId) tgtName = link.target.originalId;
    // Also check for @pathway_ pattern in string IDs
    if (typeof srcName === 'string' && srcName.includes('@pathway_')) srcName = srcName.split('@')[0];
    if (typeof tgtName === 'string' && tgtName.includes('@pathway_')) tgtName = tgtName.split('@')[0];

    // PERSPECTIVE TRANSFORMATION for indirect interactors
    // When viewing an indirect interactor (e.g., ATM), show interaction from its perspective
    // Instead of "ATXN3 → PNKP", show "PNKP → ATM"
    const locus = getInteractionLocus(L);
    const isChainHop = locus === 'chain_hop_claim';
    const isNetEffect = locus === 'net_effect_claim';
    const isIndirect = !isChainHop && (L.interaction_type === 'indirect' || isNetEffect);
    const upstream = L.upstream_interactor;
    const indirectTarget = L.primary || tgtName;
    const isViewingIndirectInteractor = isIndirect && lookupId === indirectTarget && lookupId !== SNAP.main;
    // NEW: When viewing the MEDIATOR of an indirect chain (e.g., RHEB in
    // ATXN3→RHEB→MTOR), show the TRUE indirect endpoints (ATXN3→MTOR),
    // not the mediator perspective. ``chainIncludesNode`` handles chains
    // where the query can sit at any position.
    const isViewingMediator = isIndirect && !isViewingIndirectInteractor &&
      lookupId !== SNAP.main && (upstream === lookupId || chainIncludesNode(L, lookupId));
    // When the query protein itself is viewing its own indirect interactions
    const isQueryProteinViewing = isIndirect && lookupId === SNAP.main;

    let displaySrc = srcName;
    let displayTgt = tgtName;

    if (isQueryProteinViewing) {
      // ATXN3 viewing its own indirect: show "ATXN3 → CTGF" not "YAP1 → CTGF"
      displaySrc = SNAP.main;
      displayTgt = indirectTarget;
    } else if (isViewingMediator) {
      // RHEB is mediator in ATXN3→RHEB→MTOR: show true indirect pair "ATXN3 → MTOR"
      displaySrc = SNAP.main;
      displayTgt = indirectTarget;
    } else if (isViewingIndirectInteractor && upstream) {
      // When viewing indirect interactor's modal, show: query_protein → indirect_target
      // Use SNAP.main (the query protein that initiated the chain) not upstream (the mediator)
      displaySrc = SNAP.main;
      displayTgt = indirectTarget;
    }

    const safeSrc = escapeHtml(displaySrc || '-');
    const safeTgt = escapeHtml(displayTgt || '-');

    // S1: all directions are asymmetric. Arrow is always source → target.
    const direction = L.direction || link.direction || 'main_to_primary';
    const arrowSymbol = '→';

    // Type badge
    let typeBadgeHTML = '';
    if (sectionType === 'shared') {
      typeBadgeHTML = '<span class="mechanism-badge" style="background: #9333ea; color: white;">SHARED</span>';
    } else if (sectionType === 'chain' || isChainHop) {
      const displayHopIndex = getDisplayHopIndex(L);
      const hopLabel = displayHopIndex != null ? `CHAIN HOP ${displayHopIndex + 1}` : 'CHAIN HOP';
      const pathwayLabel = L.hop_local_pathway || L.chain_context_pathway || '';
      typeBadgeHTML = `<span class="mechanism-badge badge-chain" title="${escapeHtml(pathwayLabel || 'Adjacent hop within an indirect chain')}">${hopLabel}</span>`;
    } else if (sectionType === 'net' || isNetEffect) {
      const via = Array.isArray(L.via) && L.via.length ? ` via ${L.via.map(escapeHtml).join(' → ')}` : '';
      typeBadgeHTML = `<span class="mechanism-badge badge-net">NET EFFECT${via}</span>`;
    } else if (sectionType === 'indirect') {
      // Build chain path display for INDIRECT label
      // PERSPECTIVE-AWARE: Show relevant portion based on which protein we're viewing
      let chainDisplay = '';

      if (isViewingIndirectInteractor && upstream) {
        // Viewing indirect interactor (e.g., BCL2): show full chain "ATXN3 → PARK2 → BCL2"
        chainDisplay = `${escapeHtml(SNAP.main)} → ${escapeHtml(upstream)} → ${escapeHtml(indirectTarget)}`;
      } else {
        // Viewing from main protein's perspective: show full chain
        const functions = L.functions || [];
        const firstChainFunc = functions.find(f => f._context && f._context.type === 'chain' && f._context.chain);
        if (firstChainFunc && firstChainFunc._context.chain) {
          chainDisplay = buildFullChainPath(SNAP.main, firstChainFunc._context.chain, L);
        }

        // Fallback: use upstream_interactor if no chain found
        if (!chainDisplay && upstream) {
          chainDisplay = `${escapeHtml(SNAP.main)} → ${escapeHtml(upstream)} → ${escapeHtml(indirectTarget)}`;
        }
      }

      typeBadgeHTML = chainDisplay
        ? `<span class="mechanism-badge" style="background: #f59e0b; color: white;">${chainDisplay}</span>`
        : `<span class="mechanism-badge" style="background: #f59e0b; color: white;">INDIRECT</span>`;
    } else {
      typeBadgeHTML = '<span class="mechanism-badge" style="background: #10b981; color: white;">DIRECT</span>';
    }

    // Interaction title - use perspective-aware display names
    const interactionTitle = `${safeSrc} ${arrowSymbol} ${safeTgt}`;

    // Arrow type badge
    const arrow = L.arrow || link.arrow || 'binds';
    const normalizedArrow = arrow === 'activates' || arrow === 'activate' ? 'activates'
      : arrow === 'inhibits' || arrow === 'inhibit' ? 'inhibits'
        : arrow === 'regulates' || arrow === 'regulate' || arrow === 'modulates' ? 'regulates'
          : 'binds';
    const isDarkMode = document.body.classList.contains('dark-mode');
    const arrowColors = isDarkMode ? {
      'activates': { bg: '#065f46', text: '#a7f3d0', border: '#047857', label: 'ACTIVATES' },
      'inhibits': { bg: '#991b1b', text: '#fecaca', border: '#b91c1c', label: 'INHIBITS' },
      'regulates': { bg: '#92400e', text: '#fde68a', border: '#b45309', label: 'REGULATES' },
      'binds': { bg: '#5b21b6', text: '#ddd6fe', border: '#6d28d9', label: 'BINDS' }
    } : {
      'activates': { bg: '#d1fae5', text: '#047857', border: '#059669', label: 'ACTIVATES' },
      'inhibits': { bg: '#fee2e2', text: '#b91c1c', border: '#dc2626', label: 'INHIBITS' },
      'regulates': { bg: '#fef3c7', text: '#92400e', border: '#f59e0b', label: 'REGULATES' },
      'binds': { bg: '#ede9fe', text: '#6d28d9', border: '#7c3aed', label: 'BINDS' }
    };
    const colors = arrowColors[normalizedArrow];

    // Functions
    function deduplicateFunctions(functionArray) {
      if (!Array.isArray(functionArray)) return [];
      const seen = new Set();
      return functionArray.filter(fn => {
        if (!fn) return false;
        const key = `${fn.function || ''}|${fn.arrow || ''}|${fn.cellular_process || ''}|${fn.pathway || ''}`;
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      });
    }

    // Prefer claims (atomic, from interaction_claims table) over merged JSONB functions
    const claims = Array.isArray(L.claims) ? L.claims : [];
    const rawFunctions = Array.isArray(L.functions) ? L.functions : [];

    // Deduplicate claims by VISIBLE CONTENT.
    //
    // Key = function_name + mechanism + pathway_name.
    //   - `arrow` dropped: a claim whose arrow drifted between writes
    //     ("inhibits" vs "regulates") is the same biology — showing it
    //     twice with two arrow labels is the bug the user hit.
    //   - `claim.id` dropped as the PRIMARY key: two DB rows can share
    //     visible content but differ only in `function_context`
    //     ("direct" vs "chain_derived") because the unique index
    //     `uq_claim_interaction_fn_pw_ctx` (models.py:714) COALESCEs
    //     function_context into the uniqueness key. The modal never
    //     surfaces function_context, so the user sees identical text
    //     twice. Content-based dedup collapses those.
    //   - ID is still useful when content is empty (new rows without
    //     mechanism/pathway yet); append it as a tiebreak only then.
    const seenClaimNames = new Set();
    const uniqueClaims = claims.filter(c => {
        const content = `${c.function_name || ''}|${c.mechanism || ''}|${c.pathway_name || ''}`;
        // Content-only fallback when all three fields are blank — use id
        // so genuinely-distinct "empty" placeholders don't collapse into one.
        const key = content.replace(/\|/g, '').trim()
            ? content
            : `id:${c.id != null ? c.id : Math.random()}`;
        if (seenClaimNames.has(key)) return false;
        seenClaimNames.add(key);
        return true;
    });

    const functions = uniqueClaims.length > 0
        ? uniqueClaims.map(c => ({
            function: c.function_name,
            arrow: c.arrow,
            cellular_process: c.mechanism,
            effect_description: c.effect_description,
            biological_consequence: c.biological_consequences,
            specific_effects: c.specific_effects,
            evidence: c.evidence,
            pmids: c.pmids,
            pathway: c.pathway_name,
            _hierarchy: c._hierarchy || [],
            _interaction_pathways: c._interaction_pathways,
            interaction_effect: c.interaction_effect,
            function_effect: c.interaction_effect,
            interaction_direction: c.direction,
            direction: c.direction,
            locus: c.locus,
            _context: (c.context_data && typeof c.context_data === 'object' && !Array.isArray(c.context_data)) ? c.context_data : null,
            function_context: c.function_context,
            _claim_id: c.id,
        }))
        : deduplicateFunctions(rawFunctions);

    // Filter out auto-generated garbage claim names
    const _garbageClaimPattern = /^__fallback__$|^(activates?|inhibits?|binds?|regulates?|interacts?) interaction$|^\w+ (interacts?\s+with|activates?|inhibits?|binds?|regulates?) \w+$/i;
    const filteredFunctions = functions.filter(f => !_garbageClaimPattern.test(f.function || ''));
    const allGarbage = filteredFunctions.length === 0 && functions.length > 0;
    const totalClaimCount = filteredFunctions.length;

    // Apply pathway filter if in pathway mode
    const pathwayCtxForFilter = currentPathwayId ? { id: currentPathwayId, name: pathwayLabel } : null;
    let displayFunctions;
    if (pathwayFilterMode === 'pathway_only' && pathwayCtxForFilter) {
      displayFunctions = filteredFunctions.filter(fn => {
        const fnPw = fn.pathway || '';
        const fnHierarchy = fn._hierarchy || fn.hierarchy || [];
        const fnInteractionPws = Array.isArray(fn._interaction_pathways) ? fn._interaction_pathways : null;
        // Include claims that match the pathway OR have no pathway assignment
        // (unassigned claims belong to this interaction which IS in the pathway)
        if (!fnPw) return true;
        return isPathwayInContext(fnPw, fnHierarchy, pathwayCtxForFilter, fnInteractionPws);
      });
    } else if (pathwayFilterMode === 'other_only' && pathwayCtxForFilter) {
      displayFunctions = filteredFunctions.filter(fn => {
        const fnPw = fn.pathway || '';
        const fnHierarchy = fn._hierarchy || fn.hierarchy || [];
        const fnInteractionPws = Array.isArray(fn._interaction_pathways) ? fn._interaction_pathways : null;
        // Exclude unassigned claims from "other" section (they're shown in "pathway_only")
        if (!fnPw) return false;
        return !isPathwayInContext(fnPw, fnHierarchy, pathwayCtxForFilter, fnInteractionPws);
      });
    } else {
      displayFunctions = filteredFunctions;
    }

    // Determine protein names for function rendering
    // CRITICAL FIX: For indirect interactions viewed from the indirect interactor's perspective,
    // use the perspective-transformed display names (mediator → indirect) instead of (main → mediator)
    let functionMainProtein, functionInteractorProtein;
    if (isViewingMediator) {
      // For RHEB (mediator in ATXN3→RHEB→MTOR): show functions as ATXN3 → MTOR (true indirect pair)
      functionMainProtein = displaySrc;      // ATXN3 (the main protein)
      functionInteractorProtein = displayTgt; // MTOR (the indirect target)
    } else if (isViewingIndirectInteractor && upstream) {
      // For ATM (indirect via PNKP): show functions as PNKP → ATM
      functionMainProtein = displaySrc;      // PNKP (the mediator)
      functionInteractorProtein = displayTgt; // ATM (the indirect target)
    } else {
      // Standard case: use the interaction's actual source/target (not SNAP.main)
      // This ensures BCL2's modal shows "PARK2 → BCL2" not "ATXN3 → PARK2"
      functionMainProtein = displaySrc;
      functionInteractorProtein = displayTgt;
    }

    let functionsHTML = '';
    if (displayFunctions.length > 0) {
      // Build pathway context for badge display (if in pathway mode)
      const pathwayContextForFunctions = currentPathwayId ? { id: currentPathwayId, name: pathwayLabel } : null;

      functionsHTML = displayFunctions.map(fn => {
        // Pass appropriate proteins for direction resolution based on interaction type
        // FIXED: Pass 'main_to_primary' as direction because functionMainProtein/functionInteractorProtein
        // are already resolved to biological source/target (displaySrc/displayTgt).
        // renderExpandableFunction's swap logic assumes mainProtein=SNAP.main, but here
        // mainProtein=displaySrc (already biological source), so we must prevent the swap.
        return renderExpandableFunction(fn, functionMainProtein, functionInteractorProtein, arrow, 'main_to_primary', pathwayContextForFunctions, L.step3_finalized_pathway);
      }).join('');
    } else {
      // Fallback: show interaction-level summary if available
      const fallbackSummary = L.support_summary || L.summary || '';
      if (fallbackSummary) {
        functionsHTML = `
          <div style="padding: var(--space-4);">
            <div class="modal-detail-label">SUMMARY</div>
            <div class="modal-detail-value">${escapeHtml(fallbackSummary)}</div>
          </div>`;
      } else {
        const emptyMessage = allGarbage
          ? 'Interaction confirmed but detailed function data is not yet available.'
          : sectionType === 'shared'
            ? 'Shared interactions may not include context-specific functions.'
            : 'No functions associated with this interaction.';
        functionsHTML = `
          <div style="padding: var(--space-4); color: var(--color-text-secondary); font-style: italic;">
            ${emptyMessage}
          </div>
        `;
      }
    }

    // Pathway filter hid every claim for this section — but the interaction
    // itself still exists and was drawn in the card view. Previously we
    // returned empty HTML, which meant whole rows (esp. indirect/chain ones)
    // silently vanished from the modal. Instead, emit a compact stub that
    // shows the interaction header and a note so the user can still see it.
    if (pathwayFilterMode !== 'all' && displayFunctions.length === 0) {
      const hint = pathwayFilterMode === 'pathway_only'
        ? 'Claims for this interaction are assigned to other pathways — click "Show All" above to view them.'
        : 'No claims outside the current pathway filter for this interaction.';
      const stubHTML = `
        <div class="interaction-expandable-row" style="margin-bottom: 12px; border: 1px dashed var(--color-border); border-radius: 8px; padding: 10px 14px; background: var(--color-bg-secondary); opacity: 0.85;">
          <div style="display: flex; align-items: center; gap: 12px; font-size: 13px;">
            <span style="font-weight: 600;">${interactionTitle}</span>
            <span class="interaction-type-badge" style="display: inline-block; padding: 2px 8px; background: ${colors.bg}; color: ${colors.text}; border: 1px solid ${colors.border}; border-radius: 4px; font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.3px;">${colors.label}</span>
            <span style="color: var(--color-text-secondary); font-style: italic; font-size: 12px;">${hint}</span>
          </div>
        </div>`;
      return { html: stubHTML, shownClaims: 0, totalClaims: totalClaimCount };
    }

    const sectionHTML = `
      <div class="interaction-expandable-row" style="margin-bottom: 16px; border: 1px solid var(--color-border); border-radius: 8px; overflow: hidden; transition: all 0.2s ease;">
        <div class="interaction-row-header" role="button" tabindex="0" aria-expanded="false" style="padding: 12px 16px; background: var(--color-bg-secondary); display: flex; align-items: center; justify-content: space-between; gap: 12px; cursor: pointer; transition: background 0.2s;">
          <div style="display: flex; align-items: center; gap: 12px;">
            <div class="interaction-expand-icon" style="font-size: 12px; color: var(--color-text-secondary); width: 20px; transition: transform 0.2s;">▼</div>
            <span style="font-weight: 600; font-size: 14px;">${interactionTitle}</span>
            ${typeBadgeHTML}
            <span class="interaction-type-badge" style="display: inline-block; padding: 2px 8px; background: ${colors.bg}; color: ${colors.text}; border: 1px solid ${colors.border}; border-radius: 4px; font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.3px;">
              ${colors.label}
            </span>
          </div>
        </div>
        <div class="interaction-expanded-content" style="max-height: 0; opacity: 0; overflow: hidden; transition: max-height 0.3s ease, opacity 0.2s ease;">
          <div style="padding: 16px; border-top: 1px solid var(--color-border);">
            ${(() => {
              // Drop the SUMMARY block when it's just a placeholder
              // stub ("Discovered via chain resolution"). The backend
              // still emits support_summary on chain-promoted
              // interactors for context; the user doesn't want to see
              // the same placeholder line prefixing every row.
              const sum = L.support_summary || '';
              if (!sum || _isPlaceholder(sum)) return '';
              const relevant = !SNAP?.main ||
                displaySrc === SNAP.main || displayTgt === SNAP.main ||
                !sum.toLowerCase().includes(SNAP.main.toLowerCase());
              return relevant ? `
              <div style="margin-bottom: 16px;">
                <div class="modal-detail-label">SUMMARY</div>
                <div class="modal-detail-value">${escapeHtml(sum)}</div>
              </div>` : '';
            })()}
            ${(() => {
              // Check if this hop is on the unrecoverable list — pipeline
              // tried multiple times and could not generate a claim. Render
              // a clear "no published evidence" state instead of leaving
              // the user staring at silence.
              const _diag = (typeof SNAP !== 'undefined' && SNAP && SNAP._diagnostics) || {};
              const _unrecoverable = _diag.chain_pair_unrecoverable || [];
              const _pairFwd = `${L.upstream_interactor || ''}->${L.primary || ''}`;
              const _pairRev = `${L.primary || ''}->${L.upstream_interactor || ''}`;
              const _isUnrecoverable = _unrecoverable.includes(_pairFwd) || _unrecoverable.includes(_pairRev);
              if (_isUnrecoverable && displayFunctions.length === 0) {
                return `
                  <div class="modal-empty-evidence" role="note" aria-live="polite">
                    <span class="icon" aria-hidden="true">⚠</span>
                    No published evidence found for this chain hop. The pipeline
                    attempted ${escapeHtml(_pairFwd)} multiple times via the
                    chain-claim recovery path but Gemini could not produce a
                    citable claim. The hop is preserved for chain topology,
                    but the biology beyond this link is uncertain.
                  </div>
                `;
              }
              if (L._is_stub_hop && displayFunctions.length === 0) {
                return `
                  <div style="padding: 12px; border: 1px dashed var(--color-border); border-radius: 6px; color: var(--color-text-secondary); font-size: 13px; font-style: italic;">
                    No specific claim was generated for this chain hop.
                    The interaction is inferred from chain context
                    (${escapeHtml(L._stub_reason || 'chain_hop_no_llm_claim')}).
                  </div>
                `;
              }
              return `
                <div class="modal-functions-header" style="font-size: 16px; margin-bottom: 12px;">${uniqueClaims.length > 0 ? 'Scientific Claims' : 'Biological Functions'} (${displayFunctions.length})</div>
                ${functionsHTML}
              `;
            })()}
          </div>
        </div>
      </div>
    `;
    return { html: sectionHTML, shownClaims: displayFunctions.length, totalClaims: totalClaimCount };
  }

  // Render interaction sections — when pathway context exists, split into
  // pathway-specific claims (shown) vs other claims (collapsed at bottom).
  // When "Show All" is active, treat as unfiltered so all claims render inline.
  const hasPathwayFilter = !!currentPathwayId && !showAll;
  let otherClaimsHTML = '';  // Accumulates non-pathway claims for collapsed section
  let otherClaimsCount = 0;

  function renderLinkGroup(links, sectionType, headerClass, headerLabel) {
    if (links.length === 0) return;

    let groupMainHTML = '';
    let groupOtherHTML = '';
    let mainCount = 0;

    links.forEach(link => {
      if (hasPathwayFilter) {
        // Pathway mode: split claims
        const main = renderInteractionSection(link, sectionType, 'pathway_only');
        const other = renderInteractionSection(link, sectionType, 'other_only');
        if (main.html) { groupMainHTML += main.html; mainCount++; }
        if (other.html) { groupOtherHTML += other.html; otherClaimsCount += other.shownClaims; }
      } else {
        // No pathway context: show everything
        const result = renderInteractionSection(link, sectionType, 'all');
        groupMainHTML += result.html;
        mainCount++;
      }
    });

    // Add main (pathway-matching) content to sectionsHTML
    if (groupMainHTML) {
      sectionsHTML += `<div class="modal-section-header modal-section-header--${headerClass}">
        <h3><span class="modal-section-header__dot"></span> ${headerLabel} (${mainCount})</h3>
      </div>`;
      sectionsHTML += groupMainHTML;
    }

    // Accumulate other content for the collapsed section
    if (groupOtherHTML) {
      otherClaimsHTML += `<div class="modal-section-header modal-section-header--${headerClass}" style="margin-top:8px;">
        <h3 style="font-size:13px;"><span class="modal-section-header__dot"></span> ${headerLabel}</h3>
      </div>`;
      otherClaimsHTML += groupOtherHTML;
    }
  }

  renderLinkGroup(directLinks, 'direct', 'direct', 'DIRECT PAIR CLAIMS');
  renderLinkGroup(chainLinks, 'chain', 'chain', 'CHAIN-HOP CLAIMS');
  renderLinkGroup(netLinks, 'net', 'net', 'NET-EFFECT CLAIMS');
  renderLinkGroup(indirectLinks, 'indirect', 'indirect', 'INDIRECT INTERACTIONS');
  renderLinkGroup(sharedLinks, 'shared', 'shared', 'SHARED INTERACTIONS');

  // "Other Known Interactions" section (from DB, not in current network)
  if (otherKnownLinks.length > 0) {
    const { direct: otherDirect, chain: otherChain, net: otherNet, indirect: otherIndirect, shared: otherShared } = groupByType(otherKnownLinks);

    sectionsHTML += `<div class="modal-section-header modal-section-header--other" style="margin-top: 32px;">
      <h3>
        <span class="mechanism-badge" style="background: #64748b; color: white; font-size: 10px; padding: 2px 6px; border-radius: 4px;">DATABASE</span>
        OTHER KNOWN INTERACTIONS (${otherKnownLinks.length})
      </h3>
      <div style="font-size: 12px; color: var(--color-text-secondary); margin-top: 4px;">From previous queries — not in the current network graph</div>
    </div>`;

    const otherAll = [...otherDirect, ...otherChain, ...otherNet, ...otherIndirect, ...otherShared];
    otherAll.forEach(link => {
      const L = link.data || {};
      const sType = getInteractionSectionType(L);
      const result = renderInteractionSection(link, sType, 'all');
      sectionsHTML += result.html;
    });
  }

  // Loading spinner for async DB fetch
  if (options.loading) {
    sectionsHTML += `
      <div id="modal-db-loading" style="padding: 16px; text-align: center; color: var(--color-text-secondary); font-size: 13px;">
        <span style="display: inline-block; width: 14px; height: 14px; border: 2px solid #d1d5db; border-top-color: #3b82f6; border-radius: 50%; animation: spin 0.8s linear infinite; vertical-align: middle; margin-right: 8px;"></span>
        Loading all interactions from database...
      </div>
      <style>@keyframes spin { to { transform: rotate(360deg); } }</style>
    `;
  }

  // Expand/collapse footer
  // For pathway-expanded nodes, use lookupId for queries
  const isPathwayNode = !!clickedNode.pathwayId;
  const queryProteinId = lookupId;  // Use original protein name for queries
  const isMainProtein = lookupId === SNAP.main;
  const isExpanded = expanded.has(lookupId) || expanded.has(nodeId);
  const canExpand = true;
  const hasInteractions = actualLinks.length > 0;

  const safeProteinAttr = escapeHtml(queryProteinId);
  const btnStyle = 'padding: 8px 20px; border: none; border-radius: 6px; font-weight: 500; cursor: pointer; font-size: 14px; font-family: var(--font-sans); transition: background 0.2s;';

  let footerHTML = '';
  if (isMainProtein) {
    footerHTML = `
      <div class="modal-footer" style="border-top: 1px solid var(--color-border); padding: 16px; background: var(--color-bg-secondary);">
        <button data-action="query" data-protein="${safeProteinAttr}" class="btn-primary" style="${btnStyle} background: #10b981; color: white;">
          Find New Interactions
        </button>
      </div>
    `;
  } else if (isPathwayNode) {
    footerHTML = `
      <div class="modal-footer" style="border-top: 1px solid var(--color-border); padding: 16px; background: var(--color-bg-secondary);">
        <button data-action="query" data-protein="${safeProteinAttr}" class="btn-primary" style="${btnStyle} background: #10b981; color: white;">
          Query ${escapeHtml(queryProteinId)}
        </button>
      </div>
    `;
  } else {
    footerHTML = `
      <div class="modal-footer" style="border-top: 1px solid var(--color-border); padding: 16px; background: var(--color-bg-secondary);">
        <div style="display: flex; gap: 12px; align-items: center; flex-wrap: wrap;">
          ${!isExpanded && hasInteractions ? `
            <button data-action="expand" data-protein="${safeProteinAttr}" class="btn-primary" style="${btnStyle} background: #3b82f6; color: white;">
              Expand
            </button>
          ` : ''}
          ${isExpanded ? `
            <button data-action="collapse" data-protein="${safeProteinAttr}" class="btn-secondary" style="${btnStyle} background: #ef4444; color: white;">
              Collapse
            </button>
          ` : ''}
          <button data-action="query" data-protein="${safeProteinAttr}" class="btn-primary" style="${btnStyle} background: #10b981; color: white;">
            Query
          </button>
        </div>
        <div style="margin-top: 12px; font-size: 12px; color: var(--color-text-secondary); font-family: var(--font-sans);">
          Expand uses existing data &bull; Query finds new interactions
        </div>
      </div>
    `;
  }

  // Build modal title - show mediator relationship for indirect interactors
  let titleDisplay = nodeLabel;
  if (isIndirectNode && mediator) {
    // For indirect interactors, show the direct relationship: "PNKP → ATM"
    titleDisplay = `${mediator} → ${nodeLabel}`;
  }

  // Count unique interactions by _db_id to avoid inflated counts from duplicates
  const uniqueInteractionCount = new Set(
    actualLinks.map(l => (l.data || {})?._db_id).filter(Boolean)
  ).size || actualLinks.length;
  const modalTitle = `${escapeHtml(titleDisplay)} - Interactions (${uniqueInteractionCount})`;

  // Build collapsed "Other Interactions & Claims" section if pathway filtering produced non-pathway content
  let otherCollapsedHTML = '';
  if (hasPathwayFilter && otherClaimsHTML) {
    otherCollapsedHTML = `
      <div style="margin-top: 24px; border: 1px solid var(--color-border); border-radius: 8px; overflow: hidden;">
        <div class="other-claims-toggle" style="padding: 12px 16px; background: var(--color-bg-secondary); cursor: pointer; display: flex; align-items: center; gap: 10px; user-select: none; transition: background 0.2s;"
             onclick="(function(el){
               var content = el.nextElementSibling;
               var arrow = el.querySelector('.other-claims-arrow');
               if (content.style.display === 'none') {
                 content.style.display = 'block';
                 arrow.textContent = '▼';
               } else {
                 content.style.display = 'none';
                 arrow.textContent = '▶';
               }
             })(this)">
          <span class="other-claims-arrow" style="font-size: 11px; color: var(--color-text-secondary); width: 16px;">▶</span>
          <span style="font-weight: 600; font-size: 14px; color: var(--color-text-secondary);">Other Interactions & Claims (${otherClaimsCount})</span>
        </div>
        <div style="display: none; padding: 16px; border-top: 1px solid var(--color-border);">
          ${otherClaimsHTML}
        </div>
      </div>
    `;
  }

  // Upstream-regulator banner — rendered ONLY when the user clicked
  // the query protein's own node and the ITER0 upstream-context
  // iteration populated SNAP.upstream_of_main. Surfaces proteins that
  // act ON the query (kinases-of, ligases-of, upstream regulators) so
  // users don't have to crack the JSON payload to see them. Silent on
  // any other node's modal.
  let upstreamHeaderHTML = '';
  try {
    const isQueryNode = nodeId && SNAP && SNAP.main &&
      String(nodeId).toUpperCase() === String(SNAP.main).toUpperCase();
    const upstream = (SNAP && Array.isArray(SNAP.upstream_of_main))
      ? SNAP.upstream_of_main.filter(p => p && typeof p === 'string')
      : [];
    if (isQueryNode && upstream.length > 0) {
      const chips = upstream
        .map(p => `<span style="display:inline-block;padding:3px 10px;margin:0 6px 4px 0;border-radius:12px;background:var(--color-bg-tertiary,#f3f4f6);border:1px solid var(--color-border-subtle,#e5e7eb);font-size:12px;font-family:var(--font-mono,monospace);">${escapeHtml(p)}</span>`)
        .join('');
      upstreamHeaderHTML = `
        <div style="margin-bottom:16px;padding:12px 14px;border-radius:8px;background:var(--color-bg-secondary,#f8f9fa);border:1px solid var(--color-border-subtle,#e5e7eb);">
          <div style="font-size:12px;font-weight:600;color:var(--color-text-secondary,#6b7280);text-transform:uppercase;letter-spacing:0.04em;margin-bottom:8px;">
            🔼 Upstream regulators of ${escapeHtml(SNAP.main)}
          </div>
          <div>${chips}</div>
          <div style="margin-top:6px;font-size:11px;color:var(--color-text-secondary,#6b7280);font-style:italic;">
            These proteins act on ${escapeHtml(SNAP.main)} (discovered in the upstream-context pass).
          </div>
        </div>`;
    }
  } catch (_e) {
    // Never let the banner block modal rendering.
    upstreamHeaderHTML = '';
  }

  // Assemble modal content:
  // 0. Upstream regulators (only on query-node modal; see above)
  // 1. Pathway filter indicator (if filtering applied)
  // 2. Chain context banner (for indirect interactors)
  // 3. Interaction sections (pathway-filtered if applicable)
  // 4. Collapsed "Other" section (if pathway filtering)
  // 5. Other pathways section (clickable tags)
  // 6. Footer (expand/collapse/query buttons)
  const modalContent = upstreamHeaderHTML + pathwayFilterIndicatorHTML + chainContextHTML + sectionsHTML + otherCollapsedHTML + otherPathwaysHTML + footerHTML;

  openModal(modalTitle, modalContent);

  // Auto-expand the first interaction row for better UX
  requestAnimationFrame(() => {
    const firstRow = document.querySelector('.interaction-expandable-row');
    if (firstRow && !firstRow.classList.contains('expanded')) {
      firstRow.classList.add('expanded');
      const content = firstRow.querySelector('.interaction-expanded-content');
      if (content) {
        content.style.maxHeight = content.scrollHeight + 'px';
        content.style.opacity = '1';
        // After transition, switch to max-height:none + overflow:visible
        // so child function/claim rows can expand without being clipped
        const onEnd = (evt) => {
          if (evt.propertyName !== 'max-height') return;
          if (firstRow.classList.contains('expanded')) {
            content.style.maxHeight = 'none';
            content.style.overflow = 'visible';
          }
          content.removeEventListener('transitionend', onEnd);
        };
        content.addEventListener('transitionend', onEnd);
      }
      const icon = firstRow.querySelector('.interaction-expand-icon');
      if (icon) icon.style.transform = 'rotate(180deg)';
    }
  });
}

/* Helper functions for expand/collapse from modal */
function handleExpandFromModal(proteinId) {
  closeModal();
  const node = nodeMap.get(proteinId); // PERFORMANCE: O(1) lookup
  if (node) {
    expandInteractor(node);
  }
}

function handleCollapseFromModal(proteinId) {
  closeModal();
  collapseInteractor(proteinId);
}

/**
 * Switch from one expanded pathway to another (for "Also appears in" tags)
 * Collapses current pathway and expands the new one
 */
function switchToPathway(newPathwayId, currentPathwayId) {
  closeModal();

  // Find and collapse current pathway
  if (currentPathwayId) {
    const currentPathwayNode = nodeMap.get(currentPathwayId);
    if (currentPathwayNode && currentPathwayNode.expanded) {
      collapsePathway(currentPathwayNode);
    }
  }

  // Find and expand new pathway
  const newPathwayNode = nodeMap.get(newPathwayId);
  if (newPathwayNode) {
    // Small delay for visual feedback
    setTimeout(() => {
      expandPathway(newPathwayNode);
      updateSimulation();
      renderGraph();
    }, 200);
  }
}

async function handleQueryFromModal(proteinId) {
  closeModal();

  // Get configuration from localStorage
  const queryConfig = {
    interactor_rounds: parseInt(localStorage.getItem('interactor_rounds')) || 3,
    function_rounds: parseInt(localStorage.getItem('function_rounds')) || 3,
    skip_validation: localStorage.getItem('skip_validation') === 'true',
    skip_deduplicator: localStorage.getItem('skip_deduplicator') === 'true',
    skip_arrow_determination: localStorage.getItem('skip_arrow_determination') === 'true',
    skip_schema_validation: localStorage.getItem('skip_schema_validation') === 'true',
    skip_interaction_metadata: localStorage.getItem('skip_interaction_metadata') === 'true',
    skip_pmid_update: localStorage.getItem('skip_pmid_update') === 'true',
    skip_arrow_validation: localStorage.getItem('skip_arrow_validation') === 'true',
    skip_clean_names: localStorage.getItem('skip_clean_names') === 'true',
    skip_finalize_metadata: localStorage.getItem('skip_finalize_metadata') === 'true'
  };

  try {
    const response = await fetch('/api/query', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        protein: proteinId,
        ...queryConfig
      })
    });

    if (!response.ok) {
      const errorData = await response.json();
      showNotificationMessage(`<span style="color: #ef4444;">Query failed: ${errorData.error || 'Unknown error'}</span>`);
      return;
    }

    const data = await response.json();

    if (data.status === 'processing') {
      // Add job to tracker with reload callback
      vizJobTracker.addJob(proteinId, {
        ...queryConfig,
        onComplete: () => {
          // Reload page to show updated data
          vizJobTracker.saveToSessionStorage(); // Persist jobs before reload
          window.location.reload();
        }
      });
    } else if (data.status === 'complete') {
      // Already complete - reload immediately
      showNotificationMessage(`<span>Query complete! Reloading...</span>`);
      vizJobTracker.saveToSessionStorage(); // Persist jobs before reload
      setTimeout(() => { window.location.reload(); }, 500);
    } else {
      showNotificationMessage(`<span style="color: #ef4444;">Unexpected status: ${data.status}</span>`);
    }
  } catch (error) {
    console.error('[ERROR] Query from modal failed:', error);
    showNotificationMessage(`<span style="color: #ef4444;">Failed to start query</span>`);
  }
}

// Search protein from visualizer page
async function searchProteinFromVisualizer(proteinName) {
  showNotificationMessage(`<span>Searching for ${proteinName}...</span>`);

  try {
    const response = await fetch(`/api/search/${encodeURIComponent(proteinName)}`);

    if (!response.ok) {
      const errorData = await response.json();
      showNotificationMessage(`<span style="color: #ef4444;">${errorData.error || 'Search failed'}</span>`);
      return;
    }

    const data = await response.json();

    if (data.status === 'found') {
      // Protein exists - navigate to it
      showNotificationMessage(`<span>Found! Loading ${proteinName}...</span>`);
      localStorage.setItem('lastQueriedProtein', proteinName.toUpperCase());
      setTimeout(() => {
        window.location.href = `/api/visualize/${encodeURIComponent(proteinName)}?t=${Date.now()}`;
      }, 500);
    } else {
      // Not found - show query prompt
      showNotificationMessage(`<span>${proteinName} not found. <button onclick="startQueryFromVisualizer('${proteinName}')" style="padding: 4px 12px; background: #3b82f6; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 12px; margin-left: 8px;">Start Query</button></span>`);
    }
  } catch (error) {
    console.error('[ERROR] Search failed:', error);
    showNotificationMessage(`<span style="color: #ef4444;">Search failed</span>`);
  }
}

// Start query from visualizer page
async function startQueryFromVisualizer(proteinName) {
  // IMMEDIATELY hide notification message when starting query
  const msg = document.getElementById('notification-message');
  if (msg) {
    msg.style.display = 'none';
    msg.innerHTML = '';
  }

  const queryConfig = {
    interactor_rounds: parseInt(localStorage.getItem('interactor_rounds')) || 3,
    function_rounds: parseInt(localStorage.getItem('function_rounds')) || 3,
    skip_validation: localStorage.getItem('skip_validation') === 'true',
    skip_deduplicator: localStorage.getItem('skip_deduplicator') === 'true',
    skip_arrow_determination: localStorage.getItem('skip_arrow_determination') === 'true',
    skip_schema_validation: localStorage.getItem('skip_schema_validation') === 'true',
    skip_interaction_metadata: localStorage.getItem('skip_interaction_metadata') === 'true',
    skip_pmid_update: localStorage.getItem('skip_pmid_update') === 'true',
    skip_arrow_validation: localStorage.getItem('skip_arrow_validation') === 'true',
    skip_clean_names: localStorage.getItem('skip_clean_names') === 'true',
    skip_finalize_metadata: localStorage.getItem('skip_finalize_metadata') === 'true'
  };

  try {
    const response = await fetch('/api/query', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        protein: proteinName,
        ...queryConfig
      })
    });

    if (!response.ok) {
      const errorData = await response.json();
      showNotificationMessage(`<span style="color: #ef4444;">Query failed: ${errorData.error || 'Unknown error'}</span>`);
      return;
    }

    const data = await response.json();

    if (data.status === 'processing') {
      // Add job to tracker with completion callback
      vizJobTracker.addJob(proteinName, {
        ...queryConfig,
        onComplete: () => {
          // Navigate to visualization
          localStorage.setItem('lastQueriedProtein', proteinName.toUpperCase());
          vizJobTracker.saveToSessionStorage(); // Persist jobs before navigation
          window.location.href = `/api/visualize/${encodeURIComponent(proteinName)}?t=${Date.now()}`;
        }
      });
    } else if (data.status === 'complete') {
      // Already complete - navigate immediately
      showNotificationMessage(`<span>Query complete! Loading visualization...</span>`);
      localStorage.setItem('lastQueriedProtein', proteinName.toUpperCase());
      vizJobTracker.saveToSessionStorage(); // Persist jobs before navigation
      setTimeout(() => {
        window.location.href = `/api/visualize/${encodeURIComponent(proteinName)}?t=${Date.now()}`;
      }, 500);
    } else {
      showNotificationMessage(`<span style="color: #ef4444;">Unexpected status: ${data.status}</span>`);
    }
  } catch (error) {
    console.error('[ERROR] Query failed:', error);
    showNotificationMessage(`<span style="color: #ef4444;">Failed to start query</span>`);
  }
}

function showFunctionModalFromNode(fnNode) {
  // Find the corresponding link to get the normalized arrow
  const linkId = `${fnNode.parent}-${fnNode.id}`;
  const correspondingLink = links.find(l => l.id === linkId);

  // Leverage the same renderer as link, but pass the fields explicitly
  showFunctionModal({
    fn: fnNode.data,
    interactor: fnNode.interactorData,
    affected: fnNode.parent,
    label: fnNode.label,
    linkArrow: correspondingLink ? correspondingLink.arrow : undefined
  });
}

/* Function modal (from function link click) */
function showFunctionModalFromLink(link) {
  const payload = link.data || {};
  showFunctionModal({
    fn: payload.fn || {},
    interactor: payload.interactor || {},
    affected: (payload.interactor && payload.interactor.primary) || '—',
    label: (payload.fn && payload.fn.function) || 'Function',
    linkArrow: link.arrow  // Pass the link's already-normalized arrow
  });
}

/* Render function modal (interactor → fn) */
function showFunctionModal({ fn, interactor, affected, label, linkArrow }) {

  // Format references with full paper details from evidence using beautiful wrappers
  const evs = Array.isArray(fn.evidence) ? fn.evidence : [];
  const evHTML = evs.length ? `<div class="expanded-evidence-list">${evs.map(ev => {
    const primaryLink = ev.pmid ? `https://pubmed.ncbi.nlm.nih.gov/${encodeURIComponent(String(ev.pmid))}` : (ev.doi ? `https://doi.org/${encodeURIComponent(String(ev.doi))}` : null);
    return `<div class="expanded-evidence-wrapper">
      <div class="expanded-evidence-card" data-evidence-link="${escapeHtml(primaryLink || '')}" data-has-link="${primaryLink ? 'true' : 'false'}">
        <div class="expanded-evidence-title">${escapeHtml(ev.paper_title || ev.title || 'Title not available')}</div>
        <div class="expanded-evidence-meta">
          ${ev.authors ? `<div class="expanded-evidence-meta-item"><strong>Authors:</strong> ${escapeHtml(ev.authors)}</div>` : ''}
          ${ev.journal ? `<div class="expanded-evidence-meta-item"><strong>Journal:</strong> ${escapeHtml(ev.journal)}</div>` : ''}
          ${ev.year ? `<div class="expanded-evidence-meta-item"><strong>Year:</strong> ${escapeHtml(String(ev.year))}</div>` : ''}
        </div>
        ${ev.relevant_quote ? `<div class="expanded-evidence-quote">"${escapeHtml(ev.relevant_quote)}"</div>` : ''}
        <div class="expanded-evidence-pmids" style="margin-top:8px;">
          ${ev.pmid ? `<a href="https://pubmed.ncbi.nlm.nih.gov/${encodeURIComponent(String(ev.pmid))}" target="_blank" class="expanded-pmid-badge" onclick="event.stopPropagation();">PMID: ${escapeHtml(String(ev.pmid))}</a>` : ''}
          ${ev.doi ? `<a href="https://doi.org/${encodeURIComponent(String(ev.doi))}" target="_blank" class="expanded-pmid-badge" onclick="event.stopPropagation();">DOI: ${escapeHtml(ev.doi)}</a>` : ''}
        </div>
      </div>
    </div>`;
  }).join('')}</div>` : (Array.isArray(fn.pmids) && fn.pmids.length
    ? fn.pmids.map(p => `<a class="pmid-link" target="_blank" href="https://pubmed.ncbi.nlm.nih.gov/${escapeHtml(String(p))}">PMID: ${escapeHtml(String(p))}</a>`).join(', ')
    : '<div class="expanded-empty">No references available</div>');

  // Format specific effects with 3D wrappers
  let effectsHTML = '';
  if (Array.isArray(fn.specific_effects) && fn.specific_effects.length) {
    const effectChips = fn.specific_effects.map(s => `
      <div class="expanded-effect-chip-wrapper">
        <div class="expanded-effect-chip">${escapeHtml(s)}</div>
      </div>`).join('');
    effectsHTML = `
      <tr class="info-row">
        <td class="info-label">SPECIFIC EFFECTS</td>
        <td class="info-value">
          <div class="expanded-effects-grid">${effectChips}</div>
        </td>
      </tr>`;
  }

  // Format biological cascade - NORMALIZED VERTICAL FLOWCHART
  const createCascadeHTML = (value) => {
    const segments = Array.isArray(value) ? value : (value ? [value] : []);
    if (segments.length === 0) {
      return '<div class="expanded-empty">Cascading biological effects not specified</div>';
    }

    // Normalize: flatten all segments and split by arrow (→)
    const allSteps = [];
    segments.forEach(segment => {
      const text = (segment == null ? '' : segment).toString().trim();
      if (!text) return;

      // Split by arrow and clean each step
      const steps = text.split('→').map(s => s.trim()).filter(s => s.length > 0);
      allSteps.push(...steps);
    });

    if (allSteps.length === 0) {
      return '<div class="expanded-empty">Cascading biological effects not specified</div>';
    }

    // Create vertical flowchart blocks
    const items = allSteps.map(step =>
      `<div class="cascade-flow-item">${escapeHtml(step)}</div>`
    ).join('');

    return `<div class="cascade-wrapper"><div class="cascade-flow-container">${items}</div></div>`;
  };
  const biologicalConsequenceHTML = createCascadeHTML(fn.biological_consequence);

  const mechanism = interactor && interactor.intent ? (interactor.intent[0].toUpperCase() + interactor.intent.slice(1)) : 'Not specified';

  // EFFECT TYPE: Use the link's already-normalized arrow
  // The link was created with the normalized arrow, so we MUST use that for consistency
  const normalizedArrow = linkArrow || 'binds';  // Default to binds if no link arrow provided
  const arrowColor = normalizedArrow === 'activates' ? '#059669' : (normalizedArrow === 'inhibits' ? '#dc2626' : '#7c3aed');
  const arrowStr = fn.effect_description ?
    `<strong style="color:${arrowColor};">${fn.effect_description}</strong>` :
    (normalizedArrow === 'activates' ?
      '<strong style="color:#059669;">✓ Function is enhanced or activated</strong>' :
      (normalizedArrow === 'inhibits' ?
        '<strong style="color:#dc2626;">✗ Function is inhibited or disrupted</strong>' :
        '<strong style="color:#7c3aed;">⊕ Binds/Interacts</strong>'));

  // Check for validity field (from fact-checker)
  const validity = fn.validity || 'TRUE';
  const validationNote = fn.validation_note || '';
  const isConflicting = validity === 'CONFLICTING';
  const isFalse = validity === 'FALSE';

  // Build conflict warning HTML if needed
  let conflictWarningHTML = '';
  if (isConflicting || isFalse) {
    const warningType = isFalse ? 'Invalid Claim' : 'Conflicting Evidence';
    const warningIcon = isFalse ? '❌' : '⚠️';
    const warningColor = isFalse ? '#dc2626' : '#f59e0b';
    conflictWarningHTML = `
      <tr class="info-row">
        <td colspan="2">
          <div style="background:${isFalse ? '#fee2e2' : '#fff3cd'};border-left:4px solid ${warningColor};padding:12px 16px;margin:8px 0;border-radius:4px;">
            <div style="font-weight:600;color:${warningColor};margin-bottom:4px;">
              ${warningIcon} <strong>${warningType}</strong>
            </div>
            <div style="color:#374151;font-size:13px;">${escapeHtml(validationNote)}</div>
          </div>
        </td>
      </tr>`;
  }

  // Update function label to show asterisk for conflicting claims
  const functionLabel = isConflicting ? `⚠ ${label} *` : label;

  // Wrap mechanism with beautiful wrapper
  const mechanismHTML = mechanism !== 'Not specified'
    ? `<div class="expanded-mechanism-wrapper"><span class="mechanism-badge">${escapeHtml(mechanism)}</span></div>`
    : '<span class="muted-text">Not specified</span>';

  // Wrap cellular process with beautiful wrapper
  const cellularHTML = fn.cellular_process
    ? `<div class="expanded-cellular-wrapper"><div class="expanded-cellular-process"><div class="expanded-cellular-process-text">${escapeHtml(fn.cellular_process)}</div></div></div>`
    : '<div class="expanded-empty">Molecular mechanism not specified</div>';

  // ========== PATHWAY CONTEXT ==========
  // FIXED: Use fn.pathway directly instead of searching nodes
  // This ensures the function's pathway matches what was assigned in Script 05
  let pathwayContextHTML = '';
  if (pathwayMode) {
    const relevantPathways = [];

    // NEW: First check if function has pathway assigned directly
    if (fn.pathway && fn.pathway.name) {
      const fnPathway = fn.pathway;
      relevantPathways.push({
        name: fnPathway.canonical_name || fnPathway.name,
        hierarchy: fnPathway.hierarchy || [fnPathway.name],
        level: fnPathway.level || 0,
        is_leaf: fnPathway.is_leaf !== false,
        id: fnPathway.name,
        ontologyId: fnPathway.ontology_id,
        ontologySource: fnPathway.ontology_source
      });
    } else if (interactor) {
      // Fallback: Check pathway nodes to find ones containing this interactor
      const interactorId = interactor.primary || interactor.id || affected;
      nodes.filter(n => n.type === 'pathway').forEach(pathwayNode => {
        const interactorIds = pathwayNode.interactorIds || [];
        if (interactorIds.includes(interactorId)) {
          relevantPathways.push({
            name: pathwayNode.label,
            id: pathwayNode.id,
            hierarchy: pathwayNode.hierarchy || [pathwayNode.label],
            level: pathwayNode.level || 0,
            ontologyId: pathwayNode.ontologyId,
            ontologySource: pathwayNode.ontologySource
          });
        }
      });
    }

    if (relevantPathways.length > 0) {
      // Determine role based on function arrow
      const roleText = normalizedArrow === 'activates'
        ? `Activates ${label} within this pathway`
        : normalizedArrow === 'inhibits'
          ? `Inhibits ${label} within this pathway`
          : normalizedArrow === 'regulates'
            ? `Regulates ${label} within this pathway`
            : `Interacts with ${label} in this pathway`;

      const pathwayBadges = relevantPathways.map(pw => {
        const ontologyLink = pw.ontologyId && pw.ontologySource
          ? (pw.ontologySource === 'GO'
            ? `<a href="https://www.ebi.ac.uk/QuickGO/term/${encodeURIComponent(String(pw.ontologyId))}" target="_blank" class="ontology-link">${escapeHtml(pw.ontologyId)}</a>`
            : `<span class="ontology-badge">${escapeHtml(pw.ontologyId)}</span>`)
          : '';

        // NEW: Show hierarchy chain if available
        const hierarchyChain = pw.hierarchy && pw.hierarchy.length > 1
          ? `<div class="pathway-hierarchy-chain" style="font-size: 10px; color: #6b7280; margin-top: 4px;">${pw.hierarchy.map(part => escapeHtml(part)).join(' → ')}</div>`
          : '';

        // NEW: Show level and leaf badge
        const levelBadge = pw.level !== undefined
          ? `<span class="pathway-level-badge" style="font-size: 9px; background: #e0e7ff; color: #4338ca; padding: 1px 4px; border-radius: 3px; margin-left: 6px;">Level ${pw.level}</span>`
          : '';
        const leafBadge = pw.is_leaf
          ? `<span class="pathway-leaf-badge" style="font-size: 9px; background: #d1fae5; color: #059669; padding: 1px 4px; border-radius: 3px; margin-left: 4px;">LEAF</span>`
          : '';

        return `<div class="pathway-context-badge">
          <div style="display: flex; align-items: center; flex-wrap: wrap;">
            <span class="pathway-name">${escapeHtml(pw.name)}</span>
            ${levelBadge}
            ${leafBadge}
            ${ontologyLink}
          </div>
          ${hierarchyChain}
        </div>`;
      }).join('');

      pathwayContextHTML = `
        <tr class="info-row">
          <td class="info-label">PATHWAY CONTEXT</td>
          <td class="info-value">
            <div class="pathway-context-wrapper">
              <div class="pathway-badges">${pathwayBadges}</div>
              <div class="pathway-role">${escapeHtml(roleText)}</div>
            </div>
          </td>
        </tr>`;
    }
  }

  // Wrap effect type with beautiful wrapper
  const effectTypeColor = normalizedArrow === 'activates' ? 'activates' : (normalizedArrow === 'inhibits' ? 'inhibits' : 'binds');
  const effectTypeText = fn.effect_description || (normalizedArrow === 'activates' ? '✓ Function is enhanced or activated' : (normalizedArrow === 'inhibits' ? '✗ Function is inhibited or disrupted' : '⊕ Binds/Interacts'));
  const effectTypeHTML = `<div class="expanded-effect-type ${effectTypeColor}"><span class="effect-type-badge ${effectTypeColor}">${escapeHtml(effectTypeText)}</span></div>`;

  // Wrap function and protein names prominently
  const functionHTML = `<div class="function-name-wrapper ${effectTypeColor}"><span class="function-name ${effectTypeColor}" style="font-size: 18px;">${escapeHtml(functionLabel)}</span></div>`;
  const affectedHTML = `<div class="interaction-name-wrapper"><div class="interaction-name" style="font-size: 16px;">${escapeHtml(affected)}</div></div>`;

  const body = `
    <table class="info-table">
      ${conflictWarningHTML}
      <tr class="info-row"><td class="info-label">FUNCTION</td><td class="info-value">${functionHTML}</td></tr>
      <tr class="info-row"><td class="info-label">AFFECTED PROTEIN</td><td class="info-value">${affectedHTML}</td></tr>
      ${pathwayContextHTML}
      <tr class="info-row"><td class="info-label">EFFECT TYPE</td><td class="info-value">${effectTypeHTML}</td></tr>
      <tr class="info-row"><td class="info-label">MECHANISM</td><td class="info-value">${mechanismHTML}</td></tr>
      <tr class="info-row"><td class="info-label">CELLULAR PROCESS</td><td class="info-value">${cellularHTML}</td></tr>
      <tr class="info-row"><td class="info-label">BIOLOGICAL CASCADE</td><td class="info-value">${biologicalConsequenceHTML}</td></tr>
      ${effectsHTML}
      <tr class="info-row"><td class="info-label">REFERENCES</td><td class="info-value">${evHTML}</td></tr>
    </table>`;
  openModal(`Function: ${escapeHtml(label)}`, body);
}
