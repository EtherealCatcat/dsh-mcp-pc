"""Command execution for the controlled PC (PowerShell / cmd / Python)."""

from __future__ import annotations

import locale
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

CREATE_NO_WINDOW = 0x08000000


class ExecError(RuntimeError):
    pass


class BlockedCommand(ExecError):
    pass


def _decode(data: bytes) -> str:
    if not data:
        return ""
    for encoding in ("utf-8", locale.getpreferredencoding(False) or "utf-8", "gbk", "cp936"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if limit <= 0 or len(text) <= limit:
        return text, False
    head = max(1, int(limit * 0.6))
    tail = max(1, limit - head)
    marker = f"\n\n... [truncated {len(text) - head - tail} characters] ...\n\n"
    return text[:head] + marker + text[-tail:], True


def _find_powershell() -> str:
    for candidate in ("pwsh.exe", "pwsh", "powershell.exe", "powershell"):
        path = shutil.which(candidate)
        if path:
            return path
    for candidate in (
        r"C:\Program Files\PowerShell\7\pwsh.exe",
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
    ):
        if os.path.isfile(candidate):
            return candidate
    raise ExecError("PowerShell is not available on this machine")


def _resolve_shell(name: str) -> str:
    key = (name or "pwsh").strip().lower()
    if key in {"pwsh", "powershell", "ps", "ps1"}:
        return "powershell"
    if key in {"cmd", "cmd.exe", "batch", "bat"}:
        return "cmd"
    if key in {"python", "py"}:
        return "python"
    if key in {"auto", "default"}:
        return "powershell"
    raise ExecError(f"unsupported shell {name!r}; use pwsh, powershell, cmd or python")


def _popen_kwargs() -> dict[str, Any]:
    return {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "creationflags": CREATE_NO_WINDOW}


def _read_stream(stream, sink: bytearray, cap: int) -> None:
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            if len(sink) < cap:
                sink.extend(chunk[: cap - len(sink)])
    except Exception:
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _kill_tree(pid: int) -> None:
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        pass


def run_command(
    command: str,
    *,
    shell: str = "pwsh",
    cwd: str | None = None,
    timeout_sec: int = 60,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
    max_output_chars: int = 20000,
    blocklist: list[str] | None = None,
) -> dict[str, Any]:
    if not command or not str(command).strip():
        raise ExecError("command is empty")
    if len(command) > 60000:
        raise ExecError("command is too long (limit 60000 characters)")

    for pattern in blocklist or []:
        try:
            if re.search(pattern, command, flags=re.IGNORECASE):
                raise BlockedCommand(f"command rejected by the local blocklist pattern: {pattern}")
        except re.error:
            continue

    if cwd:
        cwd_path = Path(cwd).expanduser()
        if not cwd_path.is_dir():
            raise ExecError(f"working directory does not exist: {cwd}")
        cwd = str(cwd_path)

    kind = _resolve_shell(shell)
    timeout_sec = max(1, min(600, int(timeout_sec or 60)))
    child_env = dict(os.environ)
    child_env.setdefault("PYTHONIOENCODING", "utf-8")
    if env:
        for key, value in env.items():
            if not isinstance(key, str) or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
                raise ExecError(f"invalid environment variable name: {key!r}")
            child_env[key] = str(value)

    temp_path: str | None = None
    try:
        if kind == "powershell":
            executable = _find_powershell()
            fd, temp_path = tempfile.mkstemp(prefix="dshpc-", suffix=".ps1")
            with os.fdopen(fd, "w", encoding="utf-8-sig", newline="\r\n") as handle:
                handle.write(
                    "$ProgressPreference = 'SilentlyContinue'\n"
                    "try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch {}\n"
                    "$OutputEncoding = [Console]::OutputEncoding\n"
                )
                handle.write(command)
                if not command.endswith("\n"):
                    handle.write("\n")
            args = [
                executable,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                temp_path,
            ]
        elif kind == "cmd":
            executable = os.environ.get("COMSPEC") or r"C:\Windows\System32\cmd.exe"
            args = [executable, "/D", "/S", "/C", command]
        else:
            executable = sys.executable
            args = [executable, "-c", command]

        started = time.time()
        process = subprocess.Popen(args, cwd=cwd, env=child_env, **_popen_kwargs())
        stdout = bytearray()
        stderr = bytearray()
        cap = 4 * 1024 * 1024
        readers = [
            threading.Thread(target=_read_stream, args=(process.stdout, stdout, cap), daemon=True),
            threading.Thread(target=_read_stream, args=(process.stderr, stderr, cap), daemon=True),
        ]
        for reader in readers:
            reader.start()
        if stdin is not None and process.stdin is not None:
            try:
                process.stdin.write(str(stdin).encode("utf-8"))
                process.stdin.close()
            except OSError:
                pass
        elif process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass

        timed_out = False
        try:
            exit_code = process.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(process.pid)
            try:
                exit_code = process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                exit_code = -1
        for reader in readers:
            reader.join(timeout=5)
        duration_ms = int((time.time() - started) * 1000)
        stdout_text, stdout_truncated = _truncate(_decode(bytes(stdout)), max_output_chars)
        stderr_text, stderr_truncated = _truncate(_decode(bytes(stderr)), max_output_chars)
        return {
            "exit_code": int(exit_code),
            "timed_out": timed_out,
            "duration_ms": duration_ms,
            "shell": kind,
            "executable": executable,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "cwd": cwd or os.getcwd(),
        }
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
