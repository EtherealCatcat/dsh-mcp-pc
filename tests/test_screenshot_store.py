#!/usr/bin/env python3
"""Cross-platform regression tests for ScreenshotStore (P3).

    python3 tests/test_screenshot_store.py

The store used one predictable ``<id>.png.tmp`` name per screenshot and its
cleanup() removed *every* ``.tmp`` it found, so a cleanup() running next to a
save() could delete the temp file before os.replace() consumed it.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import dshpc_tools  # noqa: E402
from dshpc_tools import ScreenshotStore  # noqa: E402


class ScreenshotStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dshpc-store-"))
        self.store = ScreenshotStore(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_lands_even_if_cleanup_runs_mid_save(self) -> None:
        """Deterministic race: cleanup() executes while the temp file exists."""
        store = self.store
        real_replace = os.replace

        def cleanup_then_replace(src, dst):
            store.cleanup()
            real_replace(src, dst)

        with mock.patch.object(dshpc_tools.os, "replace", cleanup_then_replace):
            shot_id = store.save(b"\x89PNG\r\n\x1a\nfake")

        self.assertTrue((self.tmp / f"{shot_id}.png").is_file())
        self.assertEqual(list(self.tmp.glob("*.tmp")), [])

    def test_cleanup_reclaims_stale_temp_files(self) -> None:
        stale = self.tmp / ".leftover-from-crash.tmp"
        stale.write_bytes(b"half-written")
        old = time.time() - ScreenshotStore.STALE_TEMP_SECONDS - 60
        os.utime(stale, (old, old))
        self.store.cleanup()
        self.assertFalse(stale.exists())

    def test_cleanup_keeps_fresh_temp_files(self) -> None:
        fresh = self.tmp / ".in-flight.tmp"
        fresh.write_bytes(b"still writing")
        self.store.cleanup()
        self.assertTrue(fresh.exists())

    def test_concurrent_saves_and_cleanups(self) -> None:
        errors: list[BaseException] = []
        shots: list[str] = []
        lock = threading.Lock()

        def worker(index: int) -> None:
            try:
                for round_ in range(15):
                    shot = self.store.save(f"frame-{index}-{round_}".encode() * 8)
                    with lock:
                        shots.append(shot)
                    if round_ % 3 == 0:
                        self.store.cleanup()
            except BaseException as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)

        self.assertEqual(errors, [])
        self.assertEqual(len(shots), 6 * 15)
        for shot in shots:
            self.assertTrue((self.tmp / f"{shot}.png").is_file(), shot)
        self.assertEqual(list(self.tmp.glob("*.tmp")), [])

    def test_cleanup_enforces_max_files_but_keeps_newest(self) -> None:
        store = ScreenshotStore(self.tmp, max_files=10)
        shots = [store.save(b"x" * 32) for _ in range(20)]
        remaining = sorted(path.stem for path in self.tmp.glob("*.png"))
        self.assertLessEqual(len(remaining), 10)
        for shot in shots[-5:]:
            self.assertIn(shot, remaining)

    def test_path_for_rejects_malformed_ids(self) -> None:
        shot = self.store.save(b"x")
        self.assertIsNotNone(self.store.path_for(shot, "png"))
        for bad in ("", "../etc/passwd", shot.upper(), "0" * 31):
            self.assertIsNone(self.store.path_for(bad, "png"), bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
