"""Shared prompt text blocks and system prompt helpers.

All six text constants live here as the single source of truth.
Other modules import from here; legacy import paths re-export these.
"""
from __future__ import annotations

from typing import Optional

# ----------------------------------------------------------------
# Global constants
# ----------------------------------------------------------------
MAX_OUTPUT_TOKENS: int = 60000
DYNAMIC_SEARCH_THRESHOLD: float = 0.0

# ----------------------------------------------------------------
# Temperature strategy
# ----------------------------------------------------------------
# Discovery steps: 0.3 — low temperature for consistent, reproducible naming
# Function mapping: 1.0 — high temperature for diverse function discovery
# Deep research:   1.0 — high temperature for creative literature mining
# QC / validation:  1.0 — model default (validation doesn't need low temp)
# Evidence validator (post-processing): 1.0 — both generateContent and interaction modes

# ----------------------------------------------------------------
# Shared text blocks
# ----------------------------------------------------------------

DIFFERENTIAL_OUTPUT_RULES = """╔═══════════════════════════════════════════════════════════════╗
║  DIFFERENTIAL OUTPUT RULES (CRITICAL FOR TOKEN EFFICIENCY)   ║
╚═══════════════════════════════════════════════════════════════╝

SCOPE OF THESE RULES:
These differential rules apply ONLY when previous-iteration context
has been provided in the conversation. For the FIRST iteration of a
new query (no prior interactors in the conversation history), output
EVERYTHING you find — there is nothing to deduplicate against. Treat
"incremental" as "everything new" on iteration 1. Returning only a
handful of interactors on iteration 1 because you're trying to stay
"differential" is wrong behavior and will be flagged as a failure.

YOU MUST OUTPUT ONLY **NEW OR MODIFIED DATA** FROM THIS STEP!

DO NOT re-output existing interactors or functions from previous steps.
The runner will merge your incremental changes into the full context.

OUTPUT FORMAT:
{
  "ctx_json": {
    "main": "{user_query}",  // Always include this
    "interactors": [
      // ONLY interactors you are ADDING or MODIFYING in THIS step
      // If you're adding functions to existing interactor, include only that interactor with new functions
      // If you're adding new interactors, include only the new ones
    ],
    "interactor_history": ["NEW1", "NEW2", ...],  // ONLY newly discovered names
    "function_history": {"PROTEIN": [...]},       // ONLY updated entries
    "function_batches": ["BATCH1", ...],          // ONLY new batches processed
    "search_history": ["query1", ...]             // ONLY new search queries
  },
  "step_json": {"step": "...", ...}
}

EXAMPLES:
❌ BAD (re-outputting everything):
  "interactors": [all 32 interactors with all their functions]

✅ GOOD (incremental update):
  "interactors": [the 5 NEW interactors you just discovered]

❌ BAD (for function mapping step):
  "interactors": [all 32 interactors]

✅ GOOD (for function mapping step):
  "interactors": [
    {
      "primary": "PROTEIN_X",  // only this interactor
      "functions": [new function 1, new function 2]  // only NEW functions for this protein
    }
  ]

The runner will intelligently merge your output with existing data."""

STRICT_GUARDRAILS = """╔═══════════════════════════════════════════════════════════════╗
║  GROUNDING RULES                                              ║
╚═══════════════════════════════════════════════════════════════╝
- Every claim must be grounded in primary literature you can actually locate.
- Do not fabricate citations, paper titles, or evidence details.
- Evidence validation happens in a dedicated post-processing step — placeholder evidence structures are fine where the schema allows them.
- Output ONLY valid JSON (no markdown, no prose, no explanations).
- If you genuinely cannot substantiate a name or claim after searching, leave it out.

╔═══════════════════════════════════════════════════════════════╗
║  GENERIC ENTITY RULES (HARD)                                  ║
╚═══════════════════════════════════════════════════════════════╝
- The following terms are GENERIC BIOMOLECULE CLASSES, not gene symbols:
  RNA, mRNA, pre-mRNA, tRNA, rRNA, lncRNA, miRNA, snRNA, snoRNA,
  DNA, ssDNA, dsDNA, Ubiquitin, SUMO, NEDD8,
  Proteasome, Ribosome, Spliceosome,
  Actin, Tubulin, "Stress Granules", "P-bodies".
- They MUST NOT appear as a top-level "primary" interactor of the query.
  Every entry in interactors[*].primary must be a real gene symbol matching
  the regex ^[A-Z][A-Z0-9]{0,14}([-/][A-Z0-9]+)?$ (uppercase, 1-15 chars,
  optional hyphen/slash suffix).
- They MAY appear as a MEDIATOR inside mediator_chain / full_chain /
  chain_link_functions ONLY when the biology genuinely passes through that
  class (e.g. "FUS binds UG-rich mRNA motifs to recruit STMN2 transcripts").
- When a generic class is used as a mediator, the claim's cellular_process
  MUST specify which subclass / motif / paralog is involved (e.g. "UG-rich
  3' UTR mRNA segments", "K48-linked polyubiquitin chains") rather than
  the bare term. If the literature does not specify, drop the hop.
- For Ubiquitin specifically: prefer the canonical encoding gene
  (UBC, UBA52, UBB, UBA1) when the paper identifies it; fall back to the
  generic "Ubiquitin" only when the paper itself uses the generic term."""

SCHEMA_HELP = """╔═══════════════════════════════════════════════════════════════╗
║  SCHEMA SPECIFICATION                                         ║
╚═══════════════════════════════════════════════════════════════╝

ctx_json = {
  'main': '<HGNC_SYMBOL>',
  'interactors': [{
      'primary': '<HGNC_SYMBOL>',

      // NEW: INTERACTION TYPE (determined in discovery phase)
      'interaction_type': 'direct' | 'indirect',
      'upstream_interactor': '<HGNC>' | null,  // null for direct, protein name for indirect
      'mediator_chain': ['<PROTEIN1>', '<PROTEIN2>', ...],  // Ordered path of intermediate proteins between main and this interactor (empty for direct). No length cap — include EVERY intermediate protein the literature supports, even if the chain is 4, 5, or more proteins long.
      'depth': <integer ≥ 1>,  // Total path length from main to primary, counting both endpoints (1 = direct, 2 = one mediator, 3 = two mediators, N = N-1 mediators). Do NOT cap at 3.

      // QUERY-POSITION-AGNOSTIC CHAIN (optional, highly preferred when
      // the biology places {user_query} anywhere other than the head):
      //   mediator_chain is "path FROM query TO interactor" — it forces
      //   the query to position 0. That is WRONG when the query
      //   biologically sits in the middle of a cascade. Use
      //   chain_context.full_chain to express the true ordering with
      //   the query at whatever index it biologically belongs to.
      //   Examples:
      //     upstream:  ['VCP', '{user_query}']            // query at END
      //     mid-chain: ['VCP', '{user_query}', 'MTOR']    // query middle
      //     downstream:['{user_query}', 'RHEB', 'MTOR']   // query at HEAD
      //   Backend db_sync honors this case-insensitively and does not
      //   require the query at index 0. When both are emitted on the
      //   same interactor, full_chain wins for rendering.
      'chain_context': { 'full_chain': ['<P1>', '<P2>', '<P3>', ...] },

      // ARROW/DIRECTION (determined AFTER function discovery in Step 2b)
      'direction': 'main_to_primary' | 'primary_to_main',
      'arrow': 'activates' | 'inhibits' | 'binds',
      'intent': 'phosphorylation' | 'ubiquitination' | 'binding' | ...,

      'multiple_mechanisms': true | false,
      'mechanism_details': ['<specific mechanism 1>', '<mechanism 2>', ...],

      'pmids': [],  // Extracted from paper titles in function discovery
      'evidence': [{
          'paper_title': '<FULL title from literature>',
          'relevant_quote': '<1-2 sentence paraphrase of key finding, NOT verbatim>',
          'year': <int>,
          'doi': '<DOI if available>',
          'authors': '<first author et al.>',
          'journal': '<journal name>',
          'assay': '<experimental method>',
          'species': '<organism>'
      }],

      'support_summary': '<brief evidence summary>',

      // FUNCTIONS (discovered in Step 2a-2a5, enriched with paper titles)
      'functions': [{
          'function': '<ULTRA-SPECIFIC function name — see FUNCTION_NAMING_RULES>',
          'arrow': 'activates' | 'inhibits',
          'function_context': 'direct' | 'net',  // REQUIRED — see FUNCTION_CONTEXT_LABELING
          'cellular_process': '<6+ sentences: domains, residues, PTMs, compartment, triggers>',
          'effect_description': '<4+ sentences: quantitative impact, downstream signaling>',

          'biological_consequence': [
              '<cascade 1: 6+ named steps, distinct PATHWAY A → physiological outcome A. e.g. "ATXN3 binds VCP → VCP extracts misfolded substrate from ER membrane → substrate K48-polyubiquitinated by HRD1 → 26S proteasome recognizes Ub-chain → polypeptide unfolded by Rpt1-6 ATPases → 20S core hydrolyzes peptide bonds → cytosolic peptide pool replenished → ER proteostasis restored"',
              '<cascade 2: 6+ named steps, DIFFERENT pathway B → different outcome B. e.g. "ATXN3 deubiquitinates BECN1 K117 → BECN1 dissociates from BCL2 → BECN1 recruits PIK3C3/VPS34 → PI3P generated at ER cradle → WIPI2 binds PI3P → ATG16L1-ATG5-ATG12 conjugates LC3-II to PE → autophagosome elongates → STX17/SNAP29/VAMP8 fuse with lysosome → cargo degraded → autophagic flux"',
              '<cascade 3: 6+ named steps, DIFFERENT pathway C → different outcome C. e.g. "ATXN3 binds PNKP → PNKP processes 3\\'-phosphate ends at SSBs → XRCC1 scaffold recruits POLB and LIG3 → gap-filling synthesis seals nick → ATM-CHK2 axis attenuated → γH2AX foci resolved → genomic stability maintained → neuronal survival"',
              '... (3-5 cascades total — each must be a DIFFERENT biological pathway, not paraphrases of the same one)'
          ],

          'specific_effects': [
              '<Finding 1: technique + model system + measurable result>',
              '<Finding 2: technique + model system + measurable result>',
              '... (3+ entries, include as many as evidence supports)'
          ],

          'pmids': [],
          'evidence': [{
              'paper_title': '<FULL title from literature>',
              'relevant_quote': '<1-2 sentence paraphrase of key finding from RESULTS, NOT verbatim>',
              'year': <int>,
              'assay': '<primary experimental technique(s) used>',
              'species': '<organism and cell line/tissue>',
              'key_finding': '<one-sentence summary of what this paper shows for this function>'
          }],
          'mechanism_id': '<link to mechanism_details>',
          'note': '<optional clarification>',
          'normal_role': '<optional canonical function context>',
          'pathway': '<biological pathway context, e.g. "ERAD", "mTOR signaling">'
      }, ...],

      // CHAIN LINK FUNCTIONS (ONLY for indirect interactors — see FUNCTION_CONTEXT_LABELING)
      // Dict keyed by directional '<FROM>-><TO>' pair strings (ASCII arrow
      // '->', uppercase HGNC symbols) for each mediator hop in
      // mediator_chain. Each entry is a list of functions describing that
      // single hop in isolation. Omit entirely for direct interactors.
      // Every per-hop function MUST carry function_context='chain_derived'.
      'chain_link_functions': {
          '<FROM>-><MED>': [{ 'function_context': 'chain_derived', ... }],
          '<MED>-><TO>':   [{ 'function_context': 'chain_derived', ... }]
      },

      // CHAIN WITH ARROWS (ONLY for indirect interactors)
      // Ordered array of per-hop arrow objects, upstream->downstream.
      // Must agree with the arrow each function uses in chain_link_functions.
      'chain_with_arrows': [
          { 'from': '<FROM>', 'to': '<MED>', 'arrow': 'activates' | 'inhibits' | 'binds' | 'regulates' },
          { 'from': '<MED>',  'to': '<TO>',  'arrow': '...' }
      ]
  }, ...],

  // TRACKING FIELDS
  'interactor_history': ['<HGNC>', ...],
  'indirect_interactors': [{
      'name': '<HGNC>',
      'upstream_interactor': '<HGNC>',
      'discovered_in_function': '<function name>',
      'role_in_cascade': '<description>',
      'depth': <int>  // 0=query, 1=direct, 2+=indirect
  }],
  'function_history': {'<PROTEIN>': [<functions>], ...},
  'function_batches': ['<HGNC>', ...],
  'search_history': ['<query 1>', '<query 2>', ...]
}

KEY REMINDERS (NEW PIPELINE STRUCTURE):
- Interactor discovery: Find protein NAMES only (direct/indirect)
- Function discovery: Find mechanisms + COLLECT PAPER TITLES for each function
- Arrow determination: Happens AFTER all functions discovered (Step 2b)
- Paper titles: NO constraints (collect ANY relevant title, even without query name)
- Indirect interactors: Track proteins discovered in cascades during function discovery"""

FUNCTION_NAMING_RULES = """╔═══════════════════════════════════════════════════════════════╗
║  FUNCTION NAMING RULES (ABSOLUTE REQUIREMENT)                ║
╚═══════════════════════════════════════════════════════════════╝

RULE 1: NEVER use "regulation" in function names — the arrow field already
indicates the regulatory relationship. "Apoptosis Regulation" → "Apoptosis".

RULE 2: NEVER use outcome-based verb forms (-tion, -sion, -ment) like
Suppression, Activation, Inhibition, Promotion, Enhancement.
They create confusing double-negatives with arrows.
"Apoptosis Suppression" + inhibits = "inhibits suppression" (confusing!)
"Apoptosis" + inhibits = "inhibits apoptosis" (clear!)

RULE 3: ARROW–NAME ALIGNMENT FOR COMPOUND NAMES. If you use the
"Mechanism & Outcome" compound form, the `arrow` field MUST describe
the OUTCOME clause (the last, downstream, biological one), NOT the
mechanism clause. Rationale: the outcome is what the query protein
*does* to the target; the mechanism is *how*. Example:

  GOOD:
    function = "TBP-DNA Binding & Transcription Initiation"
    arrow   = "activates"        # aligns with "initiates transcription"

  BAD (will emit a data-quality warning):
    function = "TBP-DNA Binding & Transcription Initiation"
    arrow   = "binds"            # only matches the mechanism clause

  If your arrow is 'binds', the function name MUST describe a pure
  physical interaction — "TBP-DNA Binding" with no outcome clause. If
  your arrow is 'activates/inhibits', the name MUST end with a biological
  outcome (pathway activity, downstream event, consequence).

RULE 4: PREFER ATOMIC NAMES WHEN ARROWS WOULD CONFLICT. If a protein
both *binds* and *activates* in a way that makes a compound name
ambiguous, emit TWO separate function entries:
  - one with function="Physical Binding" + arrow="binds"
  - one with function="Downstream Pathway Name" + arrow="activates"
Do this instead of a single compound "Binding & Activation" entry.

GOOD examples:
  - "mTORC1 Kinase Activity & Protein Synthesis & Cell Growth"
  - "Caspase-3 Protease Activity & Apoptotic Cell Death"
  - "Autophagosome-Lysosome Fusion & Protein Degradation"
  - "Apoptosis" (NOT "Apoptosis Regulation" or "Apoptosis Suppression")

BAD examples:
  - "Regulation of Immunometabolism" → too vague
  - "Cell Survival" → which pathway?
  - "Protein Quality Control" → too broad
  - "X Binding & Y Activation" with arrow='binds' → arrow disagrees
    with the outcome clause; pick arrow='activates' or drop the
    outcome clause and use arrow='binds'

FORMAT: "[Molecular Target] [Activity Type] & [Biological Outcome]"
TEST 1: Can you read "inhibits [FUNCTION]" without confusion? If no, simplify.
TEST 2: Does the last clause of the name align with the arrow? If no,
rewrite the name or split it into two entries."""

FUNCTION_CONTEXT_LABELING = """╔═══════════════════════════════════════════════════════════════╗
║  FUNCTION_CONTEXT LABELING (REQUIRED — SCHEMA CONSTRAINT)    ║
╚═══════════════════════════════════════════════════════════════╝

Every function dict MUST include a 'function_context' field naming the
discovery perspective it represents. The database schema enforces this
as an enum — a claim with a missing or unknown function_context is
silently dropped.

Valid values (pick exactly one per function):

  • 'direct'        — pair-specific mechanism. Use when the interactor
                      is interaction_type='direct' and this function
                      describes the binary query↔interactor mechanism
                      backed by physical evidence (Co-IP, Y2H, SPR,
                      pull-down, structural data).

  • 'net'           — net effect through a cascade. Use when the
                      interactor is interaction_type='indirect' and
                      this function describes the OVERALL consequence
                      of the query protein on the target via the
                      whole mediator_chain. One or more 'net' entries
                      live in the interactor's top-level 'functions'
                      list alongside the chain metadata.

  • 'chain_derived' — single-hop function inside a chain. Use ONLY
                      inside 'chain_link_functions' entries. Each
                      per-hop function describes what happens in ONE
                      mediator→mediator step in isolation, independent
                      of the query. Never appears in the top-level
                      'functions' list.

  • 'mixed'         — RESERVED for post-processing. Do NOT emit this
                      label; the pipeline assigns it to interactions
                      that accumulate multiple contexts across runs.

╔═══════════════════════════════════════════════════════════════╗
║  DUAL-TRACK PATTERN FOR INDIRECT INTERACTORS (READ CAREFULLY) ║
╚═══════════════════════════════════════════════════════════════╝

When you discover a chain like ATXN3 -> RHEB -> MTOR, output ONE
interactor entry (MTOR) with interaction_type='indirect' and THREE
chain-related collections filled in:

  1. 'functions': a list of NET-EFFECT functions describing what
     ATXN3 does to MTOR via the full cascade. Each function carries
     function_context='net'. This is the query->target story
     end-to-end. Mention the mediator chain where relevant.

  2. 'chain_link_functions': a dict with per-hop functions, keyed
     by directional '<FROM>-><TO>' pair strings (uppercase HGNC
     symbols, ASCII arrow '->', NOT the unicode '→').

     YOU MUST EMIT ONE KEY PER HOP. A chain of N proteins has
     N-1 hops; ALL of them must appear. Missing hops cause the
     intermediate pair's modal to display an empty placeholder
     instead of real biology.

        Example for a 3-protein chain (2 hops, 2 keys):
          'chain_link_functions': {
            'ATXN3->RHEB': [{...function_context: 'chain_derived'...}],
            'RHEB->MTOR':  [{...function_context: 'chain_derived'...}]
          }

        Example for a 4-protein chain PERK -> EIF2S1 -> ATF4 -> DDIT3
        (3 hops, 3 keys — DO NOT skip the middle or final hop):
          'chain_link_functions': {
            'PERK->EIF2S1': [{...function_context: 'chain_derived'...}],
            'EIF2S1->ATF4': [{...function_context: 'chain_derived'...}],
            'ATF4->DDIT3':  [{...function_context: 'chain_derived'...}]
          }

        Example for a 5-protein chain A -> B -> C -> D -> E
        (4 hops, 4 keys — every adjacent pair MUST be present):
          'chain_link_functions': {
            'A->B': [...], 'B->C': [...], 'C->D': [...], 'D->E': [...]
          }

     Each per-hop entry carries function_context='chain_derived'
     and describes ONE mediator link in isolation (e.g. 'RHEB
     binds TSC2 GAP domain and displaces 14-3-3…'). Per-hop
     functions MUST NOT mention the query protein — they must
     stand alone as binary pair descriptions.

  3. 'chain_with_arrows': an ORDERED array of per-hop arrow
     objects, one per mediator link, in upstream->downstream order.
     Each entry is ``{from, to, arrow}`` with ``arrow`` one of
     ``activates | inhibits | binds | regulates``. Use ``binds`` for
     physical/co-complex formation.

        Example matching the chain above:
          'chain_with_arrows': [
            {'from': 'ATXN3', 'to': 'RHEB', 'arrow': 'binds'},
            {'from': 'RHEB',  'to': 'MTOR', 'arrow': 'activates'}
          ]

     The arrow on each hop MUST agree with the arrow the per-hop
     function uses in chain_link_functions. If you say RHEB
     activates MTOR in chain_link_functions['RHEB->MTOR'], the
     matching chain_with_arrows entry MUST also say activates.

The runner splits your single interactor output into multiple
database rows automatically:
  • one INDIRECT row for the net-effect (function_context='net'),
  • one DIRECT row per mediator->mediator hop, extracted from
    chain_link_functions (function_context='direct' downstream,
    tagged '_inferred_from_chain'),
  • chain_with_arrows is mirrored onto the IndirectChain row so
    the frontend can render typed arrows per segment without
    reconstructing them from function text.

You do NOT need to output the direct mediator rows yourself — the
post-processor extracts them from chain_link_functions. Your job
is to fill ALL THREE collections (net functions + chain_link_functions
+ chain_with_arrows) completely and correctly.

For DIRECT interactors (interaction_type='direct'), omit
chain_link_functions and chain_with_arrows entirely and tag every
top-level function with function_context='direct'.

╔═══════════════════════════════════════════════════════════════╗
║  PER-HOP CLAIM DISCIPLINE (HARD)                              ║
╚═══════════════════════════════════════════════════════════════╝
- Each chain_link_functions['<FROM>-><TO>'] claim describes the
  FROM↔TO pair in isolation. It MUST NOT mention the query protein
  unless the query is one of FROM or TO (i.e. unless the hop is a
  query-touching edge).
- Cascade-level / net-effect biology that mentions the query belongs
  in the parent indirect interactor's top-level 'functions' list
  (function_context='net'), NOT inside chain_link_functions.
- A server-side Locus Router enforces this: it will reroute violating
  chain_link_functions entries to the parent indirect or drop them,
  with structured logging. Rerouting wastes tokens and confuses the
  per-hop modal — emit each claim in its correct slot the first time.

╔═══════════════════════════════════════════════════════════════╗
║  HOP COMPLETENESS (HARD)                                      ║
╚═══════════════════════════════════════════════════════════════╝
- For every indirect interactor with mediator_chain of length M, the
  resulting full_chain has M+2 nodes and M+1 hops.
- chain_link_functions MUST contain exactly M+1 entries — one per hop.
- Skipping middle hops is FORBIDDEN. If a middle hop has no published
  evidence, emit an entry with function_context='chain_derived',
  cellular_process describing the inferred coupling, and evidence=[].
  Do NOT silently omit. The chain audit gate flags missing hops and
  the frontend renders a placeholder card for any gap."""

CONTENT_DEPTH_REQUIREMENTS = """╔═══════════════════════════════════════════════════════════════╗
║  CONTENT DEPTH REQUIREMENTS (MANDATORY FOR ALL FUNCTIONS)    ║
╚═══════════════════════════════════════════════════════════════╝

Every function entry MUST meet these MINIMUM depth requirements.
If a field falls short, you MUST research further before outputting.
There are NO upper limits — include as much detail as evidence supports.

EACH FIELD MUST STATE DISTINCT INFORMATION. Never paraphrase the
same mechanism across fields. cellular_process = HOW it works;
effect_description = WHAT RESULTS; biological_consequence =
DOWNSTREAM CASCADE; specific_effects = EXPERIMENTAL EVIDENCE;
evidence = PAPERS. Going deep is encouraged; restating the same
content under another heading is not.

1. cellular_process — 6+ SENTENCES MINIMUM (no ceiling — more is better)
   Include: binding domains/residues, subcellular compartment, specific PTMs
   (e.g., K48-linked Ub, phospho-Ser65), regulatory conditions/triggers,
   conformational changes, stoichiometry when known, species conservation.
   COUNT YOUR SENTENCES: If < 6, add more molecular detail!

2. effect_description — 6+ SENTENCES MINIMUM (no ceiling — more is better)
   Include: quantitative impact (fold-changes, Kd, half-life), downstream signaling
   consequences, temporal dynamics (rapid/slow, transient/sustained),
   context specificity (cell type, stress condition, disease state).
   COUNT YOUR SENTENCES: If < 6, add quantitative and contextual detail!

3. biological_consequence — 3-5 CASCADES, 6+ NAMED STEPS EACH (HARD MINIMUM 3 — runtime validator
   enforces this and shallow output is auto-redispatched)
   Each cascade: name EVERY intermediate protein/complex, specify molecular events
   (phosphorylation, ubiquitination, translocation), end with physiological outcome.
   Use → arrows between steps. The 3 cascades MUST cover DIFFERENT biological pathways
   (e.g. proteasomal vs autophagic vs DNA-damage), NOT paraphrases of one mechanism.
   If literature only supports 1 cascade with confidence, you still need 2 more — search
   harder for adjacent pathways the protein touches; do not pad with generic "cell
   survival" platitudes. Length cap is 5 — beyond that, merge close cascades.

4. specific_effects — 3+ EXPERIMENTAL FINDINGS (no ceiling — more is better)
   Each MUST cite: experimental technique (Co-IP, SPR, CRISPR KO, ITC),
   model system (HEK293T, primary neurons, Drosophila),
   measurable result (fold-change, Kd, p-value, half-life).

5. evidence — 3+ PAPERS WITH CITATIONS (no ceiling — more is better)
   Each MUST include: paper_title, relevant_quote (paraphrased, NOT verbatim),
   year, assay, species, key_finding.
   Do NOT fabricate citations. Only cite papers you are confident exist.

SELF-CHECK: Count entries per field before output. If below minimum,
research more. Check that no field paraphrases another — distinct
content across all five.

DEPTH ENFORCEMENT: A runtime validator (utils/quality_validator.py)
counts cellular_process sentences and biological_consequence cascades
on every function. Functions with <6 sentences or <3 cascades are
flagged with `_depth_issues` and the runner re-dispatches that
interactor through step2a with a focused expand directive that names
the failing rules. Saving tokens by emitting 1-2 cascades does NOT
work — the redispatch will fire and the second pass costs more total
than just hitting 3+ on the first pass. Hit the depth on round 1."""

INTERACTOR_TYPES = """╔═══════════════════════════════════════════════════════════════╗
║  INTERACTOR CLASSIFICATION (DIRECT vs INDIRECT)              ║
╚═══════════════════════════════════════════════════════════════╝

⚠️  CRITICAL: Classification is SET in Phase 1 and PRESERVED throughout pipeline!
You MUST classify EACH interactor based on EVIDENCE TYPE (not just description).

═══════════════════════════════════════════════════════════════════
DIRECT INTERACTORS (Physical/Molecular Binding)
═══════════════════════════════════════════════════════════════════

**Evidence types that indicate DIRECT interaction:**
✓ Co-immunoprecipitation (Co-IP)
✓ Pull-down assays (GST pull-down, FLAG-tag, etc.)
✓ Yeast two-hybrid (Y2H)
✓ BioID proximity labeling
✓ Förster resonance energy transfer (FRET)
✓ Surface plasmon resonance (SPR)
✓ Isothermal titration calorimetry (ITC)
✓ Crosslinking mass spectrometry (XL-MS)
✓ Structural data (X-ray crystallography, cryo-EM showing complex)
✓ Fluorescence polarization assays
✓ Native mass spectrometry
✓ Papers saying "binds to", "forms complex with", "interacts directly"

**How to classify:**
- If literature mentions ANY of the above assays → interaction_type: "direct"
- If papers explicitly say "direct interaction" → interaction_type: "direct"
- If structural data shows them in same complex → interaction_type: "direct"

═══════════════════════════════════════════════════════════════════
INDIRECT INTERACTORS (Functional/Pathway Relationship)
═══════════════════════════════════════════════════════════════════

**Evidence types that indicate INDIRECT interaction:**
✓ Genetic epistasis (double mutant analysis)
✓ Pathway analysis without binding evidence
✓ Multi-step cascades (A → B → C means A and C are indirect)
✓ Transcriptional regulation (unless TF-DNA binding shown directly)
✓ Functional assays (phosphorylation, ubiquitination) without binding evidence
✓ Papers saying "regulates", "modulates", "affects" WITHOUT binding data
✓ Downstream in signaling pathway
✓ Identified through RNA-seq, proteomics, but no binding shown

**How to classify:**
- If protein appears in CASCADE description → interaction_type: "indirect"
  - Example: "VCP activates mTOR which phosphorylates S6K"
  - S6K is INDIRECT (through mTOR)
- If only functional relationship shown → interaction_type: "indirect"
- If papers say "indirectly regulates" → interaction_type: "indirect"

**For INDIRECT interactors, you MUST set:**
- interaction_type: "indirect"
- upstream_interactor: "<protein that mediates the connection>" OR null

**Two types of indirect interactors:**

1. **Multi-hop indirect** (mediator known):
   - Example: VCP → mTOR → S6K means S6K has upstream_interactor: "mTOR"
   - Set: interaction_type="indirect", upstream_interactor="mTOR"

2. **First-ring indirect** (mediator unknown):
   - Example: VCP functionally regulates TFEB but direct mediator not elucidated
   - Evidence: Only functional assays ("VCP regulates TFEB nuclear translocation")
   - Set: interaction_type="indirect", upstream_interactor=null
   - This indicates indirect relationship exists but pathway is incomplete

═══════════════════════════════════════════════════════════════════
DECISION TREE FOR CLASSIFICATION
═══════════════════════════════════════════════════════════════════

⚠️ CRITICAL RULE: ANY direct binding assay evidence ALWAYS means interaction_type = "direct",
even if the protein also participates in pathways or cascades. Direct evidence OVERRIDES all
other considerations. A protein can be both a direct interactor AND part of a pathway.

For EACH interactor, ask:

1. **Does literature mention Co-IP, Y2H, BioID, SPR, pull-down, FRET, crosslinking, or other binding assay?**
   → YES: interaction_type = "direct" (STOP — do NOT override this with "indirect")
   → NO: Continue to question 2

2. **Does literature explicitly say "binds to", "forms complex", "physically interacts", or "direct interaction"?**
   → YES: interaction_type = "direct" (STOP — do NOT override this with "indirect")
   → NO: Continue to question 3

3. **Is protein mentioned in multi-step cascade (A→B→C) with NO direct binding evidence?**
   → YES: interaction_type = "indirect", set upstream_interactor to mediator
   → NO: Continue to question 4

4. **Does literature only show functional relationship ("regulates", "affects") with NO binding data?**
   → YES: interaction_type = "indirect", upstream_interactor = null (first-ring indirect)
   → NO: Default to "direct" if clearly connected, omit if uncertain

═══════════════════════════════════════════════════════════════════
EXAMPLES
═══════════════════════════════════════════════════════════════════

Example 1: VCP and NPLOC4
Literature: "Co-IP shows VCP forms a complex with NPLOC4 and UFD1L"
→ Classification: interaction_type = "direct" (Co-IP evidence)

Example 2: VCP and S6K
Literature: "VCP activates mTORC1, which phosphorylates S6K"
→ Classification: interaction_type = "indirect", upstream_interactor = "mTORC1"
→ Reasoning: S6K is downstream in cascade, no direct binding evidence

Example 3: VCP and LAMP2
Literature: "VCP regulates autophagosome-lysosome fusion through LC3"
→ Classification: interaction_type = "indirect", upstream_interactor = "LC3"
→ Reasoning: LAMP2 affected through LC3, no VCP-LAMP2 binding shown

Example 4: p53 and MDM2
Literature: "Y2H screen identified MDM2 as p53-binding partner"
→ Classification: interaction_type = "direct" (Y2H evidence)

Example 5: VCP and TFEB (FIRST-RING INDIRECT)
Literature: "VCP regulates TFEB nuclear translocation in response to starvation"
→ Classification: interaction_type = "indirect", upstream_interactor = null
→ Reasoning: Only functional relationship shown ("regulates"), no binding evidence
→ Mediator unknown: VCP affects TFEB but direct mediator not elucidated in literature
→ This is first-ring indirect: indirect by nature, but no mediator specified

═══════════════════════════════════════════════════════════════════
VALIDATION CHECKLIST
═══════════════════════════════════════════════════════════════════

Before outputting, verify:
✓ EVERY interactor has interaction_type: "direct" or "indirect"
✓ INDIRECT interactors have upstream_interactor set to mediator OR null if unknown
✓ Classification matches evidence type (not just paper phrasing)
✓ Cascade proteins are marked indirect with proper upstream
"""


# ----------------------------------------------------------------
# System prompt composition helpers
# ----------------------------------------------------------------

FUNCTION_HISTORY_HEADER = (
    "EXISTING FUNCTIONS PER PROTEIN (DO NOT REGENERATE — create only NEW, DISTINCT mechanisms):\n"
    "{ctx_json.function_history}\n"
    "\nFor each protein listed above, its existing function names are shown. "
    "Do NOT generate functions with the same or overlapping mechanism. "
    "Two functions covering the same biological pathway count as duplicates "
    "even if named differently.\n"
)


def get_system_prompt_text() -> str:
    """Build the default system-level instruction text."""
    return STRICT_GUARDRAILS + "\n\n" + DIFFERENTIAL_OUTPUT_RULES


# ----------------------------------------------------------------
# Discovery opening (shared across discovery / iterative / deep research)
# ----------------------------------------------------------------
# The single place every discovery prompt pulls its "go search freely, skip
# what's already known" framing from. Use ``{user_query}`` and
# ``{exclusion_block}`` as placeholders; callers substitute at render time.

DISCOVERY_OPENING = """TASK: Find protein interactors of {user_query}.

{exclusion_block}

SEARCH APPROACH — GO FREE:
You have Google Search. Pick your own queries, your own databases, your own
angles. You know the literature better than any rigid template — do not
follow a scripted list. Skip anything already in the exclusion list above
and keep searching until the literature itself runs out of new partners
worth reporting.
"""


# ----------------------------------------------------------------
# Batch directive templates
# ----------------------------------------------------------------
# Previously duplicated verbatim across several ``_run_parallel_batched_phase``
# call sites in runner.py — extracted here so the DEPTH REQUIREMENT and
# UNIQUENESS rules have a single source of truth. Callers pick a variant
# label ("interactors", "NEWLY DISCOVERED interactors", "CHAIN-PROMOTED
# interactors", etc.) via ``make_batch_directive``; the rest of the template
# is shared.

BATCH_DIRECTIVE_DEPTH_UNIQUENESS = (
    "DEPTH: see CONTENT_DEPTH_REQUIREMENTS in the system prompt. "
    "Apply the same minimums to every function you emit.\n\n"
    "UNIQUENESS: each function must describe a distinct biological "
    "mechanism. Before emitting, check existing functions for this "
    "interactor in ctx_json — do not restate a mechanism already covered."
)


def make_batch_directive(variant_label: str = "interactors") -> str:
    """Build a batch-assignment directive template for a parallel batch phase.

    ``variant_label`` is interpolated into the first line to distinguish
    different batch contexts in logs and prompts. Callers then run
    ``directive.format(count=N, batch_names=", ".join(names))`` to fill the
    per-batch values.
    """
    return (
        "BATCH — process these {count} "
        f"{variant_label}:\n"
        "{batch_names}\n"
        "Emit results for these interactors only. You may reference others "
        "from ctx_json as context. Add every name you processed to "
        "function_batches.\n\n"
        + BATCH_DIRECTIVE_DEPTH_UNIQUENESS
    )


def make_depth_expand_batch_directive() -> str:
    """Batch directive that targets `_depth_issues`-tagged functions.

    Template-compatible with ``_run_parallel_batched_phase`` (uses
    ``{count}`` and ``{batch_names}`` placeholders). The directive points
    the model at the inline ``_depth_issues`` tag that
    ``utils.quality_validator.validate_payload_depth`` already stamped on
    every shallow function — so the model sees the exact failing rule per
    function in ctx_json, not a generic "expand more" exhortation. This is
    why hooking the strict validator into ``_tag_shallow_functions`` is
    paired with this directive: validator tags rules → directive references
    rules → model targets exactly the failing fields.
    """
    return (
        "BATCH — DEPTH-EXPAND these {count} interactor(s):\n"
        "{batch_names}\n\n"
        "For EACH interactor, walk its existing functions[] in ctx_json. "
        "Find every function entry that has `_depth_issues` set (a list of "
        "rule names). The rules and their fixes are:\n"
        "  • 'min_sentences' → cellular_process needs 6-10 sentences with "
        "    molecular detail (domains, residues, PTMs, compartment, "
        "    triggers, kinetics, conformational changes).\n"
        "  • 'min_cascades' → biological_consequence needs 3-5 distinct "
        "    cascades, each describing a DIFFERENT pathway with 6+ named "
        "    molecular steps. NOT paraphrases of the same cascade.\n"
        "  • 'max_sentences' → trim cellular_process to <=10 sentences "
        "    while preserving molecular specificity.\n"
        "  • 'max_cascades' → merge biological_consequence to <=5 cascades.\n\n"
        "Re-emit ONLY the flagged functions with the fixes named — do NOT "
        "re-emit functions that have no `_depth_issues` tag. The runner "
        "will merge your output into the existing payload. Add every "
        "interactor name you processed to function_batches.\n\n"
        + BATCH_DIRECTIVE_DEPTH_UNIQUENESS
    )


