import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ["setup.sh", "start.sh", "stop.sh", "status.sh", "scripts/install_autostart.sh",
           "scripts/uninstall_autostart.sh", "scripts/launchd_run.sh"]


class ScriptTests(unittest.TestCase):
    def test_all_shell_scripts_are_valid_and_executable(self):
        for s in SCRIPTS:
            p = ROOT / s
            self.assertTrue(p.exists(), s)
            self.assertTrue(os.access(p, os.X_OK), f"{s} is not executable")
            r = subprocess.run(["bash", "-n", str(p)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"{s}: {r.stderr}")

    def test_setup_check_only_reports_every_requirement_and_changes_nothing(self):
        before = {p.name for p in ROOT.iterdir()}
        r = subprocess.run(["bash", str(ROOT / "setup.sh"), "--check-only"], capture_output=True, text=True, cwd=ROOT, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for label in ("Homebrew", "Python", "Ollama", "AI model", "check-only"):
            self.assertIn(label, r.stdout)
        self.assertEqual(before, {p.name for p in ROOT.iterdir()}, "check-only must not create files")

    def test_package_checker_lists_missing_packages_only(self):
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_env.py")], capture_output=True, text=True)
        out = r.stdout.split()
        self.assertTrue(set(out) <= {"PyYAML", "python-dotenv", "pybit"})
        self.assertEqual(r.returncode, 1 if out else 0)

    def test_setup_script_offers_installs_and_never_installs_silently(self):
        text = (ROOT / "setup.sh").read_text()
        for needle in ("ask \"Install Homebrew", "ask \"Install Python", "ask \"Install Ollama", "ask \"Install them with pip",
                       "ask \"Download $MODEL", "ask \"Create it?", "ask \"Start it now?"):
            self.assertIn(needle, text)
        self.assertIn("~${SIZE} GB", text)

    def test_defaults_are_safe_in_shipped_files(self):
        env = (ROOT / ".env.example").read_text()
        for line in ("BYBIT_TESTNET=true", "TRADING_ENABLED=false", "AUTONOMOUS_MODE=false", "TELEGRAM_ENABLED=false"):
            self.assertIn(line, env)
        self.assertRegex((ROOT / "config" / "config.yaml").read_text(), r"(?m)^mode: PAPER")
        self.assertRegex((ROOT / "config" / "config.yaml").read_text(), r"allow_live_autostart: false")
        gi = (ROOT / ".gitignore").read_text()
        for pat in (".env", "*.db", "logs/"):
            self.assertIn(pat.rstrip("/"), gi)

    def test_no_secrets_or_cloud_llm_clients_in_source(self):
        banned = ("api.openai.com", "api.anthropic.com", "generativelanguage.googleapis.com")
        for p in list((ROOT / "bot").rglob("*.py")) + list((ROOT / "dashboard").rglob("*.py")):
            text = p.read_text()
            for b in banned:
                self.assertNotIn(b, text, f"{p} references a cloud LLM endpoint")
        for p in (ROOT / "bot").rglob("*.py"):
            t = p.read_text()
            self.assertNotIn("/v5/asset", t, f"{p} must not touch asset/withdraw endpoints")
            self.assertNotIn("/v5/user/create-sub", t)


if __name__ == "__main__":
    unittest.main()
