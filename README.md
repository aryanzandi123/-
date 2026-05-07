# ProPaths

Bioinformatics web application that uses Google Gemini AI to research
protein-protein interactions from scientific literature, validate findings
with evidence citations, and visualize interaction networks as interactive
D3.js force-directed graphs.

---

## Features

- Multi-round LLM research pipeline discovering protein interactors from literature
- Evidence validation with PMID verification and paper quote extraction
- Direct and indirect (cascade chain) interaction classification
- Interactive D3.js v7 force-directed graph visualization
- Sortable card/table view with filtering and CSV export
- LLM-powered chat interface for Q&A about displayed networks
- Node expansion to explore subgraphs of interactor proteins
- Multi-job tracking with cross-page persistence
- Pathway grouping with KEGG/Reactome/GO ontology mapping
- Health and metrics endpoints for production observability

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.10+, Flask, SQLAlchemy |
| Database | PostgreSQL (Railway) with JSONB storage |
| AI/LLM | Google Gemini 3 Pro (`google-genai` SDK) |
| Frontend | D3.js v7, vanilla JavaScript, HTML/CSS |
| Deployment | Railway (gunicorn + gthread) |

---

## Quick Start

### Prerequisites

- Python 3.10+
- PostgreSQL database (local or Railway)
- Google Gemini API key

### Setup

```bash
# Clone and enter the project
git clone <repo-url>
cd propaths

# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your values (see Environment Variables below)

# Start the server
python app.py
# Server runs at http://127.0.0.1:5007
```

Database tables are created automatically on first startup.

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GOOGLE_API_KEY` | Yes | Google Gemini API key |
| `DATABASE_URL` | Yes* | PostgreSQL connection string (Railway sets automatically) |
| `DATABASE_PUBLIC_URL` | No | External DB URL for local dev against Railway |
| `GEMINI_MODEL_CORE` | No | Override core model (default: `gemini-3.1-pro-preview`) |
| `GEMINI_MODEL_EVIDENCE` | No | Override evidence model (default: `gemini-2.5-pro`) |
| `GEMINI_MODEL_FLASH` | No | Override flash model (default: `gemini-3-flash-preview`) |
| `GEMINI_REQUEST_MODE` | No | `standard` (default) or `batch` |
| `GEMINI_BATCH_POLL_SECONDS` | No | Batch polling interval (default: `15`) |
| `GEMINI_BATCH_MAX_WAIT_SECONDS` | No | Batch max wait (default: `86400`) |
| `ENABLE_STEP_LOGGING` | No | Enable detailed pipeline logging |
| `VALIDATION_MAX_WORKERS` | No | Parallel validation workers (default: `2`) |

*Falls back to SQLite if unset (local development only).

---

## Project Structure

```
app.py                  Flask shell: app factory, DB init, blueprints
models.py               SQLAlchemy models (5 tables)
runner.py               Pipeline engine and LLM orchestration
visualizer.py           HTML template generator with embedded D3.js

routes/                 Blueprint layer (6 blueprints)
  query.py              /api/search, /api/query, /api/status, /api/cancel
  results.py            /api/results, /api/expand/*, /api/pathway/*
  chat.py               /api/chat
  pipeline.py           /api/pipeline/*, /api/queries
  visualization.py      /, /api/visualize
  health.py             /health, /metrics

services/               Business logic
  data_builder.py       JSON builders for DB results
  chat_service.py       Chat context and LLM calls
  query_service.py      Query orchestration
  state.py              Shared state (jobs, locks, paths)
  metrics.py            Metrics collection
  error_helpers.py      Standardized error responses

utils/                  Processing and validation
  gemini_runtime.py     Centralized Gemini SDK interface
  db_sync.py            PostgreSQL sync with canonical ordering
  evidence_validator.py Evidence enrichment and PMID validation
  schema_validator.py   Structural validation and fixes
  pruner.py             Subgraph selection for node expansion

pipeline/               Pipeline configuration
  config_gemini_MAXIMIZED.py   Base config (7+6 rounds)
  config_dynamic.py            Dynamic config generator

static/                 Frontend assets
  visualizer.js         D3.js graph rendering (6800+ LOC)
  script.js             Landing page logic and JobTracker
  styles.css            Shared styles
  viz-styles.css        Visualization styles

templates/              Jinja2 templates
  index.html            Landing page
```

---

## Testing

```bash
pip install pytest
pytest tests/ -v
```

---

## Deployment

ProPaths deploys on [Railway](https://railway.app) using the included Procfile:

```
web: gunicorn --bind 0.0.0.0:$PORT --worker-class gthread --threads 10 app:app
```

Railway automatically sets `DATABASE_URL` for the internal PostgreSQL connection.
Push to the main branch triggers auto-deploy.

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Landing page |
| POST | `/api/query` | Start research pipeline for a protein |
| GET | `/api/search/<protein>` | Check if protein exists in DB |
| GET | `/api/status/<protein>` | Poll job progress |
| POST | `/api/cancel/<protein>` | Cancel a running job |
| GET | `/api/results/<protein>` | Fetch full interaction JSON |
| GET | `/api/visualize/<protein>` | Serve HTML visualization |
| POST | `/api/expand/pruned` | Start node expansion |
| GET | `/api/expand/status/<job_id>` | Poll expansion status |
| GET | `/api/expand/results/<job_id>` | Fetch expansion results |
| POST | `/api/chat` | LLM chat about protein network |
| GET | `/api/queries` | List all queried proteins |
| GET | `/health` | Liveness probe |
| GET | `/metrics` | Operational metrics |

---

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) -- System architecture, data flow, database schema
- [API_MIGRATION.md](API_MIGRATION.md) -- Gemini SDK migration guide
- [docs/SYSTEM.md](docs/SYSTEM.md) -- Detailed system documentation with workflows and schemas
