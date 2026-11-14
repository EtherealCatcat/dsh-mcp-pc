"""Local control CLI for the dsh-pc-remote controlled PC.

Works with or without the server running: feature switches are stored in the
same ``state.json`` the server watches, so ``dshpc disable shell`` takes effect
within about a second even if the CLI never talks to the server.

Examples::

    dshpc status
    dshpc disable shell,screenshot
    dshpc all-off
    dshpc sleep-hold --display
    dshpc remote-control off
    dshpc restart
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import dshpc_state  # noqa: E402
import dshpc_version  # noqa: E402
import dshpc_win  # noqa: E402

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


class CliError(RuntimeError):
    pass


def resolve_data_dir(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("DSHPC_DATA_DIR", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    sibling = ROOT.parent / "data"
    if (sibling / "config.json").is_file():
        return sibling.resolve()
    return dshpc_state.default_data_dir().resolve()


def load_config(data_dir: Path) -> dshpc_state.Config:
    config_path = data_dir / "config.json"
    if not config_path.is_file():
        raise CliError(
            f"configuration not found at {config_path}. Run deploy\\install.ps1 first, "
            "or pass --data-dir."
        )
    return dshpc_state.Config.load(config_path)


def health(port: int, timeout: float = 2.0) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


def urls(config: dshpc_state.Config) -> list[str]:
    hosts: list[str] = []
    public = str(config.data.get("public_host") or "").strip()
    if public:
        hosts.append(public)
    try:
        hosts.extend(dshpc_win.tailscale_addresses())
    except Exception:
        pass
    result: list[str] = []
    for host in hosts:
        candidate = host.rstrip("/") if host.startswith(("http://", "https://")) else f"http://{host}:{config.port}"
        if candidate not in result:
            result.append(candidate)
    if not result:
        result.append(f"http://127.0.0.1:{config.port}")
    return result


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def feature_table(state: dict[str, Any]) -> str:
    lines = []
    for name in dshpc_state.FEATURES:
        enabled = bool(state["features"].get(name))
        label = dshpc_state.FEATURE_LABELS[name]["zh"]
        lines.append(f"  [{'on ' if enabled else 'off'}] {name:<10} {label}")
    return "\n".join(lines)


def cmd_status(args: argparse.Namespace) -> int:
    data_dir = resolve_data_dir(args.data_dir)
    config = load_config(data_dir)
    state = dshpc_state.StateStore(data_dir / "state.json").snapshot()
    pid = dshpc_state.read_pid(data_dir / "server.pid")
    alive = dshpc_state.pid_alive(pid)
    live = health(config.port) if alive else None
    payload = {
        "service": dshpc_version.SERVICE_NAME,
        "version": dshpc_version.__version__,
        "hostname": socket.gethostname(),
        "data_dir": str(data_dir),
        "config_path": str(config.path),
        "port": config.port,
        "bind": config.bind,
        "pid": pid,
        "server_running": bool(live),
        "health": live,
        "urls": urls(config),
        "mcp_url": urls(config)[0] + "/mcp",
        "features": state["features"],
        "remote_control": state["remote_control"],
        "sleep_hold": state["sleep_hold"],
        "sleep_keep_display": state["sleep_keep_display"],
        "updated_at": state.get("updated_at"),
        "updated_by": state.get("updated_by"),
    }
    if args.json:
        print_json(payload)
        return 0
    print(f"{dshpc_version.SERVICE_TITLE} {dshpc_version.__version__} on {payload['hostname']}")
    print(f"  server : {'running' if live else 'stopped'} (pid {pid if alive else '-'}) port {config.port} bind {config.bind}")
    print(f"  data   : {data_dir}")
    print(f"  mcp    : {payload['mcp_url']}")
    print("  features:")
    print(feature_table(state))
    print(f"  remote control: {'on' if state['remote_control'] else 'LOCKED (local only)'}")
    print(
        f"  sleep hold   : {'on' if state['sleep_hold'] else 'off'}"
        + (" (display kept on)" if state["sleep_keep_display"] and state["sleep_hold"] else "")
    )
    print(f"  state updated: {state.get('updated_at')} by {state.get('updated_by')}")
    if not live and alive:
        print("  warning: pid file says running but /healthz did not answer; try `dshpc restart`.")
    return 0


def _mutate(args: argparse.Namespace, mutator) -> int:
    data_dir = resolve_data_dir(args.data_dir)
    load_config(data_dir)
    store = dshpc_state.StateStore(data_dir / "state.json")
    result = mutator(store)
    if args.json:
        print_json(result)
    else:
        print("当前功能状态 / current features:")
        print(feature_table(result))
    return 0


def _parse_feature_list(values: list[str]) -> list[str]:
    features: list[str] = []
    for value in values:
        for part in str(value).replace(";", ",").split(","):
            part = part.strip()
            if part:
                features.append(part)
    return features


def cmd_enable(args: argparse.Namespace) -> int:
    features = _parse_feature_list(args.features)
    if not features:
        raise CliError("specify at least one feature: info, shell, screenshot, input, sleep")
    original = list(features)

    def mutate(store: dshpc_state.StateStore):
        updated, good, bad = store.set_features(features, True, by="local-cli")
        if bad:
            raise CliError(f"unknown feature(s): {', '.join(bad)}; valid: {', '.join(dshpc_state.FEATURES)}")
        return updated

    return _mutate(args, mutate)


def cmd_disable(args: argparse.Namespace) -> int:
    features = _parse_feature_list(args.features)
    if not features:
        raise CliError("specify at least one feature: info, shell, screenshot, input, sleep")

    def mutate(store: dshpc_state.StateStore):
        updated, good, bad = store.set_features(features, False, by="local-cli")
        if bad:
            raise CliError(f"unknown feature(s): {', '.join(bad)}; valid: {', '.join(dshpc_state.FEATURES)}")
        return updated

    return _mutate(args, mutate)


def cmd_all(args: argparse.Namespace) -> int:
    return _mutate(
        args,
        lambda store: store.set_all(bool(args.on), by="local-cli"),
    )


def cmd_sleep_hold(args: argparse.Namespace) -> int:
    data_dir = resolve_data_dir(args.data_dir)
    load_config(data_dir)
    store = dshpc_state.StateStore(data_dir / "state.json")
    if not store.is_enabled("sleep"):
        raise CliError("feature 'sleep' is disabled; run `dshpc enable sleep` first")
    state = store.set_sleep_hold(True, bool(args.display), by="local-cli")
    if args.json:
        print_json(state)
    else:
        print("sleep hold: ON" + (" (display kept on)" if args.display else ""))
        print("已请求阻止空闲睡眠；合盖/电源键/手动睡眠仍可能生效。")
    return 0


def cmd_sleep_release(args: argparse.Namespace) -> int:
    data_dir = resolve_data_dir(args.data_dir)
    load_config(data_dir)
    store = dshpc_state.StateStore(data_dir / "state.json")
    state = store.set_sleep_hold(False, False, by="local-cli")
    if args.json:
        print_json(state)
    else:
        print("sleep hold: OFF")
    return 0


def cmd_reset_gate(args: argparse.Namespace) -> int:
    data_dir = resolve_data_dir(args.data_dir)
    load_config(data_dir)
    store = dshpc_state.StateStore(data_dir / "state.json")
    state = store.bump_info_gate(by="local-cli")
    print("first-call gate re-armed; the next remote control call must be get_system_info.")
    if args.json:
        print_json(state)
    return 0


def cmd_remote_control(args: argparse.Namespace) -> int:
    data_dir = resolve_data_dir(args.data_dir)
    load_config(data_dir)
    store = dshpc_state.StateStore(data_dir / "state.json")
    state = store.set_remote_control(bool(args.on), by="local-cli")
    print(f"remote control: {'on' if args.on else 'LOCKED (remote may only read status)'}")
    if args.json:
        print_json(state)
    return 0


def cmd_url(args: argparse.Namespace) -> int:
    config = load_config(resolve_data_dir(args.data_dir))
    for base in urls(config):
        print(base + "/mcp")
    return 0


def cmd_token(args: argparse.Namespace) -> int:
    config = load_config(resolve_data_dir(args.data_dir))
    print(config.token)
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    data_dir = resolve_data_dir(args.data_dir)
    load_config(data_dir)
    path = data_dir / "logs" / "server.log"
    if not path.is_file():
        raise CliError(f"log file not found: {path}")
    if args.path:
        print(str(path))
        return 0
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-max(1, args.tail) :]:
        print(line)
    return 0


def _pythonw() -> str:
    candidate = Path(sys.executable).with_name("pythonw.exe")
    if candidate.is_file():
        return str(candidate)
    return sys.executable


def _server_script() -> Path:
    return ROOT / "server" / "dshpc_server.py"


def _scheduled_task_exists() -> bool:
    if os.name != "nt":
        return False
    try:
        completed = subprocess.run(
            ["schtasks.exe", "/query", "/tn", "DSHRemotePC"],
            capture_output=True,
            creationflags=CREATE_NO_WINDOW,
            timeout=10,
        )
        return completed.returncode == 0
    except Exception:
        return False


def _run_scheduled_task() -> bool:
    try:
        completed = subprocess.run(
            ["schtasks.exe", "/run", "/tn", "DSHRemotePC"],
            capture_output=True,
            creationflags=CREATE_NO_WINDOW,
            timeout=15,
        )
        return completed.returncode == 0
    except Exception:
        return False


def cmd_start(args: argparse.Namespace) -> int:
    data_dir = resolve_data_dir(args.data_dir)
    config = load_config(data_dir)
    if health(config.port):
        print(f"already running on port {config.port}")
        return 0
    script = _server_script()
    if not script.is_file():
        raise CliError(f"server script not found: {script}")
    if _scheduled_task_exists() and _run_scheduled_task():
        print("starting via the DSHRemotePC scheduled task...")
    else:
        log_path = data_dir / "logs" / "server.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        creation = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        with log_path.open("ab") as log:
            process = subprocess.Popen(
                [_pythonw(), str(script), "--data-dir", str(data_dir)],
                cwd=str(ROOT),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                creationflags=creation,
                close_fds=True,
            )
        print(f"starting {dshpc_version.SERVICE_TITLE} (pid {process.pid})...")
    for _ in range(40):
        time.sleep(0.25)
        if health(config.port, timeout=1.0):
            print(f"running: {urls(config)[0]}/mcp")
            return 0
    raise CliError("server did not become healthy within 10 seconds; see dshpc logs")


def cmd_stop(args: argparse.Namespace) -> int:
    data_dir = resolve_data_dir(args.data_dir)
    config = load_config(data_dir)
    pid_path = data_dir / "server.pid"
    pid = dshpc_state.read_pid(pid_path)
    if _scheduled_task_exists():
        try:
            subprocess.run(
                ["schtasks.exe", "/end", "/tn", "DSHRemotePC"],
                capture_output=True,
                creationflags=CREATE_NO_WINDOW,
                timeout=10,
            )
        except Exception:
            pass
    if not dshpc_state.pid_alive(pid):
        print("server is not running")
        try:
            pid_path.unlink(missing_ok=True)
        except OSError:
            pass
        return 0
    if args.force:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=CREATE_NO_WINDOW)
    else:
        subprocess.run(["taskkill", "/PID", str(pid), "/T"], capture_output=True, creationflags=CREATE_NO_WINDOW)
        for _ in range(20):
            if not dshpc_state.pid_alive(pid):
                break
            time.sleep(0.2)
        if dshpc_state.pid_alive(pid):
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=CREATE_NO_WINDOW
            )
    print(f"stopped (pid {pid})")
    return 0


def cmd_restart(args: argparse.Namespace) -> int:
    cmd_stop(args)
    time.sleep(0.5)
    return cmd_start(args)


def cmd_check(args: argparse.Namespace) -> int:
    data_dir = resolve_data_dir(args.data_dir)
    config = load_config(data_dir)
    script = _server_script()
    completed = subprocess.run(
        [sys.executable, str(script), "--check", "--data-dir", str(data_dir)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        creationflags=CREATE_NO_WINDOW,
    )
    print(completed.stdout.strip())
    if completed.stderr.strip():
        print(completed.stderr.strip(), file=sys.stderr)
    return completed.returncode


def cmd_version(args: argparse.Namespace) -> int:
    print(f"{dshpc_version.SERVICE_NAME} {dshpc_version.__version__}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dshpc",
        description="Local control for the DSH Remote PC controlled-side service.",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", default=argparse.SUPPRESS, help="data directory")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print JSON")
    parser.add_argument("--data-dir", help="data directory (default: %%LOCALAPPDATA%%\\DSHRemotePC\\data)")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    sub = parser.add_subparsers(dest="command")

    def add(name: str, help_text: str, **kwargs):
        return sub.add_parser(name, help=help_text, parents=[common], **kwargs)

    add("status", "show server and feature status").set_defaults(func=cmd_status)

    for name, help_text in (("enable", "enable one or more features"), ("disable", "disable one or more features")):
        command = add(name, help_text)
        command.add_argument("features", nargs="+", help="info shell screenshot input sleep (comma separated ok)")
        command.set_defaults(func=cmd_enable if name == "enable" else cmd_disable)

    for name, help_text, value in (("all-on", "enable every feature", True), ("all-off", "disable every feature", False)):
        add(name, help_text).set_defaults(func=cmd_all, on=value)

    sleep_hold = add("sleep-hold", "prevent idle system sleep")
    sleep_hold.add_argument("--display", action="store_true", help="also keep the display awake")
    sleep_hold.set_defaults(func=cmd_sleep_hold)

    add("sleep-release", "release the sleep-prevention request").set_defaults(func=cmd_sleep_release)
    add("reset-info-gate", "require get_system_info again before remote control").set_defaults(func=cmd_reset_gate)

    remote = add("remote-control", "allow or lock remote feature management")
    remote.add_argument("value", choices=["on", "off"])
    remote.set_defaults(func=cmd_remote_control, on=None)

    add("url", "print MCP endpoint URL(s)").set_defaults(func=cmd_url)
    add("token", "print the bearer token (keep it secret)").set_defaults(func=cmd_token)

    logs = add("logs", "show recent server log lines")
    logs.add_argument("--tail", type=int, default=40)
    logs.add_argument("--path", action="store_true", help="print the log file path only")
    logs.set_defaults(func=cmd_logs)

    add("start", "start the server in the background").set_defaults(func=cmd_start)

    stop = add("stop", "stop the running server")
    stop.add_argument("--force", action="store_true", help="kill immediately")
    stop.set_defaults(func=cmd_stop)

    restart = add("restart", "restart the server")
    restart.add_argument("--force", action="store_true", help="kill immediately")
    restart.set_defaults(func=cmd_restart)

    add("check", "validate configuration and print status").set_defaults(func=cmd_check)
    add("version", "print the version").set_defaults(func=cmd_version)
    return parser

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        args.json = args.json
        args.data_dir = args.data_dir
        return cmd_status(args)
    try:
        if args.command == "remote-control":
            args.on = args.value == "on"
        return int(args.func(args) or 0)
    except CliError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except PermissionError as error:
        print(
            f"error: permission denied while writing the shared state ({error}). "
            "Run dshpc from a normal terminal of the installing user, or use the remote control_features tool.",
            file=sys.stderr,
        )
        return 1
    except OSError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
