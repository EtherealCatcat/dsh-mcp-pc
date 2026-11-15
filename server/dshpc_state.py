"""Configuration and feature state for the controlled Windows PC.

Layout under the data directory (``%LOCALAPPDATA%\\DSHRemotePC\\data`` by
default)::

    config.json     ports, bind mode, bearer token, limits
    state.json      feature switches shared by local CLI and remote MCP tools
    server.pid      running server process id
    logs\\           rotating server log
    screenshots\\    recent screenshots served to the controller

Both the server and the local control CLI read/write ``state.json``.  Writes
are atomic (temp file + ``os.replace``) and serialised across processes with a
small lock file, so a remote ``control_features`` call and a local
``dshpc disable shell`` cannot corrupt the file.
"""

from __future__ import annotations

import copy
import json
import os
import secrets
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import dshpc_version

try:  # Windows only, but keep the module importable for tests elsewhere.
    import msvcrt
except Exception:  # pragma: no cover - non-Windows
    msvcrt = None  # type: ignore[assignment]

FEATURES: tuple[str, ...] = ("info", "shell", "screenshot", "input", "sleep")

FEATURE_LABELS: dict[str, dict[str, str]] = {
    "info": {"zh": "系统信息", "en": "System information"},
    "shell": {"zh": "命令执行", "en": "Shell / PowerShell execution"},
    "screenshot": {"zh": "屏幕截图", "en": "Screen capture"},
    "input": {"zh": "键鼠操作", "en": "Keyboard and mouse input"},
    "sleep": {"zh": "阻止睡眠", "en": "Sleep prevention"},
}

FEATURE_ALIASES: dict[str, str] = {
    "info": "info",
    "system_info": "info",
    "system-info": "info",
    "sysinfo": "info",
    "shell": "shell",
    "command": "shell",
    "commands": "shell",
    "pwsh": "shell",
    "powershell": "shell",
    "exec": "shell",
    "screenshot": "screenshot",
    "screen": "screenshot",
    "capture": "screenshot",
    "display": "screenshot",
    "input": "input",
    "keyboard": "input",
    "mouse": "input",
    "input_control": "input",
    "sleep": "sleep",
    "power": "sleep",
    "keep_awake": "sleep",
}

# ---------------------------------------------------------------------------
# Single source of truth for limits.  The same numbers appear in the MCP JSON
# schemas (``dshpc_tools``), the gateway (``gateway/timeout.js``) and the
# PowerShell installer, which cannot import this module; tests check that those
# copies stay equal.  Change a value here first, then run the test suite.
# ---------------------------------------------------------------------------
DEFAULT_PORT = dshpc_version.DEFAULT_PORT
MAX_REQUEST_BYTES = 2 * 1024 * 1024
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUPS = 3
DEFAULT_TIMEOUT_SEC = 60
MAX_TIMEOUT_SEC = 600
MAX_OUTPUT_CHARS = 20_000
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
LOG_COMMAND_CHARS = 500
SCREENSHOT_RETENTION_HOURS = 24
SCREENSHOT_MAX_FILES = 200

CONFIG_DEFAULTS: dict[str, Any] = {
    "version": 1,
    "port": DEFAULT_PORT,
    "bind": "tailscale",  # tailscale | address | all
    "bind_address": "",
    "public_host": "",
    "token": "",
    "log_level": "INFO",
    "log_max_bytes": LOG_MAX_BYTES,
    "log_backups": LOG_BACKUPS,
    "log_commands": True,
    "log_command_chars": LOG_COMMAND_CHARS,
    "info_gate": True,
    "max_request_bytes": MAX_REQUEST_BYTES,
    "shell": {
        "default_timeout_sec": DEFAULT_TIMEOUT_SEC,
        "max_timeout_sec": MAX_TIMEOUT_SEC,
        "max_output_chars": MAX_OUTPUT_CHARS,
        "max_output_bytes": MAX_OUTPUT_BYTES,
        "blocklist": [],
    },
    "screenshot": {
        "max_width": 0,
        "format": "png",
        "jpeg_quality": 85,
        "include_image": True,
        "retention_hours": SCREENSHOT_RETENTION_HOURS,
        "max_files": SCREENSHOT_MAX_FILES,
    },
    "sleep": {
        "keep_display_default": False,
    },
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_data_dir() -> Path:
    override = os.environ.get("DSHPC_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "DSHRemotePC" / "data"
    # POSIX: the server is Windows-only, but the module is imported by tests and
    # by ``dshpc config`` on other platforms.  Do not create ~/AppData there.
    return Path.home() / ".dshpc"


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge ``override`` into a copy of ``base`` recursively."""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def normalize_feature(name: str) -> str | None:
    key = str(name or "").strip().lower()
    return FEATURE_ALIASES.get(key)


def normalize_features(names: Iterable[str]) -> tuple[list[str], list[str]]:
    good: list[str] = []
    bad: list[str] = []
    for name in names or []:
        feature = normalize_feature(name)
        if feature is None:
            bad.append(str(name))
        elif feature not in good:
            good.append(feature)
    return good, bad


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


@contextmanager
def _file_lock(path: Path, timeout: float = 5.0):
    """Best-effort cross-process lock for state writes (Windows msvcrt)."""
    if msvcrt is None:
        yield
        return
    lock_path = path.with_name(path.name + ".lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
    except OSError:
        # Some very restricted environments cannot create the lock file. The
        # atomic replace below still prevents torn reads, so proceed unlocked.
        yield
        return
    acquired = False
    deadline = time.monotonic() + timeout
    try:
        handle.seek(0)
        if handle.read(1) == b"":
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        handle.close()


class Config:
    """Loaded ``config.json`` with defaults filled in."""

    def __init__(self, path: Path, data: dict[str, Any], *, created: bool = False):
        self.path = Path(path)
        self.data = deep_merge(CONFIG_DEFAULTS, data)
        self.created = created
        self.data_dir = self.path.parent
        if not self.data.get("token"):
            self.data["token"] = secrets.token_urlsafe(32)
        if not self.data.get("data_dir"):
            self.data["data_dir"] = str(self.path.parent)

    # -- accessors ---------------------------------------------------------
    @property
    def port(self) -> int:
        try:
            port = int(self.data.get("port", DEFAULT_PORT))
        except (TypeError, ValueError):
            port = DEFAULT_PORT
        return port if 1 <= port <= 65535 else DEFAULT_PORT

    @property
    def bind(self) -> str:
        value = str(self.data.get("bind") or "tailscale").strip().lower()
        return value if value in {"tailscale", "address", "all"} else "tailscale"

    @property
    def token(self) -> str:
        return str(self.data.get("token") or "")

    @property
    def info_gate(self) -> bool:
        return bool(self.data.get("info_gate", True))

    @property
    def shell(self) -> dict[str, Any]:
        return self.data.setdefault("shell", copy.deepcopy(CONFIG_DEFAULTS["shell"]))

    @property
    def screenshot(self) -> dict[str, Any]:
        return self.data.setdefault("screenshot", copy.deepcopy(CONFIG_DEFAULTS["screenshot"]))

    @property
    def sleep(self) -> dict[str, Any]:
        return self.data.setdefault("sleep", copy.deepcopy(CONFIG_DEFAULTS["sleep"]))

    def save(self) -> None:
        _atomic_write_json(self.path, self.data)

    # -- constructors ------------------------------------------------------
    @classmethod
    def load(cls, path: Path) -> "Config":
        raw = _read_json(path)
        return cls(path, raw or {}, created=raw is None)

    @classmethod
    def load_or_create(
        cls,
        path: Path,
        *,
        port: int | None = None,
        bind: str | None = None,
        bind_address: str | None = None,
        rotate_token: bool = False,
    ) -> "Config":
        path = Path(path)
        existing = cls.load(path)
        changed = existing.created
        if port is not None and int(port) != existing.port:
            existing.data["port"] = int(port)
            changed = True
        if bind:
            existing.data["bind"] = bind
            changed = True
        if bind_address is not None and bind_address != existing.data.get("bind_address"):
            existing.data["bind_address"] = bind_address
            changed = True
        if rotate_token or not existing.token:
            existing.data["token"] = secrets.token_urlsafe(32)
            changed = True
        if rotate_token:
            existing.data["token_rotated_at"] = utc_now_iso()
        if changed:
            existing.path.parent.mkdir(parents=True, exist_ok=True)
            existing.save()
        return existing


def initial_state(*, remote_control: bool = True, keep_display: bool = False) -> dict[str, Any]:
    return {
        "version": 1,
        "features": {name: True for name in FEATURES},
        "remote_control": bool(remote_control),
        "sleep_hold": False,
        "sleep_keep_display": bool(keep_display),
        "info_gate_reset_seq": 0,
        "updated_at": utc_now_iso(),
        "updated_by": "system",
    }


def _normalize_state(raw: dict[str, Any] | None) -> dict[str, Any]:
    state = initial_state()
    if not raw:
        return state
    features = raw.get("features") if isinstance(raw.get("features"), dict) else {}
    for name in FEATURES:
        state["features"][name] = bool(features.get(name, True))
    state["remote_control"] = bool(raw.get("remote_control", True))
    state["sleep_hold"] = bool(raw.get("sleep_hold", False))
    state["sleep_keep_display"] = bool(raw.get("sleep_keep_display", False))
    try:
        state["info_gate_reset_seq"] = int(raw.get("info_gate_reset_seq", 0))
    except (TypeError, ValueError):
        state["info_gate_reset_seq"] = 0
    if isinstance(raw.get("updated_at"), str):
        state["updated_at"] = raw["updated_at"]
    if isinstance(raw.get("updated_by"), str):
        state["updated_by"] = raw["updated_by"]
    return state


class StateStore:
    """Thread-safe, cross-process-safe feature state."""

    def __init__(self, path: Path, *, remote_control: bool = True, keep_display: bool = False):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._mtime_ns = -1
        self._size = -1
        if not self.path.exists():
            self._state = initial_state(remote_control=remote_control, keep_display=keep_display)
            self._write_locked(self._state)
        else:
            self._state = _normalize_state(_read_json(self.path))
            self._remember_stat()
        self._validate()

    # -- internals ---------------------------------------------------------
    def _validate(self) -> None:
        if not isinstance(self._state.get("features"), dict):
            self._state = initial_state()

    def _remember_stat(self) -> None:
        try:
            stat = self.path.stat()
            self._mtime_ns = stat.st_mtime_ns
            self._size = stat.st_size
        except OSError:
            self._mtime_ns = -1
            self._size = -1

    def _changed_on_disk(self) -> bool:
        try:
            stat = self.path.stat()
        except OSError:
            return False
        return stat.st_mtime_ns != self._mtime_ns or stat.st_size != self._size

    def _reload_locked(self, force: bool = False) -> None:
        if force or self._changed_on_disk():
            raw = _read_json(self.path)
            if raw is not None:
                self._state = _normalize_state(raw)
            self._remember_stat()

    def _write_locked(self, state: dict[str, Any]) -> None:
        with _file_lock(self.path):
            # Re-read under the lock so a concurrent CLI write is not clobbered.
            raw = _read_json(self.path)
            if raw is not None:
                state = deep_merge(_normalize_state(raw), state)
            _atomic_write_json(self.path, state)
            self._state = state
            self._remember_stat()

    # -- public API --------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._reload_locked()
            return copy.deepcopy(self._state)

    def features(self) -> dict[str, bool]:
        return self.snapshot()["features"]

    def is_enabled(self, feature: str) -> bool:
        name = normalize_feature(feature) or feature
        return bool(self.snapshot()["features"].get(name, False))

    def remote_control_enabled(self) -> bool:
        return bool(self.snapshot()["remote_control"])

    def update(self, mutator: Callable[[dict[str, Any]], None], *, by: str) -> dict[str, Any]:
        with self._lock:
            self._reload_locked(force=True)
            state = copy.deepcopy(self._state)
            mutator(state)
            state["version"] = 1
            state["updated_at"] = utc_now_iso()
            state["updated_by"] = by
            self._write_locked(state)
            return copy.deepcopy(self._state)

    def set_features(
        self, features: Iterable[str], enabled: bool, *, by: str
    ) -> tuple[dict[str, Any], list[str], list[str]]:
        requested = list(features)
        good, bad = normalize_features(requested)
        if not good:
            return self.snapshot(), [], bad

        def mutate(state: dict[str, Any]) -> None:
            for name in good:
                state["features"][name] = bool(enabled)
            if not enabled and "sleep" in good:
                state["sleep_hold"] = False

        return self.update(mutate, by=by), good, bad

    def set_all(self, enabled: bool, *, by: str) -> dict[str, Any]:
        def mutate(state: dict[str, Any]) -> None:
            for name in FEATURES:
                state["features"][name] = bool(enabled)
            if not enabled:
                state["sleep_hold"] = False

        return self.update(mutate, by=by)

    def set_remote_control(self, enabled: bool, *, by: str) -> dict[str, Any]:
        def mutate(state: dict[str, Any]) -> None:
            state["remote_control"] = bool(enabled)

        return self.update(mutate, by=by)

    def set_sleep_hold(self, enabled: bool, keep_display: bool, *, by: str) -> dict[str, Any]:
        def mutate(state: dict[str, Any]) -> None:
            state["sleep_hold"] = bool(enabled)
            state["sleep_keep_display"] = bool(keep_display)
            if enabled:
                state["features"]["sleep"] = True

        return self.update(mutate, by=by)

    def bump_info_gate(self, *, by: str) -> dict[str, Any]:
        def mutate(state: dict[str, Any]) -> None:
            state["info_gate_reset_seq"] = int(state.get("info_gate_reset_seq", 0)) + 1

        return self.update(mutate, by=by)


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def write_pid(path: Path, pid: int) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(pid), encoding="ascii")
    except OSError:
        pass


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return code.value == 259  # STILL_ACTIVE
                return True
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
