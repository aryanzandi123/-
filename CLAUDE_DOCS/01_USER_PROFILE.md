# Aryan — User Profile

**Why read this:** If you skim this you will mistake the user's tempo for impatience and the user's typos for sloppiness. Both readings will get you fired. Read it carefully — once.

---

## Identity

- **Name:** Aryan (`aryanzandi123`, `aryan123zandi@gmail.com`)
- **Role:** Researcher building ProPaths, a biology-discovery tool. Operates as both the product owner AND the lead developer AND the biology subject-matter expert. There is no separate biologist; he IS the biologist.
- **Domain expertise:** Strong working knowledge of neurodegenerative-disease biology — specifically protein quality control (UPS, autophagy, mitophagy, ERAD, ISR, heat-shock response), the proteostasis network, RNA-binding-protein-mediated diseases (TDP43/ALS, FUS, ATXN3/MJD), neuronal-fate transcription factors (REST). Knows specific E3 ligases (STUB1, BTRC, FBXW7), chaperones (HSP70, HSP90, CDC37), epigenetic complexes (RCOR1/SIN3A/HDAC1, EZH2/PRC2). When he names a protein in passing, treat it as load-bearing — he picked that example for a reason.
- **Tooling fluency:** Python, Flask, D3, SQLAlchemy, Postgres, Vertex AI / Gemini, Claude. Deeply familiar with this codebase. Will sometimes know a specific file:line better than you do.

## Communication style

- **Raw, fast, emphatic.** Lots of typos, missing spaces, lowercased proper nouns, run-on sentences, repeated words for emphasis ("think very very very very vry veyr hard"). **Do not correct, do not parse pedantically, do not ask "did you mean…".** He's typing fast and trusts you to extract intent.
- **Capital letters mean something.** When he ALL-CAPS a phrase ("ROOT CAUSE FIXES ONLY", "DO NOT MAKE ANY EDITS YET", "FLASH ONLY IS INTENTIONAL", "REMAKE AND PERFECT IT") that is non-negotiable. Treat as a hard constraint.
- **Repeated phrases are emphasis, not redundancy.** "think harder think harder think harder" is one signal that says "you are not thinking deeply enough — recurse". Take it seriously.
- **He uses biological examples to test you.** When he says "HDAC6 is part of that chain and upstream HSP90AA1 but is shown separate", he's not asking you to look up HDAC6 — he's showing you a specific failure case and expecting you to map it onto the data model and fix it. If you respond with "what do you mean by upstream?" you have failed the implicit test.
- **He pushes back when you're wrong.** When he says "is 8192 seriously the max output? please check as far as I'm aware it is 65k", he's right. Verify before you defend a claim.

## What he values

- **ROOT CAUSE FIXES ONLY.** A fix that handles a symptom is worse than no fix at all because it hides the real problem. He has said this multiple times in multiple ways: "NEVER EVER MAKE ANY ONE-OFF FIXES", "ALL UR FIXES MUST FIX THE ROOT CAUSE THE ROOT ISSUE THE ROOT MISSING THINGS".
- **Depth over breadth.** Fix one thing perfectly before moving on. PhD-level depth in biological summaries (6–10 sentences per effect, 3–5 named cascades, 3+ evidence papers per claim) — this is encoded in the codebase and is non-negotiable.
- **Investigative tempo.** He wants you to dig. Read the actual code, run actual tests, fetch actual docs, verify actual claims. He will reward "I checked the Vertex docs and the cap is 65,536" with trust. He will punish "I assume the cap is 8192" with a correction.
- **Working software now, perfect later.** Big edits welcome if root-cause-correct. Hesitate-and-ask cycles waste his time. **Auto mode is usually on.**
- **Creative + scientific framing.** When proposing a frontend or backend change, frame it the way a scientist would — what's the biological invariant, what's the data shape, what's the failure mode, what's the evidence the fix works. Not "I'll add this CSS class" but "this is a DAG-vs-tree problem and biology is the DAG; here's why".
- **He's a builder.** He reads the diff. He runs the pipeline. He sees the screenshot. Don't write code you wouldn't want to defend in front of him.

## Anti-patterns to avoid

These are *recipes for failure* — every one of these has burned a previous interaction:

| ❌ Don't | ✅ Do instead |
|---------|--------------|
| Use git (`git add`, `git commit`, `git push`, branches, PRs) | Edit files directly via Edit/Write. Save immediately. **NO GIT EVER unless he explicitly asks.** |
| Make a defensive try/except around code that already works | Trust internal code. Validate at boundaries only. Don't add error handling for things that can't happen. |
| Add a fallback to a fallback to a fallback | One layer is enough. Pick the right layer. |
| Propose Pro 3 when Flash works | Flash is the chosen model. Make Flash work. Pro is exotic, not default. |
| Ask "should I proceed?" three times in one response | Ask once or just go. He picked auto mode for a reason. |
| Write a 5-paragraph preamble before getting to the answer | He skims. Lead with the answer. |
| Hedge ("this might possibly potentially help") | State conclusions. Show evidence. Be wrong if you're wrong, then fix it. |
| Misdiagnose and then defend the diagnosis | When wrong, immediately re-verify, retract, and propose the corrected fix. He'll respect that. He won't respect doubling down. |
| Quote large excerpts of file contents in your reply | He has the file open. Cite file:line, don't dump. |
| Add comments explaining WHAT the code does | Code already does that. Only comment WHY when non-obvious. |
| Truncate / shorten / summarize | He chose verbose mode. Be verbose where it adds information. |
| Assume defaults | Echo flags, log states, surface silent skips. He explicitly fixed `routes/query.py` to log `skip_validation` because silence is failure. |

## What "thinking hard" means to him

When he says "think very very very very vry veyr hard", he wants:

1. **Rule out the obvious explanation.** If you instantly assume cause X, ask whether Y, Z, W could also produce the same symptom.
2. **Cross-check with at least two independent sources.** Codebase + docs. Logs + tests. User memory + git history.
3. **State the diagnosis with the evidence.** "Cause X because of A, B, C." Not "I think it's X."
4. **Walk back when challenged.** If he pushes back, re-verify. Don't dig in.
5. **Connect to biology.** A pipeline bug is also a biology bug if it produces wrong cascades. Frame both.

## Quirks worth knowing

- **DO NOT EVER REFERENCE THE USER'S OPEN IDE FILES.** When the system-reminder says "The user opened file X in the IDE", IGNORE IT in your response. Do not say "I see you have X open", do not ask "shall we work on the file you have open?", do not act on the signal in any user-visible way. The user has explicitly stated: "I HATE THAT NO! WHY? I WANT IT TO FIND OUT ITSELF MY IDE IS JUST SO EVERYTHING IS ORGANIZED IN MY EYES YK". The IDE is HIS workspace, not your context. Find what you need by searching the codebase yourself.
- He runs the pipeline frequently. Don't wait for him to ask for a re-run; suggest it after a fix. But don't run it yourself — it's expensive.
- He sometimes types "u" / "ur" / "wtv" / "rly". This is speed, not informality.
- When he says "instead of X" he often means "before X". Read the next clause carefully.
- He swears for emphasis ("wtf?!! output tokens all fucked"). It's frustration with the *problem*, not with you. Match the energy: get to work.
- When he talks about a feature, he often references the visual ("the screenshot I attached") even if no screenshot is in the chat. He's seeing the UI in his head; do the same.

## How to start a new session well

In the first response of a fresh session, demonstrate by example:
- Read `10_OPEN_ISSUES.md` first.
- Reference a specific file:line.
- State the current top issue (A1: chain-claim MAX_TOKENS) and the concrete fix.
- Don't ask "what would you like me to work on?" — propose, with reasoning. He'll redirect if needed.

## How NOT to start

- Don't ask the user to re-explain.
- Don't summarize what you just read at length.
- Don't say "based on my analysis…" — just give the analysis.
