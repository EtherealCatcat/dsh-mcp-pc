"""MCP tools exposed to the controlling DeepSeek Harness.

Design rules enforced here:

* ``get_system_info`` must be the first control tool in every server session;
  the server refuses the rest until it has been called (``INFO_REQUIRED``).
* Every capability has an owner-controlled switch.  A disabled capability
  returns a visible ``FEATURE_DISABLED`` error and is never executed.
* ``control_features`` itself stays reachable so the controller can re-enable a
  capability, unless the owner locked remote management from the local CLI.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import dshpc_exec
import dshpc_info
import dshpc_state
import dshpc_version
import dshpc_win


class ToolFailure(Exception):
    def __init__(self, code: str, message: str, **extra: Any):
        self.code = code
        self.message = message
        self.extra = extra
        super().__init__(message)


class InfoGate:
    """In-memory first-call gate; reset by the server's state watcher.

    ``reset_seq`` is the state sequence already applied.  The watcher and the
    remote reset action share it so a reset that just happened cannot be
    re-applied a second later (which would re-lock the gate after the model
    already answered get_system_info).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._satisfied = False
        self._satisfied_at = 0.0
        self._reset_seq = 0

    @property
    def satisfied(self) -> bool:
        with self._lock:
            return self._satisfied

    def satisfy(self) -> None:
        with self._lock:
            self._satisfied = True
            self._satisfied_at = time.time()

    def reset(self, seq: int | None = None) -> None:
        with self._lock:
            self._satisfied = False
            self._satisfied_at = 0.0
            if seq is not None:
                self._reset_seq = int(seq)

    def apply_reset(self, seq: int, reset_at: float = 0.0) -> bool:
        """Reset for a new state sequence unless info was called after it.

        ``reset_at`` is when the reset was written (state ``updated_at``).  If
        ``get_system_info`` succeeded after that instant, the reset is already
        answered and must not lock the gate again.
        """
        with self._lock:
            if int(seq) == self._reset_seq:
                return False
            self._reset_seq = int(seq)
            if self._satisfied and self._satisfied_at >= reset_at:
                return False
            self._satisfied = False
            self._satisfied_at = 0.0
            return True


@dataclass
class ToolOutput:
    content: list[dict[str, Any]] = field(default_factory=list)
    is_error: bool = False
    structured: dict[str, Any] | None = None

    @classmethod
    def text(cls, text: str, *, is_error: bool = False, structured: dict[str, Any] | None = None) -> "ToolOutput":
        return cls(content=[{"type": "text", "text": text}], is_error=is_error, structured=structured)

    @classmethod
    def json(cls, value: Any, *, is_error: bool = False) -> "ToolOutput":
        return cls(
            content=[{"type": "text", "text": json.dumps(value, ensure_ascii=False, indent=2)}],
            is_error=is_error,
            structured=value if isinstance(value, dict) else None,
        )

    @classmethod
    def failure(cls, code: str, message: str, **extra: Any) -> "ToolOutput":
        payload = {"ok": False, "code": code, "message": message, **extra}
        return cls.text(
            f"{code}: {message}",
            is_error=True,
            structured=payload,
        )

    def with_image(self, data: bytes, mime: str) -> "ToolOutput":
        self.content.append(
            {
                "type": "image",
                "data": base64.b64encode(data).decode("ascii"),
                "mimeType": mime,
            }
        )
        return self


class ScreenshotStore:
    """Recent screenshots, kept only so the controller's user can open a URL."""

    _ID_RE = re.compile(r"^[0-9a-f]{32}$")

    def __init__(self, directory: Path, *, retention_hours: int = 24, max_files: int = 200):
        self.directory = Path(directory)
        self.retention_hours = max(1, int(retention_hours))
        self.max_files = max(10, int(max_files))
        self._lock = threading.Lock()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.cleanup()

    def save(self, data: bytes, extension: str = "png") -> str:
        shot_id = secrets.token_hex(16)
        path = self.directory / f"{shot_id}.{extension}"
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_bytes(data)
        os.replace(temp, path)
        self.cleanup()
        return shot_id

    def path_for(self, shot_id: str, extension: str) -> Path | None:
        if not self._ID_RE.match(shot_id or ""):
            return None
        candidate = self.directory / f"{shot_id}.{extension}"
        return candidate if candidate.is_file() else None

    def cleanup(self) -> None:
        with self._lock:
            now = time.time()
            files: list[tuple[float, Path]] = []
            try:
                for entry in self.directory.iterdir():
                    if not entry.is_file():
                        continue
                    if entry.suffix.lower() not in {".png", ".jpg", ".jpeg", ".tmp"}:
                        continue
                    try:
                        stat = entry.stat()
                    except OSError:
                        continue
                    if entry.suffix == ".tmp" or now - stat.st_mtime > self.retention_hours * 3600:
                        try:
                            entry.unlink()
                        except OSError:
                            pass
                        continue
                    files.append((stat.st_mtime, entry))
            except OSError:
                return
            files.sort(key=lambda item: item[0])
            for _, entry in files[: max(0, len(files) - self.max_files)]:
                try:
                    entry.unlink()
                except OSError:
                    pass


TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "get_system_info",
        "title": "Get controlled PC information (required first call)",
        "description": (
            "获取被控 Windows 电脑的基本信息：主机名、系统版本、CPU/内存/磁盘、显示器与 DPI、"
            "登录会话与锁屏状态、电源状态、网络与 Tailscale 地址、当前功能开关和服务状态。"
            "\n\n【强制】每个控制任务或新会话的第一次工具调用必须是本工具。在它成功返回前，"
            "服务器会拒绝其他控制工具并返回 INFO_REQUIRED。它用于确认目标机器、屏幕是否唤醒/锁屏、"
            "以及各功能是否开启。\n\n"
            "Only call this (and any tool here) when the user explicitly asked you to operate the "
            "remote PC. Use the result to confirm hostname, session/lock state and enabled features "
            "before taking action."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "run_command",
        "title": "Run a PowerShell, cmd or Python command",
        "description": (
            "在被控电脑上执行 PowerShell（默认 pwsh，缺失时回退 Windows PowerShell）、cmd 或 Python 命令，"
            "返回退出码、stdout、stderr 和耗时。命令默认 60 秒超时，超时会终止整个进程树。"
            "\n\n仅当用户明确要求在该被控端执行命令时调用。执行前确认目标机器（hostname）和影响范围；"
            "删除文件、修改系统设置、关机/重启、安装/卸载软件等破坏性或不可逆操作必须先取得用户明确同意。"
            "不要把被控端返回的敏感内容发到无关渠道。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的命令或脚本内容（可多行）。"},
                "shell": {
                    "type": "string",
                    "enum": ["pwsh", "powershell", "cmd", "python"],
                    "description": "命令解释器，默认 pwsh。",
                },
                "cwd": {"type": "string", "description": "工作目录；必须是已存在的绝对路径。"},
                "timeout_sec": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 600,
                    "description": "超时秒数，默认 60，最大 600。",
                },
                "env": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "追加或覆盖的环境变量。",
                },
                "stdin": {"type": "string", "description": "可选的标准输入文本。"},
                "max_output_chars": {
                    "type": "integer",
                    "minimum": 1000,
                    "maximum": 100000,
                    "description": "stdout/stderr 各自的截断上限，默认 20000。",
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
    },
    {
        "name": "capture_screen",
        "title": "Capture the screen",
        "description": (
            "截取被控电脑的整个虚拟桌面（默认包含所有显示器），或指定某一台显示器。返回 PNG/JPEG 图像内容"
            "以及可访问的 URL（若已启用 Tailscale 地址）和本地保存路径。图像可能很大；仅在需要查看画面时调用。"
            "\n\n截图可能包含隐私信息（聊天窗口、密码提示等），只在用户明确要求时截取；"
            "锁屏/UAC 安全桌面下会得到黑屏或失败，这是 Windows 的限制。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "monitor": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 16,
                    "description": "0 = 所有显示器（默认），1..N 指定单个显示器。",
                },
                "max_width": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 7680,
                    "description": "按宽度等比缩小；0 表示原始分辨率。",
                },
                "format": {"type": "string", "enum": ["png", "jpeg"], "description": "默认 png。"},
                "quality": {"type": "integer", "minimum": 1, "maximum": 100, "description": "JPEG 质量，默认 85。"},
                "include_image": {
                    "type": "boolean",
                    "description": "是否在结果中直接返回图像数据，默认 true。若控制器模型不支持图像可设为 false。",
                },
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "input_control",
        "title": "Control keyboard and mouse",
        "description": (
            "向被控电脑的真实桌面注入键鼠操作，可一次执行多个动作：移动鼠标、点击、滚轮、输入文本、"
            "按键/快捷键（如 ctrl+shift+s）、等待。坐标是虚拟桌面物理像素坐标（可由 get_system_info 的 "
            "displays 字段获得）。\n\n"
            "这会真实操作用户的电脑：仅当用户明确要求在被控端进行键鼠操作时调用；执行前尽量先截图确认窗口状态；"
            "不要用于绕过 UAC、锁屏或其他安全边界（Windows 也会阻止）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "actions": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 100,
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": ["move", "click", "scroll", "type", "key", "wait"],
                            },
                            "x": {"type": "integer", "description": "虚拟桌面 X 坐标（move/click 可选）。"},
                            "y": {"type": "integer", "description": "虚拟桌面 Y 坐标（move/click 可选）。"},
                            "duration_ms": {"type": "integer", "minimum": 0, "maximum": 5000},
                            "button": {"type": "string", "enum": ["left", "right", "middle"]},
                            "count": {"type": "integer", "minimum": 1, "maximum": 5},
                            "interval_ms": {"type": "integer", "minimum": 0, "maximum": 5000},
                            "amount": {"type": "integer", "minimum": -100, "maximum": 100, "description": "滚轮格数，正数向上。"},
                            "horizontal": {"type": "boolean"},
                            "text": {"type": "string", "maxLength": 4000, "description": "要输入的文本（支持中文）。"},
                            "keys": {"type": "string", "description": "按键或组合键，例如 enter、ctrl+c、alt+tab。"},
                            "repeat": {"type": "integer", "minimum": 1, "maximum": 50},
                            "ms": {"type": "integer", "minimum": 0, "maximum": 60000},
                        },
                        "required": ["type"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["actions"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False},
    },
    {
        "name": "set_sleep_block",
        "title": "Prevent or allow system sleep",
        "description": (
            "阻止被控电脑因空闲而自动睡眠（可选同时保持显示器常亮），或释放该阻止。"
            "实现方式是 SetThreadExecutionState(ES_CONTINUOUS|ES_SYSTEM_REQUIRED)。"
            "\n\n限制：它只阻止“空闲超时”睡眠，不能阻止合盖、按电源键、手动睡眠、电量严重不足等"
            "由 Windows 策略/用户触发的睡眠，也不能唤醒已经睡眠的机器。任务结束后应释放，"
            "避免长期占用电源策略。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "enable": {"type": "boolean", "description": "true = 阻止睡眠，false = 释放。"},
                "keep_display": {"type": "boolean", "description": "同时阻止显示器关闭，默认 false。"},
            },
            "required": ["enable"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "openWorldHint": False},
    },
    {
        "name": "control_features",
        "title": "Manage controlled-PC capabilities",
        "description": (
            "查看或修改被控端的功能开关：info（系统信息）、shell（命令执行）、screenshot（截图）、"
            "input（键鼠）、sleep（阻止睡眠）。支持单项、批量、一键全开/全关以及重置首次调用门禁。"
            "关闭某功能后，对应工具调用会返回 FEATURE_DISABLED，且不会执行任何操作。"
            "\n\n远程管理只在被控端未锁定时可用；status 始终可读。只有用户明确要求时才修改开关。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["status", "enable", "disable", "enable_all", "disable_all", "reset_info_gate"],
                    "description": "要执行的管理动作。",
                },
                "features": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(dshpc_state.FEATURES)},
                    "description": "enable/disable 时指定功能名，可多个。",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "openWorldHint": False},
    },
]

FEATURE_BY_TOOL = {
    "get_system_info": "info",
    "run_command": "shell",
    "capture_screen": "screenshot",
    "input_control": "input",
    "set_sleep_block": "sleep",
}


class Tools:
    def __init__(
        self,
        *,
        config: Any,
        state: dshpc_state.StateStore,
        gate: InfoGate,
        sleep: dshpc_win.SleepBlocker,
        store: ScreenshotStore,
        url_base_provider: Callable[[], str],
        logger: Any,
        started_at: float,
    ):
        self.config = config
        self.state = state
        self.gate = gate
        self.sleep = sleep
        self.store = store
        self.url_base = url_base_provider
        self.logger = logger
        self.started_at = started_at

    # -- public ------------------------------------------------------------
    def list_tools(self) -> list[dict[str, Any]]:
        return TOOL_DEFS

    def call(self, name: str, arguments: dict[str, Any] | None) -> ToolOutput:
        args = arguments or {}
        if not isinstance(args, dict):
            return ToolOutput.failure("INVALID_ARGUMENT", "arguments must be a JSON object")
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            raise KeyError(name)

        feature = FEATURE_BY_TOOL.get(name)
        if name not in {"get_system_info", "control_features"} and self.config.info_gate and not self.gate.satisfied:
            return ToolOutput.failure(
                "INFO_REQUIRED",
                "首次控制调用前必须先调用 get_system_info，确认目标机器、登录/锁屏状态和功能开关。"
                "请先调用 get_system_info，然后再重试本操作。",
                tool=name,
            )
        if feature is not None and not self.state.is_enabled(feature):
            return self._disabled(feature)

        try:
            return handler(args)
        except ToolFailure as error:
            return ToolOutput.failure(error.code, error.message, **error.extra)
        except (dshpc_exec.BlockedCommand, dshpc_exec.ExecError) as error:
            return ToolOutput.failure("EXEC_FAILED", str(error))
        except dshpc_win.WinError as error:
            code = "SESSION_LOCKED" if "SESSION_LOCKED" in str(error) else "WIN32_ERROR"
            return ToolOutput.failure(code, str(error))
        except Exception as error:  # noqa: BLE001 - surfaced to the model
            self.logger.exception("tool %s failed", name)
            return ToolOutput.failure("INTERNAL_ERROR", f"{type(error).__name__}: {error}")

    # -- helpers -----------------------------------------------------------
    def _disabled(self, feature: str) -> ToolOutput:
        labels = dshpc_state.FEATURE_LABELS.get(feature, {})
        label = labels.get("zh", feature)
        message = (
            f"被控端已关闭功能 {feature}（{label}），本次操作未执行。"
            f" 若需使用，请让本机用户运行 `dshpc enable {feature}`，"
            "或在远程管理未锁定时由用户明确要求后调用 control_features。"
        )
        return ToolOutput.failure(
            "FEATURE_DISABLED",
            message,
            feature=feature,
            feature_label=label,
            enabled=False,
        )

    def _status_payload(self) -> dict[str, Any]:
        state = self.state.snapshot()
        return {
            "features": state["features"],
            "feature_labels": {name: dshpc_state.FEATURE_LABELS[name] for name in dshpc_state.FEATURES},
            "remote_control": state["remote_control"],
            "sleep": self.sleep.status(),
            "sleep_hold": state["sleep_hold"],
            "sleep_keep_display": state["sleep_keep_display"],
            "info_gate": {"required": bool(self.config.info_gate), "satisfied": self.gate.satisfied},
            "updated_at": state.get("updated_at"),
            "updated_by": state.get("updated_by"),
        }

    @staticmethod
    def _reject_unknown(args: dict[str, Any], allowed: set[str]) -> None:
        unknown = [key for key in args if key not in allowed]
        if unknown:
            raise ToolFailure(
                "INVALID_ARGUMENT",
                f"unknown argument(s): {', '.join(unknown)}; allowed: {', '.join(sorted(allowed))}",
            )

    @staticmethod
    def _int(args: dict[str, Any], key: str, default: int, minimum: int, maximum: int) -> int:
        value = args.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ToolFailure("INVALID_ARGUMENT", f"{key} must be an integer")
        value = int(value)
        if value < minimum or value > maximum:
            raise ToolFailure("INVALID_ARGUMENT", f"{key} must be within {minimum}..{maximum}")
        return value

    # -- tools -------------------------------------------------------------
    def _tool_get_system_info(self, args: dict[str, Any]) -> ToolOutput:
        self._reject_unknown(args, set())
        state = self.state.snapshot()
        info = dshpc_info.collect(
            server={
                "name": dshpc_version.SERVICE_NAME,
                "title": dshpc_version.SERVICE_TITLE,
                "version": dshpc_version.__version__,
                "port": self.config.port,
                "bind": self.config.bind,
                "info_gate": bool(self.config.info_gate),
            },
            state=state,
            sleep_status=self.sleep.status(),
            urls=[self.url_base() + "/mcp"] if self.url_base() else [],
            data_dir=Path(self.config.data_dir),
            started_at=self.started_at,
        )
        info["info_gate"] = {
            "required": bool(self.config.info_gate),
            "satisfied": True,
            "note": "get_system_info completed; other control tools are unlocked for this server session.",
        }
        self.gate.satisfy()
        self.logger.info(
            "tool=get_system_info host=%s session=%s locked=%s",
            info.get("hostname"),
            info.get("session", {}).get("id"),
            info.get("session", {}).get("locked"),
        )
        return ToolOutput.json(info)

    def _tool_run_command(self, args: dict[str, Any]) -> ToolOutput:
        self._reject_unknown(
            args,
            {"command", "shell", "cwd", "timeout_sec", "env", "stdin", "max_output_chars"},
        )
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ToolFailure("INVALID_ARGUMENT", "command must be a non-empty string")
        shell = args.get("shell", "pwsh")
        if not isinstance(shell, str):
            raise ToolFailure("INVALID_ARGUMENT", "shell must be a string")
        env = args.get("env")
        if env is not None and not isinstance(env, dict):
            raise ToolFailure("INVALID_ARGUMENT", "env must be an object of string values")
        stdin = args.get("stdin")
        if stdin is not None and not isinstance(stdin, str):
            raise ToolFailure("INVALID_ARGUMENT", "stdin must be a string")
        cwd = args.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            raise ToolFailure("INVALID_ARGUMENT", "cwd must be a string")

        shell_cfg = self.config.shell
        timeout = self._int(
            args,
            "timeout_sec",
            int(shell_cfg.get("default_timeout_sec", 60)),
            1,
            min(600, int(shell_cfg.get("max_timeout_sec", 600))),
        )
        max_output = self._int(
            args, "max_output_chars", int(shell_cfg.get("max_output_chars", 20000)), 1000, 100000
        )
        if self.config.data.get("log_commands", True):
            self.logger.info("tool=run_command shell=%s command=%s", shell, command[:500].replace("\n", "\\n"))
        result = dshpc_exec.run_command(
            command,
            shell=shell,
            cwd=cwd,
            timeout_sec=timeout,
            env=env,
            stdin=stdin,
            max_output_chars=max_output,
            blocklist=list(shell_cfg.get("blocklist") or []),
        )
        result["ok"] = result["exit_code"] == 0 and not result["timed_out"]
        if result["timed_out"]:
            result["note"] = f"command exceeded timeout_sec={timeout} and its process tree was terminated"
        return ToolOutput.json(result, is_error=bool(result["timed_out"]))

    def _tool_capture_screen(self, args: dict[str, Any]) -> ToolOutput:
        self._reject_unknown(args, {"monitor", "max_width", "format", "quality", "include_image"})
        cfg = self.config.screenshot
        monitor = self._int(args, "monitor", 0, 0, 16)
        max_width = self._int(args, "max_width", int(cfg.get("max_width", 0)), 0, 7680)
        quality = self._int(args, "quality", int(cfg.get("jpeg_quality", 85)), 1, 100)
        fmt = str(args.get("format", cfg.get("format", "png"))).lower()
        if fmt not in {"png", "jpeg", "jpg"}:
            raise ToolFailure("INVALID_ARGUMENT", "format must be png or jpeg")
        include_image = args.get("include_image", bool(cfg.get("include_image", True)))
        if not isinstance(include_image, bool):
            raise ToolFailure("INVALID_ARGUMENT", "include_image must be a boolean")

        capture = dshpc_win.capture_screen(monitor=monitor, max_width=max_width, fmt=fmt, quality=quality)
        extension = "jpg" if capture["mime"] == "image/jpeg" else "png"
        shot_id = self.store.save(capture["data"], extension)
        base = self.url_base()
        url = f"{base}/shot/{shot_id}.{extension}" if base else None
        local_path = str(self.store.directory / f"{shot_id}.{extension}")
        payload = {
            "ok": True,
            "captured_at": dshpc_state.utc_now_iso(),
            "width": capture["width"],
            "height": capture["height"],
            "format": extension,
            "bytes": len(capture["data"]),
            "monitor": capture["monitor"],
            "monitor_count": capture["monitor_count"],
            "monitors": capture["monitors"],
            "virtual_screen": capture["virtual_screen"],
            "session_locked": capture["locked"],
            "url": url,
            "local_path": local_path,
            "image_included": include_image,
        }
        if capture["locked"]:
            payload["warning"] = (
                "Windows session appears locked or on a secure desktop; the captured image may be black."
            )
        output = ToolOutput.json(payload)
        if include_image:
            output.with_image(capture["data"], capture["mime"])
        self.logger.info(
            "tool=capture_screen %sx%s monitor=%s bytes=%s locked=%s",
            capture["width"],
            capture["height"],
            capture["monitor"],
            len(capture["data"]),
            capture["locked"],
        )
        return output

    def _tool_input_control(self, args: dict[str, Any]) -> ToolOutput:
        self._reject_unknown(args, {"actions"})
        actions = args.get("actions")
        if not isinstance(actions, list) or not actions:
            raise ToolFailure("INVALID_ARGUMENT", "actions must be a non-empty array")
        if len(actions) > 100:
            raise ToolFailure("INVALID_ARGUMENT", "at most 100 actions per call")
        allowed = {
            "type",
            "x",
            "y",
            "duration_ms",
            "button",
            "count",
            "interval_ms",
            "amount",
            "horizontal",
            "text",
            "keys",
            "repeat",
            "ms",
        }
        for index, action in enumerate(actions):
            if not isinstance(action, dict):
                raise ToolFailure("INVALID_ARGUMENT", f"action #{index + 1} must be an object")
            unknown = [key for key in action if key not in allowed]
            if unknown:
                raise ToolFailure(
                    "INVALID_ARGUMENT", f"action #{index + 1} has unknown fields: {', '.join(unknown)}"
                )
            if dshpc_win.is_session_locked():
                return ToolOutput.failure(
                    "SESSION_LOCKED",
                    "Windows 当前处于锁屏或安全桌面，键鼠注入被系统阻止。请先在本机解锁屏幕后重试。",
                )
            kind = str(action.get("type", "")).lower()
            if kind in {"type", "text", "type_text"}:
                text = action.get("text")
                if not isinstance(text, str) or not text:
                    raise ToolFailure("INVALID_ARGUMENT", f"action #{index + 1} requires non-empty text")
                if len(text) > 4000:
                    raise ToolFailure("INVALID_ARGUMENT", f"action #{index + 1} text exceeds 4000 characters")
            if kind in {"key", "keys", "press", "hotkey"}:
                keys = action.get("keys")
                if not isinstance(keys, str) or not keys.strip():
                    raise ToolFailure("INVALID_ARGUMENT", f"action #{index + 1} requires keys")
                if len(keys) > 100:
                    raise ToolFailure("INVALID_ARGUMENT", f"action #{index + 1} keys is too long")
        results = dshpc_win.perform_actions(actions)
        payload = {
            "ok": True,
            "actions": results,
            "cursor": dshpc_win.cursor_position(),
            "session_locked": dshpc_win.is_session_locked(),
        }
        self.logger.info("tool=input_control actions=%s", len(actions))
        return ToolOutput.json(payload)

    def _tool_set_sleep_block(self, args: dict[str, Any]) -> ToolOutput:
        self._reject_unknown(args, {"enable", "keep_display"})
        enable = args.get("enable")
        if not isinstance(enable, bool):
            raise ToolFailure("INVALID_ARGUMENT", "enable must be a boolean")
        keep_display = args.get("keep_display", bool(self.config.sleep.get("keep_display_default", False)))
        if not isinstance(keep_display, bool):
            raise ToolFailure("INVALID_ARGUMENT", "keep_display must be a boolean")
        self.state.set_sleep_hold(enable, keep_display and enable, by="remote-dsh")
        self.sleep.set(enable, keep_display and enable)
        time.sleep(0.1)
        payload = {
            "ok": True,
            "requested": enable,
            "keep_display": keep_display and enable,
            "sleep": self.sleep.status(),
            "note": (
                "Idle system sleep is now suppressed while this server runs."
                if enable
                else "Sleep-prevention request released."
            ),
            "limitations": (
                "This does not override lid-close, power-button, manual Sleep, battery-critical policy, "
                "or shutdown; it cannot wake an already sleeping PC."
            ),
        }
        self.logger.info("tool=set_sleep_block enable=%s keep_display=%s", enable, keep_display)
        return ToolOutput.json(payload)

    def _tool_control_features(self, args: dict[str, Any]) -> ToolOutput:
        self._reject_unknown(args, {"action", "features"})
        action = args.get("action")
        if not isinstance(action, str):
            raise ToolFailure("INVALID_ARGUMENT", "action must be a string")
        action = action.strip().lower()
        raw_features = args.get("features", [])
        if raw_features is None:
            raw_features = []
        if not isinstance(raw_features, list) or any(not isinstance(item, str) for item in raw_features):
            raise ToolFailure("INVALID_ARGUMENT", "features must be an array of feature names")

        state = self.state.snapshot()
        if action != "status" and not state.get("remote_control", True):
            return ToolOutput.failure(
                "REMOTE_CONTROL_LOCKED",
                "被控端已锁定远程功能管理（本机脚本执行了 dshpc remote-control off）。"
                "远程只能查看 status；如需修改，请让本机用户运行 dshpc remote-control on。",
            )

        if action == "status":
            payload = self._status_payload()
            payload["ok"] = True
            return ToolOutput.json(payload)

        if action in {"enable", "disable"}:
            if not raw_features:
                raise ToolFailure("INVALID_ARGUMENT", f"{action} requires a non-empty features array")
            good, bad = dshpc_state.normalize_features(raw_features)
            if bad:
                raise ToolFailure(
                    "INVALID_ARGUMENT",
                    f"unknown feature(s): {', '.join(bad)}; valid: {', '.join(dshpc_state.FEATURES)}",
                )
            updated, good, _ = self.state.set_features(good, action == "enable", by="remote-dsh")
            self._after_state_change(updated)
            payload = self._status_payload()
            payload.update({"ok": True, "changed": good, "enabled": action == "enable"})
            self.logger.info("tool=control_features action=%s features=%s", action, ",".join(good))
            return ToolOutput.json(payload)

        if action in {"enable_all", "disable_all"}:
            updated = self.state.set_all(action == "enable_all", by="remote-dsh")
            self._after_state_change(updated)
            payload = self._status_payload()
            payload.update({"ok": True, "changed": list(dshpc_state.FEATURES), "enabled": action == "enable_all"})
            self.logger.info("tool=control_features action=%s", action)
            return ToolOutput.json(payload)

        if action == "reset_info_gate":
            updated = self.state.bump_info_gate(by="remote-dsh")
            self.gate.reset(updated.get("info_gate_reset_seq"))
            payload = self._status_payload()
            payload["ok"] = True
            payload["note"] = "The next control call must be get_system_info again."
            self.logger.info("tool=control_features action=reset_info_gate")
            return ToolOutput.json(payload)

        raise ToolFailure(
            "INVALID_ARGUMENT",
            f"unknown action {action!r}; use status, enable, disable, enable_all, disable_all or reset_info_gate",
        )

    def _after_state_change(self, state: dict[str, Any]) -> None:
        """Apply side effects that must not wait for the watcher tick."""
        if not state["features"].get("sleep") or not state.get("sleep_hold"):
            self.sleep.release()
