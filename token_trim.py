#!/usr/bin/env python3
"""token_trim: report and trim unused tool schemas / skill descriptions.

Every Claude Code request carries the schema of every loaded tool and a
name+description line for every skill. This script measures both from your own
transcripts (~/.claude/projects/**/*.jsonl), compares with what you actually
called, and can deny unused heavy tools and set unused skills to "name-only".

Idea credit: JayPokale/Chisle `tools` (MIT). Clean-room rewrite, zero deps.

Usage:
  token_trim.py report  [--days 30] [--top 25] [--json]
  token_trim.py trim    [--days 30] [--scope project|global] [--apply] [--keep A,B]
  token_trim.py restore [--scope project|global] [--apply]

Safety:
  - report never writes. trim/restore are dry-run unless --apply.
  - Core tools, MCP tools and anything in --keep are never denied.
  - Only entries this script added are removed by restore (tracked in a record file).
  - Settings are backed up once to <file>.tokentrim-bak before the first write.
  - project scope -> ./.claude/settings.local.json ; global -> ~/.claude/settings.json
"""
import argparse, hashlib, json, os, re, shutil, sys, time
from pathlib import Path

CORE = {
    "Bash", "Read", "Edit", "MultiEdit", "Write", "Glob", "Grep", "NotebookEdit", "Agent",
    "Task", "TodoWrite", "ToolSearch", "Skill", "WebFetch", "WebSearch", "AskUserQuestion",
    "EnterPlanMode", "ExitPlanMode", "Monitor", "TaskStop", "SendMessage", "ListAgents", "LSP",
}
MIN_TOOL_CHARS = 2000   # smaller schemas are not worth a deny entry
MIN_SKILL_CHARS = 150
CHARS_PER_TOKEN_TOOL = 5.5   # measured by Chisle on tool schemas
CHARS_PER_TOKEN_SKILL = 4
RECORD = ".tokentrim-record.json"


def claude_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def transcripts(since):
    root = claude_dir() / "projects"
    for p in root.rglob("*.jsonl"):
        try:
            if p.stat().st_mtime >= since:
                yield p
        except OSError:
            pass


def scan(files):
    size, calls, skill_size, skill_calls, listings = {}, {}, {}, {}, []
    requests = 0
    n_files = 0
    for f in files:
        n_files += 1
        seen = set()
        try:
            lines = f.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if '"prompt_snapshot"' in line:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                for t in (e.get("attachment") or {}).get("tools") or []:
                    if isinstance(t, dict) and t.get("name"):
                        n = len(json.dumps(t, separators=(",", ":")))
                        size[t["name"]] = max(size.get(t["name"], 0), n)
            elif '"skill_listing"' in line:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                content = (e.get("attachment") or {}).get("content")
                if not isinstance(content, str):
                    continue
                entries = []
                for item in content.split("\n- "):
                    item = item[2:] if item.startswith("- ") else item
                    k = item.find(": ")
                    if k < 0:
                        continue
                    name, desc = item[:k], item[k + 2:]
                    skill_size[name] = max(skill_size.get(name, 0), len(desc))
                    entries.append((name, len(desc)))
                if entries:
                    listings.append(entries)
            elif '"tool_use"' in line or '"usage"' in line:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                m = e.get("message")
                if not isinstance(m, dict) or e.get("type") != "assistant":
                    continue
                if m.get("usage") and m.get("id") and m["id"] not in seen:
                    seen.add(m["id"])
                    requests += 1
                content = m.get("content")
                for b in content if isinstance(content, list) else []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        calls[b.get("name")] = calls.get(b.get("name"), 0) + 1
                        s = (b.get("input") or {}).get("skill") if b.get("name") == "Skill" else None
                        if s:
                            skill_calls[s] = skill_calls.get(s, 0) + 1
    return size, calls, skill_size, skill_calls, listings, requests, n_files


def build_plan(days, keep):
    files = transcripts(time.time() - days * 86400)
    size, calls, skill_size, skill_calls, listings, requests, n = scan(files)
    tools = sorted(
        (
            {
                "name": name, "tokens": round(chars / CHARS_PER_TOKEN_TOOL), "calls": calls.get(name, 0),
                "trim": name not in CORE and name not in keep and not calls.get(name),
            }
            for name, chars in size.items()
            if chars >= MIN_TOOL_CHARS and not name.startswith("mcp__")
        ),
        key=lambda r: -r["tokens"],
    )
    skills = sorted(
        (
            {
                "name": name, "tokens": round(chars / CHARS_PER_TOKEN_SKILL),
                "calls": skill_calls.get(name, 0),
                "trim": name not in keep and not skill_calls.get(name),
            }
            for name, chars in skill_size.items()
            if chars >= MIN_SKILL_CHARS
        ),
        key=lambda r: -r["tokens"],
    )
    # A session lists only its own project's skills: average over real listings.
    unused = {s["name"] for s in skills if s["trim"]}
    skill_saving = (
        round(sum(c for l in listings for nm, c in l if nm in unused) / len(listings) / CHARS_PER_TOKEN_SKILL)
        if listings else 0
    )
    tool_saving = sum(t["tokens"] for t in tools if t["trim"])
    return {"sessions": n, "requests": requests, "tools": tools, "skills": skills,
            "tool_saving": tool_saving, "skill_saving": skill_saving}


def settings_path(scope, project=None):
    if scope == "project":
        return Path(project or Path.cwd()) / ".claude" / "settings.local.json"
    return claude_dir() / "settings.json"


# The record of what WE blocked lives in Sai Matrix's own data dir, never inside a project:
# a repository could otherwise ship a forged record and make `restore` strip deny rules.
DATA = Path(os.environ.get("SAI_MATRIX_DATA") or Path(__file__).resolve().parent / ".data")
TOOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
SKILL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


def record_path(sp):
    key = hashlib.sha1(str(Path(sp).resolve()).encode()).hexdigest()[:16]
    return DATA / "trim-records" / f"{key}.json"


def load_record(sp):
    rp = record_path(sp)
    rec = load_json(rp, None)
    if rec is None:
        rec = {"tools": [], "skills": []}
        old = Path(sp).parent / RECORD            # legacy location: trusted only for the user-level file
        if Path(sp) == claude_dir() / "settings.json" and old.is_file():
            rec = load_json(old, rec)
            save_record(sp, rec)
            old.unlink(missing_ok=True)
    return {"tools": [t for t in rec.get("tools", []) if isinstance(t, str) and TOOL_RE.match(t)],
            "skills": [x for x in rec.get("skills", []) if isinstance(x, str) and SKILL_RE.match(x)]}


def save_record(sp, rec):
    rp = record_path(sp)
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(json.dumps(rec, indent=2) + "\n")


def load_json(p, default):
    try:
        return json.loads(p.read_text())
    except FileNotFoundError:
        return default
    except ValueError:
        sys.exit(f"{p} is not valid JSON; refusing to touch it")


def save_settings(sp, data):
    sp.parent.mkdir(parents=True, exist_ok=True)
    bak = sp.with_name(sp.name + ".tokentrim-bak")
    if sp.exists() and not bak.exists():
        shutil.copy2(sp, bak)
    tmp = sp.with_name(sp.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, sp)


def api_trim(project, days=30, keep=(), apply=False, tools_only=False, plan=None):
    """Programmatic trim for one project (used by skill_switchboard). Returns a summary dict."""
    p = plan or build_plan(days, set(keep))
    sp = settings_path("project" if project else "global", project)
    data = load_json(sp, {})
    deny = (data.get("permissions") or {}).get("deny", [])
    over = data.get("skillOverrides") or {}
    keep = set(keep)
    new_tools = [t["name"] for t in p["tools"] if t["trim"] and t["name"] not in keep and t["name"] not in deny]
    new_skills = [] if tools_only else [s["name"] for s in p["skills"]
                                        if s["trim"] and s["name"] not in keep and s["name"] not in over]
    if apply and (new_tools or new_skills):
        if new_tools:
            data.setdefault("permissions", {})["deny"] = deny + new_tools
        if new_skills:
            data["skillOverrides"] = {**over, **{n: "name-only" for n in new_skills}}
        save_settings(sp, data)
        rec = load_record(sp)
        rec["tools"] = sorted(set(rec["tools"]) | set(new_tools))
        rec["skills"] = sorted(set(rec["skills"]) | set(new_skills))
        save_record(sp, rec)
    return {"file": str(sp), "tools": new_tools, "skills": new_skills, "applied": bool(apply)}


def api_restore(project, apply=False):
    sp = settings_path("project" if project else "global", project)
    rec = load_record(sp)
    data = load_json(sp, {})
    deny = (data.get("permissions") or {}).get("deny", [])
    over = data.get("skillOverrides") or {}
    rm_t = [t for t in rec["tools"] if t in deny]
    rm_s = [s for s in rec["skills"] if over.get(s) == "name-only"]
    if apply and (rm_t or rm_s or rec["tools"] or rec["skills"]):
        if "permissions" in data:
            data["permissions"]["deny"] = [d for d in deny if d not in rec["tools"]]
            if not data["permissions"]["deny"]:
                del data["permissions"]["deny"]
            if not data["permissions"]:
                del data["permissions"]
        for s in rm_s:
            data["skillOverrides"].pop(s, None)
        if "skillOverrides" in data and not data["skillOverrides"]:
            del data["skillOverrides"]
        save_settings(sp, data)
        record_path(sp).unlink(missing_ok=True)
    return {"file": str(sp), "tools": rm_t, "skills": rm_s, "applied": bool(apply)}


def current_state(project):
    """What this scope already trimmed (from the record file)."""
    sp = settings_path("project" if project else "global", project)
    return load_record(sp)


def cmd_report(a):
    p = build_plan(a.days, set())
    if a.json:
        print(json.dumps(p, indent=1))
        return
    print(f"Last {a.days}d: {p['sessions']} sessions, {p['requests']} requests\n")
    print("TOOLS (schema tokens sent every request)")
    for t in p["tools"][: a.top]:
        tag = "deny candidate" if t["trim"] else ("core" if t["name"] in CORE else "used")
        print(f"  {t['name']:<28}~{t['tokens']:>6} tok {t['calls']:>6} calls  {tag}")
    unused = [s for s in p["skills"] if s["trim"]]
    print(f"\nSKILLS: {len(p['skills'])} listed, {len(unused)} never invoked (top {a.top} by size)")
    for s in p["skills"][: a.top]:
        print(f"  {s['name']:<40}~{s['tokens']:>4} tok {s['calls']:>4} calls  {'name-only candidate' if s['trim'] else 'used'}")
    print(f"\nPotential saving per request: ~{p['tool_saving'] + p['skill_saving']:,} tokens "
          f"(tools {p['tool_saving']:,} + skills {p['skill_saving']:,})")
    print("Report only. Nothing changed. Use `trim` (dry-run) then `trim --apply`.")


def cmd_trim(a):
    keep = {x.strip() for x in (a.keep or "").split(",") if x.strip()}
    p = build_plan(a.days, keep)
    sp = settings_path(a.scope)
    data = load_json(sp, {})
    deny = (data.get("permissions") or {}).get("deny", [])
    over = data.get("skillOverrides") or {}
    new_tools = [t["name"] for t in p["tools"] if t["trim"] and t["name"] not in deny]
    new_skills = [s["name"] for s in p["skills"] if s["trim"] and s["name"] not in over]  # never override user's own
    print(f"Target: {sp}")
    print(f"deny {len(new_tools)} tools: {', '.join(new_tools) or '-'}")
    print(f"name-only {len(new_skills)} skills (first 15): {', '.join(new_skills[:15]) or '-'}")
    print(f"Estimated saving: ~{p['tool_saving'] + p['skill_saving']:,} tokens/request")
    if not a.apply:
        print("DRY-RUN. Re-run with --apply to write.")
        return
    if new_tools:
        data.setdefault("permissions", {})["deny"] = deny + new_tools
    if new_skills:
        data["skillOverrides"] = {**over, **{n: "name-only" for n in new_skills}}
    save_settings(sp, data)
    rec = load_record(sp)
    rec["tools"] = sorted(set(rec["tools"]) | set(new_tools))
    rec["skills"] = sorted(set(rec["skills"]) | set(new_skills))
    save_record(sp, rec)
    print(f"Applied. Undo: token_trim.py restore --scope {a.scope} --apply")


def cmd_restore(a):
    sp = settings_path(a.scope)
    rp = record_path(sp)
    rec = load_record(sp)
    data = load_json(sp, {})
    deny = (data.get("permissions") or {}).get("deny", [])
    over = data.get("skillOverrides") or {}
    rm_t = [t for t in rec["tools"] if t in deny]
    rm_s = [s for s in rec["skills"] if over.get(s) == "name-only"]
    print(f"Target: {sp}\nrestore {len(rm_t)} tools, {len(rm_s)} skills")
    if not a.apply:
        print("DRY-RUN. Re-run with --apply.")
        return
    if "permissions" in data:
        data["permissions"]["deny"] = [d for d in deny if d not in rec["tools"]]
        if not data["permissions"]["deny"]:
            del data["permissions"]["deny"]
        if not data["permissions"]:
            del data["permissions"]
    if "skillOverrides" in data:
        for s in rm_s:
            del data["skillOverrides"][s]
        if not data["skillOverrides"]:
            del data["skillOverrides"]
    save_settings(sp, data)
    rp.unlink(missing_ok=True)
    print("Restored.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("report", cmd_report), ("trim", cmd_trim), ("restore", cmd_restore)):
        s = sub.add_parser(name)
        s.set_defaults(fn=fn)
        s.add_argument("--days", type=int, default=30)
        s.add_argument("--top", type=int, default=25)
        s.add_argument("--json", action="store_true")
        s.add_argument("--scope", choices=["project", "global"], default="project")
        s.add_argument("--apply", action="store_true")
        s.add_argument("--keep", default="")
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
