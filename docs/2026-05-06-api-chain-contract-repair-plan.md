# API Chain Contract Repair Plan

Date: 2026-05-06

Status: repair implemented and verified locally on 2026-05-07. No schema
writes or migrations were performed.

Related docs:

- `docs/2026-05-06-chain-semantics-contract-audit.md`
- `docs/2026-05-06-react-v2-architecture-plan.md`

## Decision

React v2 remains the frontend direction, but it is blocked until
`/api/results/<protein>` stops emitting false chain semantics.

The next approved work should be the smallest backend/API repair that makes the
payload trustworthy for direct, chain-hop, and net-effect records. React v2 must
not paper over `/api/results` changing an indirect/net DB row into a direct row.

## P0 Bugs To Repair

### 1. Net-effect row relabeled as direct

DB truth for chain `2624`:

- interaction `14655`
- pair `TDP43 / DDX3X`
- `interaction_type = indirect`
- `function_context = net`
- `chain_id = 2624`
- chain `TDP43 -> GLE1 -> DDX3X`

Current API problem:

- `/api/results/TDP43` emits the same row as:
  - `type = direct`
  - `interaction_type = direct`
  - `function_context = net`

Required repair:

- Preserve DB interaction locus for net-effect rows.
- Never relabel a `function_context = net` row as direct.
- Net-effect rows must have explicit `locus = "net_effect_claim"` and
  `is_net_effect = true`.

### 2. DB-backed hop replaced with synthesized stale JSONB

DB truth for chain `2624`:

- interaction `14656`
- pair `GLE1 / DDX3X`
- `interaction_type = direct`
- `function_context = direct`
- `chain_id = 2624`
- claim `28556`
- claim pathway `Stress Granule Dynamics`

Current API problem:

- `/api/results/TDP43` emits `GLE1 -> DDX3X` as:
  - `_db_id = null`
  - `_is_chain_link = true`
  - `function_context = chain_derived`
  - `claims = []`
  - function pathway from stale parent JSONB:
    `RNA Metabolism & Translation Control`

Required repair:

- Prefer a real DB hop row whenever one exists for the adjacent chain pair.
- Synthesize a topology-only hop only when no DB row exists.
- Parent JSONB `chain_link_functions` may fill an otherwise empty synthesized
  hop, but it must not replace a DB-backed hop and claim.

## Minimal Repair Scope

Expected code scope when implementation is approved:

- `services/data_builder.py`
  - preserve net-effect row type
  - repair chain-hop DB lookup
  - add explicit v2-ready locus fields to API rows
  - include claim-level `chain_id`
- focused tests around data-builder reconstruction and `/api/results/TDP43`

No route shell changes are required. `routes/results.py` can continue calling
`build_full_json_from_db(protein)`.

No schema writes are expected. Existing tables already carry the required
storage:

- `interactions.interaction_type`
- `interactions.function_context`
- `interactions.chain_id`
- `interactions.chain_context`
- `indirect_chains.chain_proteins`
- `indirect_chains.pathway_name`
- `indirect_chains.chain_with_arrows`
- `chain_participants.role`
- `interaction_claims.function_context`
- `interaction_claims.chain_id`
- `interaction_claims.pathway_name`

Schema writes should be considered only if implementation proves hop index,
source, or target cannot be reconstructed reliably from existing chain data.
The expected first repair is API serialization only.

## Contract Fields To Expose

Every interaction row in `/api/results/<protein>` should expose these fields
additively:

```json
{
  "locus": "direct_claim | chain_hop_claim | net_effect_claim",
  "chain_id": 2624,
  "hop_index": 1,
  "chain_members": ["TDP43", "GLE1", "DDX3X"],
  "source": "GLE1",
  "target": "DDX3X",
  "chain_context_pathway": "Stress Granule Dynamics",
  "hop_local_pathway": "Stress Granule Dynamics",
  "is_net_effect": false,
  "via": [],
  "mediators": []
}
```

Field rules:

- `locus`
  - `direct_claim`: real pair evidence only.
  - `chain_hop_claim`: adjacent hop inside a specific chain.
  - `net_effect_claim`: query-to-terminal consequence through a chain.
- `chain_id`
  - Present for chain-hop and net-effect rows.
  - `null` for ordinary direct rows unless the row also has chain membership;
    direct display classification still comes from `locus`, not from `chain_id`.
- `hop_index`
  - Zero-based adjacent-hop index for `chain_hop_claim`.
  - `null` for `direct_claim` and `net_effect_claim`.
- `chain_members`
  - Full ordered chain array for chain-hop and net-effect rows.
  - Supports arbitrary chain length.
- `source` / `target`
  - Biological display endpoints for the row's locus.
  - Chain-hop rows use adjacent hop endpoints, not query/terminal endpoints.
- `chain_context_pathway`
  - Whole-chain grouping/display pathway.
  - Comes from `IndirectChain.pathway_name`, `all_chains[].pathway_name`, or
    equivalent chain summary.
- `hop_local_pathway`
  - Claim/function pathway for the hop itself.
  - May differ from `chain_context_pathway`, but the source must be explicit.
- `is_net_effect`
  - `true` only for `net_effect_claim`.
- `via` / `mediators`
  - For `net_effect_claim`, intermediates between query and terminal.
  - For `TDP43 -> DDX3X`, both should be `["GLE1"]`.

Claim objects should also expose:

```json
{
  "id": 28556,
  "function_name": "RNA Helicase Activity & Translation Initiation & Stress Granule Dynamics",
  "function_context": "direct",
  "chain_id": 2624,
  "locus": "chain_hop_claim",
  "hop_index": 1,
  "source": "GLE1",
  "target": "DDX3X",
  "pathway_name": "Stress Granule Dynamics"
}
```

## Repair Details

### A. Preserve net-effect row locus

Current failure path:

- `services/data_builder.py` applies a read-time direct-assay correction to
  indirect rows.
- That correction can change the serialized payload type to direct even when
  the row is a net-effect chain record.

Repair rule:

- Before any read-time type correction, classify net-effect rows:
  - `interaction.function_context == "net"`
  - any serialized claim has `function_context == "net"`
  - `_net_effect` is already present
  - chain membership role is `origin` or `net_effect` and the row has
    query-to-terminal chain context
- If net-effect, skip direct-assay relabeling.
- Emit:
  - `interaction_type = "indirect"`
  - `type = "indirect"`
  - `locus = "net_effect_claim"`
  - `is_net_effect = true`
  - `via` / `mediators`
  - `chain_members`
  - `chain_context_pathway`

Compatibility note:

- Legacy default route consumes the same `/api/results` payload, but this is a
  semantic correction. Legacy should continue working and should no longer see
  the net-effect row as a normal direct record.

### B. Prefer DB hop rows over synthesized rows

Current failure path:

- `_reconstruct_chain_links()` builds a protein lookup map from mediators.
- It later adds the query protein.
- It does not add all terminal chain members.
- `_lookup_chain_link("GLE1", "DDX3X")` fails because `DDX3X` is absent from
  the map.
- The read path synthesizes a hop from parent JSONB.

Repair rule:

- Build the chain protein lookup map from every protein in every
  `chain_view.full_chain`, not only mediators.
- Build `_chain_link_map` from all interactions touching any protein in that
  full-chain set.
- For each adjacent hop:
  - look up the canonical pair
  - if DB row exists, use it
  - overlay only hop-scoped chain metadata
  - serialize DB-backed claims
  - do not replace DB-backed claim pathway with parent JSONB pathway
- Only synthesize when no DB row exists.
- Synthesized rows must include:
  - `_db_id = null`
  - `locus = "chain_hop_claim"`
  - `source_of_row = "synthesized_topology"`
  - `claim_state = "no_claim"` unless parent JSONB provides clearly scoped
    hop functions

### C. Add explicit pathway scopes

For every chain-hop row:

- `chain_context_pathway = chain.pathway_name`
- `hop_local_pathway = first claim/function pathway for that hop`
- if they differ, do not hide or merge the row; expose both values

For the audited TDP43 case after repair:

- `GLE1 -> DDX3X` should be DB-backed.
- Both `chain_context_pathway` and `hop_local_pathway` should resolve to
  `Stress Granule Dynamics`, because the stored claim is already unified.
- If a future chain has a truly distinct hop-local pathway, React v2 can show
  both labels because the API will expose both scopes explicitly.

### D. Add claim-level chain metadata

Normal claim serialization should include:

- `chain_id`
- `locus`
- `hop_index` when applicable
- row-local `source`
- row-local `target`

This prevents React v2 from inferring claim locus from row shape or from raw
pair equality.

## Expected API Examples

### Before: net-effect row

Current `/api/results/TDP43`:

```json
{
  "_db_id": 14655,
  "source": "TDP43",
  "target": "DDX3X",
  "type": "direct",
  "interaction_type": "direct",
  "function_context": "net",
  "_net_effect": true,
  "chain_id": 2624
}
```

### After: net-effect row

Expected:

```json
{
  "_db_id": 14655,
  "source": "TDP43",
  "target": "DDX3X",
  "type": "indirect",
  "interaction_type": "indirect",
  "function_context": "net",
  "locus": "net_effect_claim",
  "is_net_effect": true,
  "chain_id": 2624,
  "hop_index": null,
  "chain_members": ["TDP43", "GLE1", "DDX3X"],
  "via": ["GLE1"],
  "mediators": ["GLE1"],
  "chain_context_pathway": "Stress Granule Dynamics",
  "hop_local_pathway": null
}
```

### Before: final chain hop

Current `/api/results/TDP43`:

```json
{
  "_db_id": null,
  "source": "GLE1",
  "target": "DDX3X",
  "type": "direct",
  "interaction_type": "direct",
  "function_context": "chain_derived",
  "chain_id": 2624,
  "_is_chain_link": true,
  "_chain_position": 1,
  "functions": [
    {
      "pathway": "RNA Metabolism & Translation Control",
      "function_context": "chain_derived"
    }
  ],
  "claims": []
}
```

### After: final chain hop

Expected:

```json
{
  "_db_id": 14656,
  "source": "GLE1",
  "target": "DDX3X",
  "type": "direct",
  "interaction_type": "direct",
  "function_context": "direct",
  "locus": "chain_hop_claim",
  "is_net_effect": false,
  "chain_id": 2624,
  "hop_index": 1,
  "_is_chain_link": true,
  "_chain_position": 1,
  "_chain_length": 3,
  "chain_members": ["TDP43", "GLE1", "DDX3X"],
  "via": [],
  "mediators": [],
  "chain_context_pathway": "Stress Granule Dynamics",
  "hop_local_pathway": "Stress Granule Dynamics",
  "claims": [
    {
      "id": 28556,
      "function_context": "direct",
      "chain_id": 2624,
      "locus": "chain_hop_claim",
      "hop_index": 1,
      "source": "GLE1",
      "target": "DDX3X",
      "pathway_name": "Stress Granule Dynamics"
    }
  ]
}
```

## Read-Only Verification Plan

Run these before and after the repair. They are read-only.

### DB: TDP43/GLE1/DDX3X interactions

```sql
SELECT
  i.id,
  pa.symbol AS a,
  pb.symbol AS b,
  i.interaction_type,
  i.function_context,
  i.chain_id,
  i.depth,
  i.upstream_interactor,
  i.data->>'step3_finalized_pathway' AS data_pathway
FROM interactions i
JOIN proteins pa ON pa.id = i.protein_a_id
JOIN proteins pb ON pb.id = i.protein_b_id
WHERE
  (pa.symbol, pb.symbol) IN (
    ('TDP43', 'DDX3X'),
    ('DDX3X', 'TDP43'),
    ('GLE1', 'DDX3X'),
    ('DDX3X', 'GLE1'),
    ('TDP43', 'GLE1'),
    ('GLE1', 'TDP43')
  )
ORDER BY pa.symbol, pb.symbol, i.id;
```

Expected DB truth:

- `14655` remains `indirect/net`, `chain_id = 2624`.
- `14656` exists for `GLE1 / DDX3X`, `direct/direct`, `chain_id = 2624`.
- `14609` exists for `TDP43 / GLE1`, `direct/direct`, `chain_id = 2624`.

### DB: chain participants

```sql
SELECT
  cp.chain_id,
  cp.interaction_id,
  cp.role,
  ic.chain_proteins,
  ic.pathway_name,
  ic.chain_with_arrows
FROM chain_participants cp
JOIN indirect_chains ic ON ic.id = cp.chain_id
WHERE cp.chain_id = 2624
ORDER BY cp.role, cp.interaction_id;
```

Expected:

- participant `14655` present for the net-effect/origin row
- participant `14609` present for hop `TDP43 -> GLE1`
- participant `14656` present for hop `GLE1 -> DDX3X`

### DB: claims

```sql
SELECT
  c.id,
  c.interaction_id,
  pa.symbol AS a,
  pb.symbol AS b,
  c.function_name,
  c.function_context,
  c.chain_id,
  c.pathway_name,
  c.direction,
  c.arrow
FROM interaction_claims c
JOIN interactions i ON i.id = c.interaction_id
JOIN proteins pa ON pa.id = i.protein_a_id
JOIN proteins pb ON pb.id = i.protein_b_id
WHERE c.chain_id = 2624 OR i.id IN (14655, 14656, 14609)
ORDER BY c.interaction_id, c.id;
```

Expected:

- claim `28553` for `14655`, `function_context = net`, `chain_id = 2624`
- claim `28556` for `14656`, `function_context = direct`, `chain_id = 2624`,
  `pathway_name = Stress Granule Dynamics`

### API: net-effect row

```bash
curl -fsS http://127.0.0.1:5003/api/results/TDP43 \
  | jq '.snapshot_json.interactions[]
    | select(._db_id == 14655)
    | {
        source,
        target,
        type,
        interaction_type,
        function_context,
        locus,
        is_net_effect,
        chain_id,
        hop_index,
        chain_members,
        via,
        mediators,
        chain_context_pathway,
        hop_local_pathway
      }'
```

Before repair:

- `type = direct`
- `interaction_type = direct`
- no `locus`

After repair:

- `type = indirect`
- `interaction_type = indirect`
- `locus = net_effect_claim`
- `is_net_effect = true`
- `via = ["GLE1"]`

### API: final hop row

```bash
curl -fsS http://127.0.0.1:5003/api/results/TDP43 \
  | jq '.snapshot_json.interactions[]
    | select(.source == "GLE1" and .target == "DDX3X" and .chain_id == 2624)
    | {
        db_id: ._db_id,
        source,
        target,
        type,
        interaction_type,
        function_context,
        locus,
        chain_id,
        hop_index,
        chain_members,
        chain_context_pathway,
        hop_local_pathway,
        claims
      }'
```

Before repair:

- `_db_id = null`
- `claims = []`
- function pathway comes from parent JSONB

After repair:

- `_db_id = 14656`
- `locus = chain_hop_claim`
- `hop_index = 1`
- `claims[0].id = 28556`
- `claims[0].chain_id = 2624`
- `hop_local_pathway = Stress Granule Dynamics`

## Acceptance Criteria

- `/api/results/TDP43` no longer emits `TDP43 -> DDX3X` as direct.
- `TDP43 -> DDX3X` emits `locus = net_effect_claim`.
- `TDP43 -> DDX3X` emits `is_net_effect = true`.
- `TDP43 -> DDX3X` emits `via = ["GLE1"]`.
- `/api/results/TDP43` emits `GLE1 -> DDX3X` with `_db_id = 14656`.
- `GLE1 -> DDX3X` emits `locus = chain_hop_claim`.
- `GLE1 -> DDX3X` emits `hop_index = 1`.
- `GLE1 -> DDX3X` emits claim `28556` with `chain_id = 2624`.
- `chain_context_pathway` and `hop_local_pathway` are both explicit.
- Synthesized chain hops remain possible only when no DB row exists.
- Legacy default route still loads and can ignore the added fields.
- Current `?spa=1` route still loads and can ignore the added fields.
- No schema writes are performed unless implementation proves they are required
  and a separate approval is given.

## Implementation Notes 2026-05-07

Scope kept to the read-side contract:

- `services/data_builder.py`
  - net-effect rows now compute `function_context` before the read-time
    direct-assay correction and skip that correction when
    `function_context = net`
  - indirect/net rows no longer rewrite `source` to `upstream_interactor`;
    net-effect rows keep the query-to-terminal display endpoints
  - `_reconstruct_chain_links()` now builds the chain lookup from every valid
    protein in `chain_view.full_chain`, not only mediator symbols
  - DB-backed hop rows keep their own functions/claims instead of being
    overwritten by parent `chain_link_functions`
  - `/api/results` rows now emit `locus`, `chain_members`,
    `chain_context_pathway`, `hop_local_pathway`, `is_net_effect`, `via`, and
    `mediators`
  - claim objects now include `chain_id` and row-local locus/source/target/hop
    metadata
- `tests/test_data_builder_chain_links.py`
  - added coverage for net-effect rows resisting the direct-assay relabel
  - added coverage for terminal chain hops preferring real DB rows over stale
    parent JSONB

The API schema marker is now `2026-05-07`.

## Captured Before/After

Pre-repair `/api/results/TDP43` was captured locally in:

- `/tmp/tdp43-results-before-chain-contract.json`

Observed before repair:

```json
{
  "_db_id": 14655,
  "source": "TDP43",
  "target": "DDX3X",
  "type": "direct",
  "interaction_type": "direct",
  "function_context": "net",
  "chain_id": 2624,
  "locus": null,
  "is_net_effect": null
}
```

```json
{
  "_db_id": null,
  "source": "GLE1",
  "target": "DDX3X",
  "type": "direct",
  "interaction_type": "direct",
  "function_context": "chain_derived",
  "chain_id": 2624,
  "_is_chain_link": true,
  "_chain_position": 1,
  "claims": [],
  "functions": [
    {
      "pathway": "RNA Metabolism & Translation Control"
    }
  ]
}
```

Post-repair `/api/results/TDP43` was captured locally in:

- `/tmp/tdp43-results-after-chain-contract.json`

Observed after repair:

```json
{
  "_db_id": 14655,
  "source": "TDP43",
  "target": "DDX3X",
  "type": "indirect",
  "interaction_type": "indirect",
  "function_context": "net",
  "locus": "net_effect_claim",
  "is_net_effect": true,
  "chain_id": 2624,
  "hop_index": null,
  "chain_members": ["TDP43", "GLE1", "DDX3X"],
  "chain_context_pathway": "Stress Granule Dynamics",
  "hop_local_pathway": null,
  "via": ["GLE1"],
  "mediators": ["GLE1"]
}
```

```json
{
  "_db_id": 14656,
  "source": "GLE1",
  "target": "DDX3X",
  "type": "direct",
  "interaction_type": "direct",
  "function_context": "direct",
  "locus": "chain_hop_claim",
  "is_net_effect": false,
  "chain_id": 2624,
  "hop_index": 1,
  "chain_members": ["TDP43", "GLE1", "DDX3X"],
  "chain_context_pathway": "Stress Granule Dynamics",
  "hop_local_pathway": "Stress Granule Dynamics",
  "via": [],
  "mediators": [],
  "claims": [
    {
      "id": 28556,
      "function_context": "direct",
      "chain_id": 2624,
      "locus": "chain_hop_claim",
      "hop_index": 1,
      "source": "GLE1",
      "target": "DDX3X",
      "pathway_name": "Stress Granule Dynamics"
    }
  ]
}
```

## Verification 2026-05-07

Read-only API checks:

- `/api/results/TDP43` returned `200`
- `/api/results/TDP43` no longer emits `TDP43 -> DDX3X` as direct
- `/api/results/TDP43` emits `GLE1 -> DDX3X` from DB row `14656` with claim
  `28556`
- `/api/visualize/TDP43` returned `200`
- `/api/visualize/TDP43?spa=1` returned `200` and served the React shell/assets

Local test checks:

```bash
python3 -m py_compile services/data_builder.py
pytest -q tests/test_data_builder_chain_links.py
pytest -q tests/test_routes_visualization.py
pytest -q tests/test_data_builder_chain_links.py tests/test_routes_visualization.py
```

Result:

- `tests/test_data_builder_chain_links.py`: `6 passed, 1 warning`
- `tests/test_routes_visualization.py`: `5 passed, 1 warning`
- combined focused route/data-builder pass: `11 passed, 1 warning`

## Stop Point

After this repair is implemented and verified, React v2 can resume at the
adapter/fixture step. Until then, UI work should remain paused because the API
payload is known to misclassify core TDP43 chain semantics.
