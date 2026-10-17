# context-nudge

A Claude Code plugin that gets Claude ready for compaction before it happens. At the first band Claude writes a compact-prep file; at the second you are prompted to `/compact` or `/clear`; after any compaction the prep file is put back into context. It never compacts and never blocks. It runs in the main session and in subagents.

## What it does

The hook runs on `UserPromptSubmit` and `PostToolUse`. It reads the token count of the last assistant reply from the tail of the caller's own transcript: `input_tokens + cache_read_input_tokens + cache_creation_input_tokens`. Inside a subagent that is `<session dir>/subagents/agent-<agent_id>.jsonl`, because hook input carries the session transcript even there.

| Band | Main session | Subagent |
|------|--------------|----------|
| 300k | You see a one-line notice. Claude is asked to write the compact-prep file and keep it current. | Same request to Claude, no notice to you. |
| 350k | You see: at the next task boundary, `/compact <hint>` or `/clear`. Claude is asked to propose a hint naming every user correction to keep. | Claude is asked to finish the current step and refresh the prep file. No notice to you. |

Each band fires once per session, or once per subagent. State is keyed by `session_id`, plus `agent_id` inside a subagent.

After a compaction, the hook sees the new `compact_boundary` record in the caller's transcript on its next run. It re-arms the bands and injects the prep file once as context headed "re-read: written before compaction", capped at 8,000 characters. Each time the prep file changes before a compaction, the hook copies it to `*.snapshot.md`, and it injects that copy, so a rewrite by the first tool call after compaction cannot replace it. Afterwards the injected copy becomes `*.injected.md` and the live file `*.consumed.md`, so neither injects twice. In the main session after `/compact` that is your next prompt; after an auto-compaction mid-task, and in subagents, it is the next tool result.

The section list Claude fills in lives in one file, `hooks/prep_template.md`.

## Prep files

Prep files are plain local files at `~/.claude/state/compact-prep/<session_id>.md`, or `<session_id>-<agent_id>.md` for a subagent. They may hold sensitive task details, such as file paths, data state and quoted instructions. They stay on your machine and are never sent anywhere by this plugin. Files older than 14 days are deleted at session start.

## Install

From a local clone:

```
/plugin marketplace add /path/to/context-nudge
/plugin install context-nudge@context-nudge
```

Requires `python3` on your `PATH`. Standard library only.

## Auto-compact

A plugin cannot change your settings. Pair the bands with an auto-compact window above them, so the prep file exists before auto-compaction runs, for example in `~/.claude/settings.json`:

```json
{ "autoCompactWindow": 450000 }
```

Subagents use the same auto-compact logic as the main conversation. With auto-compact off, a subagent that fills its context stops with a "Prompt is too long" error instead of compacting.

## Configure

| Variable | Default | Effect |
|----------|---------|--------|
| `CONTEXT_NUDGE_BANDS` | `300k,350k` | Comma-separated token counts, plain integers or `k`/`m` suffixes. The lowest band asks for the prep file; every higher band uses the compact message. |
| `CONTEXT_NUDGE_PREP_DIR` | `~/.claude/state/compact-prep` | Where prep files are written and read. |

Set them in the `env` block of your settings or in the shell that starts Claude Code. Band state lives in `${CLAUDE_PLUGIN_DATA}`, one small JSON file per session or subagent, pruned after 14 days.

## Behaviour on error

Any error, such as a missing or corrupt transcript or an unwritable directory, exits 0 with no output. The hook reads at most the last 4 MB of a transcript, so it stays fast on long sessions.

## Test

```
python3 tests/test_context_nudge.py
```

The script pipes synthetic hook input and synthetic transcripts through the real hook and asserts the JSON shape Claude Code parses. It covers both bands in the main session and in subagents, per-agent state and prep paths, re-inject, snapshot before overwrite and truncation after compaction, re-arm, pruning, fail-open cases, custom bands, and timing.

## License

MIT
