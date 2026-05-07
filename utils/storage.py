"""
Unified Storage Layer for ProPaths.

PostgreSQL primary, file cache write-through.
Single entry point replaces three-stage save in runner.py.
"""

import json
import os
import sys
import time
import traceback
from contextlib import contextmanager
from copy import deepcopy
from typing import Any, Dict, List, Optional

from utils.interaction_contract import normalize_arrow

CACHE_DIR = "cache"


class StorageLayer:
    """Unified facade for pipeline result persistence.

    PostgreSQL is primary storage. File cache is best-effort write-through.
    All writes go through save_pipeline_results(); all reads through
    load_protein_data() or get_known_interactions().
    """

    def __init__(self, flask_app=None, max_retries: int = 3):
        self._flask_app = flask_app
        self._max_retries = max_retries

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _has_db(self) -> bool:
        return self._flask_app is not None

    @contextmanager
    def _db_context(self):
        if not self._flask_app:
            raise RuntimeError("No Flask app provided -- cannot access database")
        with self._flask_app.app_context():
            yield

    # ------------------------------------------------------------------
    # PUBLIC: save_pipeline_results
    # ------------------------------------------------------------------

    def save_pipeline_results(
        self,
        protein_symbol: str,
        final_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Atomic save: PostgreSQL primary + file cache write-through.

        Args:
            protein_symbol: Query protein (e.g., "ATXN3").
            final_payload: Dict with 'snapshot_json' and optionally 'ctx_json'.

        Returns:
            Stats dict with db_synced, file_cached, interactions_created, etc.
        """
        if not protein_symbol:
            raise ValueError("protein_symbol cannot be empty")

        final_payload = self._enrich_snapshot_from_ctx(final_payload)
        self._audit_chain_integrity(protein_symbol, final_payload, phase="final_save")

        stats = {
            "db_synced": False,
            "file_cached": False,
            "interactions_created": 0,
            "interactions_updated": 0,
            "proteins_created": 0,
        }

        # --- PRIMARY: PostgreSQL via DatabaseSyncLayer ---
        if self._has_db():
            db_stats = self._sync_to_db_with_retry(protein_symbol, final_payload)
            if db_stats is not None:
                # SUCCESS: mark complete only when DB actually has the data.
                stats["db_synced"] = True
                stats["interactions_created"] = db_stats.get("interactions_created", 0)
                stats["interactions_updated"] = db_stats.get("interactions_updated", 0)
                stats["proteins_created"] = db_stats.get("proteins_created", 0)
                # Invalidate stale cache before rewrite
                self._invalidate_file_cache(protein_symbol)
                try:
                    with self._db_context():
                        from models import Protein, db
                        protein = Protein.query.filter_by(symbol=protein_symbol).first()
                        if protein:
                            protein.pipeline_status = "complete"
                            protein.last_pipeline_phase = "complete"
                            db.session.commit()
                except Exception:
                    pass
            else:
                # FAILURE: keep the protein out of the "complete" state.
                # Marking complete after a failed sync means /api/results
                # returns thin/empty data with no signal to the frontend
                # that the run actually broke. Set ``pipeline_status``
                # to ``failed`` and leave ``last_pipeline_phase`` at
                # whatever the orchestrator wrote so the operator can
                # see where it died. Frontend renders a "pipeline
                # failed — re-run" banner instead of a blank chart.
                try:
                    with self._db_context():
                        from models import Protein, db
                        protein = Protein.query.filter_by(symbol=protein_symbol).first()
                        if protein:
                            protein.pipeline_status = "failed"
                            if not protein.last_pipeline_phase or protein.last_pipeline_phase == "running":
                                protein.last_pipeline_phase = "db_sync_failed"
                            db.session.commit()
                except Exception:
                    pass
        else:
            print(
                f"[StorageLayer] No flask_app provided -- file cache only",
                file=sys.stderr,
            )

        # --- SECONDARY: Best-effort file cache ---
        try:
            self._write_file_cache(protein_symbol, final_payload)
            stats["file_cached"] = True
        except Exception as e:
            print(
                f"[StorageLayer] File cache write failed for '{protein_symbol}': {e}",
                file=sys.stderr,
            )

        # --- TERTIARY: Best-effort protein_database.py write-through ---
        # Skip when PostgreSQL sync succeeded (data is already persisted).
        if not stats["db_synced"]:
            try:
                self._write_to_protein_db(protein_symbol, final_payload)
            except Exception as e:
                print(
                    f"[StorageLayer] protein_database write-through failed: {e}",
                    file=sys.stderr,
                )

        return stats

    # ------------------------------------------------------------------
    # PUBLIC: save_checkpoint
    # ------------------------------------------------------------------

    def save_checkpoint(
        self,
        protein_symbol: str,
        payload: Dict[str, Any],
        phase_name: str,
    ) -> None:
        """Mark pipeline phase progress on the Protein row.

        2026-05-03: stripped of its eager full-DB-sync side effect.
        Previously this method called ``_sync_to_db_with_retry`` to write
        the partial payload, BUT the same payload is then post-processed
        and written again via ``save_pipeline_results``. The duplicate
        sync caused the entire ``sync_chain_relationships`` log block
        (``[DB SYNC] Preserving direct interaction``, ``[CHAIN] Using
        LLM-emitted chain as-is``, ``[LOCUS ROUTER]``) to emit twice
        per pipeline run. It also doubled the DB write cost — every
        chain re-creating IndirectChain rows that the post-processed
        save would only overwrite seconds later.

        The eager sync pretended to be "crash recovery" but couldn't
        actually recover anything: the payload only stabilizes after
        post-processing, so a crash before ``save_pipeline_results``
        would leave the DB with arrows / chains derived from un-
        validated arrows anyway. The runner does the real recovery
        with a clean re-run.

        Now: just update ``pipeline_status`` / ``last_pipeline_phase``
        for status visibility (so the SSE stream and any "is X still
        running?" check have an answer). The payload argument is
        ignored on purpose — kept in the signature for back-compat
        with any caller that still passes it.
        """
        if not self._has_db():
            return

        with self._db_context():
            from models import Protein, db
            protein = Protein.query.filter_by(symbol=protein_symbol).first()
            if protein:
                protein.pipeline_status = "running"
                protein.last_pipeline_phase = phase_name
                db.session.commit()

    # ------------------------------------------------------------------
    # PUBLIC: load_protein_data
    # ------------------------------------------------------------------

    def load_protein_data(self, protein_symbol: str) -> Optional[Dict[str, Any]]:
        """Load full protein data. PostgreSQL first, file cache fallback."""
        # PRIMARY: PostgreSQL
        if self._has_db():
            try:
                with self._db_context():
                    from app import build_full_json_from_db

                    result = build_full_json_from_db(protein_symbol)
                    if result:
                        return result
            except Exception as e:
                print(
                    f"[StorageLayer] DB read failed for '{protein_symbol}': {e}. "
                    f"Trying file cache.",
                    file=sys.stderr,
                )

        # FALLBACK: File cache
        return self._read_file_cache(protein_symbol)

    # ------------------------------------------------------------------
    # PUBLIC: get_known_interactions
    # ------------------------------------------------------------------

    def get_known_interactions(self, protein_symbol: str) -> List[Dict[str, Any]]:
        """Get known interaction partners for pipeline exclusion context."""
        # PRIMARY: PostgreSQL
        if self._has_db():
            try:
                with self._db_context():
                    from models import Protein, Interaction
                    from models import db as models_db

                    protein_obj = Protein.query.filter_by(
                        symbol=protein_symbol
                    ).first()

                    if not protein_obj:
                        print(
                            f"[StorageLayer] No known interactions found for "
                            f"{protein_symbol} - first query",
                            file=sys.stderr,
                        )
                        return []

                    db_interactions = models_db.session.query(Interaction).filter(
                        (Interaction.protein_a_id == protein_obj.id)
                        | (Interaction.protein_b_id == protein_obj.id)
                    ).all()

                    results = []
                    for interaction in db_interactions:
                        if interaction.protein_a_id == protein_obj.id:
                            partner = interaction.protein_b
                        else:
                            partner = interaction.protein_a
                        results.append({
                            "primary": partner.symbol,
                            "confidence": float(interaction.confidence or 0.5),
                            # C2: canonical primary_arrow (reads arrows
                            # JSONB first, then legacy scalar).
                            "arrow": normalize_arrow(interaction.primary_arrow, default="binds"),
                        })

                    print(
                        f"[StorageLayer] Found {len(results)} known interactions "
                        f"for {protein_symbol}",
                        file=sys.stderr,
                    )
                    return results

            except Exception as e:
                print(
                    f"[StorageLayer] DB query failed for '{protein_symbol}': {e}. "
                    f"Using file fallback.",
                    file=sys.stderr,
                )

        # FALLBACK: File-based protein database
        import utils.protein_database as pdb

        return pdb.get_all_interactions(protein_symbol)

    # ------------------------------------------------------------------
    # Internal: DB sync with retry
    # ------------------------------------------------------------------

    @staticmethod
    def _enrich_snapshot_from_ctx(final_payload: Dict[str, Any]) -> Dict[str, Any]:
        """Copy rich ctx_json fields into snapshot_json before persistence.

        Several persistence/read paths still consume snapshot_json first. Keep
        it compact for ordinary fields, but never let it lose chain_context or
        chain_link_functions when ctx_json has them.
        """
        if not isinstance(final_payload, dict):
            return final_payload
        snapshot = final_payload.get("snapshot_json")
        ctx = final_payload.get("ctx_json")
        if not isinstance(snapshot, dict) or not isinstance(ctx, dict):
            return final_payload
        snap_interactors = snapshot.get("interactors")
        ctx_interactors = ctx.get("interactors")
        if not isinstance(snap_interactors, list) or not isinstance(ctx_interactors, list):
            return final_payload

        def _key(item: Dict[str, Any]) -> str:
            return str((item or {}).get("primary") or "").strip().upper()

        rich_by_key = {
            _key(item): item
            for item in ctx_interactors
            if isinstance(item, dict) and _key(item)
        }
        chain_fields = (
            "chain_context",
            "chain_link_functions",
            "chain_with_arrows",
            "mediator_chain",
            "upstream_interactor",
            "depth",
            "interaction_type",
            "function_context",
            "_chain_pathway",
            "step3_finalized_pathway",
            "pathways",
        )

        seen: set[str] = set()
        enriched = []
        for snap_item in snap_interactors:
            if not isinstance(snap_item, dict):
                enriched.append(snap_item)
                continue
            key = _key(snap_item)
            rich_item = rich_by_key.get(key)
            if not rich_item:
                enriched.append(snap_item)
                if key:
                    seen.add(key)
                continue
            merged = deepcopy(snap_item)
            for field in chain_fields:
                val = rich_item.get(field)
                if val not in (None, "", [], {}):
                    merged[field] = deepcopy(val)
            enriched.append(merged)
            seen.add(key)

        for key, rich_item in rich_by_key.items():
            if key not in seen:
                enriched.append(deepcopy(rich_item))

        snapshot["interactors"] = enriched
        if ctx.get("_chain_claim_phase_ran"):
            snapshot["_chain_claim_phase_ran"] = True
        final_payload["snapshot_json"] = snapshot
        return final_payload

    @staticmethod
    def _audit_chain_integrity(protein_symbol: str, payload: Dict[str, Any], phase: str) -> None:
        """Emit a pre-persistence chain contract summary."""
        try:
            ctx = payload.get("ctx_json") if isinstance(payload, dict) else {}
            if not isinstance(ctx, dict):
                return
            interactors = ctx.get("interactors") or []
            if not isinstance(interactors, list):
                return

            indirect_missing = [
                i.get("primary")
                for i in interactors
                if isinstance(i, dict)
                and i.get("interaction_type") == "indirect"
                and len(((i.get("chain_context") or {}).get("full_chain") or [])) < 2
            ]

            expected_pairs = list((ctx.get("_chain_pair_context") or {}).keys())
            missing_pairs: List[str] = []
            if expected_pairs:
                from utils.chain_resolution import canonical_pair_key
                generated = set()
                for interactor in interactors:
                    if not isinstance(interactor, dict):
                        continue
                    clf = interactor.get("chain_link_functions") or {}
                    if not isinstance(clf, dict):
                        continue
                    for pair_key, funcs in clf.items():
                        if not funcs or not isinstance(pair_key, str):
                            continue
                        if "->" in pair_key:
                            a, b = pair_key.split("->", 1)
                        elif "|" in pair_key:
                            a, b = pair_key.split("|", 1)
                        else:
                            continue
                        generated.add(canonical_pair_key(a, b))
                for pair in expected_pairs:
                    if not isinstance(pair, str) or "->" not in pair:
                        continue
                    a, b = pair.split("->", 1)
                    if canonical_pair_key(a, b) not in generated:
                        missing_pairs.append(pair)

            if indirect_missing or missing_pairs:
                print(
                    f"[CHAIN AUDIT] protein={protein_symbol} phase={phase} "
                    f"indirect_missing_chain_context={len(indirect_missing)} "
                    f"missing_chain_claim_pairs={len(missing_pairs)}",
                    file=sys.stderr,
                    flush=True,
                )
                if indirect_missing:
                    print(
                        f"[CHAIN AUDIT] Missing chain_context: {indirect_missing[:20]}"
                        + ("..." if len(indirect_missing) > 20 else ""),
                        file=sys.stderr,
                        flush=True,
                    )
                if missing_pairs:
                    print(
                        f"[CHAIN AUDIT] Missing chain_link_functions: {missing_pairs[:30]}"
                        + ("..." if len(missing_pairs) > 30 else ""),
                        file=sys.stderr,
                        flush=True,
                    )
        except Exception as exc:
            print(
                f"[CHAIN AUDIT] failed for {protein_symbol} at {phase}: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )

    def _sync_to_db_with_retry(
        self,
        protein_symbol: str,
        final_payload: Dict[str, Any],
    ) -> Optional[Dict[str, int]]:
        """Retry-wrapped PostgreSQL sync. Returns stats on success, None on failure."""
        from utils.db_sync import DatabaseSyncLayer

        snapshot = final_payload.get("snapshot_json", {})
        ctx_json = final_payload.get("ctx_json")

        for attempt in range(self._max_retries):
            try:
                if attempt > 0:
                    wait_time = attempt * 5
                    print(
                        f"[StorageLayer] DB sync retry {attempt}/{self._max_retries} "
                        f"in {wait_time}s...",
                        file=sys.stderr,
                    )
                    time.sleep(wait_time)

                with self._db_context():
                    from models import db as models_db

                    models_db.session.execute(models_db.text("SELECT 1"))

                with self._db_context():
                    sync_layer = DatabaseSyncLayer()
                    db_stats = sync_layer.sync_query_results(
                        protein_symbol=protein_symbol,
                        snapshot_json={"snapshot_json": snapshot},
                        ctx_json=ctx_json,
                    )
                    return db_stats

            except Exception as e:
                print(
                    f"[StorageLayer] DB sync attempt {attempt + 1} failed: {e}",
                    file=sys.stderr,
                )
                if attempt >= self._max_retries - 1:
                    print(
                        f"\n[StorageLayer] DB sync FAILED after {self._max_retries} "
                        f"attempts for '{protein_symbol}'.",
                        file=sys.stderr,
                    )
                    print(
                        f"   Data preserved in file cache at: cache/{protein_symbol}.json",
                        file=sys.stderr,
                    )
                    print(
                        f"   Run 'python sync_cache_to_db.py {protein_symbol}' "
                        f"to sync manually",
                        file=sys.stderr,
                    )
                    traceback.print_exc(file=sys.stderr)

        return None

    # ------------------------------------------------------------------
    # Internal: File cache write
    # ------------------------------------------------------------------

    @staticmethod
    def _write_file_cache(
        protein_symbol: str, final_payload: Dict[str, Any]
    ) -> None:
        """Write cache/{PROTEIN}.json + cache/{PROTEIN}_metadata.json."""
        os.makedirs(CACHE_DIR, exist_ok=True)

        # File 1: PROTEIN.json — snapshot only (for visualization)
        output_path = os.path.join(CACHE_DIR, f"{protein_symbol}.json")
        snapshot_only = {"snapshot_json": final_payload.get("snapshot_json", {})}
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(snapshot_only, f, ensure_ascii=False, indent=2)

        # File 2: PROTEIN_metadata.json — ctx_json (full rich metadata)
        metadata_path = os.path.join(CACHE_DIR, f"{protein_symbol}_metadata.json")
        metadata_only = {"ctx_json": final_payload.get("ctx_json", {})}
        with open(metadata_path, "w", encoding="utf-8") as f:
            json.dump(metadata_only, f, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------
    # Internal: File cache invalidation
    # ------------------------------------------------------------------

    @staticmethod
    def _invalidate_file_cache(protein_symbol: str) -> None:
        """Delete stale file cache — DB is source of truth after sync."""
        for suffix in ("", "_metadata"):
            path = os.path.join(CACHE_DIR, f"{protein_symbol}{suffix}.json")
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Internal: File cache read
    # ------------------------------------------------------------------

    @staticmethod
    def _read_file_cache(protein_symbol: str) -> Optional[Dict[str, Any]]:
        """Read from cache/{PROTEIN}.json + cache/{PROTEIN}_metadata.json."""
        snapshot_path = os.path.join(CACHE_DIR, f"{protein_symbol}.json")

        if not os.path.exists(snapshot_path):
            return None

        try:
            with open(snapshot_path, "r", encoding="utf-8") as f:
                snapshot_data = json.load(f)

            result = {"snapshot_json": snapshot_data.get("snapshot_json", {})}

            metadata_path = os.path.join(
                CACHE_DIR, f"{protein_symbol}_metadata.json"
            )
            if os.path.exists(metadata_path):
                with open(metadata_path, "r", encoding="utf-8") as f:
                    metadata = json.load(f)
                result["ctx_json"] = metadata.get("ctx_json", {})

            return result
        except (IOError, json.JSONDecodeError) as e:
            print(
                f"[StorageLayer] File cache read failed for '{protein_symbol}': {e}",
                file=sys.stderr,
            )
            return None

    # ------------------------------------------------------------------
    # Internal: protein_database.py write-through
    # ------------------------------------------------------------------

    @staticmethod
    def _write_to_protein_db(
        protein_symbol: str, final_payload: Dict[str, Any]
    ) -> None:
        """Write-through to protein_database.py file structure (best-effort)."""
        import utils.protein_database as pdb

        snapshot = final_payload.get("snapshot_json", {})
        interactors = snapshot.get("interactors", [])

        saved_count = 0
        for interactor in interactors:
            partner = interactor.get("primary")
            if partner:
                success = pdb.save_interaction(protein_symbol, partner, interactor)
                if success:
                    saved_count += 1

        pdb.update_protein_metadata(protein_symbol, query_completed=True)
        print(
            f"[StorageLayer] protein_database write-through: "
            f"{saved_count} interactions saved",
            file=sys.stderr,
        )
