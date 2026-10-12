"""AI Clutter: local models, plus caches and logs from AI tools."""
import os
import subprocess
import time

from .. import paths
from ..scan import dir_size
from . import Tool, register, remove_items
from .devjunk import _cmd_item, _run, _which, act_mixed

H = paths.HOME
AS = os.path.join(H, "Library", "Application Support")

# path, label, safety, selected, note
CACHES = [
    (os.path.join(H, "Library", "Caches", "com.openai.chat"), "ChatGPT cache", "safe", True, ""),
    (os.path.join(H, "Library", "Caches", "com.anthropic.claudefordesktop"), "Claude cache", "safe", True, ""),
    (os.path.join(AS, "Claude", "Cache"), "Claude web cache", "safe", True, ""),
    (os.path.join(AS, "Claude", "Code Cache"), "Claude code cache", "safe", True, ""),
    (os.path.join(H, "Library", "Logs", "Claude"), "Claude logs", "safe", True, ""),
    (os.path.join(AS, "Cursor", "Cache"), "Cursor cache", "safe", True, ""),
    (os.path.join(AS, "Cursor", "CachedData"), "Cursor cached data", "safe", True, ""),
    (os.path.join(AS, "Cursor", "logs"), "Cursor logs", "safe", True, ""),
    (os.path.join(H, ".gemini", "tmp"), "Gemini CLI temp files", "safe", True, ""),
    (os.path.join(H, ".codex", "log"), "Codex logs", "safe", True, ""),
    (os.path.join(H, ".cache", "torch"), "PyTorch hub cache", "review", False, ""),
    (os.path.join(H, ".cache", "whisper"), "Whisper models", "review", False, ""),
    (os.path.join(H, ".cache", "huggingface", "xet"), "Hugging Face transfer cache", "safe", True, ""),
    (os.path.join(H, ".cache", "huggingface", "datasets"), "Hugging Face datasets", "review", False, ""),
    (os.path.join(AS, "Claude", "vm_bundles"), "Claude desktop VM images", "review", False,
     "Used by Claude's computer and folder features; re-downloads if removed"),
]


def _item(p, label, safety="review", selected=False, note="", detail=None):
    if not os.path.isdir(p) or os.path.islink(p):
        return None
    size = dir_size(p)[0]
    if size < 5 * 1024 * 1024:
        return None
    try:
        mt = os.stat(p).st_mtime
        age = "changed %d days ago" % ((time.time() - mt) // 86400)
    except OSError:
        age = ""
    return {"id": p, "path": p, "label": label, "kind": "path", "safety": safety,
            "selected": selected, "bytes": size, "note": note,
            "detail": detail or "%s · %s" % (p.replace(H, "~"), age)}


def _subdirs(base, label_fn, depth=1):
    out = []
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return out
    for n in names:
        p = os.path.join(base, n)
        if n.startswith(".") or not os.path.isdir(p):
            continue
        if depth > 1:
            out += _subdirs(p, lambda m, n=n: label_fn("%s/%s" % (n, m)), depth - 1)
        else:
            it = _item(p, label_fn(n))
            if it:
                out.append(it)
    return out


def ollama_items(progress):
    progress("Ollama models...")
    ollama = _which("ollama")
    base = os.path.join(H, ".ollama", "models")
    if ollama:
        code, so, _ = _run([ollama, "list"], 20)
        if code == 0:
            out = []
            for line in so.splitlines()[1:]:
                parts = line.split()
                if len(parts) < 4:
                    continue
                name = parts[0]
                size = 0
                try:
                    size = int(float(parts[2]) * {"KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}[parts[3]])
                except (KeyError, ValueError):
                    pass
                out.append(_cmd_item("ollama:" + name, name, [ollama, "rm", name], size,
                                     selected=False, safety="review",
                                     detail="ollama rm %s · modified %s" % (name, " ".join(parts[4:]))))
            return out
    it = _item(base, "Ollama models (all)")
    return [it] if it else []


@register
class AIClutter(Tool):
    id = "ai"
    name = "AI Clutter"
    blurb = "Local models you no longer use, and caches and logs from AI tools."
    group = "Cleanup"
    icon = "spark"

    def scan(self, ctx, progress):
        groups = []
        models = ollama_items(progress)
        progress("Hugging Face models...")
        models += _subdirs(os.path.join(H, ".cache", "huggingface", "hub"),
                           lambda n: "HF " + n.replace("models--", "").replace("datasets--", "dataset ").replace("--", "/"))
        progress("LM Studio models...")
        for base in (os.path.join(H, ".lmstudio", "models"),
                     os.path.join(H, ".cache", "lm-studio", "models")):
            models += _subdirs(base, lambda n: "LM Studio " + n, depth=2)
        if models:
            models.sort(key=lambda it: -(it["bytes"] or 0))
            groups.append({"id": "ai:models", "label": "Local models", "children": models})
        progress("AI tool caches...")
        caches = [it for it in (_item(*c) for c in CACHES) if it]
        if caches:
            caches.sort(key=lambda it: -(it["bytes"] or 0))
            groups.append({"id": "ai:caches", "label": "Caches, logs and downloads", "children": caches})
        return groups

    def act(self, ctx, items, progress):
        return act_mixed(ctx, items, progress)
