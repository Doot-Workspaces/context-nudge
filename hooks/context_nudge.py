#!/usr/bin/env python3
"""context-nudge: prepare for compaction before it happens, and restore after.

Runs on UserPromptSubmit and PostToolUse, in the main session and in subagents.
Reads the context size of the last assistant reply from the tail of the
transcript that belongs to the caller: the session transcript for the main
session, <session dir>/subagents/agent-<agent_id>.jsonl for a subagent.

Lowest band (default 300k): ask Claude to write a compact-prep file from the
template in prep_template.md, and keep it current.
Every higher band (default 350k): main session gets a user message and a prompt
to propose /compact <hint> or /clear; a subagent gets context only, telling it
to finish the current step and refresh the prep file.

After a compaction (a new compact_boundary record in the caller's transcript),
all bands re-arm and the prep file, if present, is injected once as
additionalContext and then renamed to *.injected.md so it never re-injects.

SessionStart (startup) prunes state and prep files older than 14 days.
Any error exits 0 with no output.

Env: CONTEXT_NUDGE_BANDS ("300k,350k"), CONTEXT_NUDGE_PREP_DIR
(default ~/.claude/state/compact-prep).
"""
import json
import os
import re
import sys
import time

DEFAULT_BANDS = (300_000, 350_000)
TAIL_STEPS = (256 * 1024, 1024 * 1024, 4 * 1024 * 1024)
TTL_SECONDS = 14 * 24 * 3600
INJECT_CAP = 8000  # Claude Code caps a hook's additionalContext at 10,000 characters
HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(HERE, "prep_template.md")


def parse_bands(raw):
    if not raw:
        return list(DEFAULT_BANDS)
    bands = set()
    for part in raw.split(","):
        part = part.strip().lower()
        if not part:
            continue
        scale = 1
        if part.endswith("k"):
            scale, part = 1_000, part[:-1]
        elif part.endswith("m"):
            scale, part = 1_000_000, part[:-1]
        value = int(float(part) * scale)
        if value > 0:
            bands.add(value)
    return sorted(bands) or list(DEFAULT_BANDS)


def safe(text):
    return re.sub(r"[^A-Za-z0-9_-]", "_", text)


def usage_total(record):
    usage = (record.get("message") or {}).get("usage") or {}
    return sum(
        int(usage.get(key) or 0)
        for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    )


def boundary_id(record):
    return str(record.get("uuid") or record.get("timestamp") or "boundary")


def read_tail(transcript_path, own_sidechain):
    """Return (tokens, latest_boundary_id) from the tail of a transcript.

    tokens is the usage of the latest assistant reply, 0 when a compact boundary
    is newer than every assistant reply, None when nothing was readable.
    latest_boundary_id is None when no boundary sits in the last 4 MB.
    Subagent transcripts mark every record isSidechain; the main transcript
    skips such records."""
    tokens = None
    boundary = None
    size = os.path.getsize(transcript_path)
    with open(transcript_path, "rb") as handle:
        for step in TAIL_STEPS:
            start = max(0, size - step)
            handle.seek(start)
            lines = handle.read(size - start).split(b"\n")
            if start > 0:
                lines = lines[1:]  # first line is partial
            for raw in reversed(lines):
                if b'"assistant"' not in raw and b"compact_boundary" not in raw:
                    continue
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("isSidechain") and not own_sidechain:
                    continue
                kind = record.get("type")
                if kind == "system" and record.get("subtype") == "compact_boundary":
                    if boundary is None:
                        boundary = boundary_id(record)
                    if tokens is None:
                        tokens = 0
                elif kind == "assistant" and tokens is None:
                    total = usage_total(record)
                    if total > 0:
                        tokens = total
                if tokens is not None and boundary is not None:
                    return tokens, boundary
            if start == 0:
                break
    return tokens, boundary


def own_transcript(payload):
    """The transcript holding the caller's own context. Hook input carries the
    session transcript even inside a subagent; the subagent's own file sits in
    <dirname>/<session_id>/subagents/agent-<agent_id>.jsonl."""
    transcript = payload.get("transcript_path") or ""
    agent_id = str(payload.get("agent_id") or "")
    if not agent_id:
        return transcript
    session_id = str(payload.get("session_id") or "")
    return os.path.join(
        os.path.dirname(transcript), session_id, "subagents", "agent-%s.jsonl" % agent_id
    )


def state_dir():
    base = os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.join(
        os.path.expanduser("~"), ".claude", "state", "context-nudge"
    )
    os.makedirs(base, exist_ok=True)
    return base


def prep_dir():
    return os.path.expanduser(
        os.environ.get("CONTEXT_NUDGE_PREP_DIR")
        or os.path.join("~", ".claude", "state", "compact-prep")
    )


def key_for(session_id, agent_id):
    return safe(session_id) + ("-" + safe(agent_id) if agent_id else "")


def load_state(path):
    try:
        with open(path) as handle:
            data = json.load(handle)
        return {
            "fired": [int(b) for b in data.get("fired", [])],
            "boundary": data.get("boundary"),
            "prep_mtime": data.get("prep_mtime"),
        }
    except (OSError, ValueError, TypeError, AttributeError):
        return {"fired": [], "boundary": None, "prep_mtime": None}


def save_state(path, state):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w") as handle:
        json.dump(state, handle)
    os.replace(tmp, path)


def prune(directory, suffixes):
    if not os.path.isdir(directory):
        return
    cutoff = time.time() - TTL_SECONDS
    for entry in os.scandir(directory):
        if entry.name.endswith(suffixes):
            try:
                if entry.stat().st_mtime < cutoff:
                    os.remove(entry.path)
            except OSError:
                pass


def template_text():
    try:
        with open(TEMPLATE_PATH, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return "(template missing at %s)" % TEMPLATE_PATH


def k(band):
    return "~%dk" % round(band / 1000)


def prepare_message(band, prep_path):
    return (
        "context-nudge: context is %s tokens. Write the compact-prep file now at %s, "
        "using this section list, and keep it current until compaction. Carry every user "
        "correction word for word. After a compaction it is injected back into context.\n\n%s"
        % (k(band), prep_path, template_text())
    )


def compact_messages(band, prep_path, is_subagent):
    if is_subagent:
        claude = (
            "context-nudge: this subagent's context is %s tokens. Finish the current step, "
            "refresh the compact-prep file at %s, then continue. Auto-compaction may run "
            "soon; the prep file is injected back after it." % (k(band), prep_path)
        )
        return None, claude
    user = (
        "context-nudge: %s tokens. At the next task boundary: /compact <hint> or /clear."
        % k(band)
    )
    claude = (
        "context-nudge: the conversation context is %s tokens. Refresh the compact-prep file "
        "at %s. At the next task boundary, tell the user and propose a /compact hint that "
        "names every user correction from this session to keep, or /clear if the next task "
        "is unrelated. Do not interrupt the current step." % (k(band), prep_path)
    )
    return user, claude


def sibling(prep_path, suffix):
    stem = prep_path[:-3] if prep_path.endswith(".md") else prep_path
    return stem + suffix


def mtime(path):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def snapshot(prep_path, state):
    """Copy the prep file to <key>.snapshot.md whenever it changed since the last
    run, so a rewrite right after compaction cannot replace what gets injected.
    Returns True when the state changed."""
    current = mtime(prep_path)
    if current is None or current == state["prep_mtime"]:
        return False
    with open(prep_path, "rb") as src:
        data = src.read()
    target = sibling(prep_path, ".snapshot.md")
    tmp = "%s.%d.tmp" % (target, os.getpid())
    with open(tmp, "wb") as dst:
        dst.write(data)
    os.replace(tmp, target)
    state["prep_mtime"] = current
    return True


def reinject_message(prep_path):
    """Inject the pre-compaction snapshot, or the live file when no snapshot
    exists, then mark both consumed so neither injects again."""
    snap = sibling(prep_path, ".snapshot.md")
    source = snap if os.path.isfile(snap) else prep_path
    try:
        with open(source, encoding="utf-8", errors="replace") as handle:
            body = handle.read()
    except OSError:
        return None
    kept = sibling(prep_path, ".injected.md")
    header = (
        "context-nudge: re-read: written before compaction. Re-read this before rewriting "
        "the prep file. Kept at %s.\n\n" % kept
    )
    room = INJECT_CAP - len(header)
    if len(body) > room:
        note = "\n\n[truncated by context-nudge: read the rest from %s]" % kept
        body = body[: max(0, room - len(note))] + note
    for path, suffix in ((source, ".injected.md"), (prep_path, ".consumed.md")):
        if os.path.isfile(path):
            try:
                os.replace(path, sibling(prep_path, suffix))
            except OSError:
                pass
    return header + body


def emit(event, user, claude):
    out = {}
    if user:
        out["systemMessage"] = user
    if claude:
        out["hookSpecificOutput"] = {"hookEventName": event, "additionalContext": claude}
    if out:
        sys.stdout.write(json.dumps(out))


def on_session_start():
    prune(state_dir(), (".json",))
    prune(prep_dir(), (".md",))


def main():
    payload = json.loads(sys.stdin.read() or "{}")
    if not isinstance(payload, dict):
        return
    event = payload.get("hook_event_name") or "PostToolUse"
    if event == "SessionStart":
        on_session_start()
        return
    session_id = str(payload.get("session_id") or "")
    agent_id = str(payload.get("agent_id") or "")
    if not session_id or not payload.get("transcript_path"):
        return
    transcript = own_transcript(payload)
    if not os.path.isfile(transcript):
        return
    tokens, boundary = read_tail(transcript, own_sidechain=bool(agent_id))
    if tokens is None:
        return

    bands = parse_bands(os.environ.get("CONTEXT_NUDGE_BANDS", ""))
    key = key_for(session_id, agent_id)
    path = os.path.join(state_dir(), key + ".json")
    prep_path = os.path.join(prep_dir(), key + ".md")
    state = load_state(path)
    fired = set(state["fired"])

    if boundary is not None and boundary != state["boundary"]:
        # A compaction since the last look: re-arm and restore the prep file once.
        message = None
        if os.path.isfile(prep_path) or os.path.isfile(sibling(prep_path, ".snapshot.md")):
            message = reinject_message(prep_path)
        save_state(path, {"fired": [b for b in bands if tokens >= b], "boundary": boundary,
                          "prep_mtime": None, "tokens": tokens})
        if message:
            emit(event, None, message)
        return

    changed = snapshot(prep_path, state)
    crossed = [b for b in bands if tokens >= b]
    fresh = [b for b in crossed if b not in fired]
    if tokens < bands[0] or not fresh:
        if changed or (fired and tokens < bands[0]):
            state["fired"] = [] if tokens < bands[0] else sorted(fired)
            state["tokens"] = tokens
            save_state(path, state)
        return
    os.makedirs(prep_dir(), exist_ok=True)  # before the state write, so a failure leaves the band armed
    save_state(path, {"fired": sorted(fired | set(crossed)), "boundary": state["boundary"],
                      "prep_mtime": state["prep_mtime"], "tokens": tokens})

    top = max(fresh)
    if top == bands[0]:
        claude = prepare_message(top, prep_path)
        user = None if agent_id else (
            "context-nudge: %s tokens. Claude is writing a compact-prep file." % k(top)
        )
    else:
        user, claude = compact_messages(top, prep_path, bool(agent_id))
        if bands[0] in fresh:  # jumped past both bands at once: still ask for the file
            claude = prepare_message(bands[0], prep_path) + "\n\n" + claude
    emit(event, user, claude)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
