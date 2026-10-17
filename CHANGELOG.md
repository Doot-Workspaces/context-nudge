# Changelog

## 1.1.1 - 2026-10-13

- Snapshot before inject. Each time the prep file changes, the hook copies it to `<key>.snapshot.md`. After a compaction it injects the snapshot, so a rewrite by the first tool call after compaction cannot replace the pre-compaction text. Falls back to the live file when no snapshot exists.
- Both files are marked consumed after injection: the injected one becomes `<key>.injected.md`, the other `<key>.consumed.md`.
- The re-inject text now says "Re-read this before rewriting the prep file."
- Prep template: Goal and next action, User corrections (verbatim, with approximate time), Open standing orders, Decisions with reasons, Live write and data state, Blockers, Test status, Running subagents.

## 1.1.0 - 2026-10-13

- Bands default to 300k (write the compact-prep file) and 350k (compact or clear at the next task boundary). The 600k and 850k defaults are gone. `CONTEXT_NUDGE_BANDS` still overrides.
- Runs in subagents. Reads the subagent's own transcript at `<session dir>/subagents/agent-<agent_id>.jsonl`, keeps state per `session_id` plus `agent_id`, and sends context only, with no user notice.
- Compact-prep file at `<prep dir>/<session_id>.md`, or `<session_id>-<agent_id>.md` in a subagent. Section list in `hooks/prep_template.md`. Directory set by `CONTEXT_NUDGE_PREP_DIR`.
- After a compaction (a new `compact_boundary` in the caller's transcript), re-arms the bands and injects the prep file once, capped at 8,000 characters, then renames it to `*.injected.md`.
- `SessionStart` (startup) prunes state and prep files older than 14 days.

## 1.0.0 - 2026-10-13

- First release. `UserPromptSubmit` and `PostToolUse` hook that prompts the user to `/compact` or `/clear` at 350k, 600k and 850k context tokens.
- Each band fires once per session; all bands re-arm when context drops below the lowest band.
- Skips subagent and teammate calls (`agent_id` present in hook input).
- Bands configurable through `CONTEXT_NUDGE_BANDS`.
- Reads at most the last 4 MB of the transcript; fails open on any error.
