/**
 * cv_diagnostics.js — Pipeline diagnostics surfacing for the card view.
 *
 * Reads the `_diagnostics` block emitted by `services/data_builder.py` on
 * /api/results responses (carrying pass_rate, dropped interactor counts,
 * partial chain counts, unrecoverable chain pairs) and renders a top-of-
 * card-view banner plus per-node depth-issue badges.
 *
 * Designed to be drop-in alongside card_view.js without touching its
 * 5K-line render function. Safe to load whether or not the response
 * carries diagnostics — silent when the field is absent.
 *
 * Public API on window:
 *   renderDiagnosticsBanner(snap, container)
 *   applyDepthBadges(rootElement)
 *   applyPartialChainBadges(rootElement, snapInteractions)
 *   centralizedPseudoNames()
 *
 * Single source of truth for pseudo names mirrors utils/db_sync._PSEUDO_WHITELIST.
 */
(function (global) {
  'use strict';

  // Mirror of utils/db_sync._PSEUDO_WHITELIST. Kept in sync manually for
  // now; future work: emit this via /api/pseudo_whitelist so backend is
  // the single source of truth.
  const PSEUDO_NAMES = new Set([
    'RNA', 'mRNA', 'pre-mRNA', 'tRNA', 'rRNA', 'lncRNA', 'miRNA', 'snRNA', 'snoRNA',
    'DNA', 'ssDNA', 'dsDNA',
    'Ubiquitin', 'SUMO', 'NEDD8',
    'Proteasome', 'Ribosome', 'Spliceosome',
    'Actin', 'Tubulin',
    'Stress Granules', 'P-bodies',
  ]);
  const PSEUDO_LOWER = new Set([...PSEUDO_NAMES].map(n => n.toLowerCase()));

  function isPseudoName(name) {
    if (!name) return false;
    return PSEUDO_LOWER.has(String(name).toLowerCase());
  }

  function fmtPct(rate) {
    if (rate == null || isNaN(rate)) return '—';
    return Math.round(rate * 100) + '%';
  }

  function makeStat(labelText, value, severity) {
    const wrap = document.createElement('span');
    wrap.className = 'cv-diag-stat ' + (severity || '');
    const label = document.createElement('span');
    label.textContent = labelText;
    const val = document.createElement('span');
    val.className = 'cv-diag-stat-value';
    val.textContent = value;
    wrap.appendChild(label);
    wrap.appendChild(val);
    return wrap;
  }

  function passRateBar(rate) {
    const bar = document.createElement('span');
    bar.className = 'cv-pass-rate-bar';
    bar.title = `Depth pass rate: ${fmtPct(rate)}`;
    const fill = document.createElement('span');
    fill.className = 'cv-pass-rate-bar-fill';
    fill.style.width = `${Math.max(2, Math.min(100, (rate || 0) * 100))}%`;
    bar.appendChild(fill);
    return bar;
  }

  function severityForPassRate(rate) {
    if (rate == null) return '';
    if (rate >= 0.8) return 'good';
    if (rate >= 0.5) return 'warn';
    return 'bad';
  }

  /**
   * Render the diagnostics banner above the card view.
   *
   * @param {object} snap   The full /api/results payload (object with _diagnostics).
   * @param {Element} parent The DOM node to insert the banner into. Defaults to
   *                         the `.main-content` element if not specified.
   * @returns {Element|null} The banner element, or null if no diagnostics.
   */
  function renderDiagnosticsBanner(snap, parent) {
    if (!snap || !snap._diagnostics) return null;
    const diag = snap._diagnostics;
    const qr = diag.quality_report || {};
    const passRate = qr.pass_rate;
    const total = qr.total_functions || 0;
    const flagged = qr.flagged_functions || 0;
    const dropped = (diag.zero_function_dropped || []).length;
    const unrecoverable = (diag.chain_pair_unrecoverable || []).length;
    const incomplete = (diag.chain_incomplete_hops || []).length;
    // Pathway drift: corrected (auto-rehomed at write time, P3.1) vs
    // report-only (logged but not rewritten because the implied
    // pathway wasn't in DB). Counted separately so the user can see
    // both "fixed" and "still drifted" at a glance.
    const driftEntries = Array.isArray(diag.pathway_drifts) ? diag.pathway_drifts : [];
    const driftCorrected = driftEntries.filter(d => d && d.action === 'corrected').length;
    const driftReportOnly = driftEntries.length - driftCorrected;

    // Skip the banner only when the run is genuinely clean.
    const allClean = (
      flagged === 0 &&
      dropped === 0 &&
      unrecoverable === 0 &&
      incomplete === 0 &&
      driftEntries.length === 0
    );

    // Remove any prior banner so re-renders don't stack.
    const existing = document.getElementById('cv-diagnostics-banner');
    if (existing && existing.parentNode) existing.parentNode.removeChild(existing);

    const banner = document.createElement('div');
    banner.id = 'cv-diagnostics-banner';
    banner.className = 'cv-diagnostics-banner' + (allClean ? ' cv-diag-clean' : '');
    banner.setAttribute('role', 'status');
    banner.setAttribute('aria-live', 'polite');

    if (passRate != null) {
      const sev = severityForPassRate(passRate);
      const passWrap = document.createElement('span');
      passWrap.className = 'cv-diag-stat ' + sev;
      passWrap.appendChild(passRateBar(passRate));
      const valSpan = document.createElement('span');
      valSpan.className = 'cv-diag-stat-value';
      valSpan.textContent = fmtPct(passRate);
      const labelSpan = document.createElement('span');
      labelSpan.textContent = 'PhD-depth';
      passWrap.appendChild(labelSpan);
      passWrap.appendChild(valSpan);
      passWrap.title = `${flagged} of ${total} functions flagged for depth issues (min 6 sentences / 3 cascades)`;
      banner.appendChild(passWrap);
    }

    if (flagged > 0) {
      banner.appendChild(makeStat(
        'shallow funcs:',
        `${flagged}/${total}`,
        'warn',
      ));
    }
    if (dropped > 0) {
      banner.appendChild(makeStat(
        'dropped (no functions):',
        dropped,
        'bad',
      ));
    }
    if (unrecoverable > 0) {
      banner.appendChild(makeStat(
        'unrecoverable chain hops:',
        unrecoverable,
        'bad',
      ));
    }
    if (incomplete > 0) {
      banner.appendChild(makeStat(
        'partial chains:',
        incomplete,
        'warn',
      ));
    }
    if (driftCorrected > 0) {
      banner.appendChild(makeStat(
        'pathway rehomed:',
        driftCorrected,
        'good',
      ));
    }
    if (driftReportOnly > 0) {
      banner.appendChild(makeStat(
        'pathway drift:',
        driftReportOnly,
        'warn',
      ));
    }
    if (allClean) {
      banner.appendChild(makeStat('PhD-depth all green', '✓', 'good'));
    }

    if (!allClean) {
      const toggle = document.createElement('button');
      toggle.className = 'cv-diag-details-toggle';
      toggle.type = 'button';
      toggle.textContent = 'details';
      toggle.setAttribute('aria-expanded', 'false');
      const details = document.createElement('div');
      details.className = 'cv-diag-details';
      details.id = 'cv-diag-details';

      const lines = [];
      if (dropped > 0) {
        lines.push(`Dropped (no functions): ${(diag.zero_function_dropped || []).join(', ')}`);
      }
      if (unrecoverable > 0) {
        lines.push(`Unrecoverable chain pairs: ${(diag.chain_pair_unrecoverable || []).join(', ')}`);
      }
      if (incomplete > 0) {
        const lines_inner = (diag.chain_incomplete_hops || []).map(
          h => `  • ${h.interactor}: missing ${(h.missing_hops || []).join(', ')}`
        );
        lines.push(`Partial chains:\n${lines_inner.join('\n')}`);
      }
      if (driftCorrected > 0) {
        const lines_inner = driftEntries
          .filter(d => d && d.action === 'corrected')
          .map(d => `  • ${d.interactor || '?'}.${d.function || '?'}: ${d.from} (${d.from_score}) → ${d.to} (${d.to_score})`);
        lines.push(`Pathways rehomed at write time (P3.1):\n${lines_inner.join('\n')}`);
      }
      if (driftReportOnly > 0) {
        const lines_inner = driftEntries
          .filter(d => d && d.action !== 'corrected')
          .map(d => `  • ${d.interactor || '?'}.${d.function || '?'}: assigned ${d.from} (${d.from_score}), prose favors ${d.to} (${d.to_score})`);
        lines.push(`Pathway drift (report-only):\n${lines_inner.join('\n')}`);
      }
      details.textContent = lines.join('\n\n');

      toggle.addEventListener('click', () => {
        const showing = details.classList.toggle('show');
        toggle.setAttribute('aria-expanded', showing ? 'true' : 'false');
        toggle.textContent = showing ? 'hide details' : 'details';
      });

      banner.appendChild(toggle);
      banner.appendChild(details);
    }

    const target = parent || document.querySelector('.main-content') || document.body;
    if (target) {
      target.insertBefore(banner, target.firstChild);
    }
    return banner;
  }

  /**
   * Walk the rendered card view and attach a small badge to any node whose
   * underlying interactor has any function carrying `_depth_issues`. The
   * badge exposes the failing rule names via `title` so users can hover
   * for details. No-op if the card view hasn't rendered yet.
   *
   * @param {Element} root   Root DOM element to scan (defaults to document).
   * @param {object[]} interactions  The snap.interactions list to read flags from.
   */
  function applyDepthBadges(root, interactions) {
    const scope = root || document;
    if (!Array.isArray(interactions) || !interactions.length) return;

    // Build map: interactor primary symbol → set of failing rule names
    const issuesByInteractor = new Map();
    interactions.forEach(inter => {
      if (!inter || !Array.isArray(inter.functions)) return;
      const partner = (inter.target || inter.primary || inter.partner || '').toString();
      const issues = new Set();
      inter.functions.forEach(fn => {
        const di = fn && fn._depth_issues;
        if (Array.isArray(di) && di.length) di.forEach(r => issues.add(r));
      });
      if (issues.size) issuesByInteractor.set(partner.toUpperCase(), issues);
    });

    if (!issuesByInteractor.size) return;

    // Strategy: walk every cv-node, derive its protein name from
    // (1) data-name attribute (preferred — set by future card_view rewrite),
    // (2) the text content of .cv-label / .cv-card-label / first <text>,
    // (3) the node's __data__ d3 binding if accessible.
    scope.querySelectorAll('.cv-node').forEach(node => {
      let name = node.getAttribute('data-name') || '';
      if (!name) {
        const labelEl = node.querySelector('.cv-label, .cv-card-label, text');
        if (labelEl) {
          name = (labelEl.textContent || '').trim().split(/\s+/)[0];
        }
      }
      if (!name) {
        const datum = node.__data__;
        if (datum && (datum.id || datum.name || datum.primary)) {
          name = datum.id || datum.name || datum.primary;
        }
      }
      if (!name) return;
      const upper = String(name).toUpperCase();
      if (!issuesByInteractor.has(upper)) return;
      if (node.querySelector('.cv-depth-badge')) return;
      const badge = document.createElement('span');
      badge.className = 'cv-depth-badge';
      badge.textContent = '!';
      const rules = [...issuesByInteractor.get(upper)].join(', ');
      badge.title = `Depth issues: ${rules}. Re-run query to redispatch.`;
      const labelEl = node.querySelector('.cv-label, .cv-card-label, text');
      (labelEl || node).appendChild(badge);
    });
  }

  /**
   * Walk the rendered card view and attach badges to chain-related nodes:
   *
   *   1. Indirect interactor nodes whose ``_chain_incomplete_hops`` is
   *      non-empty get a "partial" badge (title lists missing hops).
   *   2. Specific chain HOP nodes that ARE the missing hops get an
   *      individual "missing biology" badge so the user can see WHICH
   *      hop in the chain has no claims attached.  Pre-2026-05-03 the
   *      top-level "(partial)" badge was the only signal — meaning a
   *      4-hop chain with one broken middle hop showed exactly the
   *      same UI as one with four broken hops.
   *
   *   Hop-level matching uses the D3 datum's ``_chainId`` /
   *   ``_chainPosition`` / ``_chainProteins`` triplet that
   *   ``card_view.js`` stamps onto every chain participant node.
   *   Missing-hop strings come in as ``"SRC->TGT"``; we match a node
   *   when its ``_chainProteins[_chainPosition - 1] -> data.id`` pair
   *   equals one of the missing entries (case-insensitive).
   *
   * @param {Element} root   Root DOM element to scan.
   * @param {object} diag    The snap._diagnostics object.
   */
  function applyPartialChainBadges(root, diag) {
    if (!diag || !Array.isArray(diag.chain_incomplete_hops)) return;
    const byInteractor = new Map();
    const allMissingHops = new Set();   // upper-cased "SRC->TGT" strings
    diag.chain_incomplete_hops.forEach(entry => {
      if (!entry || !entry.interactor) return;
      const hops = Array.isArray(entry.missing_hops) ? entry.missing_hops : [];
      byInteractor.set(entry.interactor.toUpperCase(), hops);
      hops.forEach(h => {
        if (typeof h === 'string' && h.includes('->')) {
          allMissingHops.add(h.toUpperCase());
        }
      });
    });
    if (!byInteractor.size && !allMissingHops.size) return;

    const scope = root || document;

    // PASS 1: parent indirect interactor "partial" badge (legacy behavior).
    scope.querySelectorAll('.cv-node').forEach(node => {
      let name = node.getAttribute('data-name') || '';
      if (!name) {
        const labelEl = node.querySelector('.cv-label, .cv-card-label, text');
        if (labelEl) {
          name = (labelEl.textContent || '').trim().split(/\s+/)[0];
        }
      }
      if (!name) {
        const datum = node.__data__;
        if (datum && (datum.id || datum.name || datum.primary)) {
          name = datum.id || datum.name || datum.primary;
        }
      }
      if (!name) return;
      const missing = byInteractor.get(String(name).toUpperCase());
      if (!missing || !missing.length) return;
      if (node.querySelector('.cv-partial-chain-badge')) return;
      const badge = document.createElement('span');
      badge.className = 'cv-partial-chain-badge';
      badge.textContent = 'partial';
      badge.title = `Missing chain hops: ${missing.join(', ')}`;
      const labelEl = node.querySelector('.cv-label, .cv-card-label, text');
      (labelEl || node).appendChild(badge);
    });

    // PASS 2: per-hop "missing biology" badge on the actual missing
    // chain participant. Walk the same nodes once more, this time
    // checking each node's chain-participant payload.
    if (!allMissingHops.size) return;
    scope.querySelectorAll('.cv-node').forEach(node => {
      const datum = node.__data__ && node.__data__.data ? node.__data__.data : node.__data__;
      if (!datum) return;
      const cid = datum._chainId;
      const cpos = datum._chainPosition;
      const cprots = datum._chainProteins;
      if (cid == null || typeof cpos !== 'number' || !Array.isArray(cprots) || cpos <= 0) return;
      const src = cprots[cpos - 1];
      const tgt = datum.id || cprots[cpos];
      if (!src || !tgt) return;
      const hopKey = `${String(src).toUpperCase()}->${String(tgt).toUpperCase()}`;
      if (!allMissingHops.has(hopKey)) return;
      if (node.querySelector('.cv-hop-missing-badge')) return;
      const badge = document.createElement('span');
      badge.className = 'cv-hop-missing-badge';
      badge.textContent = 'no biology';
      badge.title =
        `Hop ${src} → ${tgt} has no validated claim — chain claim ` +
        `generation never attached a function for this pair. The cascade ` +
        `still renders so the structure is visible, but click to see ` +
        `the placeholder. Re-run the query to attempt recovery.`;
      const labelEl = node.querySelector('.cv-label, .cv-card-label, text');
      (labelEl || node).appendChild(badge);
    });
  }

  /**
   * Walk the rendered card view and attach a "drift" badge to any node
   * that the backend's pathway content validator flagged for write-time
   * pathway drift. Reads ``snap._diagnostics.pathway_drifts`` (a list of
   * ``{interactor, function, from, to, from_score, to_score, action}``
   * entries) emitted by P3.1 in scripts/pathway_v2/quick_assign.py.
   *
   * action ∈ {"corrected", "report-only"} — corrected drifts get a
   * green "rehomed" badge; report-only get an amber "drift" badge.
   *
   * @param {Element} root  Root DOM element to scan.
   * @param {object}  diag  The snap._diagnostics object.
   */
  function applyPathwayDriftBadges(root, diag) {
    if (!diag || !Array.isArray(diag.pathway_drifts) || !diag.pathway_drifts.length) return;
    const byInteractor = new Map();
    diag.pathway_drifts.forEach(entry => {
      if (!entry || !entry.interactor) return;
      const key = String(entry.interactor).toUpperCase();
      const list = byInteractor.get(key) || [];
      list.push(entry);
      byInteractor.set(key, list);
    });
    if (!byInteractor.size) return;

    const scope = root || document;
    scope.querySelectorAll('.cv-node').forEach(node => {
      let name = node.getAttribute('data-name') || '';
      if (!name) {
        const labelEl = node.querySelector('.cv-label, .cv-card-label, text');
        if (labelEl) {
          name = (labelEl.textContent || '').trim().split(/\s+/)[0];
        }
      }
      if (!name) {
        const datum = node.__data__;
        if (datum && (datum.id || datum.name || datum.primary)) {
          name = datum.id || datum.name || datum.primary;
        }
      }
      if (!name) return;
      const drifts = byInteractor.get(String(name).toUpperCase());
      if (!drifts || !drifts.length) return;
      if (node.querySelector('.cv-pathway-drift-badge')) return;
      const allCorrected = drifts.every(d => d.action === 'corrected');
      const badge = document.createElement('span');
      badge.className = 'cv-pathway-drift-badge ' + (allCorrected ? 'corrected' : 'report-only');
      badge.textContent = allCorrected ? 'rehomed' : 'drift';
      const lines = drifts.map(d => {
        const action = d.action === 'corrected' ? '→ rehomed to' : 'drift toward';
        return `  • ${d.function || '?'}: ${d.from} (${d.from_score}) ${action} ${d.to} (${d.to_score})`;
      }).join('\n');
      badge.title =
        (allCorrected
          ? 'Pathway assignment was rewritten at write time based on prose keyword analysis:\n'
          : 'Pathway assignment disagrees with prose keywords (PATHWAY_AUTO_CORRECT=false, report-only):\n')
        + lines;
      const labelEl = node.querySelector('.cv-label, .cv-card-label, text');
      (labelEl || node).appendChild(badge);
    });
  }

  function centralizedPseudoNames() {
    return PSEUDO_NAMES;
  }

  // Expose as namespaced globals so card_view.js / modal.js can invoke
  // them without ES module loaders. visualize.html loads this script
  // before card_view.js so it's always defined when card_view runs.
  global.cvDiagnostics = {
    renderBanner: renderDiagnosticsBanner,
    applyDepthBadges,
    applyPartialChainBadges,
    applyPathwayDriftBadges,
    isPseudo: isPseudoName,
    pseudoNames: centralizedPseudoNames,
  };
})(window);
