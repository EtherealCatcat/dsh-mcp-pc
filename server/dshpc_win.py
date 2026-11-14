"""Windows-specific capture, input, sleep and session primitives.

Everything here uses the Python standard library plus Win32 APIs through
``ctypes``.  Pillow is optional: when importable it is used for screen capture
(fast, colour-correct, multi-monitor), otherwise a GDI ``BitBlt`` path plus a
small pure-Python PNG encoder is used, so a fresh Windows machine needs no
third-party packages at all.
"""

from __future__ import annotations

import ctypes
import io
import os
import struct
import subprocess
import threading
import time
import zlib
from ctypes import wintypes
from typing import Any, Iterable

IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
else:  # pragma: no cover - keeps imports working on non-Windows test hosts
    user32 = kernel32 = gdi32 = shell32 = iphlpapi = None  # type: ignore[assignment]


class WinError(RuntimeError):
    def __init__(self, message: str, code: int | None = None):
        self.code = code
        super().__init__(f"{message} (WinError {code})" if code else message)


# ---------------------------------------------------------------------------
# DPI / virtual desktop
# ---------------------------------------------------------------------------

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
SM_CXSCREEN = 0
SM_CYSCREEN = 1
SM_CMONITORS = 80


def enable_dpi_awareness() -> None:
    """Make this process per-monitor DPI aware so coordinates and captures
    match physical pixels on scaled (125%/150%) displays."""
    if not IS_WINDOWS:
        return
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
        return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wintypes.DWORD),
    ]


def _get_metrics(index: int) -> int:
    if not IS_WINDOWS:
        return 0
    return int(user32.GetSystemMetrics(index))


def virtual_screen() -> dict[str, int]:
    left = _get_metrics(SM_XVIRTUALSCREEN)
    top = _get_metrics(SM_YVIRTUALSCREEN)
    width = _get_metrics(SM_CXVIRTUALSCREEN)
    height = _get_metrics(SM_CYVIRTUALSCREEN)
    if width <= 0 or height <= 0:
        width, height = _get_metrics(SM_CXSCREEN), _get_metrics(SM_CYSCREEN)
        left = top = 0
    return {"left": left, "top": top, "width": width, "height": height}


def list_monitors() -> list[dict[str, Any]]:
    if not IS_WINDOWS:
        return []
    monitors: list[dict[str, Any]] = []
    primary = (0, 0, _get_metrics(SM_CXSCREEN), _get_metrics(SM_CYSCREEN))
    enum_proc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(RECT), wintypes.LPARAM
    )

    def _callback(hmonitor, hdc, lprect, lparam):  # noqa: ANN001 - Win32 callback
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            rect = info.rcMonitor
            monitors.append(
                {
                    "left": int(rect.left),
                    "top": int(rect.top),
                    "right": int(rect.right),
                    "bottom": int(rect.bottom),
                    "width": int(rect.right - rect.left),
                    "height": int(rect.bottom - rect.top),
                    "primary": bool(info.dwFlags & 1),
                }
            )
        return True

    try:
        user32.EnumDisplayMonitors(0, None, enum_proc(_callback), 0)
    except Exception:
        monitors = []
    if not monitors:
        monitors.append(
            {
                "left": primary[0],
                "top": primary[1],
                "width": primary[2],
                "height": primary[3],
                "right": primary[2],
                "bottom": primary[3],
                "primary": True,
            }
        )
    monitors.sort(key=lambda item: (item["left"], item["top"]))
    return monitors


def dpi_scale() -> float:
    if not IS_WINDOWS:
        return 1.0
    try:
        return float(user32.GetDpiForSystem()) / 96.0
    except Exception:
        return 1.0


# ---------------------------------------------------------------------------
# Screen capture
# ---------------------------------------------------------------------------


class ScreenImage:
    def __init__(self, width: int, height: int, rgb: bytes):
        self.width = width
        self.height = height
        self.rgb = rgb

    def resize(self, max_width: int) -> "ScreenImage":
        if max_width <= 0 or self.width <= max_width:
            return self
        try:
            from PIL import Image

            image = Image.frombytes("RGB", (self.width, self.height), self.rgb)
            ratio = max_width / float(self.width)
            image = image.resize((max_width, max(1, int(round(self.height * ratio)))), Image.LANCZOS)
            return ScreenImage(image.width, image.height, image.tobytes())
        except Exception:
            return self

    def encode(self, fmt: str = "png", quality: int = 85) -> tuple[bytes, str]:
        fmt = (fmt or "png").lower()
        if fmt in {"jpg", "jpeg"}:
            try:
                from PIL import Image

                image = Image.frombytes("RGB", (self.width, self.height), self.rgb)
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=max(1, min(100, int(quality))), optimize=True)
                return buffer.getvalue(), "image/jpeg"
            except Exception:
                # Fall through to PNG: never fail a capture because of encoding.
                pass
        try:
            from PIL import Image

            image = Image.frombytes("RGB", (self.width, self.height), self.rgb)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG", optimize=True)
            return buffer.getvalue(), "image/png"
        except Exception:
            return _encode_png(self.width, self.height, self.rgb), "image/png"


def _encode_png(width: int, height: int, rgb: bytes) -> bytes:
    """Minimal RGB PNG encoder (filter 0 per row)."""
    stride = width * 3
    raw = bytearray()
    for row in range(height):
        raw.append(0)
        raw.extend(rgb[row * stride : (row + 1) * stride])
    compressed = zlib.compress(bytes(raw), 6)

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", compressed) + chunk(b"IEND", b"")


def _gdi_capture() -> ScreenImage:
    if not IS_WINDOWS:
        raise WinError("screen capture requires Windows")
    screen = virtual_screen()
    left, top, width, height = screen["left"], screen["top"], screen["width"], screen["height"]
    if width <= 0 or height <= 0:
        raise WinError("no active display detected")
    hdc_screen = user32.GetDC(0)
    if not hdc_screen:
        raise WinError("GetDC failed", ctypes.get_last_error())
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbitmap = gdi32.CreateCompatibleBitmap(hdc_screen, width, height)
    try:
        old = gdi32.SelectObject(hdc_mem, hbitmap)
        SRCCOPY = 0x00CC0020
        if not gdi32.BitBlt(hdc_mem, 0, 0, width, height, hdc_screen, left, top, SRCCOPY):
            raise WinError("BitBlt failed", ctypes.get_last_error())

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", wintypes.DWORD),
                ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD),
            ]

        class BITMAPINFO(ctypes.Structure):
            _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0
        info.bmiHeader.biSizeImage = width * height * 4
        buffer = ctypes.create_string_buffer(width * height * 4)
        DIB_RGB_COLORS = 0
        if not gdi32.GetDIBits(
            hdc_mem, hbitmap, 0, height, buffer, ctypes.byref(info), DIB_RGB_COLORS
        ):
            raise WinError("GetDIBits failed", ctypes.get_last_error())
        data = buffer.raw
        rgb = bytearray(width * height * 3)
        # BGRA -> RGB
        rgb[0::3] = data[2::4]
        rgb[1::3] = data[1::4]
        rgb[2::3] = data[0::4]
        return ScreenImage(width, height, bytes(rgb))
    finally:
        try:
            gdi32.SelectObject(hdc_mem, old)  # type: ignore[possibly-undefined]
        except Exception:
            pass
        gdi32.DeleteObject(hbitmap)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(0, hdc_screen)


def _pil_capture() -> ScreenImage:
    from PIL import ImageGrab

    image = ImageGrab.grab(all_screens=True)
    if image.mode != "RGB":
        image = image.convert("RGB")
    return ScreenImage(image.width, image.height, image.tobytes())


def capture_screen(
    *,
    monitor: int = 0,
    max_width: int = 0,
    fmt: str = "png",
    quality: int = 85,
) -> dict[str, Any]:
    """Capture all monitors or one 1-based monitor; returns encoded bytes."""
    force_gdi = os.environ.get("DSHPC_FORCE_GDI", "").strip() in {"1", "true", "yes"}
    screen = virtual_screen()
    monitors = list_monitors()
    if monitor and monitor not in range(1, len(monitors) + 1):
        raise WinError(f"monitor index {monitor} is out of range 1..{len(monitors)}")

    if force_gdi:
        full = _gdi_capture()
    else:
        try:
            full = _pil_capture()
        except Exception:
            full = _gdi_capture()

    left = screen["left"]
    top = screen["top"]
    if monitor:
        target = monitors[monitor - 1]
        x0 = max(0, target["left"] - left)
        y0 = max(0, target["top"] - top)
        x1 = min(full.width, target["right"] - left)
        y1 = min(full.height, target["bottom"] - top)
        if x1 > x0 and y1 > y0:
            try:
                from PIL import Image

                image = Image.frombytes("RGB", (full.width, full.height), full.rgb).crop((x0, y0, x1, y1))
                full = ScreenImage(image.width, image.height, image.tobytes())
            except Exception:
                # Pure-GDI capture cannot crop without rereading; fall back to
                # the virtual desktop rather than failing the call.
                pass

    full = full.resize(int(max_width or 0))
    data, mime = full.encode(fmt, quality)
    return {
        "data": data,
        "mime": mime,
        "width": full.width,
        "height": full.height,
        "monitor": int(monitor or 0),
        "monitor_count": len(monitors),
        "monitors": monitors,
        "virtual_screen": screen,
        "locked": is_session_locked(),
    }


# ---------------------------------------------------------------------------
# Keyboard and mouse input
# ---------------------------------------------------------------------------

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


if IS_WINDOWS:
    user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    user32.SendInput.restype = wintypes.UINT
    user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
    user32.SetCursorPos.restype = wintypes.BOOL
    kernel32.SetThreadExecutionState.argtypes = (wintypes.DWORD,)
    kernel32.SetThreadExecutionState.restype = wintypes.DWORD


def cursor_position() -> dict[str, int]:
    point = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(point)):
        return {"x": 0, "y": 0}
    return {"x": int(point.x), "y": int(point.y)}


def _send(inputs: list[INPUT]) -> None:
    if not inputs:
        return
    array = (INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise WinError("SendInput failed (the session may be locked or blocked by UIPI)", ctypes.get_last_error())


def _mouse_input(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> INPUT:
    item = INPUT(type=INPUT_MOUSE)
    item.mi = MOUSEINPUT(dx=dx, dy=dy, mouseData=data & 0xFFFFFFFF, dwFlags=flags, time=0, dwExtraInfo=0)
    return item


def _key_input(vk: int, flags: int = 0) -> INPUT:
    item = INPUT(type=INPUT_KEYBOARD)
    item.ki = KEYBDINPUT(wVk=vk & 0xFFFF, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0)
    return item


def _unicode_input(unit: int, keyup: bool) -> INPUT:
    item = INPUT(type=INPUT_KEYBOARD)
    item.ki = KEYBDINPUT(
        wVk=0,
        wScan=unit & 0xFFFF,
        dwFlags=KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if keyup else 0),
        time=0,
        dwExtraInfo=0,
    )
    return item


def _absolute_move(x: int, y: int) -> None:
    screen = virtual_screen()
    left, top, width, height = screen["left"], screen["top"], screen["width"], screen["height"]
    if not (left <= x < left + width and top <= y < top + height):
        raise WinError(f"point ({x}, {y}) is outside the virtual screen {screen}")
    nx = int(round((x - left) * 65535 / max(1, width - 1)))
    ny = int(round((y - top) * 65535 / max(1, height - 1)))
    _send(
        [
            _mouse_input(
                MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
                nx,
                ny,
            )
        ]
    )


def _smooth_move(x: int, y: int, duration_ms: int) -> None:
    start = cursor_position()
    if duration_ms <= 0:
        _absolute_move(x, y)
        return
    steps = max(1, min(120, int(duration_ms / 16)))
    for index in range(1, steps + 1):
        fraction = index / steps
        _absolute_move(
            int(round(start["x"] + (x - start["x"]) * fraction)),
            int(round(start["y"] + (y - start["y"]) * fraction)),
        )
        time.sleep(max(0.0, duration_ms / 1000.0 / steps))


BUTTONS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def click(button: str = "left", count: int = 1, interval_ms: int = 80, x: int | None = None, y: int | None = None) -> None:
    if button not in BUTTONS:
        raise WinError(f"unsupported mouse button {button!r}; use left, right or middle")
    count = max(1, min(5, int(count)))
    if x is not None and y is not None:
        _absolute_move(int(x), int(y))
    down, up = BUTTONS[button]
    for index in range(count):
        _send([_mouse_input(down), _mouse_input(up)])
        if index + 1 < count:
            time.sleep(max(0, interval_ms) / 1000.0)


def scroll(amount: int, horizontal: bool = False) -> None:
    delta = int(amount) * 120
    if delta == 0:
        return
    flags = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
    _send([_mouse_input(flags, data=delta & 0xFFFFFFFF)])


VK_NAMES: dict[str, int] = {
    "backspace": 0x08,
    "back": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "shift": 0x10,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "pause": 0x13,
    "break": 0x13,
    "capslock": 0x14,
    "caps": 0x14,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "pageup": 0x21,
    "pgup": 0x21,
    "pagedown": 0x22,
    "pgdn": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "printscreen": 0x2C,
    "prtsc": 0x2C,
    "insert": 0x2D,
    "ins": 0x2D,
    "delete": 0x2E,
    "del": 0x2E,
    "win": 0x5B,
    "windows": 0x5B,
    "meta": 0x5B,
    "cmd": 0x5B,
    "lwin": 0x5B,
    "rwin": 0x5C,
    "apps": 0x5D,
    "menu": 0x5D,
    "numlock": 0x90,
    "scrolllock": 0x91,
    "semicolon": 0xBA,
    "equal": 0xBB,
    "plus": 0xBB,
    "comma": 0xBC,
    "minus": 0xBD,
    "period": 0xBE,
    "dot": 0xBE,
    "slash": 0xBF,
    "grave": 0xC0,
    "backtick": 0xC0,
    "leftbracket": 0xDB,
    "backslash": 0xDC,
    "rightbracket": 0xDD,
    "quote": 0xDE,
}

EXTENDED_KEYS = {
    0x21,
    0x22,
    0x23,
    0x24,
    0x25,
    0x26,
    0x27,
    0x28,
    0x2C,
    0x2D,
    0x2E,
    0x90,
    0x5C,
    0x5D,
}


def key_vk(name: str) -> int:
    token = (name or "").strip().lower()
    if token in VK_NAMES:
        return VK_NAMES[token]
    if len(token) == 1:
        char = token.upper()
        if "A" <= char <= "Z" or "0" <= char <= "9":
            return ord(char)
        symbols = {
            " ": 0x20,
            ";": 0xBA,
            "=": 0xBB,
            "+": 0xBB,
            ",": 0xBC,
            "-": 0xBD,
            ".": 0xBE,
            "/": 0xBF,
            "`": 0xC0,
            "[": 0xDB,
            "\\": 0xDC,
            "]": 0xDD,
            "'": 0xDE,
        }
        if token in symbols:
            return symbols[token]
    if token.startswith("f") and token[1:].isdigit():
        number = int(token[1:])
        if 1 <= number <= 24:
            return 0x6F + number
    raise WinError(f"unknown key name {name!r}")


def press_keys(combo: str, repeat: int = 1) -> None:
    repeat = max(1, min(50, int(repeat)))
    raw = (combo or "").strip()
    if not raw:
        raise WinError("key combo is empty")
    if raw == "+":
        parts = ["plus"]
    else:
        parts = [part for part in raw.split("+") if part]
        if not parts:
            parts = ["plus"]
    codes = [key_vk(part) for part in parts]
    for _ in range(repeat):
        down: list[INPUT] = []
        up: list[INPUT] = []
        for vk in codes:
            flags = KEYEVENTF_EXTENDEDKEY if vk in EXTENDED_KEYS else 0
            down.append(_key_input(vk, flags))
            up.append(_key_input(vk, flags | KEYEVENTF_KEYUP))
        _send(down)
        time.sleep(0.01)
        _send(list(reversed(up)))


def type_text(text: str, interval_ms: int = 0) -> int:
    data = str(text or "")
    if not data:
        return 0
    interval = max(0, min(1000, int(interval_ms))) / 1000.0
    count = 0
    for char in data:
        if char == "\r":
            continue
        if char == "\n":
            press_keys("enter")
            count += 1
            if interval:
                time.sleep(interval)
            continue
        encoded = char.encode("utf-16-le")
        for index in range(0, len(encoded), 2):
            unit = int.from_bytes(encoded[index : index + 2], "little")
            _send([_unicode_input(unit, False), _unicode_input(unit, True)])
            count += 1
        if interval:
            time.sleep(interval)
    return count


def perform_actions(actions: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    if is_session_locked():
        raise WinError("SESSION_LOCKED: the Windows session is locked; input injection is blocked")
    results: list[dict[str, Any]] = []
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            raise WinError(f"action #{index + 1} is not an object")
        kind = str(action.get("type") or "").strip().lower()
        if kind in {"move", "mousemove", "mouse_move"}:
            x, y = int(action.get("x", 0)), int(action.get("y", 0))
            duration = int(action.get("duration_ms", 0) or 0)
            if duration > 0:
                _smooth_move(x, y, min(duration, 5000))
            else:
                _absolute_move(x, y)
            results.append({"action": kind, "x": x, "y": y})
        elif kind in {"click", "mouse_click"}:
            button = str(action.get("button", "left")).lower()
            count = int(action.get("count", 1) or 1)
            interval = int(action.get("interval_ms", 80) or 0)
            x = action.get("x")
            y = action.get("y")
            click(button, count, interval, None if x is None else int(x), None if y is None else int(y))
            results.append({"action": kind, "button": button, "count": count})
        elif kind in {"scroll", "mouse_scroll"}:
            amount = int(action.get("amount", 0) or 0)
            horizontal = bool(action.get("horizontal", False))
            scroll(amount, horizontal)
            results.append({"action": kind, "amount": amount, "horizontal": horizontal})
        elif kind in {"type", "text", "type_text"}:
            typed = type_text(str(action.get("text", "")), int(action.get("interval_ms", 0) or 0))
            results.append({"action": kind, "characters": typed})
        elif kind in {"key", "keys", "press", "hotkey"}:
            combo = str(action.get("keys", "") or "")
            repeat = int(action.get("repeat", 1) or 1)
            press_keys(combo, repeat)
            results.append({"action": kind, "keys": combo, "repeat": repeat})
        elif kind in {"wait", "sleep", "delay"}:
            ms = max(0, min(60000, int(action.get("ms", 0) or 0)))
            time.sleep(ms / 1000.0)
            results.append({"action": kind, "ms": ms})
        else:
            raise WinError(f"unsupported input action {kind!r}")
    return results


# ---------------------------------------------------------------------------
# Session, power, memory, network
# ---------------------------------------------------------------------------

DESKTOP_READOBJECTS = 0x0001
DESKTOP_SWITCHDESKTOP = 0x0100
UOI_NAME = 2


def is_session_locked() -> bool:
    if not IS_WINDOWS:
        return False
    handle = user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS | DESKTOP_SWITCHDESKTOP)
    if not handle:
        return True
    try:
        needed = wintypes.DWORD(0)
        user32.GetUserObjectInformationW(handle, UOI_NAME, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_unicode_buffer(max(2, needed.value // 2 + 1))
        if not user32.GetUserObjectInformationW(
            handle, UOI_NAME, buffer, ctypes.sizeof(buffer), ctypes.byref(needed)
        ):
            return True
        return buffer.value.strip().lower() not in {"default", ""}
    finally:
        user32.CloseDesktop(handle)


def session_id() -> int:
    if not IS_WINDOWS:
        return -1
    value = wintypes.DWORD(0)
    if kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(), ctypes.byref(value)):
        return int(value.value)
    return -1


def active_console_session() -> int:
    if not IS_WINDOWS:
        return -1
    try:
        return int(kernel32.WTSGetActiveConsoleSessionId())
    except Exception:
        return -1


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def memory_info() -> dict[str, int]:
    if not IS_WINDOWS:
        return {}
    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return {}
    return {
        "total_bytes": int(status.ullTotalPhys),
        "available_bytes": int(status.ullAvailPhys),
        "used_percent": int(status.dwMemoryLoad),
    }


class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", wintypes.BYTE),
        ("BatteryFlag", wintypes.BYTE),
        ("BatteryLifePercent", wintypes.BYTE),
        ("SystemStatusFlag", wintypes.BYTE),
        ("BatteryLifeTime", wintypes.DWORD),
        ("BatteryFullLifeTime", wintypes.DWORD),
    ]


def power_status() -> dict[str, Any]:
    if not IS_WINDOWS:
        return {}
    status = SYSTEM_POWER_STATUS()
    if not kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return {}
    ac = {0: "offline", 1: "online", 255: "unknown"}.get(int(status.ACLineStatus), "unknown")
    percent = int(status.BatteryLifePercent)
    return {
        "ac_line": ac,
        "battery_percent": percent if percent <= 100 else None,
        "battery_charging": bool(int(status.BatteryFlag) & 8),
        "battery_low": bool(int(status.BatteryFlag) & 2) or bool(int(status.SystemStatusFlag) & 1),
    }


def uptime_seconds() -> float:
    if not IS_WINDOWS:
        return 0.0
    try:
        return float(kernel32.GetTickCount64()) / 1000.0
    except Exception:
        return 0.0


class SOCKET_ADDRESS(ctypes.Structure):
    _fields_ = [("lpSockaddr", ctypes.c_void_p), ("iSockaddrLength", ctypes.c_int)]


class IP_ADAPTER_UNICAST_ADDRESS(ctypes.Structure):
    pass


IP_ADAPTER_UNICAST_ADDRESS._fields_ = [
    ("Length", wintypes.ULONG),
    ("Flags", wintypes.DWORD),
    ("Next", ctypes.POINTER(IP_ADAPTER_UNICAST_ADDRESS)),
    ("Address", SOCKET_ADDRESS),
    ("PrefixOrigin", ctypes.c_int),
    ("SuffixOrigin", ctypes.c_int),
    ("DadState", ctypes.c_int),
    ("ValidLifetime", wintypes.ULONG),
    ("PreferredLifetime", wintypes.ULONG),
    ("LeaseLifetime", wintypes.ULONG),
    ("OnLinkPrefixLength", ctypes.c_ubyte),
]


class IP_ADAPTER_ADDRESSES(ctypes.Structure):
    pass


IP_ADAPTER_ADDRESSES._fields_ = [
    ("Length", wintypes.ULONG),
    ("IfIndex", wintypes.DWORD),
    ("Next", ctypes.POINTER(IP_ADAPTER_ADDRESSES)),
    ("AdapterName", ctypes.c_char_p),
    ("FirstUnicastAddress", ctypes.POINTER(IP_ADAPTER_UNICAST_ADDRESS)),
    ("FirstAnycastAddress", ctypes.c_void_p),
    ("FirstMulticastAddress", ctypes.c_void_p),
    ("FirstDnsServerAddress", ctypes.c_void_p),
    ("DnsSuffix", ctypes.c_wchar_p),
    ("Description", ctypes.c_wchar_p),
    ("FriendlyName", ctypes.c_wchar_p),
    ("PhysicalAddress", ctypes.c_ubyte * 8),
    ("PhysicalAddressLength", wintypes.ULONG),
    ("Flags", wintypes.ULONG),
    ("Mtu", wintypes.ULONG),
    ("IfType", wintypes.ULONG),
    ("OperStatus", ctypes.c_int),
    ("Ipv6IfIndex", wintypes.ULONG),
]


class SOCKADDR_IN(ctypes.Structure):
    _fields_ = [
        ("sin_family", wintypes.USHORT),
        ("sin_port", wintypes.USHORT),
        ("sin_addr", ctypes.c_ubyte * 4),
        ("sin_zero", ctypes.c_char * 8),
    ]


if IS_WINDOWS:
    iphlpapi.GetAdaptersAddresses.argtypes = (
        wintypes.ULONG,
        wintypes.ULONG,
        ctypes.c_void_p,
        ctypes.POINTER(IP_ADAPTER_ADDRESSES),
        ctypes.POINTER(wintypes.ULONG),
    )
    iphlpapi.GetAdaptersAddresses.restype = wintypes.ULONG


def list_adapters() -> list[dict[str, Any]]:
    if not IS_WINDOWS:
        return []
    AF_UNSPEC = 0
    GAA_FLAG_SKIP_ANYCAST = 0x2
    GAA_FLAG_SKIP_MULTICAST = 0x4
    GAA_FLAG_SKIP_DNS_SERVER = 0x8
    flags = GAA_FLAG_SKIP_ANYCAST | GAA_FLAG_SKIP_MULTICAST | GAA_FLAG_SKIP_DNS_SERVER
    size = wintypes.ULONG(15 * 1024)
    for _ in range(4):
        buffer = ctypes.create_string_buffer(size.value)
        result = iphlpapi.GetAdaptersAddresses(
            AF_UNSPEC, flags, None, ctypes.cast(buffer, ctypes.POINTER(IP_ADAPTER_ADDRESSES)), ctypes.byref(size)
        )
        if result == 0:
            break
        if result != 111:  # ERROR_BUFFER_OVERFLOW
            return []
    else:
        return []

    adapters: list[dict[str, Any]] = []
    node = ctypes.cast(buffer, ctypes.POINTER(IP_ADAPTER_ADDRESSES))
    while node:
        item = node.contents
        addresses: list[str] = []
        unicast = item.FirstUnicastAddress
        while unicast:
            sockaddr = unicast.contents.Address.lpSockaddr
            if sockaddr and unicast.contents.Address.iSockaddrLength >= ctypes.sizeof(SOCKADDR_IN):
                raw = ctypes.cast(sockaddr, ctypes.POINTER(SOCKADDR_IN)).contents
                if int(raw.sin_family) == 2:  # AF_INET
                    addresses.append(".".join(str(part) for part in raw.sin_addr))
            unicast = unicast.contents.Next
        if addresses:
            adapters.append(
                {
                    "name": str(item.FriendlyName or ""),
                    "description": str(item.Description or ""),
                    "addresses": addresses,
                    "up": int(item.OperStatus) == 1,  # IfOperStatusUp
                }
            )
        node = item.Next
    return adapters


def is_cgnat(ip: str) -> bool:
    parts = (ip or "").split(".")
    if len(parts) != 4:
        return False
    try:
        first, second = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return first == 100 and 64 <= second <= 127


def tailscale_addresses() -> list[str]:
    addresses: list[str] = []
    for adapter in list_adapters():
        name = (adapter.get("name") or "").lower()
        if "tailscale" in name:
            for ip in adapter.get("addresses", []):
                if is_cgnat(ip) and ip not in addresses:
                    addresses.append(ip)
    if not addresses:
        for adapter in list_adapters():
            for ip in adapter.get("addresses", []):
                if is_cgnat(ip) and ip not in addresses:
                    addresses.append(ip)
    return addresses


def tailscale_info() -> dict[str, Any]:
    info: dict[str, Any] = {"installed": False, "service": "unknown", "addresses": tailscale_addresses()}
    try:
        completed = subprocess.run(
            ["sc.exe", "query", "Tailscale"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=0x08000000,
        )
        info["installed"] = True
        info["service"] = "running" if "RUNNING" in completed.stdout.upper() else "stopped"
    except Exception:
        pass
    return info


# ---------------------------------------------------------------------------
# Sleep prevention
# ---------------------------------------------------------------------------

ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
ES_CONTINUOUS = 0x80000000


class SleepBlocker:
    """Keeps asserting an execution-state request from one dedicated thread.

    ``SetThreadExecutionState`` is per-thread, so both the request and its
    release happen on this thread.  ``ES_CONTINUOUS`` keeps the request in
    effect until it is cleared (or the process exits).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._enabled = False
        self._keep_display = False
        self._last_error: int | None = None
        self._since: float | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="sleep-blocker", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        last_applied: tuple[bool, bool] | None = None
        while not self._stop.is_set():
            with self._lock:
                enabled, keep_display = self._enabled, self._keep_display
            if enabled:
                flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED
                if keep_display:
                    flags |= ES_DISPLAY_REQUIRED
                if last_applied != (enabled, keep_display):
                    result = kernel32.SetThreadExecutionState(flags) if IS_WINDOWS else 1
                    if result == 0:
                        self._last_error = ctypes.get_last_error()
                    else:
                        self._last_error = None
                        self._since = time.time()
                    last_applied = (enabled, keep_display)
                else:
                    # Refresh periodically; harmless and survives driver resets.
                    kernel32.SetThreadExecutionState(flags) if IS_WINDOWS else None
            elif last_applied is not None:
                if IS_WINDOWS:
                    kernel32.SetThreadExecutionState(ES_CONTINUOUS)
                last_applied = None
                self._since = None
            self._wake.wait(20.0)
            self._wake.clear()

    def set(self, enabled: bool, keep_display: bool = False) -> None:
        with self._lock:
            self._enabled = bool(enabled)
            self._keep_display = bool(keep_display)
            if enabled:
                self._since = time.time()
        self._wake.set()

    def release(self) -> None:
        self.set(False, False)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": bool(self._enabled),
                "keep_display": bool(self._keep_display),
                "active": bool(self._enabled and self._last_error is None),
                "since": self._since,
                "last_error": self._last_error,
                "api": "SetThreadExecutionState(ES_CONTINUOUS|ES_SYSTEM_REQUIRED"
                + ("|ES_DISPLAY_REQUIRED" if self._keep_display else "")
                + ")",
            }

    def stop(self) -> None:
        self.set(False, False)
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=3)
