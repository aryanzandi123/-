# Database Schema

**Source of truth:** `models.py` (905 lines). `schema.sql` is the raw SQL dump kept in sync.

**Database:** PostgreSQL (Railway-hosted; URL in `.env`). SQLite fallback available but rarely exercised.

**Connection:** Initialized in `app.py` via `flask_sqlalchemy`. Lifecycle banner at startup:
```
[DATABASE] Initializing PostgreSQL connection...
[DATABASE] URL: postgresql://***@66.33.22.253:51981/railway
[DATABASE] [OK] Connection verified
[DATABASE] [OK] Tables initialized
[DATABASE]   • Proteins table: 101 entries
[DATABASE]   • Interactions table: 156 entries
[DATABASE]   • Claims table: 191 entries
```

## Tables

### `proteins`

| Column | Type | Notes |
|--------|------|-------|
| `id` | int PK | |
| `symbol` | varchar(50) | UNIQUE NOT NULL, indexed. Stored UPPERCASE via `normalize_symbol`. |
| `first_queried`, `last_queried` | datetime | UTC, naive |
| `query_count` | int | |
| `total_interactions` | int | |
| `extra_data` | JSONB DEFAULT `{}` | Includes `is_pseudo: bool` for generic biomolecules (RNA, Ubiquitin, ...) |
| `pipeline_status` | varchar(20) | `idle` / `running` / `complete` / `partial` / `failed`, indexed |
| `last_pipeline_phase` | varchar(50) | |
| `created_at`, `updated_at` | datetime | |

**Property:** `Protein.is_pseudo` reads `extra_data.is_pseudo`.

**Relationship:** `interactions_as_a` (where this is protein_a), `interactions_as_b` (where this is protein_b), both `lazy='dynamic'`.

### `protein_aliases`

Alias → canonical mapping for symbol resolution. Greek letters, full names, hyphenation variants all canonicalize through here.

| Column | Type | Notes |
|--------|------|-------|
| `id` | int PK | |
| `alias_symbol` | varchar(100) | UNIQUE NOT NULL, indexed. Stored UPPERCASE. |
| `protein_id` | int FK | → `proteins.id` ON DELETE CASCADE |
| `source` | varchar(32) | `curated`, `HGNC_SEED`, `GREEK_NORMALIZATION`, ... |
| `created_at` | datetime | |

### `interactions`

The pairwise edge table. ONE row per unordered protein pair.

| Column | Type | Notes |
|--------|------|-------|
| `id` | int PK | |
| `protein_a_id` | int FK | → `proteins.id` ON DELETE CASCADE, indexed |
| `protein_b_id` | int FK | → `proteins.id` ON DELETE CASCADE, indexed |
| `confidence` | numeric(3,2) | 0.00 – 1.00, indexed |
| `direction` | varchar(20) | NOT NULL. CHECK: `IN ('a_to_b', 'b_to_a', 'main_to_primary', 'primary_to_main')`. **`bidirectional` is DEAD (S1)** — every interaction must be asymmetric. Symmetric arrows (binds) default to `main_to_primary` (query is canonical subject). |
| `arrow` | varchar(50) | Legacy primary arrow (`activates`, `inhibits`, `binds`, `regulates`, ...). Mirror of `arrows.a_to_b[0]` for un-migrated readers. |
| `arrows` | JSONB | NEW (#4): `{ "a_to_b": ["activates", "inhibits"], "b_to_a": [...] }`. Multi-arrow per direction. |
| `interaction_type` | varchar(100) | CHECK: `IN ('direct', 'indirect')` |
| `upstream_interactor` | varchar(50) | For indirect: the immediate upstream protein of the partner |
| `function_context` | varchar(20) | CHECK: `IN ('direct', 'net', 'chain_derived', 'mixed')`. **Stamped by post-processor before write.** |
| `mediator_chain` | JSONB | Legacy: `["VCP", "LAMP2", ...]` for indirect. Now derived from linked IndirectChain via `chain_view` property. |
| `depth` | int DEFAULT 1 | 1=direct, N>=2 for indirect via (N-1) mediators. NO upper cap. |
| `chain_context` | JSONB | Legacy: `{full_chain: [...], role: ...}`. Now derived from linked IndirectChain. |
| `chain_with_arrows` | JSONB | NEW (#2): `[{from: "VCP", to: "IκBα", arrow: "inhibits"}, ...]` |
| `data` | JSONB DEFAULT `{}` | NOT NULL. Full payload bag (functions, evidence, etc. — much migrated to `interaction_claims` but `data` is kept as a stable bag). |
| `discovered_in_query` | varchar(50) | Which protein query discovered this. |
| `discovery_method` | varchar(50) | `pipeline` / `requery` / `manual` |
| `chain_id` | int FK NULLABLE | → `indirect_chains.id` ON DELETE SET NULL. Primary chain pointer (for legacy single-chain code paths). Use `chain_memberships` for multi-chain. |
| `created_at`, `updated_at` | datetime | |

**Constraints:**
- UNIQUE `(protein_a_id, protein_b_id)` — `interaction_unique`
- CHECK `protein_a_id != protein_b_id` — `interaction_proteins_different` (no self-interactions)
- CHECK `function_context IN (...)` — see above
- CHECK `interaction_type IN ('direct', 'indirect')`
- CHECK `direction IN (...)` — no `bidirectional`
- CHECK `confidence IS NULL OR (confidence >= 0 AND confidence <= 1)`

**Indexes:** `(protein_a_id, protein_b_id)`, `(protein_a_id, protein_b_id, function_context)`, `discovered_in_query`, `depth`, `interaction_type`, `chain_id`.

**Properties on Interaction:**
- `primary_arrow` — reads `arrows` JSONB first, falls back to `arrow` scalar, then `'binds'`. **Use this, not `.arrow` directly.**
- `set_primary_arrow(value, direction='a_to_b')` — atomic writer. Updates BOTH `arrows` JSONB and legacy `arrow` scalar.
- `chain_view` — returns a `ChainView` instance. Single source of truth for chain state on this row. Reads from linked IndirectChain when present, falls back to JSONB.
- `computed_mediator_chain`, `computed_upstream_interactor`, `computed_depth` — always consistent with `chain_view`.

### `interaction_claims`

The claim-level table. ONE row per scientific claim about an interaction.

| Column | Type | Notes |
|--------|------|-------|
| `id` | int PK | |
| `interaction_id` | int FK | → `interactions.id` ON DELETE CASCADE |
| `function_name` | text | NOT NULL. The biology label. |
| `arrow` | varchar(50) | This claim's specific arrow |
| `interaction_effect` | varchar(50) | Auto-generated from arrow (`activates` → `activation`) |
| `direction` | varchar(30) | |
| `mechanism` | text | The "how it works" prose |
| `effect_description` | text | The "what it does" prose. **PhD-depth: 6-10 sentences.** |
| `biological_consequences` | JSONB DEFAULT `[]` | Array of consequences |
| `specific_effects` | JSONB DEFAULT `[]` | Array of specific effects (3+ required) |
| `evidence` | JSONB DEFAULT `[]` | List of evidence sources (3+ papers required) |
| `pmids` | JSONB DEFAULT `[]` | Array of PubMed IDs |
| `pathway_name` | varchar(200) | |
| `pathway_id` | int FK NULLABLE | → `pathways.id` ON DELETE SET NULL |
| `confidence` | numeric(3,2) | 0.00 – 1.00 |
| `function_context` | varchar(20) NOT NULL | `direct` / `net` / `chain_derived` / `mixed`. **NOT NULL since migration `20260503_0007`**; backfilled `'direct'` for legacy rows. SPA renders this as a colored badge in the modal header. |
| `context_data` | JSONB | Free-form context |
| `chain_id` | int FK NULLABLE | → `indirect_chains.id` ON DELETE SET NULL. Tags chain-derived claims. |
| `source_query` | varchar(50) | |
| `discovery_method` | varchar(50) | |
| `raw_function_data` | JSONB | Original LLM payload for debugging |
| `created_at`, `updated_at` | datetime | |

**THE 5-COLUMN UNIQUE INDEX (`uq_claim_interaction_fn_pw_ctx`) — read carefully:**

```sql
UNIQUE INDEX ON interaction_claims (
    interaction_id,
    function_name,
    COALESCE(pathway_name, ''),
    COALESCE(function_context, ''),
    COALESCE(chain_id, 0)
)
```

This is the deduplication backstop. NULL is treated as `''` or `0` so `(pathway_name=NULL, function_context=NULL)` doesn't bypass the constraint. The 5th column (`chain_id`) was added 2026-04-30 so a chain-derived claim (chain_id=N) can coexist with a direct claim (chain_id=NULL) on the same interaction+function — they describe distinct biological evidence.

**Other indexes:** `interaction_id`, `pathway_id`, `source_query`, `arrow`, `chain_id`, `(chain_id, pathway_id)`.

**Constraints:** confidence in `[0, 1]`.

### `indirect_chains`

The chain entity. ONE row per *distinct* biological cascade.

| Column | Type | Notes |
|--------|------|-------|
| `id` | int PK | |
| `chain_proteins` | JSONB | NOT NULL. `["ATXN3", "VCP", "LAMP2", ...]` |
| `origin_interaction_id` | int FK | → `interactions.id` ON DELETE CASCADE |
| `pathway_name` | varchar(200) | Cached majority-vote of child claim pathways. Use `computed_pathway_name` for live value. |
| `pathway_id` | int FK NULLABLE | → `pathways.id` ON DELETE SET NULL |
| `chain_with_arrows` | JSONB | `[{from, to, arrow}, ...]` |
| `discovered_in_query` | varchar(50) | |
| `chain_signature` | varchar(32) | NOT NULL. SHA256-derived hash of upper-cased `'->'`-joined `chain_proteins`. Allows multiple distinct chains per origin. |
| `created_at`, `updated_at` | datetime | |

**Constraints:** UNIQUE `(origin_interaction_id, chain_signature)` — `chain_origin_signature_unique`. Distinct cascades through the same endpoints are first-class.

**Methods:**
- `set_chain_proteins(proteins)` — atomic writer that keeps `chain_signature` in sync.
- `computed_pathway_name` — live majority-vote across child claim pathways.
- `recompute_pathway_name()` — overwrite cached column with computed value.

**Memory note (`decisions/pseudo_protein_storage`):** "Chains must be atomic biological cascades — never stitch chains by protein overlap; a chain is ONE claim's end-to-end described cascade."

### `chain_participants`

M2M between Interaction and IndirectChain. NEW (#12).

| Column | Type | Notes |
|--------|------|-------|
| `chain_id` | int PK | → `indirect_chains.id` ON DELETE CASCADE |
| `interaction_id` | int PK | → `interactions.id` ON DELETE CASCADE |
| `role` | varchar(30) | NOT NULL. CHECK: `IN ('origin', 'hop', 'net_effect')` |
| `created_at` | datetime | |

**Roles:**
- `origin` — the Interaction that "owns" the chain (matches `IndirectChain.origin_interaction_id`)
- `hop` — a single mediator-pair edge inside the chain
- `net_effect` — the indirect (query→target) row that summarizes the whole cascade end-to-end

**Why this exists:** before #12, `Interaction.chain_id` was a single FK so an interaction in N chains could only show one. Now an interaction has `chain_memberships` (lazy=dynamic) returning all participations. The legacy `chain_id` is kept as a "primary chain pointer" for fast read-by-Interaction.

### `pathways`

| Column | Type | Notes |
|--------|------|-------|
| `id` | int PK | |
| `name` | varchar(200) | UNIQUE NOT NULL, indexed |
| `description` | text | |
| `ontology_id` | varchar(50) | e.g. "GO:0006914", "hsa04140" |
| `ontology_source` | varchar(20) | `KEGG` / `Reactome` / `GO` |
| `canonical_term` | varchar(200) | Standardized name from ontology |
| `ai_generated` | bool DEFAULT true | |
| `usage_count` | int DEFAULT 0 | Bumped per-claim assignment |
| `extra_data` | JSONB DEFAULT `{}` | |
| `hierarchy_level` | int DEFAULT 0 | 0=root, deeper=higher |
| `is_leaf` | bool DEFAULT true | True when no child pathways |
| `protein_count` | int DEFAULT 0 | **ORPHAN as of 2026-05-04.** No writer; no longer emitted by `services/data_builder.py`. Column kept (negligible cost) for reversibility. |
| `ancestor_ids` | JSONB DEFAULT `[]` | Materialized path for fast queries |
| `created_at`, `updated_at` | datetime | |

**Indexes:** `(ontology_source, ontology_id)`, `hierarchy_level`, `is_leaf`.

### `pathway_interactions`

M2M between pathways and interactions.

| Column | Type | Notes |
|--------|------|-------|
| `id` | int PK | |
| `pathway_id` | int FK | → `pathways.id` ON DELETE CASCADE |
| `interaction_id` | int FK | → `interactions.id` ON DELETE CASCADE |
| `assignment_confidence` | numeric(3,2) DEFAULT 0.80 | |
| `assignment_method` | varchar(50) | `ai_pipeline` / `manual` / `ontology_match` |
| `created_at` | datetime | |

**Constraint:** UNIQUE `(pathway_id, interaction_id)`.

### `pathway_parents`

DAG edge table for pathway hierarchy. A child can have multiple parents.

| Column | Type | Notes |
|--------|------|-------|
| `id` | int PK | |
| `child_pathway_id` | int FK | → `pathways.id` ON DELETE CASCADE |
| `parent_pathway_id` | int FK | → `pathways.id` ON DELETE CASCADE |
| `relationship_type` | varchar(30) DEFAULT `is_a` | `is_a` / `part_of` / `regulates` |
| `confidence` | numeric(3,2) DEFAULT 1.0 | 1.0 for ontology-derived, <1.0 for AI-inferred |
| `source` | varchar(20) | `GO` / `KEGG` / `Reactome` / `AI` |
| `created_at` | datetime | |

**Constraints:**
- UNIQUE `(child_pathway_id, parent_pathway_id)` — `pathway_parent_unique`
- CHECK `child_pathway_id != parent_pathway_id` — `no_self_parent`

**Indexes:** `child_pathway_id`, `parent_pathway_id`.

## Invariants enforced at write time

1. **No self-interactions:** `protein_a_id != protein_b_id`.
2. **No bidirectional direction:** S1 — every interaction asymmetric.
3. **`function_context` is one of 4 enum values** (or null for legacy unvalidated).
4. **Direction is one of 4 enum values** (a_to_b / b_to_a / main_to_primary / primary_to_main). NOT NULL.
5. **One pair = one row** (`(protein_a_id, protein_b_id)` UNIQUE).
6. **One claim natural identity = one row** (5-col COALESCE UNIQUE index).
7. **Chain signature is sha256-derivable from chain_proteins** — `set_chain_proteins()` enforces this.

## Invariants NOT enforced (caller must respect)

1. **`Interaction.chain_id` matches at least one `chain_memberships` row.** When promoting a multi-chain interaction, both writes must happen.
2. **`InteractionClaim.chain_id` matches the parent Interaction's `chain_memberships`.** Tagging is best-effort in `_tag_claims_with_chain`.
3. **`chain_proteins` order = causal direction.** Currently the LLM emits whatever order it described; the canonicalization step (Layer 1 in `11_CHAIN_TOPOLOGY.md`) is pending.

## Recent migrations (in `scripts/` and `migrations/`)

In rough order:

- `migrate_kill_bidirectional.py` — removed legacy `direction='bidirectional'` rows
- `migrate_add_interaction_chain_id.py` — added `Interaction.chain_id` FK
- `migrate_add_chain_table.py` — added `indirect_chains` and `chain_participants` (#12 multi-chain)
- `migrate_pseudo_protein_flag.py` — added `extra_data.is_pseudo` flag
- `migrate_b6_canonicalize_arrows.py` — canonicalized arrow storage to JSONB
- `migrate_widen_interaction_type.py` — widened type column
- `migrate_add_storage_indexes.py` — added performance indexes
- `migrate_add_null_pathway_unique.py` — COALESCE-based unique on InteractionClaim
- `migrate_add_claim_dedup_constraint.py` — extended dedup to 5 cols (2026-04-30)
- `migrate_retag_legacy_chain_derived.py` — retag legacy claims with function_context

## Read paths from frontend

The frontend reads via `/api/results/<protein>` or `/api/visualize/<protein>`. Both flow through `services/data_builder.py:build_full_json_from_db(protein)`:

```python
result = {
    "snapshot_json": {
        "main": "ATXN3",
        "proteins": [...],         # list of symbols
        "interactions": [...],     # list of dicts (chain fields included)
        "pathways": [...],         # hierarchy
        "_diagnostics": {...},     # from Logs/<protein>/pipeline_diagnostics.json + quality_report.json
        "_pipeline_status": "...", # if main_protein.pipeline_status == 'partial'
        "_completed_phases": "..."
    },
    "ctx_json": {...}
}
```

`_chain_fields_for(interaction)` is the canonical chain-field emitter:
```python
{
  "chain_id": <primary chain id, can be None>,
  "mediator_chain": [...],         // when chain_view non-empty
  "upstream_interactor": "...",
  "depth": int,
  "chain_context": {...},
  "chain_ids": [42, 99],           // multi-chain — list of all chain memberships
  "all_chains": [                  // multi-chain detail
    {chain_id, role, chain_proteins, chain_with_arrows, pathway_name, discovered_in_query}
  ]
}
```
