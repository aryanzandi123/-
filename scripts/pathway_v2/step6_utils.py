#!/usr/bin/env python3
"""
Step 6 Utilities - Foundation for Pathway Reorganization
=========================================================
Shared utilities for cycle detection, graph operations, and validation.
"""

import logging
from typing import Dict, Set, List, Tuple, Optional, Any
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from models import db

logger = logging.getLogger(__name__)


# ==============================================================================
# CONSTANTS
# ==============================================================================

STRICT_ROOTS = frozenset({
    "Proteostasis",
    "Metabolism & Bioenergetics",
    "Membrane & Transport",
    "Genome Maintenance",
    "Gene Expression",
    "Signal Transduction",
    "Cytoskeletal Dynamics"
})

# Pathways that legitimately belong at level 1 (don't try to deepen these)
LEGITIMATE_LEVEL1 = frozenset({
    "Apoptosis",
    "Autophagy",
    "Necroptosis",
    "Ferroptosis",
    "MAPK Signaling",
    "PI3K/Akt Signaling",
    "Wnt Signaling",
    "Notch Signaling",
    "Glycolysis",
    "Oxidative Phosphorylation",
    "Mitosis",
    "Meiosis",
})

# Keyword mapping for smart rescue fallback - matches pathway names to appropriate roots
ROOT_KEYWORDS = {
    "Proteostasis": [
        "protein fold", "ubiquitin", "proteasome", "autophagy", "chaperone",
        "aggregate", "erad", "misfolded", "degradation", "hsp", "heat shock",
        "deubiquit", "aggresome", "lysosom", "quality control", "proteostasis",
        "proteotoxic", "unfolded", "upr", "aggrephagy"
    ],
    "Metabolism & Bioenergetics": [
        "metabol", "glycol", "oxidat", "mitochond", "atp", "energy",
        "krebs", "tca", "fatty acid", "lipid", "glucose", "insulin",
        "ampk", "mtor", "nutrient", "biosynthesis", "bioenergetics",
        "respiration", "phosphorylation", "amino acid"
    ],
    "Membrane & Transport": [
        "vesicle", "exocyt", "endocyt", "traffic", "secretion", "golgi",
        "er-golgi", "snare", "rab", "clathrin", "copi", "copii", "membrane fusion",
        "ion transport", "membrane dynamics", "er transport", "lysosomal transport"
    ],
    "Genome Maintenance": [
        "dna repair", "replication", "chromatin", "histone", "nucleosome",
        "homologous recombination", "nhej", "base excision", "nucleotide excision",
        "telomere", "dna damage", "genome stability", "dna integrity",
        "double-strand break", "single-strand break"
    ],
    "Gene Expression": [
        "transcription", "translation", "rna processing", "splicing", "mrna",
        "ribosome", "polymerase", "promoter", "enhancer", "epigenetic",
        "gene regulation", "mrna stability", "rna polymerase", "transcription factor",
        "trna", "rrna"
    ],
    "Signal Transduction": [
        "signal", "kinase", "phosphat", "receptor", "mapk", "erk", "akt",
        "pi3k", "wnt", "notch", "hedgehog", "hippo", "jak", "stat",
        "nfkb", "tgf", "egf", "vegf", "transduction", "cascade",
        "apoptotic signaling", "cell cycle checkpoint", "immune signaling",
        "neuronal signaling"
    ],
    "Cytoskeletal Dynamics": [
        "actin", "tubulin", "cytoskelet", "microtubule", "motor", "dynein",
        "kinesin", "myosin", "intermediate filament", "cell shape", "motil",
        "migration", "focal adhesion", "lamellipod", "cytoskeletal dynamics",
        "cell adhesion"
    ]
}


# ==============================================================================
# DATA STRUCTURES
# ==============================================================================

class ChangeType(Enum):
    """Types of changes made during Step 6."""
    MERGE = "merge"
    REPARENT = "reparent"
    CREATE = "create"
    DELETE = "delete"
    REASSIGN_INTERACTION = "reassign_interaction"
    UPDATE_LEVEL = "update_level"


@dataclass
class Change:
    """Record of a single change made during Step 6."""
    change_type: ChangeType
    entity_type: str  # 'pathway', 'interaction', 'link'
    entity_id: int
    old_value: Any
    new_value: Any
    reason: str


@dataclass
class PhaseResult:
    """Result of a Step 6 phase."""
    phase_name: str
    success: bool
    changes: List[Change] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def add_change(self, change: Change):
        self.changes.append(change)

    def add_error(self, msg: str):
        self.errors.append(msg)
        logger.error(f"[{self.phase_name}] {msg}")

    def add_warning(self, msg: str):
        self.warnings.append(msg)
        logger.warning(f"[{self.phase_name}] {msg}")


@dataclass
class MigrationPlan:
    """Plan for migrating data when merging/moving pathways."""
    source_pathway_id: int
    target_pathway_id: int
    children_to_reparent: List[int] = field(default_factory=list)
    interactions_to_reassign: List[int] = field(default_factory=list)
    parent_links_to_transfer: List[int] = field(default_factory=list)

    def is_empty(self) -> bool:
        return (not self.children_to_reparent and
                not self.interactions_to_reassign and
                not self.parent_links_to_transfer)


# ==============================================================================
# GRAPH OPERATIONS
# ==============================================================================

def build_parent_graph(PathwayParent) -> Dict[int, List[int]]:
    """
    Build adjacency map: child_id -> [parent_ids].

    Returns dict where each key is a child pathway ID and value is list of parent IDs.
    """
    graph = defaultdict(list)
    for link in PathwayParent.query.all():
        graph[link.child_pathway_id].append(link.parent_pathway_id)
    return dict(graph)


def build_child_graph(PathwayParent) -> Dict[int, List[int]]:
    """
    Build reverse adjacency map: parent_id -> [child_ids].

    Returns dict where each key is a parent pathway ID and value is list of child IDs.
    """
    graph = defaultdict(list)
    for link in PathwayParent.query.all():
        graph[link.parent_pathway_id].append(link.child_pathway_id)
    return dict(graph)


def get_all_ancestors(pathway_id: int, parent_graph: Dict[int, List[int]]) -> Set[int]:
    """Get all ancestors of a pathway (transitive closure upward)."""
    ancestors = set()
    queue = deque(parent_graph.get(pathway_id, []))

    while queue:
        parent_id = queue.popleft()
        if parent_id not in ancestors:
            ancestors.add(parent_id)
            queue.extend(parent_graph.get(parent_id, []))

    return ancestors


def get_all_descendants(pathway_id: int, child_graph: Dict[int, List[int]]) -> Set[int]:
    """Get all descendants of a pathway (transitive closure downward)."""
    descendants = set()
    queue = deque(child_graph.get(pathway_id, []))

    while queue:
        child_id = queue.popleft()
        if child_id not in descendants:
            descendants.add(child_id)
            queue.extend(child_graph.get(child_id, []))

    return descendants


# ==============================================================================
# CYCLE DETECTION
# ==============================================================================

def detect_cycle_from_node(
    start_node: int,
    parent_graph: Dict[int, List[int]]
) -> Optional[List[int]]:
    """
    Detect if there's a cycle reachable from start_node going upward through parents.

    Returns the cycle path if found, None otherwise.
    """
    visited = set()
    rec_stack = set()
    path = []

    def dfs(node: int) -> Optional[List[int]]:
        visited.add(node)
        rec_stack.add(node)
        path.append(node)

        for parent in parent_graph.get(node, []):
            if parent in rec_stack:
                # Found cycle - return the cycle portion
                cycle_start = path.index(parent)
                return path[cycle_start:] + [parent]

            if parent not in visited:
                result = dfs(parent)
                if result:
                    return result

        path.pop()
        rec_stack.remove(node)
        return None

    return dfs(start_node)


def would_create_cycle(
    child_id: int,
    proposed_parent_id: int,
    parent_graph: Dict[int, List[int]]
) -> bool:
    """
    Check if adding child_id -> proposed_parent_id would create a cycle.

    A cycle would occur if proposed_parent_id is already a descendant of child_id,
    or equivalently, if child_id is an ancestor of proposed_parent_id.
    """
    # If the proposed parent is the same as child, that's a self-loop
    if child_id == proposed_parent_id:
        return True

    # Check if child_id is an ancestor of proposed_parent_id
    # by traversing upward from proposed_parent_id
    ancestors_of_proposed = get_all_ancestors(proposed_parent_id, parent_graph)

    return child_id in ancestors_of_proposed


def find_all_cycles(parent_graph: Dict[int, List[int]]) -> List[List[int]]:
    """
    Find all cycles in the graph.

    Returns list of cycles, where each cycle is a list of pathway IDs.
    """
    all_nodes = set(parent_graph.keys())
    for parents in parent_graph.values():
        all_nodes.update(parents)

    visited = set()
    cycles = []

    for node in all_nodes:
        if node not in visited:
            cycle = detect_cycle_from_node(node, parent_graph)
            if cycle:
                # Normalize cycle (start from smallest ID) to avoid duplicates
                min_idx = cycle.index(min(cycle[:-1]))  # Exclude last element (duplicate of first)
                normalized = cycle[min_idx:-1] + cycle[:min_idx] + [cycle[min_idx]]
                if normalized not in cycles:
                    cycles.append(normalized)
            visited.add(node)

    return cycles


def detect_multi_parent_nodes(parent_graph: Dict[int, List[int]]) -> Dict[int, List[int]]:
    """
    Find all nodes with more than one parent.

    Returns dict: pathway_id -> [parent_ids] for nodes with multiple parents.
    """
    return {
        child_id: parents
        for child_id, parents in parent_graph.items()
        if len(parents) > 1
    }


# ==============================================================================
# HIERARCHY LEVEL CALCULATION
# ==============================================================================

def calculate_hierarchy_levels(
    Pathway,
    parent_graph: Dict[int, List[int]]
) -> Dict[int, int]:
    """
    Calculate hierarchy levels for all pathways using BFS from roots.

    Returns dict: pathway_id -> level (0 for roots, -1 for unreachable).
    """
    levels = {}

    # Initialize all pathways to -1 (unreachable)
    for pw in Pathway.query.all():
        levels[pw.id] = -1

    # Find roots and set them to level 0
    roots = Pathway.query.filter(Pathway.name.in_(STRICT_ROOTS)).all()
    queue = deque()

    for root in roots:
        levels[root.id] = 0
        queue.append(root.id)

    # Build child graph for BFS traversal downward
    child_graph = defaultdict(list)
    for child_id, parents in parent_graph.items():
        for parent_id in parents:
            child_graph[parent_id].append(child_id)

    # BFS to set levels
    while queue:
        current_id = queue.popleft()
        current_level = levels[current_id]

        for child_id in child_graph.get(current_id, []):
            if levels[child_id] == -1:  # Not yet visited
                levels[child_id] = current_level + 1
                queue.append(child_id)

    return levels


def find_orphan_pathways(levels: Dict[int, int]) -> List[int]:
    """Find pathway IDs that are unreachable from roots (level == -1)."""
    return [pw_id for pw_id, level in levels.items() if level == -1]


# ==============================================================================
# VALIDATION HELPERS
# ==============================================================================

def validate_pathway_exists(pathway_id: int, Pathway) -> bool:
    """Check if a pathway with given ID exists."""
    return db.session.get(Pathway, pathway_id) is not None


def validate_pathway_name_exists(name: str, Pathway) -> Optional[int]:
    """Check if a pathway with given name exists. Returns ID if found, None otherwise."""
    pw = Pathway.query.filter_by(name=name).first()
    return pw.id if pw else None


def validate_no_orphan_interactions(db, Interaction, PathwayInteraction) -> List[int]:
    """
    Find interactions that have no PathwayInteraction record.

    Returns list of orphaned interaction IDs.
    """
    from sqlalchemy import text

    result = db.session.execute(text("""
        SELECT i.id
        FROM interactions i
        LEFT JOIN pathway_interactions pi ON i.id = pi.interaction_id
        WHERE pi.id IS NULL
    """))

    return [row[0] for row in result]


def validate_no_dangling_pathway_links(db, PathwayInteraction, Pathway) -> List[int]:
    """
    Find PathwayInteraction records pointing to non-existent pathways.

    Returns list of dangling PathwayInteraction IDs.
    """
    from sqlalchemy import text

    result = db.session.execute(text("""
        SELECT pi.id
        FROM pathway_interactions pi
        LEFT JOIN pathways p ON pi.pathway_id = p.id
        WHERE p.id IS NULL
    """))

    return [row[0] for row in result]


# ==============================================================================
# LLM RESPONSE HELPERS
# ==============================================================================

def is_json_truncated(text: str) -> bool:
    """
    Check if a JSON response appears to be truncated.

    Detects unclosed brackets/braces.
    """
    if not text:
        return True

    # Count brackets
    open_braces = text.count('{') - text.count('}')
    open_brackets = text.count('[') - text.count(']')

    # Check for unclosed string
    in_string = False
    escape_next = False
    for char in text:
        if escape_next:
            escape_next = False
            continue
        if char == '\\':
            escape_next = True
            continue
        if char == '"':
            in_string = not in_string

    return open_braces != 0 or open_brackets != 0 or in_string


def calculate_safe_batch_size(
    items: List[Any],
    initial_size: int = 5,
    min_size: int = 1
) -> List[List[Any]]:
    """
    Split items into batches, starting with initial_size.

    Returns list of batches.
    """
    batches = []
    for i in range(0, len(items), initial_size):
        batches.append(items[i:i + initial_size])
    return batches


class AdaptiveBatcher:
    """
    Adaptive batching with automatic size reduction on failure.

    Usage:
        batcher = AdaptiveBatcher(items, initial_size=5)
        while batcher.has_next():
            batch = batcher.get_next_batch()
            success = process_batch(batch)
            if success:
                batcher.mark_success()
            else:
                batcher.mark_failure()  # Will retry with smaller batch
    """

    def __init__(
        self,
        items: List[Any],
        initial_size: int = 5,
        min_size: int = 1,
        max_retries: int = 3
    ):
        self.items = items
        self.current_size = initial_size
        self.min_size = min_size
        self.max_retries = max_retries
        self.position = 0
        self.retry_count = 0
        self.failed_items: List[Any] = []

    def has_next(self) -> bool:
        return self.position < len(self.items)

    def get_next_batch(self) -> List[Any]:
        end = min(self.position + self.current_size, len(self.items))
        return self.items[self.position:end]

    def mark_success(self):
        """Mark current batch as successful, move to next batch."""
        self.position += self.current_size
        self.retry_count = 0
        # Gradually restore batch size after success
        if self.current_size < 5:
            self.current_size = min(self.current_size + 1, 5)

    def mark_failure(self) -> bool:
        """
        Mark current batch as failed.

        Returns True if will retry with smaller batch, False if giving up.
        """
        self.retry_count += 1

        if self.retry_count >= self.max_retries:
            # Give up on this batch, save failed items
            batch = self.get_next_batch()
            self.failed_items.extend(batch)
            self.position += len(batch)
            self.retry_count = 0
            self.current_size = max(self.min_size, self.current_size)
            return False

        # Reduce batch size for retry
        if self.current_size > self.min_size:
            self.current_size = max(self.min_size, self.current_size // 2)
            return True

        # Already at minimum size, give up on this item
        batch = self.get_next_batch()
        self.failed_items.extend(batch)
        self.position += len(batch)
        self.retry_count = 0
        return False

    def get_failed_items(self) -> List[Any]:
        return self.failed_items


# ==============================================================================
# MIGRATION HELPERS
# ==============================================================================

def build_merge_migration_plan(
    source_id: int,
    target_id: int,
    PathwayParent,
    PathwayInteraction
) -> MigrationPlan:
    """
    Build a migration plan for merging source pathway into target.

    Identifies all children, interactions, and parent links that need to move.
    """
    plan = MigrationPlan(source_pathway_id=source_id, target_pathway_id=target_id)

    # Children to reparent (source's children -> target)
    children = PathwayParent.query.filter_by(parent_pathway_id=source_id).all()
    plan.children_to_reparent = [c.child_pathway_id for c in children]

    # Interactions to reassign (source's interactions -> target)
    interactions = PathwayInteraction.query.filter_by(pathway_id=source_id).all()
    plan.interactions_to_reassign = [i.interaction_id for i in interactions]

    # Parent links to transfer (source's parents -> target's parents)
    parent_links = PathwayParent.query.filter_by(child_pathway_id=source_id).all()
    plan.parent_links_to_transfer = [p.parent_pathway_id for p in parent_links]

    return plan


def execute_migration_plan(
    plan: MigrationPlan,
    db,
    Pathway,
    PathwayParent,
    PathwayInteraction
) -> Tuple[bool, List[str]]:
    """
    Execute a migration plan atomically.

    Returns (success, list of error messages).
    """
    errors = []

    try:
        target = db.session.get(Pathway, plan.target_pathway_id)
        source = db.session.get(Pathway, plan.source_pathway_id)

        if not target:
            return False, [f"Target pathway {plan.target_pathway_id} not found"]
        if not source:
            return False, [f"Source pathway {plan.source_pathway_id} not found"]

        # Reparent children
        for child_id in plan.children_to_reparent:
            link = PathwayParent.query.filter_by(
                child_pathway_id=child_id,
                parent_pathway_id=plan.source_pathway_id
            ).first()
            if link:
                # Check if target already has this child
                existing = PathwayParent.query.filter_by(
                    child_pathway_id=child_id,
                    parent_pathway_id=plan.target_pathway_id
                ).first()
                if existing:
                    db.session.delete(link)
                else:
                    link.parent_pathway_id = plan.target_pathway_id

        # Reassign interactions
        for interaction_id in plan.interactions_to_reassign:
            pi = PathwayInteraction.query.filter_by(
                pathway_id=plan.source_pathway_id,
                interaction_id=interaction_id
            ).first()
            if pi:
                # Check if target already has this interaction
                existing = PathwayInteraction.query.filter_by(
                    pathway_id=plan.target_pathway_id,
                    interaction_id=interaction_id
                ).first()
                if existing:
                    db.session.delete(pi)
                else:
                    pi.pathway_id = plan.target_pathway_id

        # Transfer parent links (source's parents become target's parents if not already)
        for parent_id in plan.parent_links_to_transfer:
            # Delete source's parent link
            source_link = PathwayParent.query.filter_by(
                child_pathway_id=plan.source_pathway_id,
                parent_pathway_id=parent_id
            ).first()
            if source_link:
                db.session.delete(source_link)

            # Add to target if doesn't exist
            existing = PathwayParent.query.filter_by(
                child_pathway_id=plan.target_pathway_id,
                parent_pathway_id=parent_id
            ).first()
            if not existing and parent_id != plan.target_pathway_id:
                new_link = PathwayParent(
                    child_pathway_id=plan.target_pathway_id,
                    parent_pathway_id=parent_id,
                    relationship_type='is_a'
                )
                db.session.add(new_link)

        # Delete source pathway
        db.session.delete(source)
        db.session.commit()

        logger.info(f"Migrated pathway '{source.name}' into '{target.name}'")
        return True, []

    except Exception as e:
        db.session.rollback()
        error_msg = f"Migration failed: {e}"
        logger.error(error_msg)
        return False, [error_msg]


# ==============================================================================
# CHECKPOINT HELPERS
# ==============================================================================

def save_checkpoint(
    db,
    Interaction,
    phase: int,
    status: str,
    changes: List[Change]
) -> bool:
    """
    Save a checkpoint to mark phase completion.

    Stores in the first interaction's data field for persistence.
    """
    import json
    from datetime import datetime, timezone

    try:
        # Get any interaction to store checkpoint
        interaction = Interaction.query.first()
        if not interaction:
            logger.warning("No interactions found, cannot save checkpoint")
            return False

        if not interaction.data:
            interaction.data = {}

        checkpoint = {
            'phase': phase,
            'status': status,
            'timestamp': datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            'change_count': len(changes)
        }

        interaction.data['_step6_checkpoint'] = checkpoint
        db.session.commit()

        logger.info(f"Checkpoint saved: Phase {phase} - {status}")
        return True

    except Exception as e:
        logger.error(f"Failed to save checkpoint: {e}")
        return False


def load_checkpoint(Interaction) -> Optional[Dict]:
    """Load the last checkpoint from interaction data."""
    interaction = Interaction.query.first()
    if interaction and interaction.data:
        return interaction.data.get('_step6_checkpoint')
    return None


# ==============================================================================
# SMART RESCUE - LLM-BASED ROOT SELECTION
# ==============================================================================

SMART_ROOT_PROMPT = """Given the biological pathway "{pathway_name}", which ROOT category does it MOST belong to?

AVAILABLE ROOTS (pick exactly one):
1. Proteostasis - Protein homeostasis, folding, degradation, autophagy, proteasome, chaperones, aggregation, ERAD
2. Metabolism & Bioenergetics - Energy production, biosynthesis, metabolic pathways, glycolysis, oxidative phosphorylation, mitochondrial function
3. Membrane & Transport - Membrane dynamics, vesicle trafficking, exocytosis, endocytosis, membrane fusion, secretion, ion transport
4. Genome Maintenance - DNA repair, replication, chromatin organization, genome stability, DNA damage response
5. Gene Expression - Transcription, translation, RNA processing, splicing, gene regulation, epigenetics
6. Signal Transduction - Signal transduction, receptor signaling, kinase cascades, MAPK, PI3K, Wnt, Notch, apoptotic/immune/neuronal signaling
7. Cytoskeletal Dynamics - Actin, microtubules, cell shape, motility, cytoskeletal organization, cell adhesion, migration

RESPOND WITH EXACTLY ONE ROOT NAME from this list:
Proteostasis, Metabolism & Bioenergetics, Membrane & Transport, Genome Maintenance, Gene Expression, Signal Transduction, Cytoskeletal Dynamics"""


def _keyword_rescue_match(pathway_name: str) -> str:
    """
    Match pathway name to root using keyword scoring.

    Returns:
        Best matching root name, or None if no match found
    """
    pathway_lower = pathway_name.lower()
    best_match = None
    best_score = 0

    for root, keywords in ROOT_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in pathway_lower)
        if score > best_score:
            best_score = score
            best_match = root

    return best_match if best_score > 0 else None


def get_smart_rescue_parent(pathway_name: str, Pathway, use_cache: bool = True):
    """
    Use LLM to pick the semantically correct root for an orphan pathway.
    Falls back to keyword matching if LLM fails.

    Args:
        pathway_name: Name of the orphan pathway to rescue
        Pathway: The Pathway model class
        use_cache: Whether to use cached results

    Returns:
        Pathway object for the appropriate root, or None if all fails
    """
    from scripts.pathway_v2.llm_utils import _call_gemini_json_cached, _call_gemini_json

    # Try cache first
    if use_cache:
        try:
            resp = _call_gemini_json_cached(
                SMART_ROOT_PROMPT.format(pathway_name=pathway_name),
                cache_key=f"rescue_{pathway_name}",
                cache_type="rescue",
                
            )
        except Exception:
            resp = None
    else:
        try:
            resp = _call_gemini_json(
                SMART_ROOT_PROMPT.format(pathway_name=pathway_name),
                
            )
        except Exception:
            resp = None

    # Parse response - could be dict with various keys or plain text
    root_name = None
    if resp:
        if isinstance(resp, dict):
            # Try multiple possible keys
            root_name = (resp.get('root') or resp.get('answer') or
                        resp.get('category') or resp.get('result') or
                        resp.get('selected_root') or resp.get('pathway'))
        elif isinstance(resp, str):
            root_name = resp.strip()

    # Find matching root from LLM response
    if root_name:
        root_name_lower = root_name.lower()
        for strict_root in STRICT_ROOTS:
            if strict_root.lower() in root_name_lower or root_name_lower in strict_root.lower():
                found = Pathway.query.filter_by(name=strict_root).first()
                if found:
                    logger.info(f"Smart rescue: '{pathway_name}' -> '{strict_root}'")
                    return found

    # Fallback 1: Try keyword-based matching before defaulting to Protein Quality Control
    keyword_match = _keyword_rescue_match(pathway_name)
    if keyword_match:
        found = Pathway.query.filter_by(name=keyword_match).first()
        if found:
            logger.info(f"Keyword rescue: '{pathway_name}' -> '{keyword_match}'")
            return found

    # Fallback 2: Default to Protein Quality Control only if all else fails
    logger.warning(f"All rescue methods failed for '{pathway_name}', using Protein Quality Control fallback")
    return Pathway.query.filter_by(name="Protein Quality Control").first()


def verify_chain_reaches_root(pathway_id: int, PathwayParent, Pathway) -> bool:
    """
    Walk up the parent chain and verify it reaches a STRICT_ROOT.

    Args:
        pathway_id: ID of the pathway to verify
        PathwayParent: The PathwayParent model class
        Pathway: The Pathway model class

    Returns:
        True if chain reaches a root, False otherwise
    """
    visited = set()
    current = pathway_id

    while current:
        if current in visited:
            return False  # Cycle detected
        visited.add(current)

        pw = db.session.get(Pathway, current)
        if pw and pw.name in STRICT_ROOTS:
            return True

        parent_link = PathwayParent.query.filter_by(child_pathway_id=current).first()
        if not parent_link:
            return False  # Chain broken
        current = parent_link.parent_pathway_id

    return False


def find_broken_chains(Pathway, PathwayParent) -> List[int]:
    """
    Find all pathways whose chains don't reach a root.

    Returns:
        List of pathway IDs with broken chains
    """
    broken = []
    for pw in Pathway.query.all():
        if pw.name not in STRICT_ROOTS:
            if not verify_chain_reaches_root(pw.id, PathwayParent, Pathway):
                broken.append(pw.id)
    return broken
