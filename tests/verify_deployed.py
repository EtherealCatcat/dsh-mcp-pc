"""Verify the deployed service end-to-end over MCP (read-only by default)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mcp_client  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
import dshpc_state  # noqa: E402



def text_of(result: dict) -> str:
    return "\n".join(block.get("text", "") for block in result.get("content", []) if block.get("type") == "text")


def call(url: str, token: str, name: str, args: dict, request_id: int) -> dict:
    return mcp_client.rpc(url, token, "tools/call", {"name": name, "arguments": args}, request_id=request_id)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        default=str(dshpc_state.default_data_dir() / "config.json"),
        help="被控端 config.json（默认取自 DSHPC_DATA_DIR 或平台默认数据目录）",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="MCP 地址；省略时用 config.json 里的 public_host（缺失则 127.0.0.1）和 port 拼出来",
    )
    parser.add_argument("--shot", default=str(ROOT / ".tmp" / "deployed-shot.png"))
    args = parser.parse_args()

    config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    token = config["token"]
    if not args.url:
        host = str(config.get("public_host") or "127.0.0.1")
        port = int(config.get("port") or dshpc_state.DEFAULT_PORT)
        args.url = f"http://{host}:{port}/mcp"

    init = mcp_client.rpc(
        args.url,
        token,
        "initialize",
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "deployed-verify", "version": "1.0.0"},
        },
        request_id=1,
    )
    print("initialize:", json.dumps(init.get("serverInfo"), ensure_ascii=False), init.get("protocolVersion"))
    tools = mcp_client.rpc(args.url, token, "tools/list", {}, request_id=2)
    print("tools:", [tool["name"] for tool in tools.get("tools", [])])
    instructions = init.get("instructions", "")
    print("instructions bytes:", len(instructions.encode("utf-8")))

    info_result = call(args.url, token, "get_system_info", {}, 3)
    info = json.loads(text_of(info_result))
    summary = {
        "hostname": info["hostname"],
        "user": info["user"],
        "session": info["session"],
        "power": info["power"],
        "features": info["features"],
        "remote_control": info["remote_control"],
        "tailscale": info["network"]["tailscale"],
        "urls": info["server"]["urls"],
        "displays": {
            "virtual_screen": info["displays"]["virtual_screen"],
            "monitor_count": len(info["displays"]["monitors"]),
        },
    }
    print("info:", json.dumps(summary, ensure_ascii=False, indent=2))

    shell_result = call(args.url, token, "run_command", {"command": "hostname; whoami"}, 4)
    shell = json.loads(text_of(shell_result))
    print("shell:", shell.get("exit_code"), repr(shell.get("stdout")), repr(shell.get("stderr")))

    shot_result = call(args.url, token, "capture_screen", {"max_width": 1600, "include_image": True}, 5)
    blocks = shot_result.get("content", [])
    shot_meta = json.loads(text_of(shot_result))
    image_blocks = [block for block in blocks if block.get("type") == "image"]
    print(
        "screenshot:",
        shot_meta.get("width"),
        shot_meta.get("height"),
        shot_meta.get("bytes"),
        "images=",
        len(image_blocks),
        "locked=",
        shot_meta.get("session_locked"),
        "url=",
        shot_meta.get("url"),
    )
    if image_blocks:
        import base64

        Path(args.shot).write_bytes(base64.b64decode(image_blocks[0]["data"]))
        print("screenshot saved:", args.shot)

    input_result = call(args.url, token, "input_control", {"actions": [{"type": "wait", "ms": 10}]}, 6)
    print("input:", input_result.get("isError"), text_of(input_result)[:100].replace("\n", " "))

    status = call(args.url, token, "control_features", {"action": "status"}, 7)
    print("control status isError:", status.get("isError"))

    sleep_on = call(args.url, token, "set_sleep_block", {"enable": True}, 8)
    sleep_on_json = json.loads(text_of(sleep_on))
    print("sleep on:", sleep_on_json["sleep"]["active"], sleep_on_json["sleep"]["keep_display"])
    sleep_off = call(args.url, token, "set_sleep_block", {"enable": False}, 9)
    sleep_off_json = json.loads(text_of(sleep_off))
    print("sleep off:", sleep_off_json["sleep"]["active"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
