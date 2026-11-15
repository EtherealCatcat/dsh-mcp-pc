#!/usr/bin/env python3
"""Controller/receiver contract tests (P1 timeout pairing, P5 tool snapshot).

    python3 tests/test_gateway_contract.py

P5: ``gateway/tools.json`` is the offline fallback for the gateway's
``tools/list``. It is a copy of ``server/dshpc_tools.TOOL_DEFS``; when the two
drift apart, a controller that starts while both PCs are offline silently
advertises a stale tool surface. There is no build step keeping them in sync,
so this test is the sync check — regenerate the snapshot from TOOL_DEFS when it
fails.

P1: the gateway must never abort a ``pc_run`` call before the receiver's own
``timeout_sec`` expires, or the command keeps running remotely while the user
sees a failure and retries it. The behaviour lives in ``gateway/timeout.js`` and
is covered by ``node tests/test_gateway_timeout.mjs``; these assertions only
check that the policy stays wired into the tool call, reading sources instead of
importing ``gateway/index.js`` (which needs the Host-provided MCP client).
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import dshpc_tools  # noqa: E402

GATEWAY = ROOT / "gateway"


class ToolSnapshotTests(unittest.TestCase):
    def test_gateway_snapshot_matches_receiver_tool_defs(self) -> None:
        snapshot = json.loads((GATEWAY / "tools.json").read_text(encoding="utf-8"))
        expected = dshpc_tools.TOOL_DEFS
        self.assertIsInstance(snapshot, list)
        self.assertEqual(
            [tool.get("name") for tool in snapshot],
            [tool.get("name") for tool in expected],
            "gateway/tools.json 的工具顺序/名称与被控端 TOOL_DEFS 不一致；请按 TOOL_DEFS 重新生成快照",
        )
        for actual, wanted in zip(snapshot, expected):
            for field in ("title", "description", "inputSchema", "annotations"):
                self.assertEqual(
                    actual.get(field),
                    wanted.get(field),
                    f"{wanted.get('name')} 的 {field} 与 gateway/tools.json 不一致；请重新生成快照",
                )


class GatewayTimeoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.index_source = (GATEWAY / "index.js").read_text(encoding="utf-8")
        cls.timeout_source = (GATEWAY / "timeout.js").read_text(encoding="utf-8")
        cls.tools_source = (ROOT / "server" / "dshpc_tools.py").read_text(encoding="utf-8")

    def _js_number(self, name: str) -> int:
        match = re.search(rf"const {name} = ([\d_]+)", self.timeout_source)
        self.assertIsNotNone(match, f"gateway/index.js 缺少 {name}")
        return int(match.group(1).replace("_", ""))

    def test_command_timeout_covers_receiver_limit(self) -> None:
        receiver_limit = re.search(r'max_timeout_sec",\s*(\d+)', self.tools_source)
        self.assertIsNotNone(receiver_limit, "server/dshpc_tools.py 中找不到 max_timeout_sec 默认值")
        receiver_seconds = int(receiver_limit.group(1))

        command_cap = self._js_number("MAX_COMMAND_TIMEOUT_SEC")
        slack_ms = self._js_number("COMMAND_TIMEOUT_SLACK_MS")
        self.assertEqual(command_cap, receiver_seconds)
        self.assertGreater(slack_ms, 0)

    def test_run_command_uses_the_adapted_timeout(self) -> None:
        self.assertIn("callTimeoutMs(spec, rest, pc)", self.index_source)
        self.assertIn("} from './timeout.js'", self.index_source)
        self.assertIn("spec.remote === 'run_command'", self.timeout_source)
        self.assertIn("'tools/call'", self.index_source)

    def test_receiver_clamps_to_the_same_limit(self) -> None:
        clamp = re.search(r'min\((\d+), int\(shell_cfg\.get\("max_timeout_sec"', self.tools_source)
        self.assertIsNotNone(clamp, "server/dshpc_tools.py 中找不到 timeout 上限的 clamp")
        self.assertEqual(int(clamp.group(1)), self._js_number("MAX_COMMAND_TIMEOUT_SEC"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
