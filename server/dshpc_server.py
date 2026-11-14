"""Entry point for the dsh-pc-remote controlled-side service.

Run modes::

    python dshpc_server.py --init                 create config/state, print pairing JSON
    python dshpc_server.py                        serve MCP over HTTP (loopback + Tailscale)
    python dshpc_server.py --stdio                serve MCP over stdin/stdout (diagnostics)
    python dshpc_server.py --check                validate configuration and print status

The service is intentionally a normal per-user process started at logon, not a
Windows service: screen capture and input injection must run inside the
interactive desktop session (session 0 services cannot see it).
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # allow `python server\dshpc_server.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import dshpc_mcp
import dshpc_state
import dshpc_tools
import dshpc_version
import dshpc_win

logger = logging.getLogger("dshpc")

STARTED_AT = time.time()


def _parse_timestamp(value: Any) -> float:
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        from datetime import datetime

        return datetime.fromisoformat(value).timestamp()
    except (ValueError, OverflowError):
        return 0.0


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def setup_logging(config: dshpc_state.Config, *, console: bool = False) -> None:
    logger.setLevel(getattr(logging, str(config.data.get("log_level", "INFO")).upper(), logging.INFO))
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    log_dir = Path(config.data_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "server.log",
        maxBytes=int(config.data.get("log_max_bytes", 2 * 1024 * 1024)),
        backupCount=int(config.data.get("log_backups", 3)),
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if console and sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        logger.addHandler(stream)


# ---------------------------------------------------------------------------
# Single-instance lock
# ---------------------------------------------------------------------------


class InstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self._handle = None

    def acquire(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b")
            handle.seek(0)
            if handle.read(1) == b"":
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            self._handle = handle
            return True
        except Exception:
            try:
                if self._handle:
                    self._handle.close()
            except Exception:
                pass
            self._handle = None
            return False

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
        try:
            self._handle.close()
        except Exception:
            pass
        self._handle = None


# ---------------------------------------------------------------------------
# HTTP listeners
# ---------------------------------------------------------------------------


class ListenerSet:
    def __init__(
        self,
        *,
        config: dshpc_state.Config,
        protocol: dshpc_mcp.McpProtocol,
        store: dshpc_tools.ScreenshotStore,
        bind_mode: str,
        bind_address: str,
        public_host: str,
    ):
        self.config = config
        self.protocol = protocol
        self.store = store
        self.bind_mode = bind_mode
        self.bind_address = bind_address
        self.public_host = public_host.strip()
        self.port = config.port
        self._lock = threading.Lock()
        self._servers: dict[str, tuple[dshpc_mcp.DshpcHttpServer, threading.Thread]] = {}
        self._tailscale_hosts: set[str] = set()

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self.bind_mode == "all" or (
            self.bind_mode == "address" and self.bind_address in {"0.0.0.0", "*"}
        ):
            self._bind("0.0.0.0", required=True)
            return
        self._bind("127.0.0.1", required=True)
        if self.bind_mode == "address" and self.bind_address:
            if self.bind_address not in {"127.0.0.1", "localhost", "0.0.0.0"}:
                self._bind(self.bind_address, required=False)
        if self.bind_mode == "tailscale":
            self.sync_tailscale(dshpc_win.tailscale_addresses(), initial=True)

    def close(self) -> None:
        with self._lock:
            servers = list(self._servers.values())
            self._servers.clear()
        for server, thread in servers:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass
            try:
                thread.join(timeout=2)
            except Exception:
                pass

    def _bind(self, host: str, *, required: bool) -> bool:
        with self._lock:
            if host in self._servers:
                return True
        try:
            server = dshpc_mcp.DshpcHttpServer(
                (host, self.port),
                protocol=self.protocol,
                token=self.config.token,
                logger=logger,
                store=self.store,
                max_request_bytes=int(self.config.data.get("max_request_bytes", 2 * 1024 * 1024)),
            )
        except OSError as error:
            message = f"cannot bind http://{host}:{self.port} : {error}"
            if required:
                logger.error(message)
                raise SystemExit(4) from error
            logger.warning(message)
            return False
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
        thread.start()
        with self._lock:
            self._servers[host] = (server, thread)
        logger.info("listening on http://%s:%s/mcp", host, self.port)
        return True

    def _unbind(self, host: str) -> None:
        with self._lock:
            entry = self._servers.pop(host, None)
        if not entry:
            return
        server, thread = entry
        logger.info("closing listener on %s", host)
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass
        try:
            thread.join(timeout=2)
        except Exception:
            pass

    # -- tailscale ---------------------------------------------------------
    def sync_tailscale(self, addresses: list[str], *, initial: bool = False) -> None:
        desired = {ip for ip in addresses if ip and ip != "127.0.0.1"}
        with self._lock:
            current = {host for host in self._servers if host not in {"127.0.0.1", "0.0.0.0"}}
        for host in sorted(current - desired):
            self._unbind(host)
        for host in sorted(desired - current):
            self._bind(host, required=False)
        if desired != self._tailscale_hosts:
            self._tailscale_hosts = desired
            if desired:
                logger.info("Tailscale addresses: %s", ", ".join(sorted(desired)))
            else:
                logger.warning(
                    "no Tailscale IPv4 address found yet; only loopback is listening. "
                    "Remote DSH connections will not work until Tailscale is up."
                )

    # -- URLs --------------------------------------------------------------
    def public_base(self) -> str:
        host = self.public_host
        if host:
            if host.startswith("http://") or host.startswith("https://"):
                return host.rstrip("/")
            if host.count(":") > 1 and not host.startswith("["):
                host = f"[{host}]"
            return f"http://{host}:{self.port}"
        with self._lock:
            hosts = [h for h in self._servers if h not in {"0.0.0.0", "127.0.0.1"}]
        if hosts:
            return f"http://{hosts[0]}:{self.port}"
        if self.bind_mode == "address" and self.bind_address and self.bind_address != "0.0.0.0":
            return f"http://{self.bind_address}:{self.port}"
        return f"http://127.0.0.1:{self.port}"

    def urls(self) -> list[str]:
        with self._lock:
            hosts = list(self._servers)
        result: list[str] = []
        for host in hosts:
            if host == "0.0.0.0":
                result.append(self.public_base())
            else:
                result.append(f"http://{host}:{self.port}")
        return sorted(set(result))


# ---------------------------------------------------------------------------
# State watcher
# ---------------------------------------------------------------------------


class StateWatcher(threading.Thread):
    def __init__(
        self,
        *,
        state: dshpc_state.StateStore,
        gate: dshpc_tools.InfoGate,
        sleep: dshpc_win.SleepBlocker,
        stop: threading.Event,
    ):
        super().__init__(name="state-watcher", daemon=True)
        self.state = state
        self.gate = gate
        self.sleep = sleep
        self.stop = stop
        self._last_seq = state.snapshot().get("info_gate_reset_seq", 0)
        self._last_sleep = (False, False)
        self._last_features: tuple[bool, ...] = tuple(state.features().get(f, False) for f in dshpc_state.FEATURES)

    def apply_initial(self) -> None:
        snapshot = self.state.snapshot()
        hold = bool(snapshot["sleep_hold"] and snapshot["features"].get("sleep"))
        keep_display = bool(snapshot["sleep_keep_display"] and hold)
        self.sleep.set(hold, keep_display)
        self._last_sleep = (hold, keep_display)

    def run(self) -> None:
        while not self.stop.wait(1.0):
            try:
                snapshot = self.state.snapshot()
            except Exception:
                logger.exception("state watcher could not read state")
                continue
            seq = snapshot.get("info_gate_reset_seq", 0)
            if seq != self._last_seq:
                self._last_seq = seq
                reset_at = _parse_timestamp(snapshot.get("updated_at"))
                if self.gate.apply_reset(seq, reset_at):
                    logger.info("first-call gate reset (state sequence %s)", seq)
            hold = bool(snapshot["sleep_hold"] and snapshot["features"].get("sleep"))
            keep_display = bool(snapshot["sleep_keep_display"] and hold)
            if (hold, keep_display) != self._last_sleep:
                self.sleep.set(hold, keep_display)
                self._last_sleep = (hold, keep_display)
                print_state = {
                    "sleep_hold": hold,
                    "keep_display": keep_display,
                }
                logger.info("sleep hold updated: %s", print_state)
            features = tuple(snapshot["features"].get(name, False) for name in dshpc_state.FEATURES)
            if features != self._last_features:
                self._last_features = features
                logger.info(
                    "features updated by %s: %s",
                    snapshot.get("updated_by"),
                    {name: snapshot["features"].get(name) for name in dshpc_state.FEATURES},
                )


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


def _resolve_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    if args.data_dir:
        data_dir = Path(args.data_dir).expanduser()
    elif args.config:
        data_dir = Path(args.config).expanduser().resolve().parent
    else:
        data_dir = dshpc_state.default_data_dir()
    data_dir = data_dir.resolve()
    config_path = Path(args.config).expanduser().resolve() if args.config else data_dir / "config.json"
    return data_dir, config_path


def _pairing_payload(config: dshpc_state.Config, data_dir: Path, *, include_token: bool) -> dict[str, Any]:
    addresses = dshpc_win.tailscale_addresses()
    public = str(config.data.get("public_host") or "").strip()
    urls: list[str] = []
    for host in ([public] if public else []) + addresses:
        if host.startswith("http://") or host.startswith("https://"):
            candidate = host.rstrip("/")
        else:
            candidate = f"http://{host}:{config.port}"
        if candidate not in urls:
            urls.append(candidate)
    if not urls:
        urls.append(f"http://127.0.0.1:{config.port}")
    payload: dict[str, Any] = {
        "ok": True,
        "service": dshpc_version.SERVICE_NAME,
        "version": dshpc_version.__version__,
        "config_path": str(config.path),
        "data_dir": str(data_dir),
        "port": config.port,
        "bind": config.bind,
        "bind_address": config.data.get("bind_address", ""),
        "public_host": public,
        "hostname": socket.gethostname(),
        "tailscale_ips": addresses,
        "urls": urls,
        "mcp_url": urls[0] + "/mcp",
        "python": sys.executable,
    }
    if include_token:
        payload["token"] = config.token
    return payload


def mode_init(args: argparse.Namespace) -> int:
    data_dir, config_path = _resolve_paths(args)
    data_dir.mkdir(parents=True, exist_ok=True)
    config = dshpc_state.Config.load_or_create(
        config_path,
        port=args.port,
        bind=args.bind,
        bind_address=args.bind_address,
        rotate_token=args.rotate_token,
    )
    if args.public_host is not None:
        config.data["public_host"] = args.public_host
        config.save()
    dshpc_state.StateStore(
        data_dir / "state.json",
        remote_control=True,
        keep_display=bool(config.sleep.get("keep_display_default", False)),
    )
    (data_dir / "screenshots").mkdir(parents=True, exist_ok=True)
    print(json.dumps(_pairing_payload(config, data_dir, include_token=True), ensure_ascii=False, indent=2))
    return 0


def mode_check(args: argparse.Namespace) -> int:
    data_dir, config_path = _resolve_paths(args)
    if not config_path.is_file():
        print(json.dumps({"ok": False, "error": f"config not found: {config_path}"}))
        return 1
    config = dshpc_state.Config.load(config_path)
    state = dshpc_state.StateStore(data_dir / "state.json").snapshot()
    payload = _pairing_payload(config, data_dir, include_token=False)
    payload.update(
        {
            "ok": True,
            "config_exists": True,
            "state_path": str(data_dir / "state.json"),
            "features": state.get("features"),
            "remote_control": state.get("remote_control"),
            "sleep_hold": state.get("sleep_hold"),
            "pid": dshpc_state.read_pid(data_dir / "server.pid"),
            "pid_alive": dshpc_state.pid_alive(dshpc_state.read_pid(data_dir / "server.pid")),
        }
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def mode_stdio(args: argparse.Namespace) -> int:
    data_dir, config_path = _resolve_paths(args)
    config = dshpc_state.Config.load_or_create(config_path)
    config.data_dir = data_dir
    setup_logging(config, console=True)
    state = dshpc_state.StateStore(
        data_dir / "state.json",
        remote_control=True,
        keep_display=bool(config.sleep.get("keep_display_default", False)),
    )
    gate = dshpc_tools.InfoGate()
    sleep = dshpc_win.SleepBlocker()
    sleep.start()
    store = dshpc_tools.ScreenshotStore(
        data_dir / "screenshots",
        retention_hours=int(config.screenshot.get("retention_hours", 24)),
        max_files=int(config.screenshot.get("max_files", 200)),
    )
    instructions = dshpc_mcp.build_instructions(
        hostname=socket.gethostname(), port=config.port, version=dshpc_version.__version__
    )
    tools = dshpc_tools.Tools(
        config=config,
        state=state,
        gate=gate,
        sleep=sleep,
        store=store,
        url_base_provider=lambda: "",
        logger=logger,
        started_at=STARTED_AT,
    )
    protocol = dshpc_mcp.McpProtocol(tools=tools, instructions=instructions, logger=logger)
    try:
        return dshpc_mcp.StdioProtocol(protocol).serve()
    finally:
        sleep.stop()


def mode_serve(args: argparse.Namespace) -> int:
    data_dir, config_path = _resolve_paths(args)
    config = dshpc_state.Config.load_or_create(config_path)
    config.data_dir = data_dir
    if args.port is not None:
        config.data["port"] = int(args.port)
    if args.bind is not None:
        config.data["bind"] = args.bind
    if args.bind_address is not None:
        config.data["bind_address"] = args.bind_address
    if args.public_host is not None:
        config.data["public_host"] = args.public_host
    setup_logging(config, console=bool(args.foreground))

    if not config.token:
        logger.error("configuration has no bearer token; run --init")
        return 2

    data_dir.mkdir(parents=True, exist_ok=True)
    lock = InstanceLock(data_dir / "server.lock")
    if not lock.acquire():
        logger.error("another dsh-pc-remote instance is already running for %s", data_dir)
        return 3

    stop = threading.Event()
    servers: ListenerSet | None = None
    watcher: StateWatcher | None = None
    watcher_stop = threading.Event()
    sleep = dshpc_win.SleepBlocker()
    try:
        dshpc_win.enable_dpi_awareness()
        dshpc_state.write_pid(data_dir / "server.pid", os.getpid())
        state = dshpc_state.StateStore(
            data_dir / "state.json",
            remote_control=True,
            keep_display=bool(config.sleep.get("keep_display_default", False)),
        )
        gate = dshpc_tools.InfoGate()
        sleep.start()
        store = dshpc_tools.ScreenshotStore(
            data_dir / "screenshots",
            retention_hours=int(config.screenshot.get("retention_hours", 24)),
            max_files=int(config.screenshot.get("max_files", 200)),
        )
        instructions = dshpc_mcp.build_instructions(
            hostname=socket.gethostname(), port=config.port, version=dshpc_version.__version__
        )
        tools = dshpc_tools.Tools(
            config=config,
            state=state,
            gate=gate,
            sleep=sleep,
            store=store,
            url_base_provider=lambda: servers.public_base() if servers else "",
            logger=logger,
            started_at=STARTED_AT,
        )
        protocol = dshpc_mcp.McpProtocol(tools=tools, instructions=instructions, logger=logger)
        servers = ListenerSet(
            config=config,
            protocol=protocol,
            store=store,
            bind_mode=config.bind,
            bind_address=str(config.data.get("bind_address") or ""),
            public_host=str(config.data.get("public_host") or ""),
        )
        servers.start()
        watcher = StateWatcher(state=state, gate=gate, sleep=sleep, stop=watcher_stop)
        watcher.apply_initial()
        watcher.start()

        if config.bind == "tailscale":
            tailscale_thread = threading.Thread(
                target=_tailscale_watcher,
                args=(servers, watcher_stop, logger),
                name="tailscale-watcher",
                daemon=True,
            )
            tailscale_thread.start()

        def _handle_signal(signum, frame):  # noqa: ANN001
            logger.info("received signal %s; shutting down", signum)
            stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handle_signal)
            except (ValueError, OSError):
                pass
        if hasattr(signal, "SIGBREAK"):
            try:
                signal.signal(signal.SIGBREAK, _handle_signal)
            except (ValueError, OSError):
                pass

        logger.info(
            "%s %s started: pid=%s port=%s bind=%s data=%s",
            dshpc_version.SERVICE_TITLE,
            dshpc_version.__version__,
            os.getpid(),
            config.port,
            config.bind,
            data_dir,
        )
        logger.info("MCP endpoints: %s", ", ".join(url + "/mcp" for url in servers.urls()))
        while not stop.wait(0.5):
            pass
        return 0
    finally:
        watcher_stop.set()
        if servers:
            servers.close()
        sleep.stop()
        try:
            pid_path = data_dir / "server.pid"
            if dshpc_state.read_pid(pid_path) == os.getpid():
                pid_path.unlink(missing_ok=True)
        except OSError:
            pass
        lock.release()
        logger.info("server stopped")


def _tailscale_watcher(servers: ListenerSet, stop: threading.Event, log: logging.Logger) -> None:
    last: list[str] = []
    while not stop.wait(15.0):
        try:
            addresses = dshpc_win.tailscale_addresses()
        except Exception:
            log.exception("could not enumerate adapters")
            continue
        if addresses != last:
            last = addresses
            servers.sync_tailscale(addresses)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dshpc_server",
        description="DSH Remote PC controlled-side MCP server (Windows).",
    )
    parser.add_argument("--config", help="path to config.json (default: <data-dir>/config.json)")
    parser.add_argument("--data-dir", help="data directory (default: %%LOCALAPPDATA%%\\DSHRemotePC\\data)")
    parser.add_argument("--port", type=int, help="override the listening port")
    parser.add_argument("--bind", choices=["tailscale", "address", "all"], help="bind mode")
    parser.add_argument("--bind-address", help="address to bind when --bind address")
    parser.add_argument("--public-host", help="hostname/IP used in screenshot and pairing URLs")
    parser.add_argument("--rotate-token", action="store_true", help="generate a new bearer token")
    parser.add_argument("--init", action="store_true", help="create config/state and print pairing JSON")
    parser.add_argument("--check", action="store_true", help="validate configuration and print status")
    parser.add_argument("--stdio", action="store_true", help="serve MCP over stdin/stdout")
    parser.add_argument("--foreground", action="store_true", help="also log to stderr")
    parser.add_argument("--version", action="version", version=f"%(prog)s {dshpc_version.__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    dshpc_win.enable_dpi_awareness()
    if args.init:
        return mode_init(args)
    if args.check:
        return mode_check(args)
    if args.stdio:
        return mode_stdio(args)
    return mode_serve(args)


if __name__ == "__main__":
    raise SystemExit(main())
