import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import dshpc_exec  # noqa: E402
import dshpc_info  # noqa: E402
import dshpc_tools  # noqa: E402
import dshpc_win  # noqa: E402


def section(title):
    print("\n=== " + title + " ===")


def main():
    dshpc_win.enable_dpi_awareness()
    section("exec pwsh")
    result = dshpc_exec.run_command('Write-Output "hello 世界"; Get-Location', shell="pwsh")
    print(json.dumps({k: result[k] for k in ("exit_code", "shell", "stdout", "stderr", "duration_ms")}, ensure_ascii=False))

    section("exec cmd")
    result = dshpc_exec.run_command("echo hi & exit /b 3", shell="cmd")
    print(json.dumps({k: result[k] for k in ("exit_code", "stdout", "stderr")}, ensure_ascii=False))

    section("exec python")
    result = dshpc_exec.run_command('import sys; print("py", sys.version_info[:2])', shell="python")
    print(json.dumps({k: result[k] for k in ("exit_code", "stdout", "stderr")}, ensure_ascii=False))

    section("exec timeout")
    result = dshpc_exec.run_command("Start-Sleep -Seconds 5; Write-Output done", shell="pwsh", timeout_sec=1)
    print(json.dumps({k: result[k] for k in ("exit_code", "timed_out", "stdout")}, ensure_ascii=False))

    section("info")
    info = dshpc_info.collect(
        server={"version": "test"},
        state={"features": {}},
        sleep_status={},
        urls=[],
        data_dir=Path("."),
        started_at=time.time(),
    )
    print(json.dumps({k: info[k] for k in ("hostname", "os", "cpu", "user", "session", "power")}, ensure_ascii=False))

    section("screen")
    capture = dshpc_win.capture_screen(max_width=800, fmt="jpeg")
    print(capture["width"], capture["height"], len(capture["data"]), capture["mime"], capture["locked"])
    (ROOT / ".tmp").mkdir(exist_ok=True)
    (ROOT / ".tmp" / "capture.jpg").write_bytes(capture["data"])

    section("sleep")
    blocker = dshpc_win.SleepBlocker()
    blocker.start()
    blocker.set(True, False)
    time.sleep(0.5)
    print(blocker.status())
    blocker.set(True, True)
    time.sleep(0.5)
    print(blocker.status())
    blocker.release()
    time.sleep(0.5)
    print(blocker.status())
    blocker.stop()

    section("input (mouse move to current position only)")
    cursor = dshpc_win.cursor_position()
    print("cursor", cursor)
    print(dshpc_win.perform_actions([{"type": "move", "x": cursor["x"], "y": cursor["y"]}, {"type": "wait", "ms": 50}]))
    print("cursor after", dshpc_win.cursor_position())

    section("tools")
    import dshpc_tools
    from dshpc_state import Config, StateStore

    tmp = ROOT / ".tmp" / "test-state"
    tmp.mkdir(parents=True, exist_ok=True)
    config = Config.load_or_create(tmp / "config.json")
    config.data_dir = tmp
    state = StateStore(tmp / "state.json")
    gate = dshpc_tools.InfoGate()
    sleep = dshpc_win.SleepBlocker()
    sleep.start()
    store = dshpc_tools.ScreenshotStore(tmp / "screenshots")
    import logging

    logger = logging.getLogger("test")
    tools = dshpc_tools.Tools(
        config=config,
        state=state,
        gate=gate,
        sleep=sleep,
        store=store,
        url_base_provider=lambda: "http://100.70.184.108:8765",
        logger=logger,
        started_at=time.time(),
    )
    gate_test = tools.call("run_command", {"command": "Write-Output blocked"})
    print("gate:", gate_test.is_error, gate_test.content[0]["text"][:120])

    info_out = tools.call("get_system_info", {})
    print("info ok:", not info_out.is_error, "gate satisfied:", gate.satisfied)

    shell_out = tools.call("run_command", {"command": "Write-Output ok; $PSVersionTable.PSVersion.ToString()"})
    print("shell:", shell_out.is_error, shell_out.content[0]["text"][:160])

    state.set_features(["shell"], False, by="test")
    disabled = tools.call("run_command", {"command": "Write-Output nope"})
    print("disabled:", disabled.is_error, disabled.content[0]["text"][:160])
    state.set_features(["shell"], True, by="test")

    screenshot_out = tools.call("capture_screen", {"max_width": 640, "include_image": False})
    print("screenshot:", screenshot_out.is_error, screenshot_out.content[0]["text"][:200])

    control = tools.call("control_features", {"action": "status"})
    print("status:", control.is_error, control.content[0]["text"][:200])

    control = tools.call("control_features", {"action": "disable", "features": ["input"]})
    print("disable input:", control.is_error)
    input_out = tools.call("input_control", {"actions": [{"type": "key", "keys": "shift"}]})
    print("input disabled:", input_out.is_error, input_out.content[0]["text"][:120])
    tools.state.set_features(["input"], True, by="test")

    sleep_out = tools.call("set_sleep_block", {"enable": True})
    print("sleep:", sleep_out.is_error, sleep_out.content[0]["text"][:200])
    sleep_out = tools.call("set_sleep_block", {"enable": False})
    print("sleep release:", sleep_out.is_error)
    sleep.stop()

    print("\nOK")


if __name__ == "__main__":
    main()
