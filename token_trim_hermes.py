#!/usr/bin/env python3
"""token_trim for Hermes: report/disable unused TOOLSETS (cli platform).

Evidence:
  sizes   -> Hermes' own venv builds the real tool schemas (model_tools.get_tool_definitions)
  usage   -> ~/.hermes/state.db, messages.tool_name (read-only)
  disable -> official `hermes tools disable|enable <toolset>` (reversible)
Records what WE disabled in ~/.hermes/.tokentrim-record.json so restore never touches your own choices.
"""
import json, os, sqlite3, subprocess, sys, time
from pathlib import Path

HOME = Path.home()
HDIR = HOME / ".hermes"
VENV_PY = HDIR / "hermes-agent" / "venv" / "bin" / "python"
RECORD = HDIR / ".tokentrim-record.json"
CHARS_PER_TOKEN = 4          # estimate; schemas are JSON-heavy
KEEP = {"terminal", "file", "skills", "clarify"}   # never auto-suggested
MIN_CHARS = 600

_SCHEMA_SNIPPET = r'''
import json, sys
sys.path.insert(0, ".")
import toolsets
from model_tools import get_tool_definitions
enabled = json.loads(sys.argv[1])
out = {}
for ts in enabled:
    try:
        defs = get_tool_definitions(enabled_toolsets=[ts], quiet_mode=True)
    except Exception:
        continue
    mine = set(toolsets.resolve_toolset(ts))      # only this toolset's own tools (not shared meta tools)
    out[ts] = {d["function"]["name"]: len(json.dumps(d)) for d in defs if d["function"]["name"] in mine}
print(json.dumps(out))
'''


def _run(args, timeout=60):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def enabled_toolsets():
    """Parse `hermes tools list` -> (enabled builtin toolsets, mcp servers)."""
    r = _run(["hermes", "tools", "list"])
    ts, section = [], None
    for line in r.stdout.splitlines():
        if line.startswith("Built-in toolsets"):
            section = "b"
        elif line.startswith(("Plugin toolsets", "MCP servers")):
            section = None
        elif section == "b" and "enabled" in line and "disabled" not in line:
            parts = line.split()
            ts.append(parts[2])
    return ts


def usage(days):
    since = time.time() - days * 86400
    db = HDIR / "state.db"
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = c.execute("select tool_name, count(*) from messages where tool_name is not null and timestamp>=? "
                     "group by 1", (since,)).fetchall()
    n_sess = c.execute("select count(distinct session_id) from messages where timestamp>=?", (since,)).fetchone()[0]
    return {k: v for k, v in rows}, n_sess


def report(days=30):
    if not VENV_PY.exists():
        return {"error": "Hermes venv not found"}
    rec = load_record()
    ts = enabled_toolsets()
    ts += [d for d in rec["disabled"] if d not in ts]      # keep what WE disabled visible (size still measurable)
    r = _run([str(VENV_PY), "-c", _SCHEMA_SNIPPET, json.dumps(ts)], 120)
    r_cwd = subprocess.run([str(VENV_PY), "-c", _SCHEMA_SNIPPET, json.dumps(ts)], capture_output=True, text=True,
                           timeout=120, cwd=str(HDIR / "hermes-agent"))
    try:
        sizes = json.loads(r_cwd.stdout.strip().splitlines()[-1])
    except Exception:
        return {"error": "could not read Hermes schemas: " + (r_cwd.stderr or r.stderr)[-200:]}
    calls, n_sess = usage(days)
    rows = []
    for name, tools in sizes.items():
        chars = sum(tools.values())
        used = sum(calls.get(t, 0) for t in tools)
        rows.append({"name": name, "tokens": round(chars / CHARS_PER_TOKEN), "tools": sorted(tools),
                     "calls": used, "trim": name not in KEEP and used == 0 and chars >= MIN_CHARS,
                     "ours": name in rec["disabled"]})
    rows.sort(key=lambda r: -r["tokens"])
    return {"agent": "hermes", "sessions": n_sess, "days": days, "rows": rows,
            "saving": sum(r["tokens"] for r in rows if r["trim"] and not r["ours"]),
            "saved": sum(r["tokens"] for r in rows if r["ours"]), "record": rec}


def load_record():
    try:
        return json.loads(RECORD.read_text())
    except Exception:
        return {"disabled": []}


def apply(toolsets, do_apply=False):
    """Disable the given toolsets (cli platform). Returns what would/did change."""
    rep = report()
    ok = {r["name"] for r in rep.get("rows", []) if r["trim"] and not r["ours"]}
    todo = [t for t in toolsets if t in ok]          # only toolsets that are genuinely unused
    if do_apply and todo:
        r = _run(["hermes", "tools", "disable", *todo])
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip()[-200:] or "hermes tools disable failed")
        rec = load_record()
        rec["disabled"] = sorted(set(rec["disabled"]) | set(todo))
        RECORD.write_text(json.dumps(rec, indent=2) + "\n")
    return {"toolsets": todo, "skipped": [t for t in toolsets if t not in ok], "applied": bool(do_apply and todo)}


def restore(do_apply=False):
    rec = load_record()
    if do_apply and rec["disabled"]:
        r = _run(["hermes", "tools", "enable", *rec["disabled"]])
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip()[-200:] or "hermes tools enable failed")
        RECORD.unlink(missing_ok=True)
    return {"toolsets": rec["disabled"], "applied": bool(do_apply and rec["disabled"])}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "report":
        rep = report()
        print(json.dumps(rep, indent=1) if "--json" in sys.argv else
              f"Hermes, {rep.get('sessions')} sessions\n" + "\n".join(
                  f"  {r['name']:<16}~{r['tokens']:>5} tok {r['calls']:>6} calls  {'TRIM' if r['trim'] else ''}"
                  for r in rep.get("rows", [])) + f"\nsaving ~{rep.get('saving')} tok/request")
