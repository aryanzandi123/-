"""Single source of truth for interaction chain state.

Historical problem: chains were described by four parallel fields on the
``Interaction`` model and its JSONB ``data`` column —

    - ``mediator_chain`` (JSONB list of non-endpoint proteins)
    - ``upstream_interactor`` (symbol of the proximal upstream protein)
    - ``depth`` (integer hop count)
    - ``data.chain_context.full_chain`` (ordered list of every protein
      in the chain, including query and target)

These four fields encoded the same underlying information in four
different shapes, and they drifted whenever any one of them was written
without updating the others. Readers had no canonical source and
frequently defaulted to whichever field was most convenient, which is
how "TDP43 → VCP → GRN" ended up contaminating the UI even though the
real chain was "TDP43 → HNRNPA1 → GRN".

This module establishes ``full_chain`` (the ordered list) as the ONLY
authoritative chain representation. All other views — legacy
``mediator_chain``, ``upstream_interactor``, ``depth`` — are computed
from ``full_chain`` on demand via ``ChainView``. Writers derive all
four from one ``full_chain`` value; readers use ``ChainView`` methods
instead of reading the stored columns directly.

``ChainView`` is pure and DB-free. It can be constructed from:
  * an ``Interaction`` ORM row (via ``chain_view_from_interaction``), or
  * raw dict data (via ``ChainView.from_interaction_data``).

The module doesn't touch ``db.session`` or import from ``models`` — that
keeps it import-safe from anywhere (tests, helpers, pipeline code).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence


@dataclass(frozen=True)
class ChainView:
    """Immutable view of an interaction's chain state.

    ``full_chain`` is the authoritative, ordered list of proteins in
    upstream → downstream order. Everything else is derived from it and
    ``query_position``.

    The ``query_position`` index is 0-based within ``full_chain``. A
    position of 0 means the query protein is the most upstream element
    (the traditional "query at head" case); ``len(full_chain) - 1``
    means the query is most downstream; anything in between means the
    query sits in the middle of a longer cascade.
    """

    full_chain: List[str] = field(default_factory=list)
    query_protein: Optional[str] = None
    query_position: Optional[int] = None

    # ------------------------------------------------------------------
    # Primary constructors
    # ------------------------------------------------------------------

    @classmethod
    def empty(cls) -> "ChainView":
        """An empty chain — no proteins, no query position."""
        return cls(full_chain=[], query_protein=None, query_position=None)

    @classmethod
    def from_full_chain(
        cls,
        full_chain: Sequence[str],
        query_protein: Optional[str] = None,
        query_position: Optional[int] = None,
    ) -> "ChainView":
        """Build a view from an explicit ``full_chain`` list.

        If ``query_position`` is omitted and ``query_protein`` is given,
        it's derived by linear search (case-insensitive). If neither is
        given, ``query_position`` stays None — the view is valid but
        callers can't ask "where is the query?" questions.
        """
        cleaned: List[str] = [
            str(p).strip().strip("^*").strip()
            for p in (full_chain or [])
            if isinstance(p, str) and str(p).strip()
        ]
        qp: Optional[int] = None
        if isinstance(query_position, int) and 0 <= query_position < len(cleaned):
            qp = query_position
        elif query_protein:
            target = query_protein.upper()
            for idx, sym in enumerate(cleaned):
                if sym.upper() == target:
                    qp = idx
                    break
        return cls(full_chain=cleaned, query_protein=query_protein, query_position=qp)

    @classmethod
    def from_interaction_data(
        cls,
        interaction_data: Optional[Dict[str, Any]],
        query_protein: Optional[str] = None,
    ) -> "ChainView":
        """Build a view from an ``Interaction.data`` dict (the JSONB blob).

        ``chain_context.full_chain`` is the only authoritative chain
        representation. When it is present, this method uses it verbatim
        (including its stored ``query_position`` — the query may sit at
        head, middle, or tail of the biological cascade).

        When ``chain_context.full_chain`` is **absent**, we do NOT try to
        reconstruct the chain from ``mediator_chain`` + ``primary``. The
        denormalised fields don't carry the query's biological position:
        historically this code force-prepended ``query_protein`` at
        position 0, which silently inverted query-at-tail chains (e.g.
        ``AKT1 → TSC2 → RHEB → MTOR → RPTOR → ULK1`` became
        ``ULK1 → TSC2 → RHEB → MTOR → RPTOR → AKT1``) and caused
        ``[CHAIN HOP CLAIM MISSING]`` mismatches between the 2ax
        enumerator (which used the real chain) and db_sync (which used
        the reconstructed, reversed chain). Returning ``empty()`` forces
        the caller to populate ``chain_context.full_chain`` at the
        write site (via ``apply_to_dict``/``apply_to_interaction``) —
        which every in-memory write path now does.
        """
        if not isinstance(interaction_data, dict):
            return cls.empty()

        ctx = interaction_data.get("chain_context") if interaction_data else None
        if isinstance(ctx, dict):
            stored_full = ctx.get("full_chain")
            stored_qp = ctx.get("query_position")
            stored_query = ctx.get("query_protein") or query_protein
            if isinstance(stored_full, list) and len(stored_full) >= 2:
                return cls.from_full_chain(
                    stored_full,
                    query_protein=stored_query,
                    query_position=stored_qp if isinstance(stored_qp, int) else None,
                )

        return cls.empty()

    # ------------------------------------------------------------------
    # Derived views
    # ------------------------------------------------------------------

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return len(self.full_chain) >= 2

    @property
    def is_empty(self) -> bool:
        return len(self.full_chain) < 2

    @property
    def chain_length(self) -> int:
        """Total number of proteins in the chain (``N``)."""
        return len(self.full_chain)

    @property
    def depth(self) -> int:
        """Legacy ``depth`` = number of hops in the chain.

        For a chain of ``N`` proteins, depth is ``N - 1`` (there are
        ``N - 1`` direct edges between them). A 1-element "chain" has
        depth 0 (degenerate / empty chain).
        """
        return max(len(self.full_chain) - 1, 0)

    @property
    def mediator_chain(self) -> List[str]:
        """Legacy ``mediator_chain`` = all non-endpoint proteins.

        For chains where the query is at the head (the common case):
        ``full_chain[1:-1]`` — the proteins strictly between query and
        target, same as the historical meaning.

        For chains where the query sits in the middle: still
        ``full_chain[1:-1]`` because "mediator" in the legacy sense
        means "not an endpoint of the stored chain", which is still the
        non-head, non-tail slice regardless of the query's position.
        Consumers that need query-relative neighbors should use
        ``upstream_of_query`` / ``downstream_of_query`` below.
        """
        if len(self.full_chain) <= 2:
            return []
        return list(self.full_chain[1:-1])

    @property
    def upstream_interactor(self) -> Optional[str]:
        """Legacy ``upstream_interactor`` = the protein immediately
        upstream of the *target* endpoint (the last element of
        ``full_chain``). Returns None for chains shorter than 2.

        Historical note: this used to be interpreted as "the protein
        immediately upstream of the query", which is only correct when
        the query is at the tail. The more common meaning — and what
        most code actually relies on — is "who acts on the final chain
        element", which is ``full_chain[-2]``.
        """
        if len(self.full_chain) < 2:
            return None
        return self.full_chain[-2]

    # ------------------------------------------------------------------
    # Query-relative views (new helpers; require query_position to be set)
    # ------------------------------------------------------------------

    @property
    def has_query_position(self) -> bool:
        return isinstance(self.query_position, int)

    def upstream_of_query(self) -> List[str]:
        """Proteins strictly upstream of the query, in upstream-to-
        downstream order. Empty list if the query is the head (or
        position is unknown).
        """
        if not self.has_query_position or self.query_position is None:
            return []
        return list(self.full_chain[: self.query_position])

    def downstream_of_query(self) -> List[str]:
        """Proteins strictly downstream of the query, in upstream-to-
        downstream order. Empty list if the query is the tail (or
        position is unknown).
        """
        if not self.has_query_position or self.query_position is None:
            return []
        return list(self.full_chain[self.query_position + 1 :])

    def immediate_upstream_of_query(self) -> Optional[str]:
        """Protein at position ``query_position - 1``, or None."""
        upstream = self.upstream_of_query()
        return upstream[-1] if upstream else None

    def immediate_downstream_of_query(self) -> Optional[str]:
        """Protein at position ``query_position + 1``, or None."""
        downstream = self.downstream_of_query()
        return downstream[0] if downstream else None

    # ------------------------------------------------------------------
    # Serialization back to JSONB (for db_sync writes)
    # ------------------------------------------------------------------

    def to_chain_context(self) -> Dict[str, Any]:
        """Render this view as a ``chain_context`` JSONB dict.

        Used by ``db_sync`` when writing an interaction row — the
        JSONB blob stores only the authoritative fields, and every
        stored field is derived from this single view.
        """
        return {
            "full_chain": list(self.full_chain),
            "query_protein": self.query_protein,
            "query_position": self.query_position,
            "chain_length": self.chain_length,
        }

    # ------------------------------------------------------------------
    # Single write surface — derives every legacy field from this view
    # ------------------------------------------------------------------

    def apply_to_dict(self, interactor_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Write chain state into an in-memory interactor dict.

        Mutates ``interactor_dict`` in place AND returns it. Sets every
        chain-related field on the dict from this view's authoritative
        ``full_chain`` so the four legacy storage shapes can never drift:

          - ``mediator_chain`` ← derived
          - ``upstream_interactor`` ← derived
          - ``depth`` ← derived
          - ``chain_context`` ← serialized via :meth:`to_chain_context`

        Used by ``runner.py`` Track A / Track B when writing chain
        annotations onto an in-memory ctx interactor (no DB involved).
        After this call, any reader of the dict — whether it goes
        through ``ChainView.from_interaction_data`` or reads any of
        the four fields directly — sees the same chain.
        """
        if self.is_empty:
            # Empty chains clear the fields rather than leaving stale
            # values behind. Callers that want to preserve a previous
            # value should not call apply_to_dict on an empty view.
            interactor_dict.pop("mediator_chain", None)
            interactor_dict.pop("upstream_interactor", None)
            interactor_dict.pop("depth", None)
            interactor_dict.pop("chain_context", None)
            return interactor_dict

        interactor_dict["mediator_chain"] = self.mediator_chain
        interactor_dict["upstream_interactor"] = self.upstream_interactor
        interactor_dict["depth"] = self.depth
        interactor_dict["chain_context"] = self.to_chain_context()
        return interactor_dict

    def apply_to_interaction(
        self,
        interaction: Any,
        chain_record: Any = None,
    ) -> Any:
        """Write chain state into a SQLAlchemy ``Interaction`` row.

        Same single-write-surface contract as :meth:`apply_to_dict`,
        but for ORM rows. Sets:

          - ``interaction.chain_id`` ← ``chain_record.id`` (when given)
          - ``interaction.mediator_chain`` ← derived
          - ``interaction.upstream_interactor`` ← derived
          - ``interaction.depth`` ← derived
          - ``interaction.chain_context`` (JSONB column) ← serialized
          - ``interaction.data["chain_context"]`` (JSONB blob inside the
            ``data`` column) ← serialized

        After this call, every chain-related field on the row is
        guaranteed consistent with the linked ``IndirectChain`` (or with
        ``self.full_chain`` when ``chain_record`` is None — JSONB only).
        Callers do not need to set any chain field directly.

        Returns ``interaction`` for chaining. Does NOT call ``flush()``
        or ``commit()`` — the caller decides when to persist.
        """
        if interaction is None:
            return interaction

        if chain_record is not None and getattr(chain_record, "id", None):
            interaction.chain_id = chain_record.id

        if self.is_empty:
            # No-chain case: scrub the legacy columns and JSONB blobs
            # so an interaction that was once part of a chain doesn't
            # carry stale values.
            interaction.mediator_chain = None
            interaction.upstream_interactor = None
            interaction.depth = 1
            interaction.chain_context = None
            data = dict(interaction.data or {})
            data.pop("chain_context", None)
            interaction.data = data
            return interaction

        interaction.mediator_chain = self.mediator_chain
        interaction.upstream_interactor = self.upstream_interactor
        interaction.depth = self.depth

        ctx_dict = self.to_chain_context()
        interaction.chain_context = ctx_dict
        # Mirror into ``data["chain_context"]`` for backward compat
        # with frontend / tooling that still reads from the JSONB blob.
        # Once those readers migrate to the API endpoint that returns
        # the linked IndirectChain directly, this mirror can be deleted.
        data = dict(interaction.data or {})
        data["chain_context"] = ctx_dict
        interaction.data = data
        return interaction


# ---------------------------------------------------------------------------
# Convenience helper for ORM code
# ---------------------------------------------------------------------------


def chain_view_from_interaction(interaction: Any) -> ChainView:
    """Build a ChainView from an SQLAlchemy ``Interaction`` row.

    Reads ``interaction.data`` (the JSONB blob) via
    ``ChainView.from_interaction_data``. If the row has a linked
    ``IndirectChain`` via the ``chain_id`` FK (introduced in the #6
    refactor), that chain wins over the JSONB copy.

    Does NOT issue any new queries — uses whatever's already loaded on
    the ORM instance. Safe to call on eagerly-loaded rows from any
    read path.
    """
    if interaction is None:
        return ChainView.empty()

    # Prefer linked IndirectChain row (canonical, shared across participants).
    chain_id = getattr(interaction, "chain_id", None)
    if chain_id:
        linked = getattr(interaction, "linked_chain", None)
        if linked is not None:
            proteins = getattr(linked, "chain_proteins", None)
            if isinstance(proteins, list) and len(proteins) >= 2:
                # Query protein lives at whichever position stored on
                # the chain_context JSONB if present; otherwise fall back
                # to scanning for the discovered_in_query symbol.
                query_protein = None
                data = getattr(interaction, "data", None) or {}
                if isinstance(data, dict):
                    ctx = data.get("chain_context")
                    if isinstance(ctx, dict):
                        query_protein = ctx.get("query_protein")
                if not query_protein:
                    query_protein = getattr(linked, "discovered_in_query", None)
                return ChainView.from_full_chain(
                    proteins, query_protein=query_protein,
                )

    # Fall back to the JSONB chain_context (or mediator_chain) on the
    # interaction row itself.
    data = getattr(interaction, "data", None) or {}
    # Use the query protein hint if the row has one stored somewhere.
    query_protein = None
    if isinstance(data, dict):
        ctx = data.get("chain_context") or {}
        if isinstance(ctx, dict):
            query_protein = ctx.get("query_protein")
    if not query_protein:
        query_protein = getattr(interaction, "discovered_in_query", None)

    return ChainView.from_interaction_data(data, query_protein=query_protein)
