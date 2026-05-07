# Chain Semantics Contract Audit: TDP43 -> GLE1 -> DDX3X

Date: 2026-05-06

Scope: no product-code edits. This audit inspected source with Serena plus read-only API/DB checks for the TDP43 chain case:

`TDP43 -> GLE1 -> DDX3X`

Primary files inspected:

- `models.py`
- `utils/db_sync.py`
- `services/data_builder.py`
- `utils/claim_locus_router.py`
- `utils/chain_resolution.py`
- `runner.py`
- `utils/post_processor.py`
- `scripts/pathway_v2/quick_assign.py`
- `static/_legacy/card_view.js`
- `static/_legacy/modal.js`
- `react-app/src/app/views/card/buildCardGraph.ts`
- `react-app/src/app/lib/interactionSurface.ts`
- `react-app/src/app/lib/claims.ts`
- live `/api/results/TDP43`
- live `/api/chain/2624`
- read-only PostgreSQL selects

## Executive Finding

`TDP43 -> DDX3X` is not stored in the database as a normal direct interaction. It is stored as an indirect/net-effect interaction:

- DB interaction `14655`
- pair: `TDP43` / `DDX3X`
- `interaction_type = indirect`
- `function_context = net`
- `chain_id = 2624`
- claim `28553`, `function_context = net`, `chain_id = 2624`
- chain: `TDP43 -> GLE1 -> DDX3X`
- chain pathway: `Stress Granule Dynamics`

The UI presents it like a normal direct interaction because the read/API layer changes the payload classification from indirect to direct. In `/api/results/TDP43`, the same DB row is emitted as:

- `type = direct`
- `interaction_type = direct`
- `function_context = net`
- `_net_effect = true`
- `_display_badge = NET EFFECT`

That is an invalid frontend contract. `type`/`interaction_type` says direct while `function_context` and chain metadata say net effect. Any frontend using `type` to decide graph/card semantics will misrender this as direct.

The `GLE1 -> DDX3X` pathway mismatch is also not primarily a biological decision in the current payload. The database has a real hop row and claim under `Stress Granule Dynamics`, but `/api/results/TDP43` fails to use that row and synthesizes the final hop from stale/raw parent JSONB `chain_link_functions`, where the original hop-local pathway is `RNA Metabolism & Translation Control`.

## Live Evidence

### DB Rows

Read-only DB check for the TDP43/GLE1/DDX3X trio:

| DB id | Pair | `interaction_type` | `function_context` | `chain_id` | `depth` | `data.step3_finalized_pathway` |
|---:|---|---|---|---:|---:|---|
| 14609 | TDP43 / GLE1 | direct | direct | 2624 | 2 | Stress Granule Dynamics |
| 14655 | TDP43 / DDX3X | indirect | net | 2624 | 2 | Stress Granule Dynamics |
| 14656 | GLE1 / DDX3X | direct | direct | 2624 | 2 | Stress Granule Dynamics |

Read-only `chain_participants` for chain `2624`:

| Chain | Interaction | Role | Chain | Chain pathway |
|---:|---:|---|---|---|
| 2624 | 14609 | hop | `["TDP43","GLE1","DDX3X"]` | Stress Granule Dynamics |
| 2624 | 14656 | hop | `["TDP43","GLE1","DDX3X"]` | Stress Granule Dynamics |
| 2624 | 14655 | origin | `["TDP43","GLE1","DDX3X"]` | Stress Granule Dynamics |

Read-only claims for chain `2624`:

| Claim | Interaction | Pair | Function | Context | Claim chain | Pathway |
|---:|---:|---|---|---|---:|---|
| 28554 | 14609 | TDP43 / GLE1 | mRNP Composition Modulation & Nuclear Export Control | direct | 2624 | Stress Granule Dynamics |
| 28555 | 14609 | TDP43 / GLE1 | mRNP Remodeling & Nuclear Export Regulation | direct | 2624 | Stress Granule Dynamics |
| 28553 | 14655 | TDP43 / DDX3X | RNA Helicase Activity & Stress Granule Dynamics | net | 2624 | Stress Granule Dynamics |
| 28556 | 14656 | GLE1 / DDX3X | RNA Helicase Activity & Translation Initiation & Stress Granule Dynamics | direct | 2624 | Stress Granule Dynamics |

The same `TDP43 / GLE1` pair also has claim `28486` for chain `2623` under `Nucleocytoplasmic Transport`, which means the pair participates in more than one chain/pathway. React v2 must not assume one pair equals one chain context.

### API Rows

`/api/results/TDP43` emits the parent/net row as:

```json
{
  "_db_id": 14655,
  "source": "TDP43",
  "target": "DDX3X",
  "type": "direct",
  "interaction_type": "direct",
  "function_context": "net",
  "_net_effect": true,
  "_display_badge": "NET EFFECT",
  "chain_id": 2624
}
```

The net-effect prose explicitly says the effect is mediated through GLE1. That prose is correct for a net-effect claim, but it is inconsistent with `type = direct`.

`/api/results/TDP43` emits the final hop as:

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
  "_chain_length": 3,
  "functions": [
    {
      "function": "RNA Helicase Activity & Translation Initiation & Stress Granule Dynamics",
      "pathway": "RNA Metabolism & Translation Control",
      "function_context": "chain_derived"
    }
  ],
  "claims": []
}
```

That is wrong relative to DB storage: there is a DB row `14656` and claim `28556` for this hop.

`/api/chain/2624` is more internally coherent than `/api/results/TDP43`: it returns chain participants `14609`, `14655`, and `14656`, with `14655` still `interaction_type = indirect` and `14656` present as the GLE1/DDX3X hop.

## Creation and Storage Path

### Models

`models.py` defines the intended dual-track system:

- `Interaction.interaction_type`: pair row type, `direct` or `indirect`.
- `Interaction.function_context`: `direct`, `net`, `chain_derived`, or `mixed`.
- `Interaction.chain_id`: denormalized pointer to an `IndirectChain`.
- `IndirectChain.chain_proteins`: arbitrary-length ordered chain.
- `IndirectChain.chain_with_arrows`: one arrow per hop.
- `ChainParticipant`: many-to-many interaction/chain membership with role `origin`, `hop`, or `net_effect`.
- `InteractionClaim.function_context` and `InteractionClaim.chain_id`: per-claim locus/context.

The model supports chains longer than three proteins. `Interaction.depth` is documented as no upper cap, and `IndirectChain.chain_proteins` is an unconstrained JSONB array.

Current ambiguity:

- The schema allows `ChainParticipant.role = net_effect`, but `utils/db_sync.py` registers the parent interaction for chain `2624` as `origin`, not `net_effect`.
- Hop claims saved after chain splitting are often `function_context = direct`, while API-synthesized hop functions use `function_context = chain_derived`.
- The DB has no first-class `hop_index`, `hop_source`, or `hop_target` columns on `ChainParticipant`; the read path derives or stamps `_chain_position`.

### Chain Claim Generation

The prompt contract in `pipeline/prompts/shared_blocks.py` says indirect interactors must emit:

- top-level `functions[]` for the query-to-terminal net effect, with `function_context = net`
- `chain_link_functions` with one key per adjacent hop, with `function_context = chain_derived`
- `chain_with_arrows`, one arrow per hop

`runner.py` preserves that model by gathering every hop from every full chain and attaching pair-keyed records into `chain_link_functions`. Its zero-skip invariant says every hop of every resolved chain must be handed to chain-claim generation.

`utils/post_processor.py` audits missing hop entries before DB sync and annotates `_chain_incomplete_hops`; it does not repair or delete data.

`utils/claim_locus_router.py` routes candidate hop claims:

- hop-local claims stay on the hop
- claims mentioning the query plus hop/mediator context are rerouted to the parent indirect/net row
- claims that belong nowhere useful are dropped or converted to a router stub

That router is conceptually correct for React v2: net-effect prose that mentions TDP43 on the GLE1/DDX3X hop should not render as a normal GLE1/DDX3X direct claim.

### DB Sync

`utils/db_sync.py` writes the parent interactor first, then calls `sync_chain_relationships()` for indirect interactions.

Relevant behavior:

- `full_chain` comes from `chain_context.full_chain` when available.
- `IndirectChain` is keyed by `(origin_interaction_id, chain_signature)`.
- `chain_pathway` is taken from `_chain_pathway` or `step3_finalized_pathway`, otherwise dominant pathway from top-level chain functions.
- Every adjacent hop is processed with `for i in range(len(full_chain) - 1)`, so arbitrary chain length is structurally supported.
- Hop `link_data` gets `_hop_signature` and `chain_context.link_position`.
- Hop functions are forced to the chain pathway and retagged `function_context = direct` before writing hop rows.
- `_tag_claims_with_chain()` tags interactions and claims with `chain_id` and registers `ChainParticipant` rows.

Important contract drift:

- The parent/net row is also tagged with `_tag_claims_with_chain(parent_interaction, chain_record)` using the default role unless the earlier origin membership already exists. In chain `2624`, the parent role is `origin`.
- Hop functions are stored as `direct` downstream, but the original raw parent JSONB `chain_link_functions` may still contain `chain_derived` and a different pathway.
- Current DB sync treats hop claims as direct pair claims once split, but React v2 needs a separate locus label: `chain_hop_claim`.

### Pathway Assignment and Unification

`scripts/pathway_v2/quick_assign.py` has a chain unification pass:

- `_pick_chain_dominant_pathway_id()` prefers `function_context = net` pathway for the chain.
- `_unify_all_chain_claims()` walks claims with a non-null `chain_id` and unifies chain claims to a dominant chain pathway.
- It also updates the `IndirectChain.pathway_name`.

For chain `2624`, DB claims and chain record are unified to `Stress Granule Dynamics`.

The visible `GLE1 -> DDX3X` API mismatch is not because DB stored it under a different pathway. The DB has:

- raw parent JSONB `chain_link_functions["GLE1->DDX3X"][0].pathway = RNA Metabolism & Translation Control`
- stored hop row `14656.data.functions[0].pathway = Stress Granule Dynamics`
- stored hop claim `28556.pathway_name = Stress Granule Dynamics`

So the read/API layer is exposing stale/raw parent JSONB rather than the stored hop row.

## Reconstruction and API Path

`routes/results.py` serves `/api/results/<protein>` by calling `services.data_builder.build_full_json_from_db(protein)`.

### Net-effect row becomes direct in API

In `services/data_builder.py`, `build_full_json_from_db()` has a read-time "interaction_type assay guard". If `interaction_type_value == "indirect"` and the prose mentions positive direct-assay keywords, it changes the payload value to direct. It explicitly says the DB is not mutated.

That guard does not protect net-effect rows. For `TDP43 -> DDX3X`, the DB row remains `indirect/net`, but the API emits `direct/net`.

Required v2 behavior:

- A row with `function_context = net`, `chain_id != null`, or a net-effect claim must never be rendered as a direct pair interaction, even if some prose contains direct-assay language.
- Any assay correction must be claim-local and pair-local, not applied to net-effect rows.

### Final hop is synthesized despite DB row existing

`services/data_builder.py._reconstruct_chain_links()` builds a mediator protein map from mediators only, then later adds the main query protein to the map. It does not add terminal chain proteins that are neither the query nor mediators.

For `TDP43 -> GLE1 -> DDX3X`:

- mediator map includes `GLE1`
- query map later includes `TDP43`
- terminal `DDX3X` is not added
- `_lookup_chain_link("GLE1", "DDX3X")` returns `None`
- the code synthesizes a chain-link payload from parent `chain_link_functions`

That is why `/api/results/TDP43` has `_db_id = null` for `GLE1 -> DDX3X`, even though DB row `14656` exists.

The same pattern appears in the TDP43 long-chain reference `GRN -> SORT1 -> CTSD -> TDP43`: `/api/results/TDP43` synthesizes `GRN -> SORT1` with `_db_id = null`, even though DB row `14627` exists, because `GRN` is a chain endpoint and not in the mediator map.

Required v2 behavior:

- Every hop lookup must resolve both endpoints from the full chain, not only mediators plus query.
- If a DB hop row exists, the API/adapter must use it and attach scoped claims.
- Synthesized hop rows are allowed only as explicit `claim_state = no_claim` or `source = chain_topology_only`, never silently equivalent to DB-backed hop claims.

### Claim serialization loses chain scoping

`services/data_builder.py._serialize_claims()` emits claim fields like `id`, `function_name`, `pathway_name`, and `function_context`, but does not emit `chain_id`. Cross-protein injection emits `chain_id`, but normal serialized claims do not.

For React v2 this is not enough. Claims must be scoped by:

- `claim_id`
- `claim_locus`
- `chain_id`
- `hop_index` when hop-local
- `source`
- `target`
- `chain_context_pathway`
- `hop_local_pathway`

Without this, a frontend cannot safely distinguish:

- direct pair evidence
- chain-hop evidence for a specific chain
- net-effect evidence for a query-to-terminal consequence
- same pair participating in multiple chains

## Frontend Rendering Assumptions

### Legacy

Legacy is semantically closer than current React in some graph/card behaviors:

- `static/_legacy/card_view.js` has an independent chain reading model with per-chain duplicate nodes and `_chainPosition`/`_chainLength` metadata.
- It allows the same protein to appear once as a direct node and again as a chain participant.
- It renders chain context banners and hop navigation in `static/_legacy/modal.js`.

But legacy also has bugs that React v2 must not preserve:

- `showInteractionModal()` uses `L.interaction_type === "indirect"` to decide indirect behavior. That fails for API rows like `direct/net`.
- Legacy modals prefer `claims` over raw `functions`; for DB-backed chain links sharing a pair row, unscoped direct claims can leak into hop modals.
- Legacy deduplicates claims by visible content and ignores `function_context`, which can hide meaningful direct/net/chain distinctions.
- Aggregate modal perspective transforms can make indirect/net effects look direct if the payload type is wrong.

### Current React

Current React should be mined only for helpers, not architecture.

Observed assumptions:

- `buildCardGraph.ts` direct mode uses `inter.type === "direct" && !inter._is_chain_link`.
- Because `/api/results/TDP43` emits `TDP43 -> DDX3X` as `type = direct`, current React includes the net-effect row in the direct pass.
- `passesFilterMode()` treats indirect as `inter.type === "indirect" || _is_chain_link`; it misses `direct/net`.
- `pathwayTouches()` only checks `pathways`, `chain_pathways`, `all_chains.pathway_name`, and `all_chains.chain_pathways`; it does not check function/claim-local pathways.
- `interactionSurface.ts` correctly prefers `functions` for `_is_chain_link` rows, which avoids some legacy claim leakage, but it still relies on the ambiguous API payload and pair/chain matching.
- `claims.ts` has useful claim normalization and special-state helpers, but it is not a full locus contract.

## Required React v2 Contract

React v2 must not classify edges from `type` or `interaction_type` alone. It needs a normalized model with explicit claim/edge locus.

### Core Entities

#### `direct_claim`

Only real pair-specific evidence.

Required fields:

- `kind: "direct_claim"`
- `interaction_id`
- `source`
- `target`
- `direction`
- `arrow`
- `claims[]`
- each claim has `claim_locus = "direct_claim"`
- `chain_id = null` unless the same pair also participates in chains; chain participation must not convert direct evidence into chain-hop evidence

Rendering rule:

- May appear as a direct graph/card edge.
- Must not include net-effect prose.

#### `chain_hop_claim`

One adjacent hop in one specific chain.

Required fields:

- `kind: "chain_hop_claim"`
- `chain_id`
- `hop_index`
- `source`
- `target`
- `interaction_id` when DB-backed, else `null`
- `source_of_row: "db" | "synthesized_topology"`
- `chain_length`
- `chain_proteins`
- `chain_with_arrows`
- `chain_context_pathway`
- `hop_local_pathway`
- `claims[]`
- each claim has `claim_locus = "chain_hop_claim"`, `chain_id`, `hop_index`, `source`, `target`

Rendering rule:

- Render inside the chain, not as an ordinary direct query edge.
- If `hop_local_pathway != chain_context_pathway`, show both labels explicitly:
  - "Chain pathway: Stress Granule Dynamics"
  - "Hop claim pathway: RNA Metabolism & Translation Control"
- A hop can be DB-backed and still be shown as a chain hop; DB `function_context = direct` must not erase chain locus.

#### `net_effect_claim`

Query-to-terminal consequence through a chain.

Required fields:

- `kind: "net_effect_claim"`
- `interaction_id`
- `chain_id`
- `source`
- `target`
- `via: string[]`
- `chain_proteins`
- `chain_with_arrows`
- `chain_context_pathway`
- `claims[]`
- each claim has `claim_locus = "net_effect_claim"` and `function_context = "net"`

Rendering rule:

- Never render as a direct edge.
- Always label as `NET EFFECT` / `INDIRECT VIA <chain>`.
- Its prose may mention TDP43 and mediated effects; that prose belongs here, not on a hop.

#### `no_claim` / partial hop

Required fields:

- `kind: "chain_hop_claim"`
- `source_of_row = "synthesized_topology"`
- `claim_state = "no_claim"`
- `claims = []`
- `chain_id`
- `hop_index`
- `source`
- `target`

Rendering rule:

- Show the hop in the chain topology.
- Do not fabricate mechanism text.
- Display a clear no-claim/thin/partial state.

### Pathway Contract

React v2 needs both pathway scopes:

- `chain_context_pathway`: the pathway used to group/display the whole chain.
- `hop_local_pathway`: the pathway attached to the individual hop claim.

Pathway filtering must include a chain/hop if any of these match the selected pathway:

- `chain_context_pathway`
- `hop_local_pathway`
- any claim `pathway_name`
- any claim pathway hierarchy ancestor
- chain `chain_pathways`

Filtering must not drop valid TDP43/PERK edges just because the whole-chain pathway and hop-local pathway differ. It should preserve the chain topology and visually mark out-of-filter hops when needed, rather than silently creating partial chains.

### Arbitrary Chain Length

React v2 must treat chains as arrays, not as query -> mediator -> target triples:

- `chain_proteins.length = N`
- hops are indices `0..N-2`
- every hop has a stable key: `chain_id + hop_index + source + target`
- repeated proteins and repeated pairs must not collapse distinct hop positions
- all modal navigation must use `hop_index`

## Backend/Data Contract Bugs vs Frontend Bugs

### Backend / API / Data Contract Bugs

1. `services/data_builder.py` changes `indirect/net` DB rows to `direct/net` API rows.
   - This is the primary cause of `TDP43 -> DDX3X` rendering like a normal direct interaction.
   - Net-effect rows must be immune to direct-assay payload correction.

2. `_reconstruct_chain_links()` can synthesize a hop even when the DB hop row exists.
   - It only resolves mediators plus the query in `_lookup_chain_link()`.
   - It misses start/end terminal proteins such as `DDX3X` or `GRN`.
   - This causes stale raw `chain_link_functions` to leak into API output.

3. Normal API claim serialization omits `chain_id`.
   - Frontends cannot scope claims by chain without falling back to row-level guesses.

4. `ChainParticipant` roles are not semantically rich enough in practice.
   - The schema supports `net_effect`, but current chain `2624` marks the parent as `origin`.
   - React v2 needs explicit `net_effect_claim` classification whether or not the DB role changes.

5. Hop-local pathway vs chain-context pathway is not explicit.
   - DB sync often unifies persisted chain claims to the chain pathway.
   - Raw parent JSONB can still carry original hop-local pathway.
   - API currently exposes whichever source the reconstruction path happens to use.

6. Durable hop metadata is incomplete.
   - `chain_context.link_position` is not consistently populated in historical/current sampled long-chain participant rows.
   - `ChainParticipant` lacks `hop_index`, `source`, and `target`.
   - The API stamps `_chain_position`, but React v2 should consume a deliberate normalized adapter, not infer from mixed legacy fields.

### Frontend Rendering Bugs

1. Legacy modal uses `interaction_type` alone for indirect behavior.
   - It does not treat `function_context = net` as an indirect/net-effect row.

2. Legacy and current React can treat `direct/net` as direct because the API provides that contradictory state.
   - Current React direct pass uses `inter.type === "direct" && !inter._is_chain_link`.

3. Claim selection is insufficiently locus-aware.
   - Legacy claims-first modal can leak direct pair claims into chain-hop displays.
   - Current React improves this by preferring `functions` for `_is_chain_link`, but still lacks an explicit normalized claim locus.

4. Pathway filtering is too shallow in current React.
   - It does not fully account for claim-local pathway/hierarchy.
   - It can omit chains/hops when chain-context and hop-local pathways diverge.

## Are Longer Chains Preserved?

Partially.

DB structural support is present:

- `IndirectChain.chain_proteins` is arbitrary length.
- `sync_chain_relationships()` iterates every adjacent hop with `range(len(full_chain) - 1)`.
- Live DB has six chains with more than three proteins.
- Sampled long chains had participant counts equal to `N` for an `N`-protein chain: `N - 1` hop participants plus one origin/net row.
- Example DB chains:
  - `GRN -> SORT1 -> CTSD -> TDP43`
  - `PERK -> EIF2S1 -> EIF2B1 -> ATF4 -> DDIT3 -> BCL2L11`
  - `PERK -> EIF2S1 -> ATF4 -> DDIT3 -> PPP1R15A`

API/read contract is not clean enough:

- `/api/results/TDP43` emits long-chain hop rows with `_chain_position` and `_chain_length`, so the frontend can render some long chains.
- But the same `_lookup_chain_link()` endpoint-resolution bug causes first/last hops to become synthesized even when DB rows exist.
- Some DB hop participant rows have missing `chain_context.link_position`.
- First-hop rows can keep `interaction_type = indirect` in DB but emit as `direct` in API, so hop classification cannot rely on `interaction_type`.

Conclusion: arbitrary chain length is structurally stored, but React v2 must require a normalized chain adapter before implementation. Do not rely directly on the current mixed `/api/results` semantics.

## Direct Answers

### Is `TDP43 -> DDX3X` being stored as direct, chain-derived, or net-effect?

It is stored as a net-effect interaction, not a normal direct interaction.

DB truth:

- `Interaction 14655`: `interaction_type = indirect`, `function_context = net`, `chain_id = 2624`
- `InteractionClaim 28553`: `function_context = net`, `chain_id = 2624`

API problem:

- `/api/results/TDP43` emits it as `interaction_type/type = direct` while preserving `function_context = net`.

### Why does the prose say mediated through GLE1 while the UI presents it like a normal interaction?

Because the prose is a net-effect claim and correctly describes mediation through GLE1, but the API payload mutates the row to `type = direct`. Legacy and current React both use `type`/`interaction_type` as a major rendering gate, so the row is visually treated as direct even though `_net_effect` and `function_context = net` say otherwise.

### Why does `GLE1 -> DDX3X` appear under a different pathway than the chain?

Because `/api/results/TDP43` synthesizes the final hop from parent JSONB instead of using DB row `14656`.

The parent JSONB has:

- `chain_link_functions["GLE1->DDX3X"][0].pathway = RNA Metabolism & Translation Control`

The DB-backed hop has:

- `Interaction 14656.data.functions[0].pathway = Stress Granule Dynamics`
- `InteractionClaim 28556.pathway_name = Stress Granule Dynamics`

The synthesis happens because `_lookup_chain_link()` cannot resolve `DDX3X` from its mediator-only map.

React v2 should support legitimate hop-local pathway labels, but this specific visible mismatch is a read/API reconstruction bug.

### Are chains longer than three proteins preserved correctly in DB and API?

DB: mostly yes structurally. The schema and sync loop preserve arbitrary-length chains and every adjacent hop in sampled long-chain rows.

API: partially, but not safely. It can emit long-chain topology, positions, and arrows, but it can synthesize endpoint hops despite existing DB rows and lacks a clean explicit claim-locus contract.

### What exact data contract does React v2 need before implementation?

React v2 needs a normalized contract with explicit `kind`/`claim_locus`:

- `direct_claim`
- `chain_hop_claim`
- `net_effect_claim`
- `no_claim` / `thin` / `shallow` / `router` / `synthetic` states

It also needs:

- `chain_id`
- `hop_index`
- `source`
- `target`
- `chain_proteins`
- `chain_with_arrows`
- `chain_context_pathway`
- `hop_local_pathway`
- claim-level `chain_id`
- claim-level pathway and evidence/PMIDs
- DB-backed vs synthesized topology provenance

### Which issues are backend/data contract bugs vs frontend rendering bugs?

Backend/data contract bugs:

- API flips net-effect rows to direct.
- API synthesizes DB-backed endpoint hops.
- API omits claim-level chain scoping in normal claim serialization.
- Hop-local vs chain-context pathway is implicit and source-dependent.
- Durable hop index/source/target metadata is missing or inconsistent.

Frontend rendering bugs:

- Legacy and React rely on `type`/`interaction_type` too much.
- Legacy modal is not claim-locus aware.
- Current React direct pass admits `direct/net`.
- Current React pathway filtering does not fully use claim-local pathway/hierarchy.

## Recommendations Before React v2 Implementation

1. Define a v2 normalization adapter before building UI components.
   - It can live server-side or as a frontend adapter, but it must output explicit `direct_claim`, `chain_hop_claim`, and `net_effect_claim` records.

2. Treat `function_context = net` plus `chain_id` as a hard override.
   - Never direct-render those rows.

3. Fix or compensate for endpoint hop reconstruction before using `/api/results` as v2 input.
   - Hop lookup must resolve all proteins in `chain_proteins`, not only mediators plus query.

4. Preserve both pathway scopes.
   - Display `chain_context_pathway` and `hop_local_pathway` separately when they differ.

5. Add claim-level chain metadata to the v2 contract.
   - Frontend should not infer claim scope from parent row shape.

6. Use `/api/chain/<id>` as a consistency reference, but not the sole contract.
   - It currently proves DB chain participants exist, but it does not include full claim/function/evidence payloads needed by React v2.

7. Make TDP43 chain acceptance non-negotiable.
   - `TDP43 -> DDX3X` must render as `NET EFFECT / INDIRECT VIA GLE1`.
   - `GLE1 -> DDX3X` must render as chain hop 2 of 2, DB-backed when row `14656` exists.
   - If hop-local pathway differs from chain pathway, both labels must be visible.
   - No net-effect prose may render as ordinary direct pair evidence.
