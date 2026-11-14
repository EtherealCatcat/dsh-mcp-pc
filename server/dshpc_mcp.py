"""Minimal, dependency-free MCP server (JSON-RPC 2.0) over Streamable HTTP.

The DSH MCP client negotiates the legacy ``initialize`` handshake (protocol
2025-11-25) and accepts a plain ``application/json`` reply for each POST, so
this server is stateless: no session id, POST returns one JSON-RPC response,
GET returns 405 (no server-initiated stream), and notifications are answered
with HTTP 202.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import dshpc_state
import dshpc_version
from dshpc_tools import Tools, ToolOutput

JSON = dict[str, Any]

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def build_instructions(*, hostname: str, port: int, version: str) -> str:
    return f"""# 被控端远程控制：{hostname}

本 MCP 服务器连接一台真实的 Windows 电脑（被控端主机名：{hostname}；端口：{port}；服务版本：{version}）。
这些工具会直接操作该电脑的命令行、屏幕、键盘鼠标和电源状态。

## 使用前提（必须遵守）
1. 只有当用户在当前对话中**明确要求**操作、查看或控制这台被控端电脑时，才调用本服务器的工具。
   用户没有明确要求时——包括用户只是在讨论、询问或操作控制端本机——绝不调用这些工具。
2. 每个新任务/新会话的第一次控制调用**必须**是 `get_system_info`，用来确认目标主机、登录/锁屏状态、
   屏幕是否唤醒以及功能开关。未调用前，服务器会拒绝其他工具并返回 `INFO_REQUIRED`。
3. 执行命令或键鼠操作前先确认目标主机与影响范围；删除数据、修改系统设置、关机/重启、安装/卸载软件等
   破坏性或不可逆操作必须先取得用户的明确同意。不要臆测用户意图，也不要“顺便”操作。
4. 截图可能包含聊天记录、密码提示等隐私内容；只在需要时截取，不要保存或转发到无关位置。
5. 功能可能被本机用户关闭。返回 `FEATURE_DISABLED`、`REMOTE_CONTROL_LOCKED` 或 `SESSION_LOCKED` 时，
   如实告知用户原因，不要尝试绕过。只有在用户明确要求时才调用 `control_features` 修改开关。
6. 任务完成后释放不需要的睡眠阻止（`set_sleep_block` enable=false），并如实汇报成功、失败和超时。

## 工具概览
- `get_system_info`：必选的首次调用；返回系统、硬件、显示器、会话/锁屏、电源、Tailscale、功能开关。
- `run_command`：在 pwsh（缺省回退 Windows PowerShell）、cmd 或 python 中执行命令，返回退出码与输出。
- `capture_screen`：截取全部或单个显示器，返回图像与可访问 URL；锁屏/UAC 安全桌面可能黑屏。
- `input_control`：批量移动/点击/滚轮/输入文本/按键；坐标为虚拟桌面物理像素。
- `set_sleep_block`：阻止或释放“空闲自动睡眠”；不能阻止合盖、电源键、手动睡眠，也不能唤醒已睡眠的机器。
- `control_features`：查看或修改 info/shell/screenshot/input/sleep 开关，支持一键全开/全关。

## 已知限制
- Windows 锁屏或 UAC 安全桌面下，截图可能黑屏，键鼠注入会被系统阻止。
- 以普通权限运行的被控端无法操作管理员权限窗口（UIPI）；UAC 提示无法自动点击。
- 屏幕截图不包含鼠标指针；DRM/受保护内容可能黑屏。
- 远程控制依赖 Tailscale 连通和本机已登录、屏幕唤醒；机器睡眠后连接会中断。
- 图像只有支持视觉输入的控制器模型才能直接看到；否则可使用返回的 URL 或本机路径。

## 安全
不要向公开渠道泄露端点 URL、Bearer token 或截图。若用户要求停止控制，可关闭全部功能
（`control_features` disable_all），或让本机用户运行 `dshpc all-off` / `dshpc remote-control off`。
"""


class McpProtocol:
    def __init__(self, *, tools: Tools, instructions: str, logger: Any):
        self.tools = tools
        self.instructions = instructions
        self.logger = logger
        self.initialized_at: float | None = None
        self.client_info: dict[str, Any] | None = None
        self._lock = threading.Lock()
        self.request_count = 0
        self.tool_call_count = 0

    # -- JSON-RPC envelope -------------------------------------------------
    @staticmethod
    def _result(request_id: Any, result: Any) -> JSON:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str, data: Any = None) -> JSON:
        error: JSON = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}

    def handle_payload(self, payload: Any) -> JSON | list[JSON] | None:
        """Process a decoded JSON-RPC payload; ``None`` means no response."""
        if isinstance(payload, list):
            if not payload:
                return self._error(None, INVALID_REQUEST, "empty batch")
            responses: list[JSON] = []
            for item in payload:
                response = self.handle_message(item)
                if response is not None:
                    responses.append(response)
            return responses or None
        return self.handle_message(payload)

    def handle_message(self, message: Any) -> JSON | None:
        if not isinstance(message, dict):
            return self._error(None, INVALID_REQUEST, "message must be a JSON object")
        is_notification = "id" not in message
        request_id = message.get("id")
        method = message.get("method")
        if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return None if is_notification else self._error(request_id, INVALID_REQUEST, "invalid JSON-RPC request")
        params = message.get("params")
        if params is not None and not isinstance(params, dict):
            return None if is_notification else self._error(request_id, INVALID_PARAMS, "params must be an object")

        if is_notification:
            if method == "notifications/initialized":
                self.logger.info("MCP initialized notification received")
            elif method == "notifications/cancelled":
                self.logger.debug("MCP cancellation notification: %s", (params or {}).get("requestId"))
            else:
                self.logger.debug("MCP notification ignored: %s", method)
            return None

        with self._lock:
            self.request_count += 1

        try:
            if method == "initialize":
                return self._result(request_id, self._initialize(params or {}))
            if method == "ping":
                return self._result(request_id, {})
            if method == "tools/list":
                return self._result(request_id, {"tools": self.tools.list_tools()})
            if method == "tools/call":
                try:
                    return self._result(request_id, self._call_tool(params or {}))
                except RpcError as error:
                    return self._error(request_id, error.code, error.message)
            if method in {"logging/setLevel"}:
                return self._result(request_id, {})
            if method in {"resources/list"}:
                return self._error(request_id, METHOD_NOT_FOUND, "resources are not provided by this server")
            if method in {"prompts/list"}:
                return self._error(request_id, METHOD_NOT_FOUND, "prompts are not provided by this server")
            return self._error(request_id, METHOD_NOT_FOUND, f"method not found: {method}")
        except Exception as error:  # noqa: BLE001
            self.logger.exception("MCP method %s failed", method)
            return self._error(request_id, INTERNAL_ERROR, f"{type(error).__name__}: {error}")

    # -- methods -----------------------------------------------------------
    def _initialize(self, params: dict[str, Any]) -> JSON:
        requested = params.get("protocolVersion")
        version = (
            requested
            if isinstance(requested, str) and requested in dshpc_version.SUPPORTED_PROTOCOL_VERSIONS
            else dshpc_version.SUPPORTED_PROTOCOL_VERSIONS[0]
        )
        client = params.get("clientInfo")
        self.client_info = client if isinstance(client, dict) else None
        self.initialized_at = time.time()
        self.logger.info(
            "MCP initialize: client=%s version=%s requested=%s",
            (self.client_info or {}).get("name", "unknown"),
            (self.client_info or {}).get("version", "unknown"),
            requested,
        )
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": dshpc_version.SERVICE_NAME,
                "title": dshpc_version.SERVICE_TITLE,
                "version": dshpc_version.__version__,
            },
            "instructions": self.instructions,
        }

    def _call_tool(self, params: dict[str, Any]) -> JSON:
        name = params.get("name")
        if not isinstance(name, str) or not name.strip():
            raise RpcError(INVALID_PARAMS, "tools/call requires a string name")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise RpcError(INVALID_PARAMS, "tools/call arguments must be an object")
        with self._lock:
            self.tool_call_count += 1
        try:
            output = self.tools.call(name, arguments)
        except KeyError:
            raise RpcError(INVALID_PARAMS, f"unknown tool: {name}") from None
        result: JSON = {"content": output.content, "isError": bool(output.is_error)}
        if output.structured is not None:
            result["structuredContent"] = output.structured
        return result


class DshpcHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    # Do not let another local process bind the same port (Windows SO_REUSEADDR
    # semantics would allow hijacking the bearer-token endpoint).
    allow_reuse_address = False

    def __init__(
        self,
        address: tuple[str, int],
        *,
        protocol: McpProtocol,
        token: str,
        logger: Any,
        store: Any,
        max_request_bytes: int,
    ):
        self.protocol = protocol
        self.token = token
        self.logger = logger
        self.store = store
        self.max_request_bytes = max(1024, int(max_request_bytes))
        super().__init__(address, McpHttpHandler)


class McpHttpHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "dsh-pc-remote/" + dshpc_version.__version__
    sys_version = ""

    # -- plumbing ----------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - stdlib name
        try:
            self.server.logger.debug("http %s - %s", self.address_string(), fmt % args)
        except Exception:
            pass

    def log_error(self, fmt: str, *args: Any) -> None:
        try:
            self.server.logger.warning("http error %s - %s", self.address_string(), fmt % args)
        except Exception:
            pass

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _send_json(self, status: int, value: Any, extra: dict[str, str] | None = None) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", extra)

    def _send_empty(self, status: int, extra: dict[str, str] | None = None) -> None:
        self._send(status, b"", "text/plain; charset=utf-8", extra)

    def _authorized(self) -> bool:
        expected = self.server.token
        provided = ""
        header = self.headers.get("Authorization", "")
        if header.lower().startswith("bearer "):
            provided = header[7:].strip()
        if not provided:
            provided = self.headers.get("X-DSHPC-Token", "").strip()
        if not provided or not expected:
            return False
        import hmac

        return hmac.compare_digest(provided, expected)

    # -- routes ------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        path = urlsplit(self.path).path
        if path == "/healthz":
            payload = {
                "ok": True,
                "service": dshpc_version.SERVICE_NAME,
                "version": dshpc_version.__version__,
                "host": socket.gethostname(),
                "time": dshpc_state.utc_now_iso(),
            }
            self._send_json(200, payload)
            return
        if path.startswith("/shot/"):
            self._serve_shot(path)
            return
        if path == "/mcp":
            self._send_empty(405, {"Allow": "POST"})
            return
        self._send_empty(404)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path != "/mcp":
            self._send_empty(404)
            return
        if not self._authorized():
            self._send_json(
                401,
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": "unauthorized"}},
                {"WWW-Authenticate": 'Bearer realm="dsh-pc-remote"'},
            )
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            self._send_empty(400)
            return
        if length <= 0:
            self._send_json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": PARSE_ERROR, "message": "empty body"}})
            return
        if length > self.server.max_request_bytes:
            self._send_empty(413)
            return
        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            self._send_json(
                400,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": PARSE_ERROR, "message": f"parse error: {error}"},
                },
            )
            return
        try:
            response = self.server.protocol.handle_payload(payload)
        except Exception as error:  # noqa: BLE001
            self.server.logger.exception("MCP dispatch failed")
            self._send_json(
                500,
                {"jsonrpc": "2.0", "id": None, "error": {"code": INTERNAL_ERROR, "message": str(error)}},
            )
            return
        if response is None:
            self._send_empty(202)
        else:
            self._send_json(200, response)

    def do_DELETE(self) -> None:  # noqa: N802
        self._send_empty(405, {"Allow": "POST"})

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send_empty(405, {"Allow": "POST"})

    # -- screenshot files --------------------------------------------------
    def _serve_shot(self, path: str) -> None:
        name = unquote(path[len("/shot/") :])
        match = re.match(r"^([0-9a-f]{32})\.(png|jpg|jpeg)$", name)
        if not match:
            self._send_empty(404)
            return
        shot_id, extension = match.group(1), match.group(2)
        file_path: Path | None = self.server.store.path_for(shot_id, extension)
        if file_path is None:
            self._send_empty(404)
            return
        content_type = "image/jpeg" if extension in {"jpg", "jpeg"} else "image/png"
        try:
            data = file_path.read_bytes()
        except OSError:
            self._send_empty(404)
            return
        self._send(200, data, content_type)


class StdioProtocol:
    """Line-delimited JSON-RPC on stdin/stdout, used for local diagnostics."""

    def __init__(self, protocol: McpProtocol):
        self.protocol = protocol

    def serve(self) -> int:
        import sys

        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as error:
                self._write({"jsonrpc": "2.0", "id": None, "error": {"code": PARSE_ERROR, "message": str(error)}})
                continue
            response = self.protocol.handle_payload(payload)
            if response is not None:
                self._write(response)
        return 0

    @staticmethod
    def _write(value: Any) -> None:
        import sys

        sys.stdout.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()
