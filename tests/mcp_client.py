"""Tiny MCP Streamable-HTTP client used by tests (no third-party deps)."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def post(url: str, token: str, payload: object) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read().decode("utf-8")
            return response.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as error:
        return error.code, {"http_error": error.code, "body": error.read().decode("utf-8", "replace")}


def rpc(url: str, token: str, method: str, params: dict | None = None, request_id: int = 1) -> dict:
    payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    status, response = post(url, token, payload)
    if status != 200:
        raise SystemExit(f"HTTP {status}: {json.dumps(response, ensure_ascii=False)}")
    if "error" in response:
        raise SystemExit(f"HTTP {status} JSON-RPC error: {response['error']}")
    return response.get("result", {})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--tool")
    parser.add_argument("--args", default="{}")
    parser.add_argument("--args-file")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--initialize", action="store_true")
    parser.add_argument("--raw", action="store_true")
    args = parser.parse_args(argv)

    if args.initialize:
        result = rpc(
            args.url,
            args.token,
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "dshpc-python-test", "version": "1.0.0"},
            },
        )
        print(json.dumps({"protocolVersion": result.get("protocolVersion"), "serverInfo": result.get("serverInfo")}))

    if args.list:
        result = rpc(args.url, args.token, "tools/list", {})
        print(json.dumps([tool["name"] for tool in result.get("tools", [])], ensure_ascii=False))
        return 0

    if args.tool:
        if args.args_file:
            parameters = json.loads(open(args.args_file, encoding="utf-8-sig").read())
        else:
            parameters = json.loads(args.args)
        result = rpc(args.url, args.token, "tools/call", {"name": args.tool, "arguments": parameters})
        if args.raw:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        print(f"isError={result.get('isError')}")
        for block in result.get("content", []):
            if block.get("type") == "text":
                print(block.get("text"))
            elif block.get("type") == "image":
                print(f"[image {block.get('mimeType')} {len(block.get('data', ''))} base64 chars]")
        return 1 if result.get("isError") else 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
