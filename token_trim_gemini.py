#!/usr/bin/env python3
"""Manual tool exclusion for Gemini CLI (settings.json -> tools.exclude).

Gemini CLI keeps no per-tool usage history we can trust, so there is NO automatic
"unused" detection here: you pick tools yourself. Entries are plain strings in
`tools.exclude` of ~/.gemini/settings.json (Gemini restarts to apply them).
The settings file is backed up once to settings.json.saimatrix-bak before the first write.
"""
import json, os, re
from pathlib import Path

SETTINGS = Path.home() / ".gemini" / "settings.json"
NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
# Built-in tool names as of gemini-cli 0.64 (hint = what you lose if excluded).
KNOWN = {
    "read_file": "reading files (core)", "write_file": "writing files (core)", "replace": "editing files (core)",
    "run_shell_command": "running commands (core)", "glob": "find files by pattern",
    "grep_search": "search file contents", "list_directory": "list a folder",
    "read_many_files": "read several files at once", "google_web_search": "Google web search",
    "web_fetch": "fetch a URL", "write_todos": "task list", "activate_skill": "load a skill",
    "ask_user": "ask you questions", "invoke_agent": "sub-agents", "enter_plan_mode": "plan mode",
    "exit_plan_mode": "plan mode", "complete_task": "task completion", "update_topic": "topic updates",
    "get_internal_docs": "Gemini CLI docs", "take_snapshot": "browser snapshot",
    "list_mcp_resources": "MCP resources", "read_mcp_resource": "MCP resources",
    "tracker_add_dependency": "task tracker", "tracker_create_task": "task tracker",
    "tracker_get_task": "task tracker", "tracker_list_tasks": "task tracker",
    "tracker_update_task": "task tracker", "tracker_visualize": "task tracker",
}
CORE = {"read_file", "write_file", "replace", "run_shell_command"}


def _load():
    try:
        return json.loads(SETTINGS.read_text())
    except FileNotFoundError:
        return {}
    except ValueError:
        raise ValueError(f"{SETTINGS} is not valid JSON; refusing to touch it")


def _excluded(d):
    ex = (d.get("tools") or {}).get("exclude")
    return list(ex) if isinstance(ex, list) else []


def state():
    ex = set(_excluded(_load()))
    rows = [{"name": n, "hint": h, "core": n in CORE, "excluded": n in ex} for n, h in KNOWN.items()]
    rows += [{"name": n, "hint": "custom", "core": False, "excluded": True} for n in sorted(ex - set(KNOWN))]
    return {"agent": "gemini", "manual": True, "file": str(SETTINGS), "rows": rows}


def apply(names, exclude, do_apply=False):
    names = [n for n in names if isinstance(n, str) and NAME_RE.match(n)]
    d = _load()
    cur = _excluded(d)
    new = sorted(set(cur) | set(names)) if exclude else [c for c in cur if c not in names]
    if do_apply and new != cur:
        d.setdefault("tools", {})
        if new:
            d["tools"]["exclude"] = new
        else:
            d["tools"].pop("exclude", None)
            if not d["tools"]:
                del d["tools"]
        bak = SETTINGS.with_name(SETTINGS.name + ".saimatrix-bak")
        if SETTINGS.exists() and not bak.exists():
            bak.write_bytes(SETTINGS.read_bytes())
        tmp = SETTINGS.with_name(SETTINGS.name + ".tmp")
        tmp.write_text(json.dumps(d, indent=2) + "\n")
        os.replace(tmp, SETTINGS)
    return {"toolsets": names, "excluded_after": new, "applied": bool(do_apply and new != cur)}
