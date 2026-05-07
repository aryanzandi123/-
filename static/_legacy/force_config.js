/* ===== force_config.js — Force simulation configuration (extracted from visualizer.js) ===== */

/**
 * Configure forces for shell layout mode.
 * Shell mode uses minimal forces - just enough to render links and resolve collisions.
 *
 * @param {Object} sim - The D3 force simulation
 * @param {Array} linksArray - The links array
 * @param {Object} opts - { mainNodeRadius, interactorNodeRadius }
 */
function configureShellForces(sim, linksArray, opts) {
    sim
      .force('link', d3.forceLink(linksArray)
        .id(d => d.id)
        .distance(100)
        .strength(0) // No pull - positions are fixed by shell calculations
      )
      .force('collide', d3.forceCollide()
        .radius(d => {
          // Very generous padding to make overlaps impossible
          if (d.type === 'main') return opts.mainNodeRadius + 20;
          if (d.type === 'pathway') return 70;  // Reduced buffer
          if (d.type === 'function' || d.isFunction) return 45;
          if (d.type === 'interactor') return (d.radius || opts.interactorNodeRadius) + 12;  // Standard buffer (44px radius -> 88px spacing)
          return (d.radius || opts.interactorNodeRadius) + 12;
        })
        .iterations(25)  // Many passes to fully resolve overlaps
        .strength(1.0)   // Maximum collision strength
      );

    // Shell mode: run collision resolution with much more time to settle
    sim.alpha(0.8).alphaDecay(0.015);  // Much more time to settle completely
}

/**
 * Configure forces for force-directed layout mode (legacy behavior).
 * Full physics simulation with charge, collision, radial, and custom forces.
 *
 * @param {Object} sim - The D3 force simulation
 * @param {Array} linksArray - The links array
 * @param {Object} opts - {
 *   mainNodeRadius, interactorNodeRadius, width, height,
 *   pathwayRingRadius, nodeMap, expandedPathways, pathwayMode,
 *   expanded, SHELL_RADIUS_BASE, SHELL_RADIUS_EXPANDED, SHELL_RADIUS_CHILDREN,
 *   forcePathwayOrbit, forceSectorConstraint, forceAngularPosition, forceShellAnchor
 * }
 */
function configureForceLayout(sim, linksArray, opts) {
    sim
      .force('link', d3.forceLink(linksArray)
        .id(d => d.id)
        .distance(d => {
          const src = typeof d.source === 'object' ? d.source : opts.nodeMap.get(d.source);
          const tgt = typeof d.target === 'object' ? d.target : opts.nodeMap.get(d.target);

          if (d.linkType === 'indirect-chain') return 60;
          if (d.type === 'pathway-interactor-link') {
            const srcId = typeof d.source === 'object' ? d.source.id : d.source;
            const tgtId = typeof d.target === 'object' ? d.target.id : d.target;
            const isExpanded = opts.expandedPathways.has(srcId) || opts.expandedPathways.has(tgtId);
            if (d.isReferenceLink) return isExpanded ? 90 : 70;
            return isExpanded ? 120 : 80;
          }
          if (d.type === 'pathway-link') return opts.pathwayRingRadius;
          if (d.type === 'function' || (tgt && (tgt.type === 'function' || tgt.isFunction))) return 80;

          if (!opts.pathwayMode && src && tgt) {
            if (src.type === 'main' || tgt.type === 'main') return opts.SHELL_RADIUS_BASE;
            if (tgt._isChildOf || src._isChildOf) return 150;
          }

          return 250;
        })
        .strength(0.4)
      )
      .force('charge', d3.forceManyBody()
        .strength(d => {
          if (d.type === 'pathway') return -350;
          if (d.isReferenceNode) return -100;
          return -200;
        })
        .distanceMax(500)
      )
      .force('center', d3.forceCenter(opts.width / 2, opts.height / 2).strength(0.03))
      .force('collide', d3.forceCollide()
        .radius(d => {
          if (d.type === 'main') return opts.mainNodeRadius + 35;

          // For pathways, approximate the half-width of the rectangle
          if (d.type === 'pathway') {
            const fontSize = 14;
            const charWidth = fontSize * 0.55;
            const textWidth = (d.label || '').length * charWidth;
            const rectWidth = Math.max(textWidth + 48, 120);
            return rectWidth / 2 + 10; // Half width + padding
          }

          if (d.type === 'function' || d.isFunction) return 55;
          if (d.type === 'interactor') return (d.radius || opts.interactorNodeRadius) + 20;  // Moderate buffer
          return (d.radius || opts.interactorNodeRadius) + 35;
        })
        .strength(0.7) // Stronger collision to ensure separation
        .iterations(2)
      )
      .force('radialPathways', d3.forceRadial(
        d => {
          if (d.type === 'pathway') {
            return opts.expandedPathways.has(d.id) ? opts.pathwayRingRadius + 80 : opts.pathwayRingRadius;
          }
          return 0;
        },
        opts.width / 2,
        opts.height / 2
      ).strength(d => d.type === 'pathway' ? 0.9 : 0))
      .force('radialShell', d3.forceRadial(
        d => {
          if (d.type === 'main') return 0;
          if (d.isFunction || d.type === 'function') return null;
          if (d.type === 'pathway') return null;

          // In pathway mode, calculate absolute radius from shell number
          // This ensures deep pathway interactors are pushed to outer rings
          if (opts.pathwayMode && d._shellData?.shell) {
            const BASE_RADIUS = 250;
            const SHELL_GAP = 150;
            return BASE_RADIUS + (d._shellData.shell - 1) * SHELL_GAP;
          }

          // Non-pathway mode: original logic
          if (d._isChildOf) return opts.SHELL_RADIUS_CHILDREN;
          if (opts.expanded.has(d.id)) return opts.SHELL_RADIUS_EXPANDED;
          return opts.SHELL_RADIUS_BASE;
        },
        opts.width / 2,
        opts.height / 2
      ).strength(0))  // Radial pull disabled — angular positioning dominates
      .force('pathwayOrbit', opts.forcePathwayOrbit().strength(0.6))
      .force('sectorConstraint', opts.forceSectorConstraint().strength(0.35))
      .force('angularPosition', opts.forceAngularPosition()
        .center(opts.width / 2, opts.height / 2)
        .strength(0.5))
      .force('shellAnchor', opts.forceShellAnchor().strength(0.22));

    sim.alpha(1);
}
