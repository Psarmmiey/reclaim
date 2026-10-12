"""Ask Claude what unfamiliar space hogs actually are.

Hard rule: the classifier can only ever make a target *more* protected, or
explain one. It can never promote something to `safe`, because `safe` is the
one level that authorises automatic deletion, and that authority stays with the
deterministic rules in rules.py.
"""
import json
import os
import re
import shutil
import subprocess

from . import rules, store

PROMPT = """You are helping a macOS developer understand what is filling their disk.

For each path below, say what it most likely is and how risky deleting it would be.

Reply with ONLY a JSON array, one object per path, in the same order:
[{"path": "...", "safety": "review"|"keep", "reason": "<12 words or fewer>"}]

safety meanings:
  review - regenerable or replaceable, but the user should look before deleting
  keep   - personal data, credentials, or something an app needs to work

Never answer "safe". Be concrete about what the folder belongs to.

Paths:
%s
"""


def _sample(path, limit=6):
    """A few child names, so the model has something to go on."""
    try:
        names = sorted(os.listdir(path))[:limit]
        return ", ".join(names)
    except OSError:
        return ""


def _describe(t):
    kind = "file" if t.get("kind") == "file" else "folder"
    gb = t["bytes"] / (1024 ** 3)
    line = "- %s (%s, %.1f GB)" % (t["path"], kind, gb)
    if kind == "folder":
        sample = _sample(t["path"])
        if sample:
            line += "\n  contains: " + sample
    return line


def claude_available(cmd="claude"):
    return shutil.which(cmd) is not None


def _extract_json(text):
    text = text.strip()
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def ask_claude(targets, cmd="claude", timeout=120):
    """Returns {path: (safety, reason)}. Empty dict on any failure."""
    if not targets or not claude_available(cmd):
        return {}
    prompt = PROMPT % "\n".join(_describe(t) for t in targets)
    try:
        proc = subprocess.run(
            [cmd, "-p", prompt],
            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode != 0:
        return {}
    data = _extract_json(proc.stdout)
    if not isinstance(data, list):
        return {}

    out = {}
    valid = {rules.REVIEW, rules.KEEP}
    for item in data:
        if not isinstance(item, dict):
            continue
        p = item.get("path")
        s = str(item.get("safety", "")).lower().strip()
        if not p or s not in valid:
            continue
        out[p] = (s, str(item.get("reason", ""))[:160])
    return out


def classify(con, scan_id, targets, cfg):
    """Enrich targets that the rules could not confidently name.

    Only `review` targets are ever sent out, and only their path, size and a
    handful of child names. Nothing is read from inside the files.
    """
    if cfg.get("classifier") != "claude":
        return 0

    pending = []
    for t in targets:
        if t["safety"] != rules.REVIEW:
            continue
        cached = store.get_verdict(con, t["path"])
        if cached:
            if rules.SAFETY_RANK[cached["safety"]] > rules.SAFETY_RANK[t["safety"]]:
                t["safety"] = cached["safety"]
            t["reason"] = cached["reason"]
            t["source"] = cached["source"]
            store.update_target_safety(con, scan_id, t["path"], t["safety"],
                                       t["reason"], t["source"])
            continue
        pending.append(t)

    if not pending:
        return 0

    pending = sorted(pending, key=lambda t: -t["bytes"])[:40]
    verdicts = ask_claude(pending, cfg.get("claude_command", "claude"),
                          int(cfg.get("claude_timeout_seconds", 120)))
    n = 0
    for t in pending:
        v = verdicts.get(t["path"])
        if not v:
            continue
        safety, reason = v
        # Only ever tighten.
        if rules.SAFETY_RANK[safety] < rules.SAFETY_RANK[t["safety"]]:
            safety = t["safety"]
        t["safety"] = safety
        t["reason"] = reason
        t["source"] = "claude"
        store.put_verdict(con, t["path"], safety, reason, "claude")
        store.update_target_safety(con, scan_id, t["path"], safety, reason,
                                   "claude")
        n += 1
    return n
