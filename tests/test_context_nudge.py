#!/usr/bin/env python3
"""Feeds synthetic hook input and synthetic transcripts through context_nudge.py.

Run: python3 tests/test_context_nudge.py
Each check pipes JSON into the real hook as a subprocess and asserts the exact
output shape Claude Code parses. Exit code 0 means every check passed.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks", "context_nudge.py")
FAILURES = []
TIMINGS = []


def assistant(tokens, sidechain=False):
    return json.dumps({
        "type": "assistant",
        "isSidechain": sidechain,
        "message": {"role": "assistant", "usage": {
            "input_tokens": 2,
            "cache_read_input_tokens": max(tokens - 1002, 0),
            "cache_creation_input_tokens": 1000,
            "output_tokens": 500,
        }},
    })


def user_line(text="ok", sidechain=False):
    return json.dumps({"type": "user", "isSidechain": sidechain,
                       "message": {"role": "user", "content": text}})


def boundary(uid, sidechain=False):
    return json.dumps({"type": "system", "subtype": "compact_boundary", "uuid": uid,
                       "isSidechain": sidechain,
                       "compactMetadata": {"trigger": "auto", "preTokens": 450000}})


class Session:
    """Main transcript at <project>/<id>.jsonl, subagents at
    <project>/<id>/subagents/agent-<agent_id>.jsonl, as Claude Code lays them out."""

    def __init__(self, root, name):
        self.id = "sess-" + name
        self.project = os.path.join(root, "project")
        os.makedirs(self.project, exist_ok=True)
        self.path = os.path.join(self.project, self.id + ".jsonl")
        open(self.path, "w").close()

    def add(self, *lines):
        append(self.path, lines)

    def agent_path(self, agent_id):
        return os.path.join(self.project, self.id, "subagents", "agent-%s.jsonl" % agent_id)

    def add_agent(self, agent_id, *lines):
        path = self.agent_path(agent_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        append(path, lines)


def append(path, lines):
    with open(path, "a") as handle:
        for line in lines:
            handle.write(line + "\n")


class Env:
    def __init__(self, root):
        self.data = os.path.join(root, "data")
        self.prep = os.path.join(root, "prep")

    def run(self, session_id, transcript, event="PostToolUse", extra=None, env_extra=None, raw=None):
        payload = {"session_id": session_id, "transcript_path": transcript,
                   "hook_event_name": event, "cwd": "/tmp"}
        payload.update(extra or {})
        env = dict(os.environ, CLAUDE_PLUGIN_DATA=self.data, CONTEXT_NUDGE_PREP_DIR=self.prep)
        env.pop("CONTEXT_NUDGE_BANDS", None)
        env.update(env_extra or {})
        start = time.perf_counter()
        proc = subprocess.run([sys.executable, HOOK],
                              input=raw if raw is not None else json.dumps(payload),
                              capture_output=True, text=True, env=env, timeout=10)
        elapsed = time.perf_counter() - start
        TIMINGS.append(elapsed)
        return proc, elapsed

    def agent(self, s, agent_id, event="PostToolUse"):
        return self.run(s.id, s.path, event=event,
                        extra={"agent_id": agent_id, "agent_type": "Explore"})[0]


def check(name, condition, detail=""):
    print(("PASS " if condition else "FAIL ") + name + (("  " + detail) if detail else ""))
    if not condition:
        FAILURES.append(name)


def parse(proc, event="PostToolUse"):
    """None when silent, a string on a bad exit or bad shape, else (systemMessage, additionalContext)."""
    if proc.returncode != 0 or proc.stderr:
        return "bad-exit"
    if not proc.stdout:
        return None
    out = json.loads(proc.stdout)
    if not set(out) <= {"systemMessage", "hookSpecificOutput"} or "hookSpecificOutput" not in out:
        return "bad-shape"
    hso = out["hookSpecificOutput"]
    if (set(hso) != {"hookEventName", "additionalContext"} or hso["hookEventName"] != event
            or not isinstance(hso["additionalContext"], str)):
        return "bad-shape"
    if "systemMessage" in out and not isinstance(out["systemMessage"], str):
        return "bad-shape"
    return out.get("systemMessage"), hso["additionalContext"]


def main():
    root = tempfile.mkdtemp(prefix="context-nudge-test-")
    env = Env(root)

    # Main session bands.
    s = Session(root, "main")
    s.add(user_line(), assistant(290_000))
    check("main below 300k: silent", parse(env.run(s.id, s.path)[0]) is None)
    s.add(assistant(305_000))
    r = parse(env.run(s.id, s.path, event="UserPromptSubmit")[0], event="UserPromptSubmit")
    main_prep = os.path.join(env.prep, s.id + ".md")
    check("main 300k: user message + prepare context on UserPromptSubmit",
          isinstance(r, tuple) and "~305k" not in (r[0] or "") and "~300k" in (r[0] or "")
          and main_prep in r[1] and "## User corrections (verbatim)" in r[1]
          and "## Goal and next action" in r[1] and "## Running subagents" in r[1], repr(r)[:300])
    s.add(assistant(320_000))
    check("main 300k does not repeat", parse(env.run(s.id, s.path)[0]) is None)
    s.add(assistant(352_000))
    r = parse(env.run(s.id, s.path)[0])
    check("main 350k: user message names /compact and /clear, context asks for hint",
          isinstance(r, tuple) and r[0] and "/compact" in r[0] and "/clear" in r[0]
          and "propose a /compact hint" in r[1] and main_prep in r[1], repr(r)[:300])
    s.add(assistant(380_000))
    check("main 350k does not repeat", parse(env.run(s.id, s.path)[0]) is None)

    j = Session(root, "jump")
    j.add(assistant(420_000))
    r = parse(env.run(j.id, j.path)[0])
    check("jump past both bands: compact message plus the prep request",
          isinstance(r, tuple) and "/compact" in (r[0] or "") and "## Open standing orders" in r[1]
          and "propose a /compact hint" in r[1], repr(r)[:300])
    check("jump: nothing more at the same size", parse(env.run(j.id, j.path)[0]) is None)

    # Subagents: own transcript, own state, context only.
    t = Session(root, "team")
    t.add(assistant(50_000))
    t.add_agent("aaa111", user_line("task", True), assistant(305_000, True))
    r = parse(env.agent(t, "aaa111"))
    agent_prep = os.path.join(env.prep, t.id + "-aaa111.md")
    check("subagent 300k: additionalContext only, no systemMessage",
          isinstance(r, tuple) and r[0] is None and "## Live write and data state" in r[1], repr(r)[:300])
    check("subagent prep path is <session_id>-<agent_id>.md", isinstance(r, tuple) and agent_prep in r[1])
    check("subagent state keyed by session+agent",
          os.path.isfile(os.path.join(env.data, t.id + "-aaa111.json"))
          and not os.path.exists(os.path.join(env.data, t.id + ".json")))
    check("subagent reads its own transcript, main at 50k stays silent", parse(env.run(t.id, t.path)[0]) is None)
    t.add_agent("aaa111", assistant(355_000, True))
    r = parse(env.agent(t, "aaa111"))
    check("subagent 350k: finish the step, refresh prep, no systemMessage",
          isinstance(r, tuple) and r[0] is None and "Finish the current step" in r[1]
          and agent_prep in r[1], repr(r)[:300])
    t.add_agent("bbb222", assistant(310_000, True))
    r = parse(env.agent(t, "bbb222"))
    check("second subagent fires on its own state",
          isinstance(r, tuple) and os.path.join(env.prep, t.id + "-bbb222.md") in r[1], repr(r)[:200])
    check("subagent with no transcript file: silent", parse(env.agent(t, "ccc333")) is None)

    # Re-inject after compaction, subagent.
    os.makedirs(env.prep, exist_ok=True)
    with open(agent_prep, "w") as handle:
        handle.write("# Compact prep\n\n## User corrections (verbatim)\n- \"two due dates = two reports\"\n")
    t.add_agent("aaa111", boundary("b-1", True), user_line("summary", True))
    r = parse(env.agent(t, "aaa111"))
    check("subagent after compaction: prep file re-injected as additionalContext only",
          isinstance(r, tuple) and r[0] is None
          and r[1].startswith("context-nudge: re-read: written before compaction")
          and "two due dates = two reports" in r[1], repr(r)[:300])
    check("injected prep file renamed to .injected.md",
          not os.path.exists(agent_prep) and os.path.isfile(agent_prep[:-3] + ".injected.md"))
    check("re-inject happens once", parse(env.agent(t, "aaa111")) is None)
    state = json.load(open(os.path.join(env.data, t.id + "-aaa111.json")))
    check("compaction re-arms the bands", state["fired"] == [] and state["boundary"] == "b-1", repr(state))
    t.add_agent("aaa111", assistant(40_000, True), assistant(302_000, True))
    r = parse(env.agent(t, "aaa111"))
    check("300k fires again after compaction", isinstance(r, tuple) and agent_prep in r[1], repr(r)[:200])

    # Truncation.
    with open(agent_prep, "w") as handle:
        handle.write("# Compact prep\n" + ("line of task state\n" * 2000))
    t.add_agent("aaa111", boundary("b-2", True))
    r = parse(env.agent(t, "aaa111"))
    check("oversize prep file truncated under 8,000 chars with a note",
          isinstance(r, tuple) and len(r[1]) <= 8000 and "[truncated by context-nudge" in r[1]
          and agent_prep[:-3] + ".injected.md" in r[1], "len=%s" % (len(r[1]) if isinstance(r, tuple) else r))

    t.add_agent("aaa111", assistant(60_000, True), boundary("b-3", True))
    check("compaction with no prep file: silent", parse(env.agent(t, "aaa111")) is None)

    # Overwrite before inject: the snapshot taken before compaction wins.
    w = Session(root, "overwrite")
    w.add(assistant(310_000))
    w_prep = os.path.join(env.prep, w.id + ".md")
    env.run(w.id, w.path)
    with open(w_prep, "w") as handle:
        handle.write("# Compact prep\nPRE-COMPACT-v1\n")
    os.utime(w_prep, ns=(1_000_000_000, 1_000_000_000))
    check("prep write seen: silent", parse(env.run(w.id, w.path)[0]) is None)
    check("snapshot taken on first sight", os.path.isfile(w_prep[:-3] + ".snapshot.md"))
    with open(w_prep, "w") as handle:
        handle.write("# Compact prep\nPRE-COMPACT-v2\n")
    os.utime(w_prep, ns=(2_000_000_000, 2_000_000_000))
    env.run(w.id, w.path)
    check("snapshot refreshed when the prep file changes",
          "PRE-COMPACT-v2" in open(w_prep[:-3] + ".snapshot.md").read())
    w.add(boundary("w-1"), user_line("summary"))
    with open(w_prep, "w") as handle:
        handle.write("# Compact prep\nPOST-COMPACT-rewrite\n")
    r = parse(env.run(w.id, w.path)[0])
    check("overwrite after compaction: snapshot injected, not the rewrite",
          isinstance(r, tuple) and "PRE-COMPACT-v2" in r[1] and "POST-COMPACT" not in r[1], repr(r)[:300])
    check("re-inject text says re-read before rewriting",
          isinstance(r, tuple) and "Re-read this before rewriting the prep file." in r[1])
    check("snapshot kept as .injected.md, rewrite kept as .consumed.md, both live names gone",
          "PRE-COMPACT-v2" in open(w_prep[:-3] + ".injected.md").read()
          and "POST-COMPACT" in open(w_prep[:-3] + ".consumed.md").read()
          and not os.path.exists(w_prep) and not os.path.exists(w_prep[:-3] + ".snapshot.md"))
    check("consumed files never re-inject", parse(env.run(w.id, w.path)[0]) is None)

    # Re-inject after compaction, main session, UserPromptSubmit.
    with open(main_prep, "w") as handle:
        handle.write("# Compact prep\n\n## Open standing orders\n- ship nothing remote\n")
    s.add(boundary("m-1"), user_line("summary"))
    r = parse(env.run(s.id, s.path, event="UserPromptSubmit")[0], event="UserPromptSubmit")
    check("main after /compact: prep file re-injected, no systemMessage",
          isinstance(r, tuple) and r[0] is None and "ship nothing remote" in r[1], repr(r)[:300])
    s.add(assistant(301_000))
    r = parse(env.run(s.id, s.path)[0])
    check("main 300k fires again after re-arm", isinstance(r, tuple) and main_prep in r[1])

    # Drop below the lowest band without a boundary also re-arms.
    c = Session(root, "drop")
    c.add(assistant(360_000))
    env.run(c.id, c.path)
    c.add(assistant(90_000))
    env.run(c.id, c.path)
    c.add(assistant(355_000))
    r = parse(env.run(c.id, c.path)[0])
    check("drop below 300k by usage re-arms", isinstance(r, tuple) and "/compact" in (r[0] or ""), repr(r)[:200])

    # Main transcript ignores sidechain records and zero-usage replies.
    side = Session(root, "sidechain")
    side.add(assistant(100_000), assistant(900_000, sidechain=True))
    check("main: sidechain record ignored", parse(env.run(side.id, side.path)[0]) is None)
    z = Session(root, "zero")
    z.add(assistant(400_000), json.dumps({"type": "assistant", "message": {"usage": {
        "input_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}}))
    check("zero-usage reply skipped, previous reading used", isinstance(parse(env.run(z.id, z.path)[0]), tuple))
    g = Session(root, "agentflag")
    g.add(assistant(310_000))
    r = parse(env.run(g.id, g.path, extra={"agent_type": "my-main-agent"})[0])
    check("--agent main session (agent_type, no agent_id) gets the main messages",
          isinstance(r, tuple) and r[0] is not None)

    # SessionStart prune.
    old_prep = os.path.join(env.prep, "sess-old.md")
    old_inj = os.path.join(env.prep, "sess-old2.injected.md")
    old_state = os.path.join(env.data, "sess-old.json")
    for path in (old_prep, old_inj, old_state):
        open(path, "w").close()
        past = time.time() - 15 * 24 * 3600
        os.utime(path, (past, past))
    proc, _ = env.run("sess-new", s.path, event="SessionStart", extra={"source": "startup"})
    check("SessionStart: silent", proc.returncode == 0 and proc.stdout == "" and proc.stderr == "")
    check("SessionStart prunes prep and state files older than 14 days",
          not any(os.path.exists(p) for p in (old_prep, old_inj, old_state))
          and os.path.isfile(main_prep[:-3] + ".injected.md"))

    # Fail open.
    bad = Session(root, "corrupt")
    with open(bad.path, "wb") as handle:
        handle.write(os.urandom(300_000) + b'\n{"type":"assistant", broken\n\xff\xfe')
    proc, _ = env.run(bad.id, bad.path)
    check("corrupt transcript fails open", proc.returncode == 0 and proc.stdout == "" and proc.stderr == "")
    proc, _ = env.run("sess-missing", os.path.join(root, "nope.jsonl"))
    check("missing transcript fails open", proc.returncode == 0 and proc.stdout == "")
    proc, _ = env.run("x", "y", raw="not json {")
    check("malformed stdin fails open", proc.returncode == 0 and proc.stdout == "" and proc.stderr == "")
    u = Session(root, "unwritable")
    u.add(assistant(310_000))
    proc, _ = env.run(u.id, u.path, env_extra={"CLAUDE_PLUGIN_DATA": "/dev/null/cannot-create"})
    check("unwritable state dir fails open", proc.returncode == 0 and proc.stdout == "" and proc.stderr == "")
    proc, _ = env.run(u.id, u.path, env_extra={"CONTEXT_NUDGE_PREP_DIR": "/dev/null/cannot-create"})
    check("unwritable prep dir fails open", proc.returncode == 0 and proc.stdout == "" and proc.stderr == "")

    # Configuration.
    e = Session(root, "envbands")
    e.add(assistant(150_000))
    r = parse(env.run(e.id, e.path, env_extra={"CONTEXT_NUDGE_BANDS": "100k,200k"})[0])
    check("CONTEXT_NUDGE_BANDS custom bands honoured", isinstance(r, tuple) and "~100k" in (r[0] or ""), repr(r)[:200])

    # Timing.
    normal = sorted(TIMINGS)
    median = normal[len(normal) // 2]
    check("median hook run under 50 ms", median < 0.05, "median %.1f ms over %d runs" % (median * 1000, len(normal)))

    big = Session(root, "big")
    filler = user_line("x" * 4000)
    with open(big.path, "w") as handle:
        while handle.tell() < 50 * 1024 * 1024:
            handle.write(filler + "\n")
            handle.write(assistant(200_000) + "\n")
        handle.write(assistant(370_000) + "\n")
    size_mb = os.path.getsize(big.path) / 1024 / 1024
    proc, elapsed = env.run(big.id, big.path)
    check("50 MB transcript, reply at tail: fires and is fast",
          isinstance(parse(proc), tuple) and elapsed < 3, "%.1f MB in %.3f s" % (size_mb, elapsed))

    worst = Session(root, "worst")
    with open(worst.path, "w") as handle:
        while handle.tell() < 45 * 1024 * 1024:
            handle.write(filler + "\n")
            handle.write(assistant(400_000) + "\n")
        handle.write(user_line("y" * (5 * 1024 * 1024)) + "\n")
    size_mb = os.path.getsize(worst.path) / 1024 / 1024
    proc, elapsed = env.run(worst.id, worst.path)
    check("50 MB transcript, 5 MB tool output after last reply: gives up silently and fast",
          proc.returncode == 0 and proc.stdout == "" and elapsed < 3, "%.1f MB in %.3f s" % (size_mb, elapsed))

    print("\n%d failed" % len(FAILURES) if FAILURES else "\nall checks passed")
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()
