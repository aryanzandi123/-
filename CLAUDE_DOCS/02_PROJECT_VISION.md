# ProPaths — Project Vision

## What it is

A bioinformatics discovery + visualization tool that, given any human protein symbol (e.g. ATXN3, TDP43, REST), uses Gemini 3 to construct the protein's full interaction landscape — direct binders, indirect cascades through mediators, the functions of each interaction, the pathways they belong to — and renders it as an explorable D3 interface where the user can drill from "all of ATXN3's biology" down to a specific 4-hop cascade with per-claim evidence and PMIDs.

The user is a researcher. The use case is **protein-of-interest investigation** — pick a protein, understand everything about it, find the non-obvious cascades, generate hypotheses worth testing.

## What problem it solves

For a researcher studying e.g. ATXN3 (ataxin-3, the protein whose polyglutamine expansion causes Machado-Joseph disease), the literature is fragmented across:
- IntAct / BioGRID for direct binders.
- KEGG / Reactome for canonical pathways.
- Hundreds of individual papers for indirect mediator chains (e.g. ATXN3 → VCP → LAMP2 → autophagy).
- Mechanism-level prose buried in introductions and discussions.

ProPaths reads everything and reconstructs the network at the *claim* level: each statement "ATXN3 binds VCP at the K48-poly-ubiquitin recognition site" is a separate row with its own PMIDs, arrow direction, function context, and pathway assignment. The user gets a unified, queryable, explorable knowledge graph of one protein's biology in roughly 20–60 minutes per query.

## Why this is hard (and why the design choices in the codebase exist)

1. **Indirect interactions matter as much as direct ones.** ATXN3 doesn't act on autophagy directly; it acts on VCP, which acts on LAMP2, which acts on the autophagosome. The cascade IS the biology. So the data model has both `Interaction` (pairwise) and `IndirectChain` (multi-hop) entities, and chain participation is many-to-many via `ChainParticipant`. See `05_DATABASE_SCHEMA.md`.

2. **Claims are not interactions.** One protein-protein pair (e.g. ATXN3↔VCP) has many independent scientific claims about it ("VCP unfolds K48-poly-Ub substrates", "VCP recruits NPLOC4 to ATXN3-bound substrates", "ATXN3 deubiquitinates VCP-substrate complexes"). The schema has `InteractionClaim` as a separate table 1:N to `Interaction`. Each claim has its own arrow, evidence, mechanism, pathway, and `function_context` (`direct` / `net` / `chain_derived`).

3. **Pathways are a DAG, not a tree.** "Mitophagy" is a child of both "Selective Autophagy" and "Mitochondrial Quality Control". The schema has `PathwayParent` as a child↔parent edge table, with a `relationship_type` (`is_a` / `part_of` / `regulates`).

4. **Chains are also a DAG, not a tree.** This is the one the FRONTEND is currently getting wrong. See `11_CHAIN_TOPOLOGY.md`. The same protein can be in multiple chains AND have an independent direct claim. The current card view collapses some of these into a single position.

5. **Pseudo-entities are real participants.** RNA, Ubiquitin, Proteasome, Spliceosome, etc. appear in cascades (e.g. TDP43 → FUS → RNA → UNC13A). They're stored as `Protein` rows with `extra_data.is_pseudo=true`. They cannot be the head of a query but they CAN be a chain mediator.

6. **The LLM is wrong sometimes.** Arrow direction, pathway assignment, depth — all need post-validation. The post-processing pipeline (12 stages) is the safety net. See `04_PIPELINE_FLOW.md`.

## What the user wants in the long run

**A frontend that respects biology.** The user has been very clear:

> "we must understand that these are NOT LINEAR NOTHING SHOULD BE CONSTRICTED TO LINEARITY. and more specifically, we must be able to consider the role of other proteins in the middle of any chain etc"

So the eventual target is:
- Card view that handles cascade DAGs, not just linear sequences
- Multi-chain rendering for proteins in N chains
- Cross-link visualization for the same protein appearing in chain context AND direct context
- Direction-aware chain edges using `chain_with_arrows` semantics
- Sub-DAG layouts for dense cascades (branching, convergence, query-in-middle)

The backend already mostly supports this (multi-chain via `ChainParticipant`, `chain_with_arrows`, `chain_context.full_chain`); the frontend hasn't caught up.

## What "PhD-level depth" means (encoded constraint)

In `utils/quality_validator.py` the depth thresholds are:
- min 6 sentences in `effect_description`
- max 10 sentences (avoid bloat)
- min 3 named cascades per claim
- max 5 cascades
- min 6 sentences in `effect_description`
- min 3 specific effects
- min 3 evidence papers

This is non-negotiable. The user reverted a previous downgrade attempt: commit `0f383ce: restore PhD-level depth requirements (6-10 sentences, 3-5 cascades)`.

Practical implication: chain-claim outputs are inherently large. A typical pair output runs 6,500–8,500 tokens of JSON for a typical protein, and 10K-15K for cofactor-rich proteins like REST. The `.env CHAIN_CLAIM_MAX_OUTPUT_TOKENS` cap was 8192 originally (causing 27%+ truncation), now 24000 as of 2026-05-03 — see `09_FIXES_HISTORY.md` § 1.7.

## What's NOT in scope

- Mouse / rat / yeast proteins. Human only.
- Protein structure (PDB / AlphaFold). Sequence and function only.
- Drug-target databases. The biology IS the target.
- Multi-protein queries. One protein at a time.
- Manual curation UI. The LLM is the curator; the UI is the explorer.

## Domain glossary (for biological literacy)

If you don't know these, look them up before responding to a biology message:

- **ATXN3 (Ataxin-3)** — DUB (deubiquitinase), poly-Q expansion → Machado-Joseph / spinocerebellar ataxia type 3. The user's recurring example query.
- **TDP43 (TARDBP)** — RNA-binding protein, mislocalization → ALS / FTD. Stress-granule biology.
- **REST (NRSF)** — neuronal-fate transcription factor, repression of neuronal genes outside neurons. The user's third recurring example query.
- **VCP (p97)** — AAA+ ATPase, segregase for K48-poly-Ub substrates, central to ERAD and autophagy.
- **HSP90AA1 / HSP70 (HSPA8) / CDC37** — chaperone system. STUB1 (CHIP) ubiquitinates clients of HSP90 for degradation.
- **STUB1 (CHIP)** — E3 ligase. Ubiquitinates HSP90 clients for proteasomal degradation. **In a chain involving HSP90AA1, STUB1 is causally upstream of HSP90AA1's degradation.** The user's STUB1/HSP90AA1 example targets exactly this.
- **HDAC6** — cytoplasmic histone deacetylase, also chaperone-network member, recognizes K63-poly-Ub aggregates → autophagy. Often appears under multiple pathways.
- **REST cofactors:** SIN3A/HDAC1/HDAC2/RCOR1/SAP30/SDS3 (transcriptional repression complex), KDM1A (LSD1), SUV39H1, EZH2 (H3K27me3). Highly densely studied — outputs for these proteins are the longest (and the most truncation-prone).
- **Pseudo-entities:** RNA, mRNA, Ubiquitin, SUMO, NEDD8, Proteasome, Ribosome, Spliceosome, Actin, Tubulin, Stress Granules, P-bodies. Whitelisted in `utils/db_sync.py:_PSEUDO_WHITELIST`. Stored as `Protein` rows with `extra_data.is_pseudo=true`.
- **Function context values:** `direct` (pair-specific claim), `net` (the indirect-cascade NET effect), `chain_derived` (a per-hop claim within a chain), `mixed` (legacy). Constrained by Postgres CHECK constraint.
