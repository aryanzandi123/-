# Prompt for New Claude Code Session

Copy this verbatim:

Read these two changelog files FULLY before doing anything:
1. docs/session-A-changes.md
2. docs/session-B-changes.md

These are from two independent Claude Code sessions that each performed deep analysis and made changes to this ProPaths codebase. Both sessions received the same instructions: find all bugs, issues, inefficiencies — especially duplicative scientific claims and excessive AI token costs — and fix them. They also optimized the PostgreSQL layer and cleaned up the UI.

Your job:
1. Read BOTH changelogs completely and understand every change each session made
2. Diff the two approaches — identify where they AGREE (high confidence those fixes are correct), where they CONFLICT (one session may have a better approach), and where one session found issues the other MISSED
3. Check the CURRENT state of the codebase — which changes from each session are already applied? Which are missing?
4. Produce a UNIFIED action plan that:
   - Keeps all changes both sessions agree on
   - For conflicts, picks the better approach (explain why)
   - Applies any missing fixes that one session found but the other didn't
   - Identifies any issues NEITHER session caught
5. Implement the unified plan

Start by reading both files, then give me your analysis before making any changes.
