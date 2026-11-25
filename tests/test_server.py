"""Automated checks for the Sai Matrix server and macOS bundle."""
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server.py"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_home(tmp: Path):
    skill = tmp / ".agents" / "skills" / "demo-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Demo skill for tests.\n---\n\nBody\n")
    (tmp / ".claude" / "skills").mkdir(parents=True)
    return skill


class Server:
    def __init__(self, home, data_dir, use_env=False):
        self.port = free_port()
        self.home = home
        self.data = data_dir
        env = {k: v for k, v in os.environ.items() if not k.startswith("SAI_MATRIX")}
        env["HOME"] = str(home)
        env["SAI_MATRIX_PORT"] = str(self.port)
        args = [sys.executable, str(SERVER)]
        if use_env:
            env["SAI_MATRIX_DATA"] = str(data_dir)
        else:
            args += ["--data-dir", str(data_dir)]
        self.proc = subprocess.Popen(args, cwd=str(ROOT), env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def wait(self, timeout=20):
        deadline = __import__("time").time() + timeout
        while __import__("time").time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("server exited: " + self.proc.stdout.read().decode())
            try:
                with urllib.request.urlopen(self.url("/api/state"), timeout=1) as r:
                    if r.status == 200:
                        return
            except OSError:
                pass
            __import__("time").sleep(0.1)
        raise RuntimeError("server did not start")

    def get(self, path):
        with urllib.request.urlopen(self.url(path), timeout=5) as r:
            return r.status, json.loads(r.read())

    def post(self, path, body):
        req = urllib.request.Request(self.url(path), data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "X-SB": "1"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        if self.proc.stdout:
            self.proc.stdout.close()


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        make_home(self.home)
        self.data = self.home / "data"
        self.srv = Server(self.home, self.data)
        self.srv.wait()

    def tearDown(self):
        self.srv.stop()
        self.tmp.cleanup()

    def test_data_dir_flag_and_env(self):
        self.assertTrue(self.data.is_dir())
        self.srv.post("/api/skill", {"name": "demo-skill", "scope": "global", "action": "pin"})
        self.assertTrue((self.data / "changes.jsonl").exists())
        self.assertTrue((self.home / ".claude" / "skills" / "demo-skill").is_symlink())

        env_data = self.home / "env-data"
        (self.home / ".claude" / "skills" / "demo-skill").unlink()
        srv = Server(self.home, env_data, use_env=True)
        try:
            srv.wait()
            self.assertTrue(env_data.is_dir())
            srv.post("/api/skill", {"name": "demo-skill", "scope": "global", "action": "pin"})
            self.assertTrue((env_data / "changes.jsonl").exists())
        finally:
            srv.stop()

    def test_cli_data_dir_selftest(self):
        out = subprocess.run([sys.executable, str(SERVER), "--selftest", "--data-dir", str(self.data)],
                             cwd=str(ROOT), env={"HOME": str(self.home), "PATH": os.environ.get("PATH", "")},
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("skills", out.stdout)

    def test_undo_journal(self):
        self.srv.post("/api/skill", {"name": "demo-skill", "scope": "global", "action": "pin"})
        link = self.home / ".claude" / "skills" / "demo-skill"
        self.assertTrue(link.is_symlink())
        code, body = self.srv.post("/api/undo", {})
        self.assertEqual(code, 200)
        self.assertIn("unpin", body["undone"])
        self.assertFalse(link.exists() or link.is_symlink())

    def test_host_header_rejected(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=5)
        conn.putrequest("GET", "/api/state", skip_host=True)
        conn.putheader("Host", "evil.example")
        conn.endheaders()
        resp = conn.getresponse()
        self.assertEqual(resp.status, 403)
        conn.close()

    def test_post_requires_xsb(self):
        req = urllib.request.Request(self.srv.url("/api/skill"),
                                     data=json.dumps({"name": "demo-skill", "scope": "global", "action": "pin"}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 403)

    def test_opencode_removed_from_state(self):
        _, state = self.srv.get("/api/state")
        self.assertNotIn("opencode", state["agents"])


class SourceTests(unittest.TestCase):
    def test_server_has_no_opencode(self):
        text = SERVER.read_text().lower()
        self.assertNotIn("opencode", text)

    def test_readme_supported_agents(self):
        lines = (ROOT / "README.md").read_text().splitlines()
        supported = next(l for l in lines if l.startswith("Supports Claude Code"))
        self.assertNotIn("OpenCode", supported)
        self.assertIn("Hermes", supported)
        # The independent Zenborg description is intentionally untouched.
        self.assertTrue(any("connects Claude Code, Codex, Gemini, OpenCode and Hermes" in l for l in lines))

    def test_build_script_uses_clean_archive(self):
        text = (ROOT / "scripts" / "build-dmg.sh").read_text()
        self.assertIn("git -C \"$ROOT\" archive --format=tar HEAD", text)
        self.assertIn("--skip-sign", text)
        self.assertIn("notarytool submit", text)


class BundleHygieneTests(unittest.TestCase):
    APP = ROOT / "build" / "Sai Matrix.app"

    @unittest.skipUnless(APP.is_dir(), "app not built yet")
    def test_no_dev_artifacts_in_bundle(self):
        bad = []
        for p in self.APP.rglob("*"):
            if p.name in (".data", "config.json", "__pycache__", ".DS_Store", ".git"):
                bad.append(str(p))
        self.assertEqual(bad, [])

    @unittest.skipUnless(APP.is_dir(), "app not built yet")
    def test_launcher_is_universal(self):
        out = subprocess.run(["lipo", "-archs", str(self.APP / "Contents/MacOS/SaiMatrix")],
                             capture_output=True, text=True, check=True)
        self.assertIn("arm64", out.stdout)
        self.assertIn("x86_64", out.stdout)


if __name__ == "__main__":
    unittest.main()
