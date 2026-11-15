#!/usr/bin/env python3
"""Cross-platform regression tests for the HTTP framing fixes (P2).

Run on any OS (no Win32 imports happen for the code paths exercised here):

    python3 tests/test_http_keepalive.py

The server used to answer 401/413/400 without reading the request body, so the
unread bytes were parsed as the next request line on the same keep-alive
connection (a bad token made the following call fail with
``501 Unsupported method ('{"jsonrpc":...}POST')``). Every early return now
either drains the body or closes the connection; these tests pin that down.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server" / "dshpc_server.py"
# Set DSHPC_TEST_URL/DSHPC_TEST_TOKEN to run the same assertions against a
# deployed receiver over Tailscale instead of a throwaway local server.
REMOTE_URL = os.environ.get("DSHPC_TEST_URL", "").strip()
REMOTE_TOKEN = os.environ.get("DSHPC_TEST_TOKEN", "").strip()
MAX_BODY = int(os.environ.get("DSHPC_TEST_MAX_BODY", "4096"))
HOST = "127.0.0.1"

VALID_REQUEST = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class HttpConn:
    """Minimal HTTP/1.1 client that exposes the raw connection (keep-alive)."""

    def __init__(self, port: int, timeout: float = 10.0):
        self.sock = socket.create_connection((HOST, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.rfile = self.sock.makefile("rb")

    def send(self, data: bytes) -> None:
        self.sock.sendall(data)

    def read_response(self) -> tuple[int, dict[str, str], bytes]:
        line = self.rfile.readline()
        if not line:
            raise ConnectionError("connection closed by peer")
        status = int(line.split()[1])
        headers: dict[str, str] = {}
        while True:
            raw = self.rfile.readline()
            if raw in (b"\r\n", b"\n", b""):
                break
            key, _, value = raw.decode("latin-1").partition(":")
            headers[key.strip().lower()] = value.strip()
        length = int(headers.get("content-length") or 0)
        return status, headers, self.rfile.read(length) if length else b""

    def close(self) -> None:
        try:
            self.rfile.close()
        finally:
            self.sock.close()


class KeepAliveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        global HOST
        if REMOTE_URL:
            parsed = urlsplit(REMOTE_URL)
            cls.port = parsed.port or 80
            cls.host = parsed.hostname or "127.0.0.1"
            cls.token = REMOTE_TOKEN
            cls.proc = None
            HOST = cls.host
            cls.tmp = Path(tempfile.mkdtemp(prefix="dshpc-http-"))
            return
        cls.host = "127.0.0.1"
        cls.tmp = Path(tempfile.mkdtemp(prefix="dshpc-http-"))
        init = subprocess.run(
            [sys.executable, str(SERVER), "--init", "--data-dir", str(cls.tmp)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if init.returncode != 0:
            raise RuntimeError(f"--init failed: {init.stderr}\n{init.stdout}")
        cls.token = json.loads(init.stdout)["token"]

        config_path = cls.tmp / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["bind"] = "all"
        config["max_request_bytes"] = MAX_BODY
        config_path.write_text(json.dumps(config), encoding="utf-8")

        cls.port = free_port()
        cls.proc = subprocess.Popen(
            [
                sys.executable,
                str(SERVER),
                "--data-dir",
                str(cls.tmp),
                "--port",
                str(cls.port),
                "--bind",
                "all",
                "--public-host",
                "127.0.0.1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", cls.port), timeout=1):
                    return
            except OSError:
                if cls.proc.poll() is not None:
                    raise RuntimeError(f"server exited: {cls.proc.stdout.read()}")
                time.sleep(0.1)
        raise RuntimeError("server did not start in time")

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.proc is None:
            shutil.rmtree(cls.tmp, ignore_errors=True)
            return
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- helpers -----------------------------------------------------------
    def post(self, conn: HttpConn, body: bytes, *, token: str | None = None, extra: str = "") -> None:
        auth = f"Bearer {token if token is not None else self.token}"
        conn.send(
            b"POST /mcp HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            + f"Authorization: {auth}\r\n".encode()
            + b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n".encode()
            + extra.encode()
            + b"\r\n"
            + body
        )

    def assert_valid_call(self, conn: HttpConn) -> None:
        self.post(conn, VALID_REQUEST)
        status, _, body = conn.read_response()
        self.assertEqual(status, 200, body[:200])
        payload = json.loads(body)
        self.assertEqual(payload.get("id"), 1)
        self.assertIn("tools", payload.get("result", {}))

    # -- tests -------------------------------------------------------------
    def test_bad_token_then_valid_call_on_same_connection(self) -> None:
        conn = HttpConn(self.port)
        try:
            self.post(conn, VALID_REQUEST, token="wrong-token")
            status, headers, _ = conn.read_response()
            self.assertEqual(status, 401)
            self.assertNotEqual(headers.get("connection", "").lower(), "close")
            self.assert_valid_call(conn)
        finally:
            conn.close()

    def test_oversize_body_is_drained_and_connection_reused(self) -> None:
        conn = HttpConn(self.port)
        try:
            self.post(conn, b"x" * (MAX_BODY + 1024))
            status, headers, _ = conn.read_response()
            self.assertEqual(status, 413)
            self.assertNotEqual(headers.get("connection", "").lower(), "close")
            self.assert_valid_call(conn)
        finally:
            conn.close()

    def test_huge_body_closes_connection_without_desync(self) -> None:
        conn = HttpConn(self.port)
        try:
            huge = b"x" * (MAX_BODY * 5)
            try:
                conn.send(
                    b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer "
                    + self.token.encode()
                    + b"\r\nContent-Length: "
                    + str(len(huge)).encode()
                    + b"\r\n\r\n"
                    + huge
                )
                status, headers, _ = conn.read_response()
                self.assertEqual(status, 413)
                self.assertEqual(headers.get("connection", "").lower(), "close")
            except (BrokenPipeError, ConnectionResetError):
                pass  # closing early is allowed for absurdly large uploads
        finally:
            conn.close()
        fresh = HttpConn(self.port)
        try:
            self.assert_valid_call(fresh)
        finally:
            fresh.close()

    def test_chunked_body_is_refused_and_closed(self) -> None:
        conn = HttpConn(self.port)
        try:
            conn.send(
                b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                b"Transfer-Encoding: chunked\r\nContent-Type: application/json\r\n\r\n"
                b"1b\r\n" + VALID_REQUEST + b"\r\n0\r\n\r\n"
            )
            status, headers, _ = conn.read_response()
            self.assertEqual(status, 411)
            self.assertEqual(headers.get("connection", "").lower(), "close")
        finally:
            conn.close()

    def test_empty_body_is_rejected_and_connection_is_still_synced(self) -> None:
        conn = HttpConn(self.port)
        try:
            conn.send(b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer " + self.token.encode() + b"\r\n\r\n")
            status, _, _ = conn.read_response()
            self.assertEqual(status, 400)
            self.assert_valid_call(conn)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
