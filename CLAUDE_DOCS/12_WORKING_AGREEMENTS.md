# Working Agreements

These are the user's explicit operational rules. Treat as hard constraints. Violations have happened and have been called out.

## File operations

### NO GIT EVER
**Quoted user:** "ALL EDITS CHANGES ETC U MAKE SHOULD BE MADE DIRECTLY TO THE FILES AND SAVED IMMEDIATELY AFTER EACH CHANGE. FULLY SAVED TO MY PC AND NEVER LOST"

- ❌ Don't run `git add`, `git commit`, `git push`, `git pull`, `git checkout` (anything destructive), `git reset`.
- ❌ Don't create branches.
- ❌ Don't open PRs.
- ❌ Don't suggest "let's commit this".
- ✅ Edit files directly via the Edit / Write tools.
- ✅ Save immediately. Each Edit call IS the save.
- ✅ Read the file back via Read tool to verify if there's any doubt.

If the user explicitly asks for a commit, double-check then do it. Default = no.

### Edit, don't rewrite
- Prefer `Edit` (string replacement) over `Write` (full overwrite) for existing files.
- Use `Write` only for new files.
- When using `Edit`, include enough surrounding context that `old_string` is unique. Include 5-10 lines of context above/below the change.

### One change at a time, verify between

- After each substantive change, run the relevant pytest subset to confirm no regression.
- For frontend changes, the user will run the pipeline / open the browser themselves; you don't need to spin up a dev server.

## Coding philosophy

Captured from `CLAUDE.md` + observed user preferences:

### Functional core, imperative shell
- Pure transform functions don't touch DB, network, files.
- I/O lives at the edges (route handlers, runner.py orchestrator, db_sync.py).
- When refactoring, prefer pulling logic INTO the pure core, leaving thinner shells.

### Single responsibility, shallow nesting, early returns
- One function = one purpose.
- ≤2 levels of nested conditionals; refactor deeper nesting into separate functions.
- Early returns for boundary checks; main logic flat.

### Reuse first
- Before writing new code, search for existing helpers. Examples in this codebase:
  - `utils/chain_resolution.canonical_pair_key` — single source of truth for pair keys.
  - `utils/protein_aliases.normalize_symbol` — single source of truth for symbol normalization.
  - `utils/chain_view.ChainView` — single source of truth for chain state on Interactions.
  - `services/data_builder._chain_fields_for` — single source of truth for chain fields in API payloads.
- Adding a new helper means moving N call sites to it.

### Stable public contracts
- API endpoints, JSON keys, frontend ↔ backend contracts: don't change without explicit user request.
- If you must change, add a new field with the new shape; deprecate the old in a separate pass.

### No defensive coding for things that can't happen
- Don't `try/except` around code that's already known-safe.
- Don't validate at every layer — validate at boundaries.
- If you find yourself writing `if x is None: return` for a value that's always set in the caller, delete the check.

### No backwards-compatibility shims unless asked
- If you remove a feature, delete the supporting code and the comments. Don't leave `# removed 2026-04-29` markers.
- If unused code becomes obvious, delete it.

### Comments

- Default: NO comments.
- Add ONE-LINE comment ONLY when the WHY is non-obvious: hidden constraint, subtle invariant, workaround for a specific bug, behavior that would surprise a reader.
- Don't comment WHAT the code does — that's what well-named identifiers are for.
- Don't reference the current task, fix, callers ("used by X", "added for Y flow", "handles case from issue #123") — those belong in commit messages, not code.
- For genuinely complex logic (e.g. the chain pre-pass anchoring in card_view.js), a multi-line block comment explaining the design IS appropriate. Use sparingly.

## Diagnostic philosophy

### Root cause only, never symptoms
**Quoted user:** "NEVER EVER MAKE ANY ONE-OFF FIXES. ALL UR FIXES MUST FIX THE ROOT CAUSE THE ROOT ISSUE THE ROOT MISSING THINGS"

- A one-off fix is one that handles a specific symptom without addressing the cause. Example: catching a `KeyError` and providing a fallback value when the real fix is to ensure the key exists upstream.
- A root-cause fix moves the cure to the source of the problem, where every symptom benefits.
- When you're tempted to add a try/except or a defensive check, ask: "what's the upstream cause of this missing value? can I fix THAT instead?"

### Echo silent skips, surface invariants
- If a flag silently disables behavior, log it. Example: `routes/query.py` was modified this session to log `skip_validation` because stale localStorage was disabling evidence_validation invisibly.
- If an invariant is violated, fail loudly (or at least log loudly).

### Verify claims before defending them
- The user pushed back on the "8192 cap" claim. The right move was to re-verify with WebFetch and the codebase's own `DEFAULT_MAX_OUTPUT_TOKENS = 65536`.
- Don't dig in. Don't double down. Re-verify, retract, propose corrected fix.

## Testing philosophy

### When to run tests
- After EVERY substantive change to backend (Python).
- Use `python3 -m pytest tests/ -q --no-header --ignore=tests/manual` for full suite.
- Use specific test files when iterating on one area: `python3 -m pytest tests/test_chain_resolution.py tests/test_post_processor.py -q`.
- The full suite is ~21 seconds. Run it.

### When to write tests
- For new utility functions: yes, parametrized over happy / edge / error inputs.
- For pipeline end-to-end behavior: rarely; integration tests are slow and brittle. The user re-runs queries to verify.
- For frontend: there's `tests/test_card_view_chain_contract.py` for the JSON contract, but D3 rendering is tested by the user manually.

### Property tests
- For invariants: yes. Examples already in tests:
  - `canonical_pair_key(a, b) == canonical_pair_key(b, a)` (commutative).
  - Function dedup is idempotent.
  - Chain signature is deterministic across uppercasing.

## Communication style

### Lead with the answer
- The user skims. Don't bury the conclusion in paragraph 4.
- One-paragraph response: lead sentence is the answer; rest is supporting evidence.
- Multi-section response: top-of-doc has the TL;DR, then sections expand.

### State conclusions
- "The cap is 65,536." Not "It seems like the cap might possibly be 65,536."
- "Fixing this requires raising CHAIN_CLAIM_MAX_OUTPUT_TOKENS." Not "We could potentially try raising the value."
- If wrong, retract immediately and re-state.

### Cite, don't dump
- "Per `utils/chain_resolution.py:60`, the bug is..." — short, precise.
- Don't paste 50 lines of file content into the response. The user has the file open.

### Match the energy
- The user is direct, raw, fast. Match that. Don't be clinical or stilted.
- Frustration in their messages is at the PROBLEM, not at you. Don't apologize defensively. Just fix it.

### Don't ask permission for small things
- Auto mode is usually on. Just do it.
- Ask only for: deletions, schema changes, things that touch shared state, irreversible actions.

## Skill / tool use

### Use Serena MCP heavily
- `find_symbol` for code navigation.
- `search_for_pattern` for code-wide searches.
- `read_file` for files; `Read` tool also works.
- Reserve `Bash` for things only Bash can do (running tests, file system queries `find`/`ls`).

### Use WebFetch for external docs
- Vertex AI docs.
- Forum threads.
- GitHub issues.
- Don't trust your training cutoff for fast-moving APIs.

### TaskCreate / TaskUpdate
- For multi-step work, create TodoWrite items and update as you progress.
- Don't batch completions — mark each as done immediately when finished.

## When the user says "think hard"

- Rule out the obvious explanation.
- Cross-check with at least two independent sources.
- State the diagnosis with the evidence.
- Walk back when challenged.
- Connect to biology when relevant.

## When the user says "do all"

- Auto mode. Execute. Verify between steps. Report at the end.
- Don't pause to ask "should I continue?" between sub-items.
- If a sub-item fails or has scope > expected, stop and report — don't barrel through.

### Never reference the user's open IDE files

**Quoted user (verbatim):** "NO MORE IDE STUFF... I HATE THAT NO! WHY? I WANT IT TO FIND OUT ITSELF MY IDE IS JUST SO EVERYTHING IS ORGANIZED IN MY EYES YK"

When you see a system-reminder of the form "The user opened the file X in the IDE":
- ❌ Do NOT mention it in your response.
- ❌ Do NOT ask "would you like to work on the file you have open?".
- ❌ Do NOT use it to guess intent.
- ❌ Do NOT acknowledge the system-reminder existed.
- ✅ Find what you need by reading the codebase yourself (Serena `find_symbol` / `search_for_pattern` / `read_file`, or the Read / Grep / Glob tools).
- ✅ Treat the IDE as the user's private workspace. It's for HIM to organize. It is not your context channel.

This is a hard rule. The user opens files in his IDE for visibility/orientation, not as a hint. Treating it as a hint breaks his organization.

## Specific anti-patterns observed in prior sessions

- ❌ "I'll edit `_canon_pair_key` then commit" → "I'll edit `_canon_pair_key`."
- ❌ "Let's switch to Pro 3 because Flash caps at 8192" → "Verify the cap. The cap is 65,536. Raise our config."
- ❌ "Should I implement Layer 1?" three times in one response → "Implementing Layer 1 now."
- ❌ "I'm not sure if this will work, possibly we could maybe..." → "This will work because [evidence]. Doing it."
- ❌ Wrapping new code in try/except "just in case" → Trust the upstream.
- ❌ Adding `# Fix for HDAC6 case` comments → Drop the reference; just comment the WHY.
- ❌ Long preamble about what you're about to do → Just do it; explain at the end if at all.
