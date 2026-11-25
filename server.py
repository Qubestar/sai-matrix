#!/usr/bin/env python3
"""Sai Matrix - local skill & MCP switchboard for AI coding agents (stdlib only, no dependencies).

Library  = ~/.agents/skills            (all installed skills, never moved or deleted)
Global   = symlinks in ~/.claude/skills  (loaded in EVERY session)
Project  = symlinks in <project>/.claude/skills (loaded only in that project)
Skills not pinned anywhere are listed in the generated `skill-index` skill, so agents
can still discover them and Read their SKILL.md on demand.

MCP: definitions are kept in ~/.claude/mcp-library.json (0600). Global = `claude mcp add/remove -s user`,
project = <project>/.mcp.json. MCP secrets are never sent to the browser.

Run: python3 server.py   ->  http://127.0.0.1:8765

Optional config.json next to this file: {"locked": [project paths], "hide_parts": [path fragments]}
"""
import json, os, re, shutil, subprocess, sys, tempfile, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

HOME = Path.home()
LIB = HOME / ".agents" / "skills"
GLOBAL = HOME / ".claude" / "skills"
CLAUDE_JSON = HOME / ".claude.json"
MCP_LIB = HOME / ".claude" / "mcp-library.json"
HERE = Path(__file__).resolve().parent


def _split_data_arg(argv):
    for i, a in enumerate(argv):
        if a == "--data-dir" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--data-dir="):
            return a.split("=", 1)[1]
    return None


_DATA_ARG = _split_data_arg(sys.argv[1:])
if _DATA_ARG:
    # token_trim.py reads the same env var, so the in-process import uses one dir.
    os.environ["SAI_MATRIX_DATA"] = str(Path(_DATA_ARG).expanduser().resolve())
DATA = (Path(os.environ["SAI_MATRIX_DATA"]).expanduser().resolve()
        if os.environ.get("SAI_MATRIX_DATA") else HERE / ".data")
DATA.mkdir(parents=True, exist_ok=True)   # local state (journal), never published
LOG = DATA / "changes.jsonl"
PORT = int(os.environ.get("SAI_MATRIX_PORT") or 8767)
INDEX_SKILL = "skill-index"
# Projects the tool may only read, never write to (production checkouts).
def _cfg():
    p = DATA / "config.json"
    if not p.is_file():
        p = HERE / "config.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


LOCKED = {str(Path(p).expanduser()) for p in _cfg().get("locked", [])}
HIDE_PARTS = ("/.claude/worktrees/", "/private/tmp") + tuple(_cfg().get("hide_parts", []))
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def claude_bin():
    return shutil.which("claude") or str(HOME / ".local/bin/claude")


# ---------- skills ----------
# (global dir relative to HOME, project dir relative to project) per agent
SKILL_DIRS = {
    "claude": (".claude/skills", ".claude/skills"),
    "antigravity": (".gemini/config/skills", ".agents/skills"),
    "codex": (".codex/skills", ".agents/skills"),
    "gemini": (".gemini/skills", ".gemini/skills"),
    "hermes": (".hermes/skills", ".hermes/skills"),
}


def skills_dir(agent, scope):
    if agent not in SKILL_DIRS:
        raise ValueError("unknown agent")
    g, p = SKILL_DIRS[agent]
    return HOME / g if scope == "global" else project_ok(scope) / p

def read_desc(skill_dir: Path) -> str:
    try:
        t = (skill_dir / "SKILL.md").read_text(errors="ignore")[:5000]
    except OSError:
        return ""
    m = re.match(r"^---\s*\n(.*?)\n---", t, re.S)
    fm = m.group(1) if m else t
    lines = fm.splitlines()
    for i, l in enumerate(lines):
        if l.startswith("description:"):
            v = l[len("description:"):].strip()
            if v in (">", ">-", "|", "|-", ""):
                parts = []
                for n in lines[i + 1:]:
                    if n.startswith((" ", "\t")):
                        parts.append(n.strip())
                    else:
                        break
                v = " ".join(parts)
            return v.strip("\"'")
    return ""


def lib_skills():
    out = {}
    if LIB.is_dir():
        for p in LIB.iterdir():
            if (p / "SKILL.md").exists() and NAME_RE.match(p.name):
                out[p.name] = p
    return out


def skill_entries(d: Path):
    """name -> 'link' | 'local' for a skills directory."""
    res = {}
    if not d.is_dir():
        return res
    for p in d.iterdir():
        if p.name.startswith(".") or p.name == INDEX_SKILL:
            continue
        if p.is_symlink():
            res[p.name] = "link"
        elif (p / "SKILL.md").exists():
            res[p.name] = "local"
    return res


# ---------- projects ----------
def codex_cfg():
    try:
        import tomllib
        return tomllib.loads((HOME / ".codex" / "config.toml").read_text())
    except Exception:
        return {}


def projects():
    try:
        data = json.loads(CLAUDE_JSON.read_text())
    except (OSError, ValueError):
        data = {}
    known = {}
    for path in data.get("projects", {}):
        known.setdefault(path, set()).add("claude")
    for path in codex_cfg().get("projects", {}):
        known.setdefault(path, set()).add("codex")
    # also pick up project folders no agent registered yet (e.g. only used from Antigravity): scan siblings of known projects
    MARKERS = (".git", ".claude", ".agents", ".gemini", ".codex", "CLAUDE.md", "AGENTS.md", "GEMINI.md")
    roots = {str(Path(p).parent) for p in known if Path(p).parent != HOME}
    for r in roots:
        try:
            for c in Path(r).iterdir():
                if c.is_dir() and not c.name.startswith(".") and str(c) not in known and any((c / m).exists() for m in MARKERS):
                    known.setdefault(str(c), set())
        except OSError:
            pass
    out = []
    for path, ags in known.items():
        if path == str(HOME) or any(h in path + "/" for h in HIDE_PARTS) or "/Documents/Codex/" in path:
            continue
        if not Path(path).is_dir():
            continue
        for a, marker in (("gemini", ".gemini"), ("antigravity", ".agents"), ("hermes", ".hermes")):
            if (Path(path) / marker).exists():
                ags.add(a)
        out.append({"path": path, "name": Path(path).name.strip(), "locked": path in LOCKED, "agents": sorted(ags)})
    out.sort(key=lambda p: p["name"].lower())
    return out


def project_ok(path, write=True):
    ps = {p["path"]: p for p in projects()}
    if path not in ps:
        raise ValueError("unknown project")
    if write and ps[path]["locked"]:
        raise ValueError("project is locked (production checkout)")
    return Path(path)


# ---------- journal ----------
def log(entry):
    entry["ts"] = int(time.time())
    with LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def read_log():
    if not LOG.exists():
        return []
    return [json.loads(l) for l in LOG.read_text().splitlines() if l.strip()]


# ---------- skill ops ----------
def skill_op(name, scope, action, journal=True, agent="claude"):
    lib = lib_skills()
    if name not in lib and action != "unpin":
        raise ValueError("skill not in library")
    target = skills_dir(agent, scope)
    link = target / name
    if action == "pin":
        if link.exists() or link.is_symlink():
            return
        target.mkdir(parents=True, exist_ok=True)
        os.symlink(lib[name], link)
    elif action == "unpin":
        if not link.is_symlink():
            if link.exists():
                raise ValueError("not a symlink - refusing to touch a real folder")
            return
        link.unlink()
    else:
        raise ValueError("bad action")
    if journal:
        log({"kind": "skill", "agent": agent, "name": name, "scope": scope, "action": action})
    if agent == "claude":
        write_index()


def write_index():
    lib = lib_skills()
    glob = skill_entries(GLOBAL)
    d = LIB / INDEX_SKILL
    if (GLOBAL / INDEX_SKILL).exists() and not (GLOBAL / INDEX_SKILL).is_symlink() and not d.exists():
        shutil.move(str(GLOBAL / INDEX_SKILL), str(d))
    d.mkdir(parents=True, exist_ok=True)
    for base in [HOME / g for g, _ in SKILL_DIRS.values()]:   # every agent gets the index as a link
        try:
            l = base / INDEX_SKILL
            if not l.exists() and not l.is_symlink() and base.is_dir():
                os.symlink(d, l)
        except OSError:
            pass
    rest = sorted(n for n in lib if n not in glob)
    lines = [f"- {n} — {read_desc(lib[n])[:110]}" for n in rest]
    (d / "index.md").write_text(
        "# Skills not loaded by default\n\nRead `~/.agents/skills/<name>/SKILL.md` for the one you need.\n"
        "Some may be pinned to the current project and already visible in your skill list.\n\n" + "\n".join(lines) + "\n")
    (d / "SKILL.md").write_text(
        "---\nname: skill-index\n"
        "description: Catalog of every installed skill that is NOT loaded by default. Use when a task may fit a specialised skill "
        "(marketing, SEO, video, n8n, design, docs, etc.) that is missing from your skill list - read index.md, pick matches, "
        "then Read ~/.agents/skills/<name>/SKILL.md and follow it.\n---\n\n"
        "Read `index.md` next to this file. It lists name and one-line description of each unloaded skill. "
        "Pick the matching one(s), Read `~/.agents/skills/<name>/SKILL.md`, and follow it exactly.\n")


# ---------- MCP ----------
def load_claude_json():
    try:
        return json.loads(CLAUDE_JSON.read_text())
    except (OSError, ValueError):
        return {}


def mcp_lib():
    try:
        lib = json.loads(MCP_LIB.read_text())
    except (OSError, ValueError):
        lib = {}
    cj = load_claude_json()
    changed = False
    for n, d in cj.get("mcpServers", {}).items():
        if n not in lib:
            lib[n] = d
            changed = True
    for pr in cj.get("projects", {}).values():
        for n, d in pr.get("mcpServers", {}).items():
            if n not in lib:
                lib[n] = d
                changed = True
    for n, d in _json(GEMINI_GLOBAL).get("mcpServers", {}).items():
        if n not in lib:
            lib[n] = d
            changed = True
    if changed or not MCP_LIB.exists():
        MCP_LIB.write_text(json.dumps(lib, indent=2))
        os.chmod(MCP_LIB, 0o600)
    return lib


def project_mcp(path):
    f = Path(path) / ".mcp.json"
    try:
        return json.loads(f.read_text()).get("mcpServers", {})
    except (OSError, ValueError):
        return {}


def _guard_secret_file(root, f):
    """A server with env/headers (secrets) is about to be written into <project>/.mcp.json.
    Refuse if git tracks that file (it would leak on push); otherwise keep it out of git locally."""
    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=15)
    try:
        if git("rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
            return
    except (OSError, subprocess.TimeoutExpired):
        return
    if git("ls-files", "--error-unmatch", ".mcp.json").returncode == 0:
        raise ValueError(".mcp.json is tracked by git: a server with secrets would be committed. "
                         "Pin it globally instead, or untrack .mcp.json first.")
    ex = git("rev-parse", "--git-path", "info/exclude").stdout.strip()
    if ex:
        ex = Path(ex) if os.path.isabs(ex) else root / ex
        ex.parent.mkdir(parents=True, exist_ok=True)
        cur = ex.read_text() if ex.exists() else ""
        if ".mcp.json" not in cur.split():
            ex.write_text(cur + ("" if cur.endswith("\n") or not cur else "\n") + ".mcp.json\n")


def mcp_op(name, scope, action, journal=True):
    lib = mcp_lib()
    if name not in lib or not NAME_RE.match(name):
        raise ValueError("unknown MCP server")
    if scope == "global":
        cj = load_claude_json().get("mcpServers", {})
        if action == "pin" and name not in cj:
            r = subprocess.run([claude_bin(), "mcp", "add-json", name, json.dumps(lib[name]), "-s", "user"],
                               capture_output=True, text=True, timeout=60)
        elif action == "unpin" and name in cj:
            r = subprocess.run([claude_bin(), "mcp", "remove", name, "-s", "user"],
                               capture_output=True, text=True, timeout=60)
        else:
            return
        if r.returncode != 0:
            raise ValueError("claude CLI failed: " + (r.stderr or r.stdout)[:200].replace(json.dumps(lib[name]), "<def>"))
    else:
        root = project_ok(scope)
        f = root / ".mcp.json"
        if f.is_symlink():
            raise ValueError(".mcp.json is a symlink; refusing to write through it")
        try:
            doc = json.loads(f.read_text())
        except (OSError, ValueError):
            doc = {}
        servers = doc.setdefault("mcpServers", {})
        if action == "pin":
            if lib[name].get("env") or lib[name].get("headers"):
                _guard_secret_file(root, f)       # raises if the file is tracked by git
            servers[name] = lib[name]
        elif action == "unpin":
            servers.pop(name, None)
        else:
            raise ValueError("bad action")
        # mkstemp = O_EXCL + 0600 + unpredictable name: a planted symlink/tmp file in the repo cannot redirect the write
        fd, tmp = tempfile.mkstemp(dir=root, prefix=".mcp.json.")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps(doc, indent=2) + "\n")
            os.replace(tmp, f)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    if journal:
        log({"kind": "mcp", "name": name, "scope": scope, "action": action})


def undo():
    entries = read_log()
    stack = []
    for e in entries:
        if e.get("kind") == "undo":
            if stack:
                stack.pop()
        else:
            stack.append(e)
    if not stack:
        raise ValueError("nothing to undo")
    e = stack[-1]
    inv = "unpin" if e["action"] == "pin" else "pin"
    ({"skill": lambda *a, **k: skill_op(*a, agent=e.get("agent", "claude"), **k), "mcp": mcp_op, "gmcp": gemini_op}[e["kind"]])(e["name"], e["scope"], inv, journal=False)
    log({"kind": "undo", "of": f'{e["action"]} {e["name"]} @ {e["scope"]}'})
    return f'{inv} {e["name"]}'


# ---------- other agents (MCP) ----------
GEMINI_GLOBAL = HOME / ".gemini" / "settings.json"
AGENTS = {
    "claude": {"label": "Claude Code", "writable": True},
    "antigravity": {"label": "Antigravity", "writable": False, "detect": str(HOME / ".gemini" / "antigravity")},
    "gemini": {"label": "Gemini CLI", "writable": True},
    "codex": {"label": "Codex", "writable": False},
    "hermes": {"label": "Hermes", "writable": False},
    # Nod (dev.nod.desktop): keeps its own data in ~/.nod-desktop (created on first launch); no shared skill/MCP dir known yet
    "nod": {"label": "Nod (/Applications/Nod.app)", "writable": False, "detect": "/Applications/Nod.app"},
}


def _json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def hermes_names():
    try:
        lines = (HOME / ".hermes" / "config.yaml").read_text().splitlines()
    except OSError:
        return []
    names, on = [], False
    for l in lines:
        if l.startswith("mcp_servers:"):
            on = True
            continue
        if on:
            if l and not l.startswith(" ") and not l.startswith("#"):
                break
            m = re.match(r"^  ([A-Za-z0-9._-]+):\s*$", l)
            if m:
                names.append(m.group(1))
    return names


def agent_global_names(agent):
    if agent == "gemini":
        return set(_json(GEMINI_GLOBAL).get("mcpServers", {}))
    if agent == "codex":
        return set(codex_cfg().get("mcp_servers", {}))
    if agent == "hermes":
        return set(hermes_names())
    return set(load_claude_json().get("mcpServers", {}))


def agent_project_names(agent, path):
    p = Path(path)
    if agent == "claude":
        return set(project_mcp(path))
    if agent == "gemini":
        return set(_json(p / ".gemini" / "settings.json").get("mcpServers", {}))
    if agent == "codex":
        try:
            import tomllib
            return set(tomllib.loads((p / ".codex" / "config.toml").read_text()).get("mcp_servers", {}))
        except Exception:
            return set()
    return set()


def to_gemini(d):
    d = dict(d)
    t = d.pop("type", None)
    if "url" in d and t != "sse":
        d["httpUrl"] = d.pop("url")
    return d


def gemini_op(name, scope, action, journal=True):
    lib = mcp_lib()
    if name not in lib or not NAME_RE.match(name):
        raise ValueError("unknown MCP server")
    f = GEMINI_GLOBAL if scope == "global" else project_ok(scope) / ".gemini" / "settings.json"
    doc = _json(f) if f.exists() else {}
    if f.exists():
        shutil.copy2(f, f.with_name(f.name + f".bak-saimatrix-{int(time.time())}"))
    servers = doc.setdefault("mcpServers", {})
    if action == "pin":
        servers[name] = to_gemini(lib[name])
    elif action == "unpin":
        if name in servers and name not in lib:
            lib[name] = servers[name]
        servers.pop(name, None)
    else:
        raise ValueError("bad action")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(doc, indent=2) + "\n")
    if journal:
        log({"kind": "gmcp", "name": name, "scope": scope, "action": action})


# ---------- state ----------
def state():
    lib = lib_skills()
    glob = skill_entries(GLOBAL)
    projs = projects()
    pe = {p["path"]: skill_entries(Path(p["path"]) / ".claude" / "skills") for p in projs}
    names = set(lib) | {n for n, k in glob.items() if k == "local"}
    skills = []
    for n in sorted(names):
        skills.append({
            "name": n,
            "desc": (read_desc(lib[n]) if n in lib else "")[:220],
            "global": glob.get(n),
            "projects": {p["path"]: pe[p["path"]][n] for p in projs if n in pe[p["path"]]},
        })
    ml = mcp_lib()
    cj = load_claude_json()
    gl = set(cj.get("mcpServers", {}))
    mcps = []
    for n in sorted(ml):
        d = ml[n]
        pm = {}
        for p in projs:
            if n in project_mcp(p["path"]):
                pm[p["path"]] = "link"
            elif n in cj.get("projects", {}).get(p["path"], {}).get("mcpServers", {}):
                pm[p["path"]] = "local"
        mcps.append({"name": n, "kind": d.get("type") or ("stdio" if d.get("command") else "?"),
                     "global": "link" if n in gl else None, "projects": pm})
    last = [e for e in read_log() if e.get("kind") != "undo"][-1:] or None
    agents = {}
    for aid, meta in AGENTS.items():
        g = agent_global_names(aid) if aid in ("claude", "gemini", "codex", "hermes") else set()
        pn = {p["path"]: agent_project_names(aid, p["path"]) for p in projs}
        names = sorted(set(ml) | g | set().union(*pn.values()) if pn else set(ml) | g)
        sk = []
        sd = SKILL_DIRS.get(aid)
        ge = skill_entries(HOME / sd[0]) if sd else {}
        pe2 = {p["path"]: (skill_entries(Path(p["path"]) / sd[1]) if sd else {}) for p in projs}
        allnames = set(lib) | set(ge) | {k for v in pe2.values() for k in v} if sd else set()
        for n in sorted(allnames):
            if n in lib:
                dpath = lib[n]
            else:  # skill that only exists inside this agent's global/project dir (not in the library)
                dpath = next((d for d in [HOME / sd[0] / n] + [Path(p) / sd[1] / n for p in pe2 if n in pe2[p]]
                              if (d / "SKILL.md").exists()), None)
            sk.append({"name": n, "desc": (read_desc(dpath) if dpath else "")[:220], "global": ge.get(n),
                       "inLib": n in lib,
                       "projects": {p: pe2[p][n] for p in pe2 if n in pe2[p]}})
        agents[aid] = {"label": meta["label"], "writable": meta["writable"], "installed": Path(meta["detect"]).exists() if meta.get("detect") else True, "skills": sk,
                       "mcps": [{"name": n, "global": "link" if n in g else None,
                                 "inLib": n in ml,
                                 "projects": {p: "link" for p, ns in pn.items() if n in ns}} for n in names]}
    return {"skills": skills, "mcps": mcps, "projects": projs, "agents": agents, "lastChange": last}



# ---------- token trim (execution/token_trim.py) ----------
import importlib.util
_TT = {"mod": None, "plan": None, "at": 0}


def tt():
    if _TT["mod"] is None:
        spec = importlib.util.spec_from_file_location("token_trim", HERE / "token_trim.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        _TT["mod"] = m
    return _TT["mod"]


def trim_plan(refresh=False):
    if refresh or not _TT["plan"] or time.time() - _TT["at"] > 600:
        _TT["plan"], _TT["at"] = tt().build_plan(30, set()), time.time()
    return _TT["plan"]


_HM = {"mod": None, "rep": None, "at": 0}
UNSUPPORTED = {
    "codex": "Codex has no safe per-tool switch for built-in tools: its logs only expose tool namespaces "
             "(functions, clock, collaboration, MCP) and its options are broad feature flags. "
             "Manage Codex MCP servers in the MCP tab.",
}
_GM = {"mod": None}


def gemini():
    if _GM["mod"] is None:
        spec = importlib.util.spec_from_file_location("token_trim_gemini", HERE / "token_trim_gemini.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        _GM["mod"] = m
    return _GM["mod"]


def hermes():
    if _HM["mod"] is None:
        spec = importlib.util.spec_from_file_location("token_trim_hermes", HERE / "token_trim_hermes.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        _HM["mod"] = m
    return _HM["mod"]


def agent_state(agent, refresh=False):
    if agent == "hermes":
        if refresh or not _HM["rep"] or time.time() - _HM["at"] > 600:
            _HM["rep"], _HM["at"] = hermes().report(30), time.time()
        return _HM["rep"]
    if agent == "gemini":
        return gemini().state()
    if agent in UNSUPPORTED:
        return {"agent": agent, "unsupported": UNSUPPORTED[agent]}
    raise ValueError("unknown agent")


def agent_op(agent, action, toolsets, apply_):
    if agent == "gemini":          # manual: action = "exclude" | "include"
        if action not in ("exclude", "include"):
            raise ValueError("bad action")
        r = gemini().apply(toolsets, action == "exclude", apply_)
        if apply_:
            with (DATA / "trim-changes.jsonl").open("a") as f:
                f.write(json.dumps({"ts": int(time.time()), "agent": agent, "action": action, "toolsets": r["toolsets"]}) + "\n")
        return r
    if agent != "hermes":
        raise ValueError(UNSUPPORTED.get(agent, "unknown agent"))
    toolsets = [t for t in toolsets if isinstance(t, str) and NAME_RE.match(t)]
    r = hermes().apply(toolsets, apply_) if action == "trim" else hermes().restore(apply_)
    if apply_:
        _HM["rep"] = None
        with (DATA / "trim-changes.jsonl").open("a") as f:
            f.write(json.dumps({"ts": int(time.time()), "agent": agent, "action": action, "toolsets": r["toolsets"]}) + "\n")
    return r


def trim_state(refresh=False):
    p = trim_plan(refresh)
    scopes = {"global": tt().current_state(None)}
    for pr in projects():
        scopes[pr["path"]] = tt().current_state(pr["path"])
    return {"sessions": p["sessions"], "requests": p["requests"], "tools": p["tools"][:40],
            "skills_unused": sum(1 for x in p["skills"] if x["trim"]), "tool_saving": p["tool_saving"],
            "scopes": scopes}


def trim_op(scope, action, keep, apply_):
    project = None if scope == "global" else str(project_ok(scope))   # lock check for production checkouts
    keep = [k for k in keep if isinstance(k, str) and NAME_RE.match(k)]
    r = (tt().api_trim(project, keep=keep, apply=apply_, tools_only=True, plan=trim_plan())
         if action == "trim" else tt().api_restore(project, apply=apply_))
    if apply_:
        # separate journal: undo() only understands pin/unpin entries; restore is the undo for trim
        with (DATA / "trim-changes.jsonl").open("a") as f:
            f.write(json.dumps({"ts": int(time.time()), "scope": scope, "action": action, "tools": r["tools"]}) + "\n")
    return r

# ---------- http ----------
OK_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _host_ok(self):
        return self.headers.get("Host", "") in OK_HOSTS

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        if self.path == "/":
            return self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/state":
            return self._send(200, state())
        if self.path.startswith("/api/trim"):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            ag = (q.get("agent") or ["claude"])[0]
            ref = "refresh=1" in self.path
            return self._send(200, trim_state(refresh=ref) if ag == "claude" else agent_state(ag, ref))
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok() or self.headers.get("X-SB") != "1":
            return self._send(403, {"error": "forbidden"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/api/skill":
                skill_op(body["name"], body["scope"], body["action"], agent=body.get("agent", "claude"))
            elif self.path == "/api/mcp":
                mcp_op(body["name"], body["scope"], body["action"])
            elif self.path == "/api/gmcp":
                gemini_op(body["name"], body["scope"], body["action"])
            elif self.path == "/api/trim" and body.get("agent", "claude") != "claude":
                return self._send(200, agent_op(body["agent"], body["action"], body.get("toolsets", []), bool(body.get("apply"))))
            elif self.path == "/api/trim":
                return self._send(200, trim_op(body["scope"], body["action"], body.get("keep", []), bool(body.get("apply"))))
            elif self.path == "/api/undo":
                return self._send(200, {"undone": undo()})
            else:
                return self._send(404, {"error": "not found"})
            self._send(200, {"ok": True})
        except (ValueError, KeyError, subprocess.TimeoutExpired) as e:
            self._send(400, {"error": str(e)})
        except OSError as e:
            self._send(500, {"error": f"filesystem: {e.strerror}"})


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        s = state()
        print(len(s["skills"]), "skills,", len(s["mcps"]), "mcps,", len(s["projects"]), "projects")
        sys.exit(0)
    write_index()
    print(f"Sai Matrix on http://127.0.0.1:{PORT}  (Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
