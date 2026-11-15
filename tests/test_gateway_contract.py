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

import dshpc_state  # noqa: E402
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
        command_cap = self._js_number("MAX_COMMAND_TIMEOUT_SEC")
        slack_ms = self._js_number("COMMAND_TIMEOUT_SLACK_MS")
        self.assertEqual(command_cap, dshpc_state.MAX_TIMEOUT_SEC)
        self.assertEqual(command_cap, dshpc_state.CONFIG_DEFAULTS["shell"]["max_timeout_sec"])
        self.assertGreater(slack_ms, 0)

    def test_run_command_uses_the_adapted_timeout(self) -> None:
        self.assertIn("callTimeoutMs(spec, rest, pc)", self.index_source)
        self.assertIn("} from './timeout.js'", self.index_source)
        self.assertIn("spec.remote === 'run_command'", self.timeout_source)
        self.assertIn("'tools/call'", self.index_source)

    def test_receiver_clamps_to_the_same_limit(self) -> None:
        self.assertIn("min(dshpc_state.MAX_TIMEOUT_SEC", self.tools_source)
        schema = dshpc_tools.TOOL_DEFS[1]["inputSchema"]["properties"]["timeout_sec"]
        self.assertEqual(schema["maximum"], self._js_number("MAX_COMMAND_TIMEOUT_SEC"))


class ToolSchemaTests(unittest.TestCase):
    """tools/list narrows the timeout bound to this machine's config."""

    def _tools(self, shell: dict) -> dshpc_tools.Tools:
        import logging

        class Config:
            data = {"info_gate": False, "log_command_chars": dshpc_state.LOG_COMMAND_CHARS}

            def __init__(self):
                self.shell = shell

        return dshpc_tools.Tools(
            config=Config(),
            state=None,
            gate=None,
            sleep=None,
            store=None,
            url_base_provider=lambda: "",
            logger=logging.getLogger("schema-test"),
            started_at=0.0,
        )

    def test_default_cap_matches_constant(self) -> None:
        prop = self._tools(dict(dshpc_state.CONFIG_DEFAULTS["shell"])).list_tools()[1][
            "inputSchema"
        ]["properties"]["timeout_sec"]
        self.assertEqual(prop["maximum"], dshpc_state.MAX_TIMEOUT_SEC)
        self.assertIn(f"默认 {dshpc_state.DEFAULT_TIMEOUT_SEC}", prop["description"])

    def test_lower_configured_cap_is_advertised(self) -> None:
        shell = dict(dshpc_state.CONFIG_DEFAULTS["shell"], max_timeout_sec=120, default_timeout_sec=30)
        prop = self._tools(shell).list_tools()[1]["inputSchema"]["properties"]["timeout_sec"]
        self.assertEqual(prop["maximum"], 120)
        self.assertIn("默认 30", prop["description"])

    def test_list_tools_does_not_mutate_the_shared_defs(self) -> None:
        shell = dict(dshpc_state.CONFIG_DEFAULTS["shell"], max_timeout_sec=120)
        self._tools(shell).list_tools()
        self.assertEqual(dshpc_tools.TOOL_DEFS[1]["inputSchema"]["properties"]["timeout_sec"]["maximum"], dshpc_state.MAX_TIMEOUT_SEC)


if __name__ == "__main__":
    unittest.main(verbosity=2)
