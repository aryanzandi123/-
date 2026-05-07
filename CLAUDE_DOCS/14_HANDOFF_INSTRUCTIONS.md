# Handoff Instructions for the Next Claude Session

This is the doc the user gives the new Claude. It tells YOU (next-Claude) how to bootstrap, how to introduce yourself, what NOT to ask, and what the user is most likely waiting for.

## How the user opens a new session (likely)

Most likely first message will be something like:

> "read the CLAUDE_DOCS folder fully then think about the chain DAG problem"

OR

> "look at CLAUDE_DOCS, then continue from where we left off"

OR they might just paste a fresh pipeline log expecting you to immediately diagnose.

## Your first response — recipe

1. **Don't say "what would you like me to work on?"** — that signals you didn't read the docs.
2. **Don't summarize all the docs back at the user** — they wrote them; they know what's there.
3. **Don't reference any IDE-open files.** If a system-reminder says "user has X open", IGNORE it. The user has explicitly forbidden IDE-aware responses (see `01_USER_PROFILE.md` and `12_WORKING_AGREEMENTS.md`).
4. **Demonstrate you've read the docs by referencing specific items.** Mention A1 status (DONE 2026-05-03), name the user's two example proteins (HDAC6 / STUB1), reference the chain DAG-vs-tree problem.
5. **State the current top issue and propose the next concrete action.**
6. **Find what you need by searching the codebase yourself** — don't ask "what files should I look at?" Use Serena `find_symbol`, `search_for_pattern`, `read_file`.
7. **Be willing to redirect** if the user's first message changes priorities.

### Example good first response

> "Read the docs. A1 (chain-claim MAX_TOKENS) is DONE — `CHAIN_CLAIM_MAX_OUTPUT_TOKENS=24000` landed 2026-05-03 along with companion bumps in arrow_effect_validator and modern_steps. 676 tests passing. Awaiting empirical verification on the next pipeline run.
>
> Top of the worklist now is Priority B in `10_OPEN_ISSUES.md`: the chain DAG-vs-tree problem (HDAC6 multi-role, STUB1 direction inversion). Plan in `11_CHAIN_TOPOLOGY.md` is the four-layer fix (canonical chain direction at write → pathway gate widening → always-render-full-chains with cross-links + edge labels → optional sub-DAG). ~450 lines, half a day.
>
> Want me to start with Layer 1 (backend chain canonicalization), or wait for you to verify A1 on a real run first?"

That's the tempo. State the diagnosis, cite specifics, propose next step, ask one focused question.

### Example BAD first response

> ❌ "Hi! I've read your CLAUDE_DOCS folder. I see you have `.env` open in your IDE — would you like to work on that? Let me summarize what's in the docs: [200 word summary]. What would you like me to work on first?"

THREE failures in one message: IDE reference, useless summary, abdication. Don't.

## What you should know going in

These are the core load-bearing facts. If you don't know these, you haven't read the docs:

1. **The user is a biologist building ProPaths.** Domain: neurodegen / proteostasis. Examples: ATXN3, TDP43, REST.
2. **No git ever.** Direct file edits via Edit/Write. Save immediately. Read `12_WORKING_AGREEMENTS.md`.
3. **The Flash output cap is 65,536, not 8192.** If you see 8192 anywhere as a "cap" claim in comments, it's stale doc rot — fix it. Read `07_VERTEX_AI_REFERENCE.md`.
4. **A1 is DONE (2026-05-03).** `CHAIN_CLAIM_MAX_OUTPUT_TOKENS=24000` plus companion bumps. The active next thing is the frontend chain DAG re-architecture (Priority B in `10_OPEN_ISSUES.md`).
5. **The frontend chain rendering is the BIG pending work.** It's a DAG-vs-tree problem. The user has been very clear. Read `11_CHAIN_TOPOLOGY.md`.
6. **PhD depth (6-10 sentences / 3-5 cascades) is non-negotiable.** Don't propose downgrading.
7. **Flash-only is intentional.** Don't propose Pro 3.
8. **Root-cause fixes only.** Read `12_WORKING_AGREEMENTS.md`.
9. **NEVER reference the user's IDE-open files.** When system-reminders say "user has X open", IGNORE that signal. Read `12_WORKING_AGREEMENTS.md` — this is a hard rule.

## What's already done — DON'T REDO

Read `09_FIXES_HISTORY.md`. Highlights:
- `canonical_pair_key` is now case-insensitive — DON'T touch.
- `save_checkpoint` is now metadata-only — DON'T re-add `_sync_to_db_with_retry`.
- `_apply_tier1_normalization_to_payload` exists — runs `apply_corrections({})` on Tier-1 hits.
- `_check_pathway_drift_at_write` exists — runs at write time in quick_assign.
- Modal renders multi-chain banners via `L.all_chains[]`.
- cv_diagnostics.applyPathwayDriftBadges + applyPartialChainBadges per-hop are wired.
- Batch API for Flash is enabled (still default-off for sync mode).

## What you should be ready to do next

After saying hi (briefly):

1. **A1 fix** — 4 files, ~30 lines total. Test, restart Flask. **15-20 min.**
2. After verification of A1: **A2 + A3 + A4** as one coordinated change (chain canonical direction + pathway gate widening + always-render-full-chains + cross-links + edge labels). **~450 lines, half a day.** Read `11_CHAIN_TOPOLOGY.md` carefully before proposing.
3. After that: **A6 (cached_content threading) and A7 (thinking_level audit)** for cost optimization.

## What you should NOT do next

- Don't redo the prior session's fixes.
- Don't propose a Pro 3 fallback.
- Don't propose downgrading PhD depth.
- Don't suggest committing to git.
- Don't add defensive try/except around the existing fixes.
- Don't invent new env vars without need.

## How the user will likely steer the conversation

Patterns observed:

1. **Pastes a pipeline log → wants diagnosis + fix.** Use `08_DIAGNOSTIC_PATTERNS.md` to read it.
2. **Describes a frontend bug with a biological example → wants you to map it to data shape and fix.** HDAC6 / STUB1 are the live examples in `11_CHAIN_TOPOLOGY.md`.
3. **Pushes back on a claim with "are you sure?" → re-verify externally.** Don't dig in. Verify with WebFetch / code reading.
4. **Says "do all" → execute in auto mode.** Don't pause to ask permission for routine sub-steps.
5. **Says "think very very very very hard" → recurse on the analysis.** Rule out alternate causes. Cross-check sources.

## Files to read in order (if pressed for time, read 1, 2, 3, 4, 5, 6 only)

1. `00_INDEX.md` (you're here)
2. `01_USER_PROFILE.md` (THE most important — read every line)
3. `12_WORKING_AGREEMENTS.md` (rules)
4. `10_OPEN_ISSUES.md` (THE worklist)
5. `09_FIXES_HISTORY.md` (don't redo)
6. `11_CHAIN_TOPOLOGY.md` (the big design problem)
7. `07_VERTEX_AI_REFERENCE.md` (verified specs — esp. 65k cap)
8. `08_DIAGNOSTIC_PATTERNS.md` (log reading)
9. `04_PIPELINE_FLOW.md` (pipeline architecture)
10. `06_FRONTEND_DEEPDIVE.md` (frontend architecture)
11. `05_DATABASE_SCHEMA.md` (DB schema)
12. `03_ARCHITECTURE.md` (file map)
13. `02_PROJECT_VISION.md` (why this exists)
14. `13_RUN_HISTORY.md` (timeline)

If you only have time for THREE: 01, 10, 11.

## What to update in this folder as you work

- After each fix: update `09_FIXES_HISTORY.md` with the new entry.
- After each fix: cross off the corresponding item in `10_OPEN_ISSUES.md`.
- After each new run: append to `13_RUN_HISTORY.md`.
- If you discover a new issue: add to `10_OPEN_ISSUES.md` with a unique A-number.
- If you discover a new diagnostic pattern: add to `08_DIAGNOSTIC_PATTERNS.md`.
- If the user gives new working preferences: add to `12_WORKING_AGREEMENTS.md`.

Keep the docs ACTIVE — they're your context across sessions.

## Final note from prior-Claude

Read `01_USER_PROFILE.md` carefully. The user's communication style takes a minute to learn but it's worth the investment. Match the energy. Lead with answers. Verify when challenged. Don't apologize defensively — just fix things.

The user has high standards. They reward depth, accuracy, and tempo. They punish hedging, defensive coding, and ignored instructions.

Good luck.

— Opus 4.7 1M, 2026-05-03
