"""Aggregate machine information returned by the ``get_system_info`` tool."""

from __future__ import annotations

import ctypes
import datetime as _dt
import os
import platform
import shutil
import socket
import sys
import time
from pathlib import Path
from typing import Any

import dshpc_state
import dshpc_win

try:
    import winreg
except Exception:  # pragma: no cover - non-Windows
    winreg = None  # type: ignore[assignment]


def _registry_value(root, path: str, name: str) -> str | None:
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value)
    except Exception:
        return None


def _os_info() -> dict[str, Any]:
    current_version = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
    product = _registry_value(winreg.HKEY_LOCAL_MACHINE, current_version, "ProductName") if winreg else None
    edition = _registry_value(winreg.HKEY_LOCAL_MACHINE, current_version, "EditionID") if winreg else None
    display_version = (
        _registry_value(winreg.HKEY_LOCAL_MACHINE, current_version, "DisplayVersion") if winreg else None
    )
    build = _registry_value(winreg.HKEY_LOCAL_MACHINE, current_version, "CurrentBuildNumber") if winreg else None
    ubr = _registry_value(winreg.HKEY_LOCAL_MACHINE, current_version, "UBR") if winreg else None
    return {
        "product": product or platform.system(),
        "edition": edition,
        "display_version": display_version,
        "build": build,
        "ubr": ubr,
        "version": platform.version(),
        "release": platform.release(),
        "architecture": platform.machine(),
        "processor": platform.processor(),
        "platform": platform.platform(),
    }


def _cpu_info() -> dict[str, Any]:
    name = None
    if winreg is not None:
        name = _registry_value(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            "ProcessorNameString",
        )
    return {
        "name": (name or platform.processor() or "unknown").strip(),
        "logical_cores": os.cpu_count() or 0,
        "architecture": platform.machine(),
    }


def _user_info() -> dict[str, Any]:
    try:
        is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        is_admin = False
    return {
        "name": os.environ.get("USERNAME") or os.environ.get("USER") or "",
        "domain": os.environ.get("USERDOMAIN") or "",
        "elevated": is_admin,
        "note": (
            "This process runs with an elevated administrator token, so commands can administer the PC. "
            "The built-in Administrator account is not UAC-filtered even when the scheduled task is Limited."
            if is_admin
            else "Running with the logged-in user's normal (non-elevated) token."
        ),
    }


def _drive_type(root: str) -> str:
    try:
        code = int(ctypes.windll.kernel32.GetDriveTypeW(root))
    except Exception:
        return "unknown"
    return {
        1: "no_root",
        2: "removable",
        3: "fixed",
        4: "network",
        5: "cdrom",
        6: "ramdisk",
    }.get(code, "unknown")


def _disks() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if os.name != "nt":
        return result
    try:
        mask = int(ctypes.windll.kernel32.GetLogicalDrives())
    except Exception:
        return result
    for index in range(26):
        if not (mask & (1 << index)):
            continue
        root = f"{chr(ord('A') + index)}:\\"
        kind = _drive_type(root)
        if kind in {"no_root", "cdrom"}:
            continue
        try:
            usage = shutil.disk_usage(root)
        except OSError:
            continue
        result.append(
            {
                "root": root,
                "type": kind,
                "total_bytes": usage.total,
                "free_bytes": usage.free,
                "used_percent": round((usage.used / usage.total) * 100, 1) if usage.total else 0.0,
            }
        )
    return result


def _gpus() -> list[str]:
    if winreg is None:
        return []
    result: list[str] = []
    class_path = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, class_path, 0, winreg.KEY_READ) as key:
            for index in range(16):
                try:
                    sub_name = winreg.EnumKey(key, index)
                except OSError:
                    break
                if not sub_name.isdigit():
                    continue
                value = _registry_value(winreg.HKEY_LOCAL_MACHINE, class_path + "\\" + sub_name, "DriverDesc")
                if value and value not in result:
                    result.append(value)
    except Exception:
        pass
    return result


def _network() -> dict[str, Any]:
    adapters = dshpc_win.list_adapters()
    addresses: list[str] = []
    for adapter in adapters:
        for address in adapter.get("addresses", []):
            if address not in addresses:
                addresses.append(address)
    tailscale = dshpc_win.tailscale_info()
    return {
        "hostname": socket.gethostname(),
        "fqdn": socket.getfqdn(),
        "adapters": adapters,
        "ipv4": addresses,
        "tailscale": tailscale,
    }


def _time_info() -> dict[str, Any]:
    now = _dt.datetime.now().astimezone()
    offset = now.utcoffset()
    return {
        "local": now.isoformat(timespec="seconds"),
        "utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "timezone": str(now.tzinfo),
        "utc_offset_minutes": int(offset.total_seconds() // 60) if offset else 0,
        "epoch": int(time.time()),
    }


def collect(
    *,
    server: dict[str, Any],
    state: dict[str, Any],
    sleep_status: dict[str, Any],
    urls: list[str],
    data_dir: Path,
    started_at: float,
) -> dict[str, Any]:
    screen = dshpc_win.virtual_screen()
    return {
        "hostname": socket.gethostname(),
        "os": _os_info(),
        "cpu": _cpu_info(),
        "memory": dshpc_win.memory_info(),
        "disks": _disks(),
        "gpus": _gpus(),
        "user": _user_info(),
        "session": {
            "id": dshpc_win.session_id(),
            "active_console_session": dshpc_win.active_console_session(),
            "interactive": dshpc_win.session_id() != 0,
            "locked": dshpc_win.is_session_locked(),
        },
        "displays": {
            "virtual_screen": screen,
            "monitors": dshpc_win.list_monitors(),
            "dpi_scale": round(dshpc_win.dpi_scale(), 3),
        },
        "power": dshpc_win.power_status(),
        "network": _network(),
        "time": _time_info(),
        "uptime_seconds": round(dshpc_win.uptime_seconds(), 1),
        "server": {
            **server,
            "pid": os.getpid(),
            "python": sys.version.split()[0],
            "executable": sys.executable,
            "data_dir": str(data_dir),
            "started_at": _dt.datetime.fromtimestamp(started_at).astimezone().isoformat(timespec="seconds"),
            "uptime_seconds": round(time.time() - started_at, 1),
            "urls": urls,
        },
        "features": state.get("features", {}),
        "feature_labels": {name: dshpc_state.FEATURE_LABELS[name] for name in dshpc_state.FEATURES},
        "remote_control": bool(state.get("remote_control", True)),
        "sleep": sleep_status,
        "info_gate": {
            "required": True,
            "satisfied": True,
            "note": "get_system_info has been called; other control tools are now unlocked for this server session.",
        },
    }
