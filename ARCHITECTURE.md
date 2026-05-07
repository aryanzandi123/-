# ProPaths Architecture

Protein interaction analysis platform powered by Google Gemini AI.
Discovers protein-protein interactions from scientific literature,
validates evidence, and renders interactive D3.js force-directed graphs.

---

## System Overview

```
User Query (protein name)
    |
    v
Flask API  -->  Research Pipeline (Gemini LLM, 4 phases)
    |                   |
    v                   v
PostgreSQL <---  Validation & Enrichment
    |
    v
D3.js Force Graph  +  Card View  +  Chat Interface
```

ProPaths accepts a protein symbol (e.g. "ATXN3"), launches a background
research pipeline that uses Gemini to discover interacting proteins from
literature, validates the findings, stores them in PostgreSQL, and serves
an interactive visualization.

---

## Data Flow

1. **Query** -- User submits a protein name via the landing page.
2. **Pipeline** -- Background thread runs multi-round LLM discovery
   (interactors, functions, evidence, biological cascades).
3. **Validation** -- PostProcessor chain enriches and validates output
   (evidence quotes, PMID checks, schema fixes, deduplication).
4. **DB Sync** -- Results are written to PostgreSQL with canonical ordering
   and cached to the filesystem as a fallback.
5. **Visualization** -- Frontend fetches the snapshot JSON and renders a
   D3.js force graph with modals for evidence, functions, and cascades.

---

## Application Structure

ProPaths uses a **Flask shell** pattern: `app.py` creates the app, configures
the database, and registers blueprints. Business logic lives in `services/`
and `utils/`.

```
app.py              Thin shell: app factory, DB init, blueprint registration
models.py           SQLAlchemy ORM (5 models)
runner.py           Pipeline engine and LLM orchestration
visualizer.py       HTML template generator with embedded D3.js
routes/             Blueprint layer (6 blueprints)
services/           Business logic layer (6 modules)
utils/              Processing, validation, and runtime helpers
pipeline/           Pipeline configuration and step types
static/             Frontend JS and CSS
templates/          Jinja2 HTML templates
```

---

## Route Layer

Six blueprints registered in `routes/__init__.py`:

### query_bp -- Query lifecycle

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/search/<protein>` | Check if protein exists in DB |
| POST | `/api/query` | Start research pipeline |
| GET | `/api/status/<protein>` | Poll job progress |
| POST | `/api/cancel/<protein>` | Cancel a running job |

### results_bp -- Data retrieval

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/results/<protein>` | Fetch full interaction JSON |
| GET | `/api/pathway/<id>/interactors` | Get pathway interactors |
| GET | `/api/databased-interactors/<protein>` | List DB-stored interactors |
| POST | `/api/expand/pruned` | Start expansion (pruned subgraph) |
| GET | `/api/expand/status/<job_id>` | Poll expansion job status |
| GET | `/api/expand/results/<job_id>` | Fetch expansion results |

### chat_bp -- LLM chat

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/chat` | Send message, get LLM response |

### pipeline_bp -- Pathway pipeline

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/queries` | List queried proteins |
| POST | `/api/pipeline/run` | Trigger pathway pipeline |
| POST | `/api/pipeline/clear` | Clear pipeline state |
| GET | `/api/pipeline/status` | Pipeline run status |
| POST | `/api/repair-pathways/<protein>` | Repair pathway data |

### viz_bp -- Visualization

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Landing page |
| GET | `/api/visualize/<protein>` | Serve HTML visualization |

### health_bp -- Observability

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness probe (DB, cache, jobs) |
| GET | `/metrics` | Operational metrics (JSON or Prometheus) |

---

## Service Layer

| Module | Responsibility |
|--------|---------------|
| `services/state.py` | Shared state: `jobs` dict, `jobs_lock`, cache paths, pipeline status |
| `services/data_builder.py` | `build_full_json_from_db`, `build_expansion_json_from_db` |
| `services/chat_service.py` | Chat context building, system prompt, LLM call |
| `services/query_service.py` | Query orchestration helpers |
| `services/metrics.py` | Metrics collection for `/metrics` endpoint |
| `services/error_helpers.py` | Standardized error responses with error codes |

---

## Research Pipeline (4 Phases)

Orchestrated by `runner.py:run_full_job()` in a background thread.

### Phase 1: Interactor Discovery (3-8 rounds)

Gemini + Google Search discovers protein names that interact with the query
protein. Each round builds on previous results, classifying interactions as
direct (physical) or indirect (cascade chains).

### Phase 2: Function Discovery (3-8 rounds)

Gemini identifies molecular mechanisms, biological functions, evidence
citations (PMIDs), and builds biological consequence chains for each
discovered interaction.

### Phase 3: Enrichment and Validation

The PostProcessor chain runs 9 stages (see below) to enrich and validate
pipeline output before database sync.

### Phase 4: Database Sync

`utils/db_sync.py:sync_query_results()` writes validated results to PostgreSQL
with canonical ordering and transaction safety, then caches to the filesystem.

---

## PostProcessor Stage Chain

Nine stages run in sequence after the discovery phases. Each stage is
classified by its execution type:

| # | Stage | Type | Module |
|---|-------|------|--------|
| 1 | Evidence validation | LLM | `utils/evidence_validator.py` |
| 2 | Claim fact-checking | LLM | `utils/claim_fact_checker.py` |
| 3 | Schema validation | PURE | `utils/schema_validator.py` |
| 4 | Function deduplication | PURE | `utils/deduplicate_functions.py` |
| 5 | Function name cleaning | PURE | `utils/clean_function_names.py` |
| 6 | Metadata generation | LLM | `utils/interaction_metadata_generator.py` |
| 7 | PMID update | EXTERNAL_API | `utils/update_cache_pmids.py` |
| 8 | Arrow aggregation | PURE | `runner.py:aggregate_function_arrows()` |
| 9 | Database sync | EXTERNAL_API | `utils/db_sync.py` |

Stage types: **PURE** (deterministic transform), **LLM** (Gemini call),
**EXTERNAL_API** (database or PubMed API).

---

## Database Schema

Five SQLAlchemy models in `models.py`:

### Protein

Core entity with query tracking.

| Column | Type | Notes |
|--------|------|-------|
| `id` | Integer PK | Auto-increment |
| `symbol` | String(50) | Unique, indexed (e.g. "ATXN3") |
| `query_count` | Integer | Incremented per query |
| `total_interactions` | Integer | Updated after sync |
| `extra_data` | JSONB | Flexible metadata |

### Interaction

Protein-protein relationship with full JSONB payload.

| Column | Type | Notes |
|--------|------|-------|
| `id` | Integer PK | Auto-increment |
| `protein_a_id` | FK -> proteins | Always < `protein_b_id` (canonical) |
| `protein_b_id` | FK -> proteins | Canonical ordering prevents duplicates |
| `confidence` | Numeric(3,2) | 0.00 - 1.00 |
| `direction` | String(20) | `bidirectional`, `a_to_b`, `b_to_a` |
| `arrow` | String(50) | Primary: `binds`, `activates`, `inhibits`, `regulates` |
| `arrows` | JSONB | Multi-arrow per direction |
| `interaction_type` | String(20) | `direct` or `indirect` |
| `depth` | Integer | 1=direct, 2+=indirect |
| `data` | JSONB | Full pipeline payload (evidence, functions, PMIDs) |
| `function_context` | String(20) | `direct`, `net`, or null |

**Unique constraint**: `(protein_a_id, protein_b_id)`.
**Check constraint**: `protein_a_id != protein_b_id`.

### Pathway

Biological pathway for grouping interactions (KEGG/Reactome/GO mapped).

| Column | Type | Notes |
|--------|------|-------|
| `id` | Integer PK | Auto-increment |
| `name` | String(200) | Unique pathway name |
| `ontology_id` | String(50) | e.g. "GO:0006914", "hsa04140" |
| `ontology_source` | String(20) | `KEGG`, `Reactome`, `GO` |
| `hierarchy_level` | Integer | 0=root, higher=deeper |
| `is_leaf` | Boolean | True if no child pathways |

### PathwayInteraction

Many-to-many join between pathways and interactions.

| Column | Type | Notes |
|--------|------|-------|
| `pathway_id` | FK -> pathways | |
| `interaction_id` | FK -> interactions | |
| `assignment_confidence` | Numeric(3,2) | 0.00 - 1.00 |
| `assignment_method` | String(50) | `ai_pipeline`, `manual`, `ontology_match` |

### PathwayParent

DAG hierarchy linking child pathways to parent pathways.

| Column | Type | Notes |
|--------|------|-------|
| `child_pathway_id` | FK -> pathways | |
| `parent_pathway_id` | FK -> pathways | |
| `relationship_type` | String(30) | `is_a`, `part_of`, `regulates` |
| `confidence` | Numeric(3,2) | 1.0 for ontology, <1.0 for AI-inferred |

---

## Key Design Decisions

### Canonical Ordering

Interactions enforce `protein_a_id < protein_b_id` to prevent storing the
same pair twice. Direction fields are converted between canonical form and
query-relative perspective during reads and writes.

### JSONB Data Column

Each interaction stores its full pipeline payload (evidence, functions,
PMIDs, biological consequences) in a single `data` JSONB column. Frequently
queried fields (`confidence`, `direction`, `arrow`) are denormalized into
dedicated columns for fast filtering.

### Dual-Track function_context

Indirect chains create two interaction records:
1. **Net effect** (`function_context='net'`): The end-to-end chain effect
   (e.g. ATXN3 -> MTOR via RHEB).
2. **Direct link** (`function_context='direct'`): The extracted mediator
   pair (e.g. RHEB -> MTOR), flagged with `_inferred_from_chain=True`.

---

## Frontend Architecture

### D3.js v7 Force Graph

The primary visualization is a force-directed graph rendered in SVG:

- **Nodes**: Circles sized by role (query protein larger, interactors smaller).
- **Links**: Arrows styled by type -- solid (direct), dashed (indirect),
  dotted (shared interactor-interactor links).
- **Arrow markers**: Color-coded by interaction arrow type (activates, inhibits,
  binds, regulates).
- **Forces**: charge repulsion, collision avoidance, link springs, center pinning.

### Views

- **Graph view** (`static/visualizer.js`): Interactive force graph with
  expand/collapse, zoom/pan, modals for evidence and functions.
- **Card view**: Sortable, filterable table of all interactions.
- **Chat interface**: LLM-powered Q&A about the displayed protein network.

### Multi-Job Tracking

- `JobTracker` class (`static/script.js`): Full job cards on the landing page.
- `VizJobTracker` class (`static/visualizer.js`): Compact chips on the viz page.
- Jobs persist across navigation via `sessionStorage`.
- Independent polling per job (5s interval, 3 retries with exponential backoff).

---

## Gemini Runtime

`utils/gemini_runtime.py` centralizes all Gemini SDK interaction:

- **Singleton client**: Thread-safe, cached by API key.
- **Model registry**: Role-based model selection with env-var overrides
  (`GEMINI_MODEL_CORE`, `GEMINI_MODEL_EVIDENCE`, etc.).
- **Config builders**: Separate builders for `models.generate_content`
  (nested thinking config) and `interactions.create` (top-level config).
- **Tool builders**: Auto-detect when to enable `google_search` and
  `url_context` based on prompt content.
- **Batch mode**: Optional async execution via `GEMINI_REQUEST_MODE=batch`.

See [API_MIGRATION.md](API_MIGRATION.md) for full SDK migration details.

---

## Observability

### GET /health

Liveness probe returning JSON with status checks:
- Database connectivity (SELECT 1)
- Cache directory writable
- Active job count

### GET /metrics

Operational metrics in JSON or Prometheus text format:
- Uptime, request counts, active jobs
- Database row counts (proteins, interactions)
- Cache file counts

### Structured Logging

`logging.basicConfig` with `[%(levelname)s] %(name)s:` format to stderr.
Pipeline steps log via `utils/step_logger.py` with per-protein log directories
under `Logs/`.
