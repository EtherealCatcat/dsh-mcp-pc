"""Guards against personal/environment values and drifting duplicate constants.

Two kinds of checks:

1. No tracked file may contain a real Tailscale address, a developer home
   directory, or a hardcoded Windows user profile path.  Examples in docs use
   the ``100.x.y.z`` placeholder style.
2. Values that are intentionally mirrored outside Python (PowerShell
   installer, gateway JS, MCP schema) must still agree with the Python source
   of truth.

Run: ``python3 tests/test_no_baked_values.py``
"""

from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import dshpc_state  # noqa: E402
import dshpc_tools  # noqa: E402
import dshpc_version  # noqa: E402

# Built from fragments so this file itself never matches the scan.
FORBIDDEN = [
    re.compile(r"100\.70\.184\.\d+"),
    re.compile(r"100\.116\.55\.\d+"),
    re.compile(r"/Users/" + r"EtherealCat"),
    re.compile(r"[A-Za-z]:[\\/]Users[\\/]" + r"Administrator"),
    re.compile(r"dsh-mcp-pc" + r"/gateway\s*$", re.MULTILINE),
]

SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".zip", ".asar", ".pyc"}
SKIP_FILES = {"tests/test_no_baked_values.py"}


def tracked_files() -> list[Path]:
    try:
        output = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        ).stdout
        names = [name for name in output.decode("utf-8").split("\0") if name]
    except (OSError, subprocess.CalledProcessError):
        names = [
            str(path.relative_to(ROOT))
            for path in ROOT.rglob("*")
            if path.is_file() and ".git" not in path.parts and "node_modules" not in path.parts
        ]
    files = []
    for name in names:
        if name in SKIP_FILES or Path(name).suffix.lower() in SKIP_SUFFIXES:
            continue
        files.append(ROOT / name)
    return files


class NoBakedValues(unittest.TestCase):
    def test_no_personal_values(self) -> None:
        offenders = []
        for path in tracked_files():
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for pattern in FORBIDDEN:
                for match in pattern.finditer(text):
                    line = text.count("\n", 0, match.start()) + 1
                    offenders.append(f"{path.relative_to(ROOT)}:{line}: {match.group(0)!r}")
        self.assertEqual(offenders, [], "personal values found:\n" + "\n".join(offenders))

    def test_example_uses_placeholders(self) -> None:
        example = (ROOT / "gateway" / "pcs.example.json").read_text(encoding="utf-8")
        self.assertIn("100.x.y.z", example)
        self.assertNotIn("100.70.", example)

    def test_port_single_source(self) -> None:
        self.assertEqual(dshpc_state.CONFIG_DEFAULTS["port"], dshpc_version.DEFAULT_PORT)
        common = (ROOT / "deploy" / "common.ps1").read_text(encoding="utf-8-sig")
        match = re.search(r"\$DshpcDefaultPort\s*=\s*(\d+)", common)
        self.assertIsNotNone(match, "deploy/common.ps1 lost $DshpcDefaultPort")
        self.assertEqual(int(match.group(1)), dshpc_version.DEFAULT_PORT)

    def test_timeout_ceiling_single_source(self) -> None:
        timeout_js = (ROOT / "gateway" / "timeout.js").read_text(encoding="utf-8")
        match = re.search(r"MAX_COMMAND_TIMEOUT_SEC\s*=\s*(\d+)", timeout_js)
        self.assertIsNotNone(match)
        self.assertEqual(int(match.group(1)), dshpc_state.MAX_TIMEOUT_SEC)
        schema = dshpc_tools.TOOL_DEFS[1]["inputSchema"]["properties"]["timeout_sec"]["maximum"]
        self.assertEqual(schema, dshpc_state.MAX_TIMEOUT_SEC)
        self.assertEqual(dshpc_state.CONFIG_DEFAULTS["shell"]["max_timeout_sec"], dshpc_state.MAX_TIMEOUT_SEC)

    def test_server_reads_config_constants(self) -> None:
        server = (ROOT / "server" / "dshpc_server.py").read_text(encoding="utf-8")
        self.assertIn("dshpc_state.MAX_REQUEST_BYTES", server)
        self.assertIn("dshpc_state.LOG_MAX_BYTES", server)
        self.assertIn("dshpc_state.SCREENSHOT_RETENTION_HOURS", server)
        self.assertNotIn("2 * 1024 * 1024", server)

    def test_deploy_scripts_share_constants(self) -> None:
        install = (ROOT / "deploy" / "install.ps1").read_text(encoding="utf-8-sig")
        uninstall = (ROOT / "deploy" / "uninstall.ps1").read_text(encoding="utf-8-sig")
        for name, text in (("install.ps1", install), ("uninstall.ps1", uninstall)):
            self.assertIn("common.ps1", text, f"{name} must dot-source deploy/common.ps1")
            self.assertNotRegex(text, r"^\$taskName\s*=", f"{name} defines the task name locally")
        self.assertIn("import dshpc_version", install)
        self.assertNotRegex(install, r"dsh-pc-remote\s+\d+\.\d+\.\d+")
        self.assertNotIn("requestTimeoutMs", install)

    def test_deploy_scripts_are_ascii(self) -> None:
        # Windows PowerShell 5.1 decodes BOM-less files as ANSI; a single curly
        # quote or em dash there turns into mojibake and breaks parsing.
        for path in sorted((ROOT / "deploy").glob("*.ps1")):
            data = path.read_bytes()
            if data.startswith(b"\xef\xbb\xbf"):
                data = data[3:]
            offenders = [i for i, byte in enumerate(data) if byte > 0x7F]
            self.assertEqual(offenders, [], f"{path.name} must stay ASCII-only")


if __name__ == "__main__":
    unittest.main(verbosity=2)
