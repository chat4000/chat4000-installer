#!/usr/bin/env python3
"""Unit tests for the post-restart readiness detection (option A): the installer
gates pairing on the plugins' OWN readiness markers instead of "process exists".

  • Hermes:   the `ready` marker file (resolve_chat4000_ready_marker) — must be
              present AND fresh (mtime >= the restart instant).
  • OpenClaw: a FRESH `runtime.hello_ok` / `runtime.rooms_ready` line in
              runtime.log (line timestamp >= the restart instant) — a stale
              marker from a previous boot must NOT count.

Hermetic: every test points HERMES_STATE_DIR / OPENCLAW_STATE_DIR at a temp dir,
so nothing touches a real ~/.hermes or ~/.openclaw. Stdlib-only.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import datetime
import importlib.util
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


def _load_installer():
    here = Path(__file__).resolve().parent
    path = here.parent / "scripts" / "installer.py"
    spec = importlib.util.spec_from_file_location("installer_under_test_ready", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


installer = _load_installer()


def _runtime_line(epoch: float, event: str, extra: str = "gateway=wss://relay") -> str:
    """A runtime.log line in the plugin's exact LOCAL-time format:
    'YYYY-MM-DD HH:MM:SS.mmm [tid:1] INFO <event> ...'."""
    dt = datetime.datetime.fromtimestamp(epoch)
    ts = dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
    return f"{ts} [tid:1] INFO {event} {extra}"


class TestHermesReadyMarker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"HERMES_STATE_DIR": self.tmp.name}, clear=False)
        self.env.start()
        self.marker = installer._hermes_ready_marker()
        self.marker.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_marker_path_under_state_dir(self):
        self.assertEqual(self.marker, Path(self.tmp.name) / "plugins" / "chat4000" / "ready")

    def test_missing_marker_is_not_fresh(self):
        self.assertFalse(installer._hermes_ready_marker_fresh(time.time()))

    def test_stale_marker_rejected(self):
        # Written well BEFORE the restart instant → not this boot.
        self.marker.write_text("ready\n")
        old = time.time() - 600
        os.utime(self.marker, (old, old))
        self.assertFalse(installer._hermes_ready_marker_fresh(time.time()))

    def test_fresh_marker_accepted(self):
        since = time.time()
        self.marker.write_text("ready\n")  # mtime ~now >= since - slack
        self.assertTrue(installer._hermes_ready_marker_fresh(since))

    def test_clear_then_reappear(self):
        # The delete→restart→await-reappear pattern: after clearing, only a NEW
        # write counts as ready.
        self.marker.write_text("stale\n")
        old = time.time() - 600
        os.utime(self.marker, (old, old))
        installer._clear_hermes_ready_marker()
        self.assertFalse(self.marker.exists())
        since = time.time()
        self.marker.write_text("ready\n")
        self.assertTrue(installer._hermes_ready_marker_fresh(since))


class TestOpenClawRuntimeReady(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"OPENCLAW_STATE_DIR": self.tmp.name}, clear=False)
        self.env.start()
        self.log = installer._openclaw_runtime_log()
        self.log.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_log_path_under_state_dir(self):
        self.assertEqual(
            self.log,
            Path(self.tmp.name) / "plugins" / "chat4000" / "logs" / "runtime.log",
        )

    def test_no_log_is_not_connected(self):
        self.assertFalse(installer._openclaw_runtime_connected_since(time.time()))

    def test_stale_marker_rejected(self):
        since = time.time()
        # hello_ok from 10 minutes ago — a previous boot, must not count.
        self.log.write_text(_runtime_line(since - 600, "runtime.hello_ok") + "\n")
        self.assertFalse(installer._openclaw_runtime_connected_since(since))

    def test_fresh_hello_ok_accepted(self):
        since = time.time()
        self.log.write_text(_runtime_line(since + 1, "runtime.hello_ok") + "\n")
        self.assertTrue(installer._openclaw_runtime_connected_since(since))

    def test_fresh_rooms_ready_accepted(self):
        since = time.time()
        self.log.write_text(_runtime_line(since + 1, "runtime.rooms_ready", "space=!s control=!c") + "\n")
        self.assertTrue(installer._openclaw_runtime_connected_since(since))

    def test_stale_then_fresh(self):
        since = time.time()
        self.log.write_text(
            _runtime_line(since - 600, "runtime.hello_ok") + "\n"
            + _runtime_line(since - 300, "some.other.event") + "\n"
            + _runtime_line(since + 2, "runtime.hello_ok") + "\n"
        )
        self.assertTrue(installer._openclaw_runtime_connected_since(since))

    def test_unparseable_ts_falls_back_to_mtime(self):
        # A marker line with no parseable leading timestamp → fall back to file mtime.
        self.log.write_text("garbage-without-timestamp runtime.hello_ok gateway=x\n")
        # Fresh file → accepted.
        self.assertTrue(installer._openclaw_runtime_connected_since(time.time() - 1))
        # Stale file mtime → rejected.
        old = time.time() - 600
        os.utime(self.log, (old, old))
        self.assertFalse(installer._openclaw_runtime_connected_since(time.time()))

    def test_non_marker_lines_ignored(self):
        since = time.time()
        self.log.write_text(_runtime_line(since + 1, "runtime.starting") + "\n")
        self.assertFalse(installer._openclaw_runtime_connected_since(since))


class TestParseRuntimeTs(unittest.TestCase):
    def test_parses_valid(self):
        epoch = time.time()
        line = _runtime_line(epoch, "runtime.hello_ok")
        parsed = installer._parse_runtime_ts(line)
        self.assertIsNotNone(parsed)
        # Within 1s (millisecond truncation in the format).
        self.assertLess(abs(parsed - epoch), 1.0)

    def test_none_for_junk(self):
        self.assertIsNone(installer._parse_runtime_ts("no timestamp here"))
        self.assertIsNone(installer._parse_runtime_ts(""))


if __name__ == "__main__":
    unittest.main()
