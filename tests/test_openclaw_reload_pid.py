#!/usr/bin/env python3
"""Unit tests for the OpenClaw deferred-reload PID wait (Fix 1).

The OpenClaw agent flow restarts the gateway only AFTER pairing resolves, so the
relaying agent isn't killed mid-send. It must wait on the EXACT pair pid, because
OpenClaw renames its processes (process.title) and `pgrep -f "chat4000 pair"`
never matches the pair process — which made the old argv-pattern wait a no-op and
fired the restart immediately (observed live on oc-9).

Stdlib-only (`python3 -m unittest`).

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest import mock


def _load_installer():
    here = Path(__file__).resolve().parent
    path = here.parent / "scripts" / "installer.py"
    spec = importlib.util.spec_from_file_location("installer_under_test_reloadpid", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


installer = _load_installer()


class TestWaitPairPidResolved(unittest.TestCase):
    def test_returns_immediately_when_pid_dead(self):
        with mock.patch.object(installer, "_pid_alive", lambda p: False), \
             mock.patch.object(installer.time, "sleep", lambda *_: None):
            installer._wait_pair_pid_resolved(4242, 330)  # returns, doesn't hang

    def test_returns_when_pid_is_zombie(self):
        # kill -0 still succeeds on a zombie (non-reaping container PID 1), so a
        # finished pair child must be detected via the zombie state.
        with mock.patch.object(installer, "_pid_alive", lambda p: True), \
             mock.patch.object(installer, "_pid_is_zombie", lambda p: True), \
             mock.patch.object(installer.time, "sleep", lambda *_: None):
            installer._wait_pair_pid_resolved(4242, 330)

    def test_blocks_until_pid_dies_then_returns(self):
        # Alive for two polls, then dies → must loop then return (not hang, not
        # return on the first poll).
        states = iter([True, True, False])

        def alive(_p):
            try:
                return next(states)
            except StopIteration:
                return False

        sleeps = {"n": 0}
        with mock.patch.object(installer, "_pid_alive", side_effect=alive), \
             mock.patch.object(installer, "_pid_is_zombie", lambda p: False), \
             mock.patch.object(installer.time, "sleep", lambda *_: sleeps.__setitem__("n", sleeps["n"] + 1)):
            installer._wait_pair_pid_resolved(4242, 330)
        self.assertGreaterEqual(sleeps["n"], 1, "must poll/sleep while the pid is still alive")


class TestDeferredReloadRouting(unittest.TestCase):
    def _run(self, pair_pid):
        calls = {"pid": 0, "argv": 0}
        with mock.patch.object(installer, "_wait_pair_pid_resolved",
                               lambda pid, mw: calls.__setitem__("pid", calls["pid"] + 1)), \
             mock.patch.object(installer, "_wait_pair_watcher_resolved",
                               lambda mw: calls.__setitem__("argv", calls["argv"] + 1)), \
             mock.patch.object(installer, "detect_restart_method", lambda: None):
            rc = installer._run_openclaw_deferred_reload(330, pair_pid=pair_pid)
        return rc, calls

    def test_with_pid_uses_pid_wait_not_argv(self):
        rc, calls = self._run(4242)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, {"pid": 1, "argv": 0})

    def test_without_pid_falls_back_to_argv_wait(self):
        rc, calls = self._run(None)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, {"pid": 0, "argv": 1})


class TestSpawnDetachedPairReturnsPid(unittest.TestCase):
    def test_reused_live_pair_returns_five_tuple_pid_none(self):
        reused = ("123456", "https://pair.chat4000.com/?code=123456", "/tmp/x.log", None)
        with mock.patch.object(installer, "_reuse_live_pair", lambda: reused):
            result = installer.spawn_detached_pair(["openclaw", "chat4000", "pair"], {})
        self.assertEqual(len(result), 5)
        self.assertEqual(result[0], "123456")
        self.assertIsNone(result[4], "reused path has no spawned child → pid None")


if __name__ == "__main__":
    unittest.main()
