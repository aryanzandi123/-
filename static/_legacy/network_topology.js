(function () {
  'use strict';

  const state = {
    mainProtein: null,
    queryEdges: [],
    databasedEdges: [],
    edgeKeySet: new Set(),
    nodeMeta: new Map(),
    databasedNeighborCache: new Map(),
    fetchedParents: new Set(),
    version: 0
  };

  function normalizeInteractionType(raw) {
    const v = String(raw || '').toLowerCase();
    if (v === 'indirect' || v === 'shared' || v === 'cross_link' || v === 'implied') {
      return v;
    }
    return 'direct';
  }

  function normalizeArrow(raw) {
    const v = String(raw || '').toLowerCase();
    if (v.includes('activ')) return 'activates';
    if (v.includes('inhib') || v.includes('suppress') || v.includes('repress')) return 'inhibits';
    if (v.includes('regulat') || v.includes('modulat')) return 'regulates';
    if (v.includes('bind')) return 'binds';
    if (v === 'complex') return 'binds';
    return ['activates', 'inhibits', 'regulates', 'binds'].includes(v) ? v : 'binds';
  }

  function normalizeDirection(raw) {
    // S1: bidirectional is dead. Legacy values and unknown inputs
    // default to main_to_primary (query acts on partner).
    const v = String(raw || '').toLowerCase();
    if (v === 'main_to_primary' || v === 'primary_to_main') return v;
    if (v === 'a_to_b') return 'main_to_primary';
    if (v === 'b_to_a') return 'primary_to_main';
    return 'main_to_primary';
  }

  function makeEdgeKey(edge) {
    return [
      edge.source,
      edge.target,
      edge.arrow,
      edge.direction,
      edge.interactionType,
      edge.origin
    ].join('|');
  }

  function registerEdge(edge) {
    const key = makeEdgeKey(edge);
    if (state.edgeKeySet.has(key)) return false;
    state.edgeKeySet.add(key);
    return true;
  }

  function touchNodeMeta(nodeId, patch) {
    if (!nodeId) return;
    const existing = state.nodeMeta.get(nodeId) || { id: nodeId };
    state.nodeMeta.set(nodeId, { ...existing, ...patch });
  }

  function rebuildNodeMetaFromEdges() {
    state.nodeMeta.clear();
    const edges = [...state.queryEdges, ...state.databasedEdges];
    edges.forEach((edge) => {
      const sourcePatch = {
        origin: edge.origin === 'databased' ? 'databased' : 'query',
        isQueryDerived: edge.origin !== 'databased'
      };
      const targetPatch = {
        origin: edge.origin === 'databased' ? 'databased' : 'query',
        isQueryDerived: edge.origin !== 'databased'
      };
      const currentSource = state.nodeMeta.get(edge.source);
      const currentTarget = state.nodeMeta.get(edge.target);

      if (!currentSource || currentSource.origin !== 'query') {
        touchNodeMeta(edge.source, sourcePatch);
      }
      if (!currentTarget || currentTarget.origin !== 'query') {
        touchNodeMeta(edge.target, targetPatch);
      }
    });

    if (state.mainProtein) {
      touchNodeMeta(state.mainProtein, {
        origin: 'query',
        isQueryDerived: true,
        shell: 0,
        parentId: null,
        directionRole: 'main'
      });
    }
  }

  function normalizeQueryEdge(interaction) {
    const source = interaction.source;
    const target = interaction.target;
    if (!source || !target || source === target) return null;

    return {
      source,
      target,
      arrow: normalizeArrow(interaction.arrow),
      direction: normalizeDirection(interaction.direction),
      interactionType: normalizeInteractionType(interaction.interaction_type || interaction.type),
      origin: 'query',
      isDatabased: false,
      data: interaction
    };
  }

  function normalizeDatabasedEdge(parentProtein, row) {
    const partner = row.partner;
    if (!partner || partner === parentProtein) return null;

    let source = parentProtein;
    let target = partner;
    const direction = normalizeDirection(row.direction);

    if (direction === 'a_to_b' || direction === 'b_to_a') {
      const parentIsA = parentProtein < partner;
      if ((direction === 'a_to_b' && !parentIsA) || (direction === 'b_to_a' && parentIsA)) {
        source = partner;
        target = parentProtein;
      }
    } else if (direction === 'primary_to_main') {
      source = partner;
      target = parentProtein;
    } else if (direction === 'main_to_primary') {
      source = parentProtein;
      target = partner;
    }

    return {
      source,
      target,
      arrow: normalizeArrow(row.arrow),
      direction,
      interactionType: normalizeInteractionType(row.interaction_type),
      origin: 'databased',
      isDatabased: true,
      discoveredInQuery: row.discovered_in_query || 'unknown',
      data: row
    };
  }

  function setBaseGraph(mainProtein, interactions) {
    state.mainProtein = mainProtein || null;
    state.queryEdges = [];
    state.databasedEdges = [];
    state.edgeKeySet.clear();
    state.fetchedParents.clear();

    (interactions || []).forEach((interaction) => {
      const edge = normalizeQueryEdge(interaction);
      if (!edge) return;
      if (!registerEdge(edge)) return;
      state.queryEdges.push(edge);
    });

    rebuildNodeMetaFromEdges();
    state.version += 1;
  }

  function addDatabasedNeighbors(parentProtein, rows) {
    let added = 0;

    (rows || []).forEach((row) => {
      const edge = normalizeDatabasedEdge(parentProtein, row);
      if (!edge) return;
      if (!registerEdge(edge)) return;
      state.databasedEdges.push(edge);
      const existingSource = state.nodeMeta.get(edge.source);
      const existingTarget = state.nodeMeta.get(edge.target);
      touchNodeMeta(edge.source, {
        origin: (existingSource && existingSource.origin === 'query') || edge.source === state.mainProtein ? 'query' : 'databased',
        isQueryDerived: (existingSource && existingSource.isQueryDerived === true) || edge.source === state.mainProtein
      });
      touchNodeMeta(edge.target, {
        origin: (existingTarget && existingTarget.origin === 'query') || edge.target === state.mainProtein ? 'query' : 'databased',
        isQueryDerived: (existingTarget && existingTarget.isQueryDerived === true) || edge.target === state.mainProtein
      });
      added += 1;
    });

    if (added > 0) {
      state.version += 1;
    }

    return added;
  }

  function getAugmentedEdges() {
    return [...state.queryEdges, ...state.databasedEdges];
  }

  function getQueryEdges() {
    return [...state.queryEdges];
  }

  function getNodeMeta(nodeId) {
    return state.nodeMeta.get(nodeId) || null;
  }

  function setNodeMeta(nodeId, patch) {
    touchNodeMeta(nodeId, patch);
  }

  function getAllNodeMeta() {
    return state.nodeMeta;
  }

  function markParentFetched(parentProtein) {
    state.fetchedParents.add(parentProtein);
  }

  function hasFetchedParent(parentProtein) {
    return state.fetchedParents.has(parentProtein);
  }

  function setDatabasedNeighborCache(parentProtein, rows) {
    state.databasedNeighborCache.set(parentProtein, rows || []);
  }

  function getDatabasedNeighborCache(parentProtein) {
    return state.databasedNeighborCache.get(parentProtein) || null;
  }

  function clearFetchedParents() {
    state.fetchedParents.clear();
  }

  window.__PROPATH_MODEL = {
    setBaseGraph,
    addDatabasedNeighbors,
    getAugmentedEdges,
    getQueryEdges,
    getNodeMeta,
    setNodeMeta,
    getAllNodeMeta,
    markParentFetched,
    hasFetchedParent,
    setDatabasedNeighborCache,
    getDatabasedNeighborCache,
    clearFetchedParents,
    getVersion: function () { return state.version; },
    getMainProtein: function () { return state.mainProtein; }
  };
})();
