#!/usr/bin/env python3
"""
SQLAlchemy Models for Protein Interaction Database

Tables:
- proteins: Core protein entities with query tracking
- interactions: Protein-protein relationships with full JSONB payload
- pathways: Biological pathways for grouping interactions (KEGG/Reactome/GO mapped)
- pathway_interactions: Many-to-many linking pathways to interactions
- pathway_parents: DAG hierarchy linking child pathways to parent pathways
- indirect_chains: Full indirect chain entities grouping claims across interactions
- interaction_claims: Atomic scientific claims extracted from interaction JSONB
"""

from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timezone
from sqlalchemy.dialects.postgresql import JSONB
from typing import Optional

from utils.interaction_contract import normalize_arrow

db = SQLAlchemy()


def _utcnow() -> datetime:
    """SQLAlchemy-compatible drop-in for the deprecated ``datetime.utcnow``.

    Returns a naive UTC datetime (tzinfo stripped) so the DB columns stay
    naive — matching the existing schema — while avoiding the Python
    3.14 deprecation path. Pass as ``default=_utcnow`` / ``onupdate=_utcnow``;
    SQLAlchemy invokes it at insert/update time.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Protein(db.Model):
    """
    Protein entity with query tracking and metadata.

    Invariants:
    - symbol is unique (enforced by DB constraint)
    - query_count increments on each query
    - total_interactions updated after sync
    """
    __tablename__ = 'proteins'

    # Primary key
    id = db.Column(db.Integer, primary_key=True)

    # Protein identifier (unique, indexed for fast lookups)
    symbol = db.Column(db.String(50), unique=True, nullable=False, index=True)

    # Query tracking
    first_queried = db.Column(db.DateTime, default=_utcnow, server_default=db.func.now(), nullable=False)
    last_queried = db.Column(db.DateTime, default=_utcnow, server_default=db.func.now(), nullable=False)
    query_count = db.Column(db.Integer, default=0, server_default='0', nullable=False)
    total_interactions = db.Column(db.Integer, default=0, server_default='0', nullable=False)

    # Flexible metadata storage (JSONB for schema evolution)
    # Note: Using 'extra_data' instead of 'metadata' (reserved by SQLAlchemy)
    extra_data = db.Column(JSONB, server_default='{}', nullable=False)

    # Pipeline tracking (crash recovery)
    pipeline_status = db.Column(db.String(20), default="idle", index=True)
    last_pipeline_phase = db.Column(db.String(50))

    # Audit timestamps
    created_at = db.Column(db.DateTime, default=_utcnow, server_default=db.func.now(), nullable=False)
    updated_at = db.Column(db.DateTime, default=_utcnow, server_default=db.func.now(), onupdate=_utcnow, nullable=False)

    # Relationships (one-to-many with interactions)
    interactions_as_a = db.relationship(
        'Interaction',
        foreign_keys='Interaction.protein_a_id',
        backref='protein_a_obj',
        lazy='dynamic'
    )
    interactions_as_b = db.relationship(
        'Interaction',
        foreign_keys='Interaction.protein_b_id',
        backref='protein_b_obj',
        lazy='dynamic'
    )

    def __repr__(self) -> str:
        return f'<Protein {self.symbol}{" [pseudo]" if self.is_pseudo else ""}>'

    @property
    def is_pseudo(self) -> bool:
        """True for generic biomolecule classes stored as Protein rows.

        Pseudo entities (RNA, Ubiquitin, Proteasome, etc.) are real DB rows
        so chain hops can reference them, but they are NOT valid stand-alone
        interactor queries. The frontend renders them italicized.
        """
        return bool((self.extra_data or {}).get("is_pseudo", False))


class ProteinAlias(db.Model):
    """Alias-to-canonical mapping for protein symbol resolution.

    Lets callers look a protein up by any known alternative name —
    common synonyms ("MJD1" → ATXN3), full names ("Ataxin-3" → ATXN3),
    Greek-letter variants ("α-synuclein" → SNCA), hyphenation variants
    ("BCL-2" → BCL2), etc. — and always resolve to the canonical row.

    Invariants:
      - ``alias_symbol`` is stored uppercased / trimmed, matching the
        normalization used by ``utils.protein_aliases.normalize_symbol``.
      - ``(alias_symbol)`` is globally unique: an alias can only point
        to one canonical protein. This is what prevents the same
        string from resolving to two different canonical rows.
      - ``protein_id`` FK cascades on Protein delete so aliases of a
        removed protein are cleaned up automatically.
    """
    __tablename__ = 'protein_aliases'

    id = db.Column(db.Integer, primary_key=True)

    # The alternative name, normalized (uppercase + stripped). Unique
    # so the lookup path can trust a single answer.
    alias_symbol = db.Column(db.String(100), unique=True, nullable=False, index=True)

    # Canonical protein this alias resolves to.
    protein_id = db.Column(
        db.Integer,
        db.ForeignKey('proteins.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )

    # Where this alias came from so ops can distinguish curated seeds
    # (HGNC_SEED, GREEK_NORMALIZATION, etc.) from LLM-discovered aliases
    # and user-submitted corrections.
    source = db.Column(db.String(32), default='curated', server_default='curated', nullable=False)

    created_at = db.Column(
        db.DateTime, default=_utcnow,
        server_default=db.func.now(), nullable=False,
    )

    protein = db.relationship(
        'Protein',
        backref=db.backref('aliases', cascade='all, delete-orphan', lazy='dynamic'),
    )

    def __repr__(self) -> str:
        return f'<ProteinAlias {self.alias_symbol} -> {self.protein_id}>'


class Interaction(db.Model):
    """
    Protein-protein interaction with full JSONB payload.

    Invariants:
    - (protein_a_id, protein_b_id) is unique
    - protein_a_id != protein_b_id (no self-interactions)
    - data JSONB contains full pipeline output (evidence, functions, PMIDs)
    - interaction_type: 'direct' (physical) or 'indirect' (cascade/pathway)
    - upstream_interactor: required for indirect interactions, null for direct
    - mediator_chain: array of mediator proteins for multi-hop paths
    - depth: 1=direct, 2+=indirect (number of hops from query protein)
    - chain_context: stores interaction from all protein perspectives in chain

    Dual-Track System (for indirect chains):
    - function_context: 'direct' (pair-specific validation), 'net' (NET effect via chain), null (legacy)
    - Example: ATXN3→RHEB→MTOR chain creates TWO records:
      1. ATXN3→MTOR: interaction_type='indirect', function_context='net' (chain NET effect)
      2. RHEB→MTOR: interaction_type='direct', function_context='direct', _inferred_from_chain=True (extracted mediator link)
    """
    __tablename__ = 'interactions'

    # Primary key
    id = db.Column(db.Integer, primary_key=True)

    # Foreign keys (protein pair)
    protein_a_id = db.Column(
        db.Integer,
        db.ForeignKey('proteins.id', ondelete='CASCADE'),
        nullable=False,
        index=True
    )
    protein_b_id = db.Column(
        db.Integer,
        db.ForeignKey('proteins.id', ondelete='CASCADE'),
        nullable=False,
        index=True
    )

    # Denormalized fields for fast filtering (extracted from data JSONB)
    confidence = db.Column(db.Numeric(3, 2), index=True)  # 0.00 to 1.00
    # 2026-04-30 — runtime audit confirmed every Interaction writer sets
    # ``direction`` explicitly (utils/db_sync.py:_save_interaction falls
    # back to 'a_to_b'; utils/direction.py:infer_direction_from_arrow
    # never returns NULL). Tightening to NOT NULL matches what the live
    # DB has had via ``ALTER COLUMN ... SET NOT NULL`` since
    # migrate_add_chain_table-era and removes a class of "row written
    # without direction" bugs the model would otherwise tolerate.
    direction = db.Column(db.String(20), nullable=False)  # 'main_to_primary', 'primary_to_main' (or canonical 'a_to_b', 'b_to_a')
    arrow = db.Column(db.String(50))  # 'binds', 'activates', 'inhibits', 'regulates' (BACKWARD COMPAT: primary arrow)
    arrows = db.Column(JSONB, nullable=True)  # NEW (Issue #4): Multiple arrow types per direction {'main_to_primary': ['activates', 'inhibits'], ...}
    interaction_type = db.Column(db.String(100))  # 'direct' (physical) or 'indirect' (cascade/pathway)
    upstream_interactor = db.Column(db.String(50), nullable=True)  # Upstream protein symbol for indirect interactions
    function_context = db.Column(
        db.String(20),
        nullable=False,
        default='direct',
        server_default='direct',
    )  # 'direct' (pair-specific), 'net' (NET effect via chain), 'chain_derived', 'mixed'

    # Chain metadata for multi-level indirect interactions. Chain length is
    # NOT capped — a 4+ mediator cascade (e.g. ATXN3→VCP→LAMP2→RAB7→target)
    # is valid and should be stored exactly as reported by the pipeline.
    mediator_chain = db.Column(JSONB, nullable=True)  # Full chain path e.g., ["VCP", "LAMP2", "RAB7"] for ATXN3→VCP→LAMP2→RAB7→target
    depth = db.Column(db.Integer, default=1, nullable=False)  # 1=direct; N>=2 for indirect interactions via (N-1) mediators (no upper cap)
    chain_context = db.Column(JSONB, nullable=True)  # Stores full chain context from all protein perspectives
    chain_with_arrows = db.Column(JSONB, nullable=True)  # NEW (Issue #2): Chain with typed arrows [{"from": "VCP", "to": "IκBά", "arrow": "inhibits"}, ...]

    # FULL PAYLOAD - Stores complete interactor JSON from pipeline
    # Contains: evidence[], functions[], pmids[], support_summary, etc.
    # Dual-track flags: _inferred_from_chain, _net_effect, _direct_mediator_link, _display_badge
    #
    # 2026-04-29 — reverted the S4d nullable=True deprecation. Multiple
    # readers (services/data_builder.build_full_json_from_db, modal
    # rendering paths) call ``interaction.data.copy()`` /
    # ``interaction.data.get(...)`` without null guards. With nullable=True
    # plus a writer that skips the blob, every read crashes. The blob is
    # cheap and useful as a stable bag for fields that don't (yet) have
    # first-class columns; keep it NOT NULL with an empty-dict default and
    # let writers always emit at least ``{}``. A separate migration
    # backfills any historical NULL rows.
    data = db.Column(
        JSONB,
        nullable=False,
        server_default='{}',
        default=dict,
    )

    # Discovery metadata
    discovered_in_query = db.Column(db.String(50))  # Which protein query found this
    discovery_method = db.Column(db.String(50), default='pipeline', server_default='pipeline')  # 'pipeline', 'requery', 'manual'

    # Canonical link to the IndirectChain this interaction participates in.
    # One chain may have several participating interactions (the hop links
    # + the net-effect row); they all share a single ``chain_id`` so chain
    # state is stored ONCE in ``indirect_chains`` instead of duplicated in
    # every participant's ``data.chain_context`` JSONB. ``ondelete=SET NULL``
    # means deleting the chain won't cascade-delete its participants.
    chain_id = db.Column(
        db.Integer,
        db.ForeignKey('indirect_chains.id', ondelete='SET NULL'),
        nullable=True,
    )

    # Audit timestamps
    created_at = db.Column(db.DateTime, default=_utcnow, server_default=db.func.now(), nullable=False)
    updated_at = db.Column(db.DateTime, default=_utcnow, server_default=db.func.now(), onupdate=_utcnow, nullable=False)

    # Constraints and indexes
    __table_args__ = (
        # Prevent duplicate interactions
        db.UniqueConstraint('protein_a_id', 'protein_b_id', name='interaction_unique'),
        # Prevent self-interactions
        db.CheckConstraint('protein_a_id != protein_b_id', name='interaction_proteins_different'),
        # Indexes for chain queries
        db.Index('idx_interactions_depth', 'depth'),
        db.Index('idx_interactions_interaction_type', 'interaction_type'),
        db.Index('idx_interactions_chain_id', 'chain_id'),
        db.Index('idx_interactions_function_context', 'function_context'),
        db.Index('idx_interactions_upstream', 'upstream_interactor'),
        db.Index('idx_interactions_data_gin', 'data', postgresql_using='gin'),
        # Enum validation
        db.CheckConstraint(
            "function_context IN ('direct', 'net', 'chain_derived', 'mixed')",
            name='valid_function_context'
        ),
        db.CheckConstraint(
            "arrow IS NULL OR arrow IN ('activates', 'inhibits', 'binds', 'regulates')",
            name='valid_interaction_arrow'
        ),
        db.CheckConstraint(
            "interaction_type IS NULL OR interaction_type IN ('direct', 'indirect')",
            name='valid_interaction_type'
        ),
        # S1: no more bidirectional. Historical rows migrated via
        # scripts/migrate_kill_bidirectional.py. New writes must be
        # asymmetric ('a_to_b' or 'b_to_a' in canonical form, or
        # 'main_to_primary' / 'primary_to_main' in semantic form).
        # ``direction`` is now NOT NULL (column declaration above), so the
        # CHECK no longer needs an ``IS NULL OR`` escape — every row must
        # carry one of the four asymmetric values.
        db.CheckConstraint(
            "direction IN ('a_to_b', 'b_to_a', 'main_to_primary', 'primary_to_main')",
            name='no_bidirectional_direction'
        ),
        # S4b: confidence must be in [0, 1] if set
        db.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name='valid_confidence_range'
        ),
        db.Index('idx_interaction_pair_lookup', 'protein_a_id', 'protein_b_id'),
        db.Index('idx_interaction_pair_context', 'protein_a_id', 'protein_b_id', 'function_context'),
        db.Index('idx_interaction_discovered_in', 'discovered_in_query'),
    )

    # Relationships (many-to-one with proteins)
    protein_a = db.relationship('Protein', foreign_keys=[protein_a_id], overlaps="interactions_as_a,protein_a_obj")
    protein_b = db.relationship('Protein', foreign_keys=[protein_b_id], overlaps="interactions_as_b,protein_b_obj")

    # Relationship to the linked IndirectChain (via ``chain_id`` FK).
    # ``foreign_keys`` must be explicit because ``IndirectChain`` also
    # carries the legacy ``origin_interaction_id`` FK in the opposite
    # direction — without this hint SQLAlchemy can't pick which FK to
    # use for the relationship.
    linked_chain = db.relationship(
        'IndirectChain',
        foreign_keys=[chain_id],
        backref=db.backref('participants', lazy='dynamic'),
    )

    @property
    def primary_arrow(self) -> str:
        """Derive primary arrow from arrows JSONB (canonical source of truth).

        Falls back to the legacy ``arrow`` column, then to ``'binds'``.

        B6 deprecation: writers MUST route through ``set_primary_arrow``
        below, never assign ``.arrow`` directly. The scalar column is kept
        for read compatibility while pre-migration code paths still exist;
        once every writer is audited, a follow-up migration drops it.
        """
        if self.arrows:
            for key in ('a_to_b', 'b_to_a'):
                vals = self.arrows.get(key)
                if vals:
                    return normalize_arrow(vals[0], default='binds')
        return normalize_arrow(self.arrow, default='binds')

    def set_primary_arrow(self, value: str, direction: str = "a_to_b") -> None:
        """Canonical writer for arrow state. Updates BOTH columns atomically.

        Writes to ``arrows`` JSONB first (the new source of truth), then
        mirrors into the legacy ``arrow`` scalar so un-migrated readers
        keep working. Callers should use this instead of direct attribute
        assignment — B6 aims to eliminate the triple-storage drift where
        different writers updated different columns.

        ``direction`` is ``"a_to_b"`` (canonical) or ``"b_to_a"``. If both
        are provided across multiple calls, the JSONB accumulates both
        keys; ``primary_arrow`` reads ``a_to_b`` first.
        """
        if not value:
            return
        clean = normalize_arrow(value, default='binds')
        current = dict(self.arrows or {})
        # Replace, don't append — one value per direction keeps the
        # property deterministic. Callers that want multi-arrow per
        # direction can still assign to ``.arrows`` directly.
        current[direction] = [clean]
        self.arrows = current
        self.arrow = clean  # mirror for legacy readers

    @property
    def chain_view(self):
        """Return a :class:`~utils.chain_view.ChainView` for this row.

        The chain view is the single source of truth for chain state:
        callers that want ``full_chain``, ``mediator_chain``,
        ``upstream_interactor``, ``depth``, or any query-relative
        neighbor should read from this view rather than touching the
        legacy columns directly. The view reads from the linked
        ``IndirectChain`` (via ``chain_id``) when present and falls back
        to the JSONB ``data.chain_context`` / ``mediator_chain`` for
        older rows that were written before #6 landed.
        """
        from utils.chain_view import chain_view_from_interaction
        return chain_view_from_interaction(self)

    @property
    def computed_mediator_chain(self):
        """Derived mediator_chain (always consistent with full_chain).

        Prefer this over the raw ``mediator_chain`` column — the column
        can be stale if a writer updated chain_context without also
        touching the column, whereas this property reads from the
        canonical chain view.
        """
        return self.chain_view.mediator_chain

    @property
    def computed_upstream_interactor(self):
        """Derived upstream_interactor (always consistent with full_chain)."""
        return self.chain_view.upstream_interactor

    @property
    def computed_depth(self) -> int:
        """Derived depth (always consistent with full_chain)."""
        cv = self.chain_view
        if cv.is_empty:
            # Direct interactions (no chain) default to depth=1.
            return self.depth or 1
        return cv.depth

    def __repr__(self) -> str:
        a_symbol = self.protein_a.symbol if self.protein_a else '?'
        b_symbol = self.protein_b.symbol if self.protein_b else '?'
        return f'<Interaction {a_symbol} ↔ {b_symbol}>'


class Pathway(db.Model):
    """
    Biological pathway for grouping protein interactions.

    Invariants:
    - name is unique (enforced by DB constraint)
    - ontology_id + ontology_source identify external reference (KEGG/Reactome/GO)
    - Pathways can be AI-generated (ontology_id=null) or mapped to standards
    - usage_count tracks how many interactions reference this pathway
    """
    __tablename__ = 'pathways'

    # Primary key
    id = db.Column(db.Integer, primary_key=True)

    # Pathway identifier (unique, indexed for fast lookups)
    name = db.Column(db.String(200), unique=True, nullable=False, index=True)
    description = db.Column(db.Text, nullable=True)

    # Ontology mapping (optional - for standardized pathways)
    ontology_id = db.Column(db.String(50), nullable=True)  # e.g., "GO:0006914", "hsa04140"
    ontology_source = db.Column(db.String(20), nullable=True)  # 'KEGG', 'Reactome', 'GO'
    canonical_term = db.Column(db.String(200), nullable=True)  # Standardized name from ontology

    # Generation metadata
    ai_generated = db.Column(db.Boolean, default=True, server_default=db.text('true'), nullable=False)
    usage_count = db.Column(db.Integer, default=0, server_default='0', nullable=False)

    # Flexible metadata storage
    extra_data = db.Column(JSONB, server_default='{}', nullable=False)

    # Hierarchy fields (for DAG structure)
    hierarchy_level = db.Column(db.Integer, default=0, nullable=False)  # 0=root, higher=deeper
    is_leaf = db.Column(db.Boolean, default=True, nullable=False)  # True if no child pathways
    protein_count = db.Column(db.Integer, default=0, nullable=False)  # Proteins in this pathway
    ancestor_ids = db.Column(JSONB, server_default='[]', nullable=False)  # Materialized path for fast queries

    # Audit timestamps
    created_at = db.Column(db.DateTime, default=_utcnow, server_default=db.func.now(), nullable=False)
    updated_at = db.Column(db.DateTime, default=_utcnow, server_default=db.func.now(), onupdate=_utcnow, nullable=False)

    # Indexes
    __table_args__ = (
        db.Index('idx_pathways_ontology', 'ontology_source', 'ontology_id'),
        db.Index('idx_pathways_hierarchy_level', 'hierarchy_level'),
        db.Index('idx_pathways_is_leaf', 'is_leaf'),
    )

    def __repr__(self) -> str:
        if self.ontology_id:
            return f'<Pathway {self.name} ({self.ontology_source}:{self.ontology_id})>'
        return f'<Pathway {self.name}>'


class PathwayInteraction(db.Model):
    """
    Many-to-many relationship: pathways ↔ interactions.

    Links interactions to their assigned biological pathways.
    An interaction can belong to multiple pathways.
    """
    __tablename__ = 'pathway_interactions'

    # Primary key
    id = db.Column(db.Integer, primary_key=True)

    # Foreign keys
    pathway_id = db.Column(
        db.Integer,
        db.ForeignKey('pathways.id', ondelete='CASCADE'),
        nullable=False,
        index=True
    )
    interaction_id = db.Column(
        db.Integer,
        db.ForeignKey('interactions.id', ondelete='CASCADE'),
        nullable=False,
        index=True
    )

    # Assignment metadata
    assignment_confidence = db.Column(db.Numeric(3, 2), default=0.80, server_default='0.80')  # 0.00 to 1.00
    assignment_method = db.Column(db.String(50), default='ai_pipeline', server_default='ai_pipeline')  # 'ai_pipeline', 'manual', 'ontology_match'

    # Audit timestamp
    created_at = db.Column(db.DateTime, default=_utcnow, server_default=db.func.now(), nullable=False)

    # Constraints
    __table_args__ = (
        db.UniqueConstraint('pathway_id', 'interaction_id', name='pathway_interaction_unique'),
    )

    # Relationships
    pathway = db.relationship('Pathway', backref=db.backref('pathway_interactions', lazy='dynamic', cascade='all, delete-orphan'))
    interaction = db.relationship('Interaction', backref=db.backref('pathway_interactions', lazy='dynamic', cascade='all, delete-orphan'))

    def __repr__(self) -> str:
        pw_name = self.pathway.name if self.pathway else '?'
        return f'<PathwayInteraction pathway={pw_name} interaction_id={self.interaction_id}>'


class PathwayParent(db.Model):
    """
    DAG (Directed Acyclic Graph) relationship between pathways.

    Enables hierarchical pathway organization where:
    - A child pathway can have multiple parents (DAG, not tree)
    - Example: "Mitophagy" has parents ["Autophagy", "Mitochondrial Quality Control"]
    - Example: "PI3K/Akt/mTOR" has parents ["mTORC1 Signaling", "Cell Growth Regulation"]

    Relationship types:
    - 'is_a': Child is a subtype of parent (e.g., Mitophagy is_a Selective Autophagy)
    - 'part_of': Child is a component of parent (e.g., mTORC1 Signaling part_of Cell Growth)
    - 'regulates': Child regulates parent process

    Invariants:
    - No self-references (enforced by CHECK constraint)
    - No duplicate parent-child pairs (enforced by UNIQUE constraint)
    - DAG must be acyclic (enforced by application logic)
    """
    __tablename__ = 'pathway_parents'

    # Primary key
    id = db.Column(db.Integer, primary_key=True)

    # Foreign keys to pathways table.
    # Note: ``index=True`` was removed (2026-05-03) — the canonical indexes
    # are declared explicitly in __table_args__ as ``idx_pathway_parents_child``
    # / ``idx_pathway_parents_parent``. Keeping ``index=True`` would also
    # create auto-named ``ix_pathway_parents_*_pathway_id`` indexes for the
    # same columns (the duplication that Audit A3 / migration 0006 removes).
    child_pathway_id = db.Column(
        db.Integer,
        db.ForeignKey('pathways.id', ondelete='CASCADE'),
        nullable=False,
    )
    parent_pathway_id = db.Column(
        db.Integer,
        db.ForeignKey('pathways.id', ondelete='CASCADE'),
        nullable=False,
    )

    # Relationship metadata
    relationship_type = db.Column(db.String(30), default='is_a', server_default='is_a', nullable=False)  # 'is_a', 'part_of', 'regulates'
    confidence = db.Column(db.Numeric(3, 2), default=1.0, server_default='1.0', nullable=False)  # 1.0 for ontology-derived, <1.0 for AI-inferred
    source = db.Column(db.String(20), nullable=True)  # 'GO', 'KEGG', 'Reactome', 'AI'

    # Audit timestamp
    created_at = db.Column(db.DateTime, default=_utcnow, server_default=db.func.now(), nullable=False)

    # Constraints
    __table_args__ = (
        db.UniqueConstraint('child_pathway_id', 'parent_pathway_id', name='pathway_parent_unique'),
        db.CheckConstraint('child_pathway_id != parent_pathway_id', name='no_self_parent'),
        db.Index('idx_pathway_parents_child', 'child_pathway_id'),
        db.Index('idx_pathway_parents_parent', 'parent_pathway_id'),
    )

    # Relationships
    child = db.relationship(
        'Pathway',
        foreign_keys=[child_pathway_id],
        backref=db.backref('parent_links', lazy='dynamic', cascade='all, delete-orphan')
    )
    parent = db.relationship(
        'Pathway',
        foreign_keys=[parent_pathway_id],
        backref=db.backref('child_links', lazy='dynamic', cascade='all, delete-orphan')
    )

    def __repr__(self) -> str:
        child_name = self.child.name if self.child else '?'
        parent_name = self.parent.name if self.parent else '?'
        return f'<PathwayParent {child_name} --[{self.relationship_type}]--> {parent_name}>'


def _compute_chain_signature(chain_proteins) -> str:
    """Stable 32-char hex digest of a directional chain.

    Multiple distinct biological cascades can run between the same two
    endpoints (ATXN3→MTOR via VCP→RHEB is a different mechanism than
    ATXN3→MTOR via TSC2→TSC1). The previous schema's
    ``UniqueConstraint('origin_interaction_id')`` allowed only ONE
    IndirectChain per origin, silently dropping the second. Replacing
    that with ``UniqueConstraint('origin_interaction_id',
    'chain_signature')`` lets a single origin own multiple chains as long
    as the signature differs.

    Empty input → empty signature (treated as "no chain", deduped naturally).
    """
    import hashlib
    if not chain_proteins:
        return ''
    canonical = '->'.join(str(p).strip().upper() for p in chain_proteins if p)
    if not canonical:
        return ''
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:32]


class IndirectChain(db.Model):
    """Full indirect chain entity grouping claims across multiple interactions."""
    __tablename__ = 'indirect_chains'

    id = db.Column(db.Integer, primary_key=True)
    chain_proteins = db.Column(JSONB, nullable=False)  # ["ATXN3", "VCP", "LAMP2"]
    origin_interaction_id = db.Column(
        db.Integer,
        db.ForeignKey('interactions.id', ondelete='CASCADE'),
        nullable=False, index=True
    )
    pathway_name = db.Column(db.String(200))
    pathway_id = db.Column(db.Integer, db.ForeignKey('pathways.id', ondelete='SET NULL'))
    chain_with_arrows = db.Column(JSONB)
    discovered_in_query = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=_utcnow, server_default=db.func.now())
    updated_at = db.Column(
        db.DateTime, default=_utcnow, server_default=db.func.now(),
        onupdate=_utcnow, nullable=False,
    )

    # Stable hash of ``chain_proteins`` (directional). Lets multiple
    # distinct chains share the same origin interaction — the new unique
    # constraint is on ``(origin_interaction_id, chain_signature)``.
    # Backfilled by migration 20260429_0004 from existing rows; new
    # rows compute it via ``IndirectChain.set_chain_proteins`` (or
    # directly via ``_compute_chain_signature`` at write time).
    chain_signature = db.Column(
        db.String(32),
        nullable=False,
        server_default='',
    )

    __table_args__ = (
        # Multiple chains per origin allowed iff their proteins differ
        # (chain_signature dedupes within an origin). Distinct biological
        # cascades through the same endpoints are now first-class.
        db.UniqueConstraint(
            'origin_interaction_id', 'chain_signature',
            name='chain_origin_signature_unique',
        ),
        db.Index('idx_indirect_chains_chain_signature', 'chain_signature'),
    )

    def set_chain_proteins(self, proteins: list) -> None:
        """Atomic writer for chain_proteins + chain_signature.

        Use this instead of assigning ``chain_proteins`` directly to keep
        the signature in sync. Old code that assigns the column is still
        supported (migration backfills the signature), but new writers
        should route through here.
        """
        self.chain_proteins = list(proteins or [])
        self.chain_signature = _compute_chain_signature(self.chain_proteins)
    # ``Interaction`` now has two FKs pointing at this table:
    #   1. ``IndirectChain.origin_interaction_id`` (legacy "origin" link,
    #      one chain owned by one interaction).
    #   2. ``Interaction.chain_id`` (new, #6 refactor — every
    #      participating interaction links here so chain state isn't
    #      duplicated in per-interaction JSONB).
    # SQLAlchemy can't pick between them automatically, so both
    # relationship directions pin ``foreign_keys`` explicitly.
    origin_interaction = db.relationship(
        'Interaction',
        foreign_keys=[origin_interaction_id],
        backref=db.backref('origin_indirect_chain', uselist=False),
    )

    def __repr__(self) -> str:
        proteins = self.chain_proteins or []
        label = '->'.join(proteins[:3])
        if len(proteins) > 3:
            label += '->...'
        return f'<IndirectChain {label} (interaction#{self.origin_interaction_id})>'

    @property
    def computed_pathway_name(self) -> str | None:
        """Compute the dominant pathway across this chain's member claims.

        B7: ``pathway_name`` as a stored column can drift whenever
        ``quick_assign`` reassigns claims to a new pathway — the column
        holds whatever was written at chain creation time, not the
        current dominant vote. This property is the live version:
        majority-vote across all child claims. Readers that want the
        always-current pathway should prefer this; readers that want
        the as-of-creation snapshot can still read ``pathway_name``.

        Returns ``None`` when the chain has no claims or no claim has
        a pathway assigned.
        """
        from collections import Counter
        claim_rows = (
            InteractionClaim.query
            .filter_by(chain_id=self.id)
            .with_entities(InteractionClaim.pathway_name)
            .all()
        )
        names = [r[0] for r in claim_rows if r[0]]
        if not names:
            return None
        return Counter(names).most_common(1)[0][0]

    def recompute_pathway_name(self) -> bool:
        """Overwrite the cached ``pathway_name`` column with the computed value.

        Callers that mutate claims (e.g., quick_assign unification) should
        invoke this to keep the cached column consistent. Returns True
        when the cached value changed, False otherwise. No-op when the
        computed value is None (leaves prior cached value alone).
        """
        fresh = self.computed_pathway_name
        if fresh is None:
            return False
        if self.pathway_name == fresh:
            return False
        self.pathway_name = fresh
        # Also refresh pathway_id to match — cheap lookup.
        pw = Pathway.query.filter_by(name=fresh).first()
        self.pathway_id = pw.id if pw else self.pathway_id
        return True


class ChainParticipant(db.Model):
    """M2M membership between Interaction and IndirectChain.

    Pre-#12, every Interaction had a single ``chain_id`` FK pointing at
    one IndirectChain — so an interaction that participated in multiple
    chains (the ATXN3→MTOR pair sitting in both the VCP-mediated and
    TSC2-mediated cascades) could only show one. This table is the
    proper many-to-many relationship.

    The ``role`` column distinguishes how each interaction participates:
      * ``origin``     — the Interaction whose pair "owns" this chain
                         (matches IndirectChain.origin_interaction_id).
      * ``hop``        — a single mediator-pair edge inside the chain.
      * ``net_effect`` — the indirect (query→target) row that summarizes
                         the whole cascade end-to-end.

    The role is denormalized — interaction.chain_id is still kept as a
    "primary chain" pointer for fast read-by-Interaction (the first or
    most-recent chain the interaction belongs to). New code prefers the
    M2M; legacy code keeps working through chain_id.
    """
    __tablename__ = 'chain_participants'

    chain_id = db.Column(
        db.Integer,
        db.ForeignKey('indirect_chains.id', ondelete='CASCADE'),
        primary_key=True,
    )
    interaction_id = db.Column(
        db.Integer,
        db.ForeignKey('interactions.id', ondelete='CASCADE'),
        primary_key=True,
    )
    role = db.Column(db.String(30), nullable=False, server_default='hop')
    created_at = db.Column(
        db.DateTime, default=_utcnow, server_default=db.func.now(), nullable=False,
    )

    __table_args__ = (
        db.CheckConstraint(
            "role IN ('origin', 'hop', 'net_effect')",
            name='valid_chain_participant_role',
        ),
        db.Index('idx_chain_participants_interaction', 'interaction_id'),
    )

    chain = db.relationship(
        'IndirectChain',
        backref=db.backref(
            'memberships', cascade='all, delete-orphan', lazy='dynamic',
        ),
    )
    interaction = db.relationship(
        'Interaction',
        backref=db.backref(
            'chain_memberships', cascade='all, delete-orphan', lazy='dynamic',
        ),
    )

    def __repr__(self) -> str:
        return (
            f'<ChainParticipant chain={self.chain_id} '
            f'interaction={self.interaction_id} role={self.role}>'
        )


class InteractionClaim(db.Model):
    """
    Atomic scientific claim about a protein-protein interaction.

    One interaction pair can have MANY claims (1:N relationship with Interaction).
    Each claim = one function/mechanism extracted from the pipeline's JSONB data blob.

    Invariants:
    - interaction_id references the parent interaction pair
    - function_name is never null
    - No duplicate (interaction_id, function_name, pathway_name) triples
    """
    __tablename__ = 'interaction_claims'

    id = db.Column(db.Integer, primary_key=True)

    # Parent interaction (the protein pair).
    # ``index=True`` removed (2026-05-03): the canonical index is the
    # explicit ``idx_claims_interaction`` declared in __table_args__.
    interaction_id = db.Column(
        db.Integer,
        db.ForeignKey('interactions.id', ondelete='CASCADE'),
        nullable=False,
    )

    # Extracted from functions[] items
    function_name = db.Column(db.Text, nullable=False)
    arrow = db.Column(db.String(50))
    interaction_effect = db.Column(db.String(50))
    direction = db.Column(
        db.String(30),
        nullable=False,
        default='main_to_primary',
        server_default='main_to_primary',
    )
    mechanism = db.Column(db.Text)
    effect_description = db.Column(db.Text)
    biological_consequences = db.Column(JSONB, server_default='[]')
    specific_effects = db.Column(JSONB, server_default='[]')

    # Evidence
    evidence = db.Column(JSONB, server_default='[]')
    pmids = db.Column(JSONB, server_default='[]')

    # Pathway
    pathway_name = db.Column(db.String(200))
    # ``index=True`` removed (2026-05-03): the canonical index is the
    # explicit ``idx_claims_pathway`` declared in __table_args__.
    pathway_id = db.Column(
        db.Integer,
        db.ForeignKey('pathways.id', ondelete='SET NULL'),
        nullable=True,
    )

    # Confidence
    confidence = db.Column(db.Numeric(3, 2))

    # Context — NOT NULL via 20260503_0007 migration.
    function_context = db.Column(
        db.String(20),
        nullable=False,
        default='direct',
        server_default='direct',
    )
    context_data = db.Column(JSONB)

    # Chain grouping (links claim to IndirectChain entity)
    chain_id = db.Column(db.Integer, db.ForeignKey('indirect_chains.id', ondelete='SET NULL'), nullable=True)

    # Discovery metadata
    source_query = db.Column(db.String(50))
    discovery_method = db.Column(db.String(50), default='pipeline')
    raw_function_data = db.Column(JSONB)

    # Audit timestamps
    created_at = db.Column(db.DateTime, default=_utcnow, server_default=db.func.now(), nullable=False)
    updated_at = db.Column(db.DateTime, default=_utcnow, server_default=db.func.now(), onupdate=_utcnow, nullable=False)

    __table_args__ = (
        db.Index('idx_claims_interaction', 'interaction_id'),
        db.Index('idx_claims_pathway', 'pathway_id'),
        db.Index('idx_claims_source_query', 'source_query'),
        db.Index('idx_claims_arrow', 'arrow'),
        db.Index('idx_claims_chain', 'chain_id'),
        db.Index('idx_claims_function_context', 'function_context'),
        db.Index('idx_claims_interaction_context', 'interaction_id', 'function_context'),
        db.Index('idx_claims_evidence_gin', 'evidence', postgresql_using='gin'),
        db.Index('idx_claims_pmids_gin', 'pmids', postgresql_using='gin'),
        # S4c: composite index for _unify_all_chain_claims queries
        db.Index('idx_claims_chain_pathway', 'chain_id', 'pathway_id'),
        # S4b: confidence must be in [0, 1] if set
        db.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name='valid_claim_confidence_range'
        ),
        db.CheckConstraint(
            "arrow IS NULL OR arrow IN ('activates', 'inhibits', 'binds', 'regulates')",
            name='valid_claim_arrow'
        ),
        db.CheckConstraint(
            "direction IN ('main_to_primary', 'primary_to_main')",
            name='valid_claim_direction'
        ),
        db.CheckConstraint(
            "function_context IN ('direct', 'net', 'chain_derived', 'mixed')",
            name='valid_claim_function_context'
        ),
        # PostgreSQL (and SQLite) treat NULLs as distinct in plain UNIQUE
        # constraints, so a straight UniqueConstraint on nullable columns lets
        # duplicate (…, NULL, NULL) rows through. Use COALESCE to collapse
        # NULL → '' (or 0 for chain_id) at the index level, giving one
        # constraint that covers the full matrix of NULL/NOT-NULL combinations
        # for (pathway_name, function_context, chain_id).
        #
        # 2026-04-30 — extended from 4 cols to 5 by adding COALESCE(chain_id, 0).
        # The same interaction pair + function/pathway/context can legitimately
        # have BOTH a chain-derived claim (chain_id=N) AND a direct claim
        # (chain_id=NULL) — they describe distinct biological evidence. The
        # 5-col rule keeps each row's "natural identity" unique while letting
        # those distinct cascades coexist. The merge-on-collision logic in
        # scripts/pathway_v2/quick_assign.py:_assign_claim_pathway_safe
        # mirrors this 5-col filter so runtime detection matches DB enforcement.
        db.Index(
            'uq_claim_interaction_fn_pw_ctx',
            'interaction_id',
            'function_name',
            db.func.coalesce(db.column('pathway_name'), ''),
            db.func.coalesce(db.column('function_context'), ''),
            db.func.coalesce(db.column('chain_id'), 0),
            unique=True,
        ),
    )

    interaction = db.relationship(
        'Interaction',
        backref=db.backref('claims', lazy='dynamic', cascade='all, delete-orphan')
    )
    pathway = db.relationship(
        'Pathway',
        backref=db.backref('claims', lazy='dynamic')
    )
    chain = db.relationship(
        'IndirectChain',
        backref=db.backref('chain_claims', lazy='dynamic')
    )

    def __repr__(self) -> str:
        return f'<Claim {self.function_name[:40] if self.function_name else "?"} for interaction#{self.interaction_id}>'
