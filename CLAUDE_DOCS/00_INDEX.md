# CLAUDE_DOCS — Handoff Package for ProPaths

**Audience:** A fresh Claude session that needs to pick up where the prior one left off, with the same depth, biological literacy, investigative tempo, and respect for the user's working agreements.

**Authoring session:** 2026-05-03 (Aryan + Opus 4.7 1M).

**Project root:** `/Users/aryan/Desktop/DADA/untitled folder 2 copy 54/`

---

## How to use these docs

If you're a fresh Claude:

1. **Start here**, then read `01_USER_PROFILE.md` and `12_WORKING_AGREEMENTS.md` BEFORE you touch any code or even respond to the user. These two are the difference between matching the user's expectations and frustrating them.
2. Then read `10_OPEN_ISSUES.md` — that's THE list of what's currently pending. **A1 is DONE as of 2026-05-03**. The active worklist now is **Priority B — the chain DAG-vs-tree rewrite (HDAC6 / STUB1 cases)** plus the broader frontend + databasing perfection plan in `/Users/aryan/.claude/plans/activate-project-users-aryan-desktop-dad-wise-lamport.md`.
3. Then skim `09_FIXES_HISTORY.md` so you understand what was already fixed in the prior session and don't re-do it or accidentally undo it.
4. The architecture/pipeline/db/frontend docs (03–06) are reference material — read the relevant section when you're about to touch that area, not all at once.
5. Use `08_DIAGNOSTIC_PATTERNS.md` when reading a pipeline log the user pastes — it tells you what every log marker means.
6. `13_RUN_HISTORY.md` is the timeline of pipeline runs the user has done; it explains what symptoms each run revealed and what fix dropped between runs.

If you only have time for two files: read **`01_USER_PROFILE.md`** and **`10_OPEN_ISSUES.md`**.

---

## Reading order for a complete catch-up

| # | File | What it covers | When to read |
|---|------|----------------|--------------|
| 00 | `00_INDEX.md` | This file | First |
| 01 | `01_USER_PROFILE.md` | Aryan: bio expertise, communication style, what he values, anti-patterns | Always read first |
| 02 | `02_PROJECT_VISION.md` | What ProPaths is, who it's for, the biological mission | Always read second |
| 12 | `12_WORKING_AGREEMENTS.md` | The rules: no git, root-cause-only, save direct, no defensive coding | Always read third |
| 10 | `10_OPEN_ISSUES.md` | THE pending-work list — what to do next | Read fourth (your worklist) |
| 09 | `09_FIXES_HISTORY.md` | What was done in the prior session | Read fifth (don't repeat work) |
| 11 | `11_CHAIN_TOPOLOGY.md` | The deep technical/biological problem the user cares about most | Read before touching card view |
| 13 | `13_RUN_HISTORY.md` | Pipeline run timeline + what each revealed | Read when interpreting a log |
| 03 | `03_ARCHITECTURE.md` | File-by-file project map | Read when navigating code |
| 04 | `04_PIPELINE_FLOW.md` | End-to-end pipeline stage map | Read before touching pipeline |
| 05 | `05_DATABASE_SCHEMA.md` | All tables, constraints, invariants | Read before touching models |
| 06 | `06_FRONTEND_DEEPDIVE.md` | Card view, modal, visualizer | Read before touching frontend |
| 07 | `07_VERTEX_AI_REFERENCE.md` | Gemini 3 model specs, real limits | Read before LLM-config changes |
| 08 | `08_DIAGNOSTIC_PATTERNS.md` | Log marker reference | Read when interpreting stderr |
| 14 | `14_HANDOFF_INSTRUCTIONS.md` | What to say in the first message of a new session | Last — for context handoff |

---

## High-level state at handoff

- **Backend root-cause fixes from prior session: ALL LANDED.** 6 backend fixes + Vertex AI tuning + frontend diagnostic enhancements. All 676 tests pass.
- **A1 fix landed 2026-05-03.** `.env CHAIN_CLAIM_MAX_OUTPUT_TOKENS=24000` (was 8192/10000). Companion bumps in `arrow_effect_validator.py` (12336/24000) and `modern_steps.py:418` (24000). Real Flash 3 cap is 65,536 per Vertex docs. 24000 is the chosen middle ground — bombproof for any realistic output, low enough to bound thinking-budget overhead. 676 tests passing; awaiting next pipeline run for empirical confirmation.
- **One major frontend re-architecture pending:** card view treats chains as linear trees but biology is a DAG. The user's HDAC6 / STUB1 examples make this concrete. See `11_CHAIN_TOPOLOGY.md` for the full design space.
- **The user has explicitly chosen Flash for everything.** Do NOT propose Pro 3 as a fix unless the only alternative is impossible. Pro is reserved for cost reasons; Flash 3 is fully capable when configured correctly.
- **The user does not use git.** Edits go directly to files via Edit/Write. Save immediately. No commits. No branches. No PRs. Read `12_WORKING_AGREEMENTS.md` for full rules.

---

## Directory layout reminder

```
/Users/aryan/Desktop/DADA/untitled folder 2 copy 54/
├── runner.py                 (9007 lines) — pipeline orchestrator
├── app.py                    (235 lines)  — Flask app factory
├── models.py                 (905 lines)  — SQLAlchemy ORM
├── visualizer.py             (265 lines)  — HTML rendering for card view
├── schema.sql, schema_after.sql           — raw schema dumps
├── .env                                   — runtime configuration (READ before changing)
├── CLAUDE.md                              — top-level project conventions
├── ARCHITECTURE.md                        — top-level architecture (older)
├── pipeline/                              — config + prompts + types
├── utils/                  (~30 files)    — post-processing
├── routes/                 (7 files)      — Flask blueprints
├── services/               (6 files)      — backend services (data_builder, state, chat)
├── scripts/                               — pathway pipeline + migrations + audits
├── static/                                — D3 + card view + modal + visualizer JS/CSS
├── templates/                             — Jinja2 (index.html, visualize.html)
├── react-app/                             — React islands (cardview-badges, pipeline-events)
├── tests/                                 — pytest (676 tests, all passing)
├── migrations/                            — alembic
├── Logs/                                  — per-protein run diagnostics
├── cache/                                 — file cache (file-cache fallback)
└── CLAUDE_DOCS/                           — THIS handoff package
```
