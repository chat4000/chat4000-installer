#!/usr/bin/env python3
"""Unit tests for installer agent-mode detection (BUG 1).

Stdlib-only (`python3 -m unittest`) to match the installer's no-deps ethos
(pytest is not installed and the installer itself imports only stdlib).

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _load_installer():
    """Import scripts/installer.py as a module without running main()."""
    here = Path(__file__).resolve().parent
    path = here.parent / "scripts" / "installer.py"
    spec = importlib.util.spec_from_file_location("installer_under_test", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


installer = _load_installer()


class TestInferAgentFromEnv(unittest.TestCase):
    """Primary signal: the OPENCLAW_SHELL env var on the tool subprocess."""

    def test_openclaw_exec_detected(self):
        # OpenClaw's autonomous exec/bash tool sets OPENCLAW_SHELL=exec.
        self.assertEqual(
            installer._infer_agent_from_env({"OPENCLAW_SHELL": "exec"}),
            "openclaw",
        )

    def test_tui_local_NOT_detected(self):
        # A HUMAN at OpenClaw's interactive TUI shell gets a different value —
        # must NOT be treated as the autonomous agent.
        self.assertIsNone(
            installer._infer_agent_from_env({"OPENCLAW_SHELL": "tui-local"})
        )

    def test_acp_client_NOT_detected(self):
        self.assertIsNone(
            installer._infer_agent_from_env({"OPENCLAW_SHELL": "acp-client"})
        )

    def test_empty_env_NOT_detected(self):
        self.assertIsNone(installer._infer_agent_from_env({}))

    def test_human_shell_NOT_detected(self):
        # A normal human shell carries the usual env, no OPENCLAW_SHELL.
        self.assertIsNone(
            installer._infer_agent_from_env(
                {"PATH": "/usr/bin", "HOME": "/home/x", "SHELL": "/bin/zsh"}
            )
        )


class TestMatchAgentArgv(unittest.TestCase):
    """Fallback signal: gateway recognition from an ancestor's argv."""

    def test_hermes_gateway(self):
        self.assertEqual(
            installer._match_agent_argv(["python", "-m", "hermes", "gateway", "run"]),
            "hermes",
        )

    def test_openclaw_gateway_run(self):
        self.assertEqual(
            installer._match_agent_argv(["openclaw", "gateway", "run"]),
            "openclaw",
        )

    def test_openclaw_renamed_daemon(self):
        # process.title rename to "<cli>-<subcommand>".
        self.assertEqual(
            installer._match_agent_argv(["openclaw-gateway"]),
            "openclaw",
        )

    def test_openclaw_bare_daemon_live_box_shape(self):
        # The live-container shape: argv is exactly ["openclaw"], no sub-args.
        self.assertEqual(installer._match_agent_argv(["openclaw"]), "openclaw")

    def test_openclaw_full_path_bare_daemon(self):
        self.assertEqual(
            installer._match_agent_argv(["/usr/local/bin/openclaw"]),
            "openclaw",
        )

    def test_human_openclaw_subcommand_NOT_matched(self):
        # A human running a real openclaw command (with sub-args, not "gateway")
        # must NOT be mistaken for the gateway daemon.
        self.assertIsNone(
            installer._match_agent_argv(["openclaw", "plugins", "install", "--link"])
        )
        self.assertIsNone(
            installer._match_agent_argv(["openclaw", "chat4000", "pair"])
        )

    def test_unrelated_process_NOT_matched(self):
        self.assertIsNone(installer._match_agent_argv(["bash", "-lc", "curl x | bash"]))
        self.assertIsNone(installer._match_agent_argv([]))


class TestInferAgentCallerProcWalk(unittest.TestCase):
    """End-to-end ancestry walk against a FAKE /proc tree."""

    def _make_fake_proc(self, tmp: Path, tree):
        """tree: {pid: (ppid, argv_list)} → write /proc/<pid>/{cmdline,status}."""
        for pid, (ppid, argv) in tree.items():
            d = tmp / str(pid)
            d.mkdir(parents=True, exist_ok=True)
            (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
            (d / "status").write_text(f"Name:\t{argv[0] if argv else '?'}\nPPid:\t{ppid}\n")

    def test_walk_finds_openclaw_bare_gateway_ancestor(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            # 100 = bare openclaw gateway daemon; 200 = bash; 300 = installer's parent.
            tree = {
                100: (1, ["openclaw"]),
                200: (100, ["bash", "-lc", "curl ... | python3 installer.py"]),
                300: (200, ["python3", "installer.py"]),
            }
            self._make_fake_proc(tmp, tree)
            # Start the walk at pid 300's parent chain (ppid=200 → 100 → gateway).
            self.assertEqual(
                installer._infer_agent_caller(proc_root=str(tmp), start_pid=200),
                "openclaw",
            )

    def test_walk_human_tree_returns_none(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            tree = {
                1: (0, ["init"]),
                10: (1, ["sshd"]),
                20: (10, ["-zsh"]),
                30: (20, ["python3", "installer.py"]),
            }
            self._make_fake_proc(tmp, tree)
            self.assertIsNone(
                installer._infer_agent_caller(proc_root=str(tmp), start_pid=20)
            )

    def test_env_signal_short_circuits_walk(self):
        # If the env var is present, detection succeeds even with no /proc.
        old = os.environ.get("OPENCLAW_SHELL")
        os.environ["OPENCLAW_SHELL"] = "exec"
        try:
            self.assertEqual(
                installer._infer_agent_caller(proc_root="/nonexistent-proc"),
                "openclaw",
            )
        finally:
            if old is None:
                os.environ.pop("OPENCLAW_SHELL", None)
            else:
                os.environ["OPENCLAW_SHELL"] = old


class TestAgentRunMarker(unittest.TestCase):
    """BUG 2: the /tmp 'install already ran in this window' guard."""

    def setUp(self):
        import tempfile

        self._td = tempfile.TemporaryDirectory()
        self._orig = installer.AGENT_RUN_MARKER
        installer.AGENT_RUN_MARKER = str(Path(self._td.name) / "marker")

    def tearDown(self):
        installer.AGENT_RUN_MARKER = self._orig
        self._td.cleanup()

    def test_no_marker_returns_none(self):
        self.assertIsNone(installer._fresh_agent_run_marker())

    def test_fresh_marker_detected(self):
        installer._write_agent_run_marker()
        m = installer._fresh_agent_run_marker()
        self.assertIsNotNone(m)
        self.assertEqual(m["pid"], os.getpid())

    def test_stale_marker_ignored_and_removed(self):
        import json
        import time as _t

        old_ts = int(_t.time()) - (installer.AGENT_RUN_MARKER_TTL_S + 60)
        Path(installer.AGENT_RUN_MARKER).write_text(json.dumps({"pid": 1, "ts": old_ts}))
        self.assertIsNone(installer._fresh_agent_run_marker())
        # Stale marker must be cleaned up so the next run proceeds normally.
        self.assertFalse(Path(installer.AGENT_RUN_MARKER).exists())

    def test_corrupt_marker_ignored_and_removed(self):
        Path(installer.AGENT_RUN_MARKER).write_text("not json at all {{{")
        self.assertIsNone(installer._fresh_agent_run_marker())
        self.assertFalse(Path(installer.AGENT_RUN_MARKER).exists())

    def test_short_circuit_none_when_no_marker(self):
        # No marker → no short-circuit → install proceeds (returns None).
        self.assertIsNone(installer._agent_already_ran_short_circuit())

    def test_short_circuit_when_fresh_marker_no_live_pair(self):
        # Fresh marker but no live pairing code → prints "already done", exit 0.
        installer._write_agent_run_marker()
        orig_reuse = installer._reuse_live_pair
        orig_emit = installer._emit
        printed = {}
        orig_print = installer._agent_print
        installer._reuse_live_pair = lambda: None
        installer._emit = lambda *a, **k: None
        installer._agent_print = lambda lines: printed.update(text="\n".join(lines))
        try:
            rc = installer._agent_already_ran_short_circuit()
        finally:
            installer._reuse_live_pair = orig_reuse
            installer._emit = orig_emit
            installer._agent_print = orig_print
        self.assertEqual(rc, 0)
        self.assertIn("Already done", printed["text"])
        self.assertIn("DO NOT run the install command again", printed["text"])


class TestDetectRestartMethodF1(unittest.TestCase):
    """F1: a disabled-service box must route to 'foreground', not the
    'openclaw-supervised' detour whose `gateway restart` no-ops on a disabled
    box. The old check only matched the literal 'service disabled' substring;
    OpenClaw 2026.6.6 prints 'Service: systemd user (disabled)'."""

    def _detect(self, status_out):
        class _R:
            returncode = 0
            stdout = status_out
            stderr = ""

        def fake_run(cmd, *a, **k):
            if cmd[1:] == ["ps", "--filter", "name=openclaw-gateway", "--format", "{{.Names}}"]:
                return _R()  # no docker container in output
            if len(cmd) >= 2 and cmd[1:] == ["gateway", "status"]:
                return _R()
            r = _R()
            r.stdout = ""
            return r

        # No docker on PATH so we exercise the `openclaw gateway status` branch.
        def fake_which(name):
            return "/usr/bin/openclaw" if name == "openclaw" else None

        with mock.patch.object(installer.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(installer.shutil, "which", side_effect=fake_which):
            return installer.detect_restart_method()

    def test_systemd_user_disabled_parenthetical_is_foreground(self):
        # The exact string from the broken box.
        self.assertEqual(self._detect("Service: systemd user (disabled)"), "foreground")

    def test_legacy_service_disabled_still_foreground(self):
        self.assertEqual(self._detect("Gateway service disabled."), "foreground")

    def test_not_installed_is_foreground(self):
        self.assertEqual(self._detect("Service is not installed"), "foreground")

    def test_enabled_supervised_box_stays_supervised(self):
        self.assertEqual(
            self._detect("Service: systemd user (enabled, running)"), "openclaw-supervised"
        )


class TestOpenclawGatewayLockfilePid(unittest.TestCase):
    """F4: read the running gateway's pid from /tmp/openclaw-<uid>/gateway.*.lock
    (JSON {"pid":N}) — argv-independent, since the live gateway runs as bare
    `openclaw`."""

    def _with_lock(self, pids, alive_pids):
        """Run _openclaw_gateway_pid with a lock dir holding `pids` and only
        `alive_pids` reported alive. Returns the pid the helper resolves."""
        with tempfile.TemporaryDirectory() as td:
            lock_dir = Path(td)
            for i, pid in enumerate(pids):
                (lock_dir / f"gateway.{i:08x}.lock").write_text(json.dumps({"pid": pid}))
            with mock.patch.object(installer, "_openclaw_gateway_lock_dir", lambda: lock_dir), \
                 mock.patch.object(installer, "_pid_alive", lambda p: p in alive_pids):
                return installer._openclaw_gateway_pid()

    def test_reads_live_pid_from_lockfile(self):
        self.assertEqual(self._with_lock([4242], alive_pids={4242}), 4242)

    def test_stale_lock_dead_pid_reads_as_no_gateway(self):
        self.assertIsNone(self._with_lock([513], alive_pids=set()))

    def test_no_lockfiles_is_none(self):
        self.assertIsNone(self._with_lock([], alive_pids=set()))

    def test_ignores_dead_keeps_live(self):
        self.assertEqual(self._with_lock([513, 999], alive_pids={999}), 999)

    def test_kill_targets_lockfile_pid(self):
        killed = {"pid": None, "sig": None}

        def fake_kill(pid, sig):
            killed["pid"], killed["sig"] = pid, sig

        with mock.patch.object(installer, "_openclaw_gateway_pid", lambda: 513), \
             mock.patch.object(installer.os, "kill", side_effect=fake_kill), \
             mock.patch.object(installer.subprocess, "run"):
            returned = installer._kill_openclaw_gateway()
        self.assertEqual(returned, 513)
        self.assertEqual(killed["pid"], 513)
        self.assertEqual(killed["sig"], 9)


class TestVerifyGatewayRestarted(unittest.TestCase):
    """META: a restart we can't prove is a failure. _verify_gateway_restarted
    succeeds only when a NEW gateway pid (alive, != the killed one) appears."""

    def test_new_pid_differs_from_killed_is_success(self):
        with mock.patch.object(installer, "_openclaw_gateway_pid", lambda: 7777):
            self.assertTrue(installer._verify_gateway_restarted(513, timeout=1))

    def test_same_pid_means_no_restart_happened(self):
        # The pid never changed — the 'restart' did nothing. Must time out False.
        with mock.patch.object(installer, "_openclaw_gateway_pid", lambda: 513), \
             mock.patch.object(installer.time, "sleep", lambda *_: None):
            self.assertFalse(installer._verify_gateway_restarted(513, timeout=0.05))

    def test_no_gateway_ever_appears_is_failure(self):
        with mock.patch.object(installer, "_openclaw_gateway_pid", lambda: None), \
             mock.patch.object(installer.time, "sleep", lambda *_: None):
            self.assertFalse(installer._verify_gateway_restarted(513, timeout=0.05))

    def test_fresh_install_pre_pid_none_any_new_pid_passes(self):
        with mock.patch.object(installer, "_openclaw_gateway_pid", lambda: 4242):
            self.assertTrue(installer._verify_gateway_restarted(None, timeout=1))


class TestRestartGatewayForcesRealRestart(unittest.TestCase):
    """F3 + F4 + META together: the foreground path must KILL the old gateway by
    pid and VERIFY a NEW one came up — never short-circuit-reuse a serving
    gateway (the upgrade case where the old gateway runs the OLD code)."""

    def test_foreground_kills_old_then_verifies_new(self):
        calls = {"killed": False, "spawned": False}

        def fake_kill_gw():
            calls["killed"] = True
            return 513  # old gateway pid

        def fake_popen(*a, **k):
            calls["spawned"] = True
            return mock.MagicMock()

        with mock.patch.object(installer, "_kill_openclaw_gateway", side_effect=fake_kill_gw), \
             mock.patch.object(installer.subprocess, "Popen", side_effect=fake_popen), \
             mock.patch.object(installer.time, "sleep", lambda *_: None), \
             mock.patch.object(installer, "_verify_gateway_restarted", lambda pre, **k: pre == 513), \
             mock.patch("builtins.open", mock.mock_open()), \
             mock.patch.object(installer.shutil, "which", lambda n: "/usr/bin/openclaw"):
            ok = installer.restart_gateway("foreground")
        self.assertTrue(ok)
        self.assertTrue(calls["killed"], "old gateway must be killed first")
        self.assertTrue(calls["spawned"], "a new gateway must be spawned")

    def test_foreground_reports_failure_when_no_new_gateway(self):
        # Verification fails (no new pid) → restart_gateway must return False,
        # never a phantom success.
        with mock.patch.object(installer, "_kill_openclaw_gateway", lambda: 513), \
             mock.patch.object(installer.subprocess, "Popen", lambda *a, **k: mock.MagicMock()), \
             mock.patch.object(installer.time, "sleep", lambda *_: None), \
             mock.patch.object(installer, "_verify_gateway_restarted", lambda *a, **k: False), \
             mock.patch("builtins.open", mock.mock_open()), \
             mock.patch.object(installer.shutil, "which", lambda n: "/usr/bin/openclaw"):
            self.assertFalse(installer.restart_gateway("foreground"))

    def test_supervised_rc0_but_unverified_falls_through_to_foreground(self):
        # F2: `gateway restart` rc 0 that didn't actually restart must NOT be
        # trusted — it routes to foreground (which kills + verifies).
        fell_through = {"v": False}

        class _R:
            returncode = 0
            stdout = "Gateway restarted."
            stderr = ""

        def fake_restart(method):
            if method == "foreground":
                fell_through["v"] = True
                return True
            return orig_restart(method)

        orig_restart = installer.restart_gateway
        with mock.patch.object(installer.subprocess, "run", lambda *a, **k: _R()), \
             mock.patch.object(installer, "_openclaw_gateway_pid", lambda: 513), \
             mock.patch.object(installer, "_verify_gateway_restarted", lambda *a, **k: False), \
             mock.patch.object(installer.shutil, "which", lambda n: "/usr/bin/openclaw"), \
             mock.patch.object(installer, "restart_gateway", side_effect=fake_restart):
            # call the REAL function body for the supervised branch:
            result = orig_restart("openclaw-supervised")
        self.assertTrue(result)
        self.assertTrue(fell_through["v"], "unverified supervised restart must fall to foreground")


class TestHermesRestartVerify(unittest.TestCase):
    """Mirror of META into Hermes: every success path must confirm a gateway
    process is live again (kill mechanism unchanged — only verification added)."""

    def test_native_success_requires_live_gateway(self):
        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        with mock.patch.object(installer.subprocess, "run", lambda *a, **k: _R()), \
             mock.patch.object(installer, "_verify_hermes_gateway_back", lambda *a, **k: True):
            self.assertEqual(installer._hermes_restart_gateway("/opt/h/bin"), "native")

    def test_native_rc0_but_no_gateway_falls_back(self):
        # rc 0 but nothing came back → must NOT report 'native'; falls through to
        # the relaunch script. We make the relaunch fail so the result is None,
        # proving 'native' was rejected.
        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        verify_calls = {"n": 0}

        def fake_verify(*a, **k):
            verify_calls["n"] += 1
            return False  # gateway never comes back, on either path

        with mock.patch.object(installer.subprocess, "run", lambda *a, **k: _R()), \
             mock.patch.object(installer, "_verify_hermes_gateway_back", side_effect=fake_verify), \
             mock.patch.object(installer.Path, "write_text", lambda *a, **k: None), \
             mock.patch.object(installer.os, "chmod", lambda *a, **k: None), \
             mock.patch.object(installer.os, "unlink", lambda *a, **k: None):
            result = installer._hermes_restart_gateway("/opt/h/bin")
        self.assertIsNone(result)
        self.assertGreaterEqual(verify_calls["n"], 1, "native success path must verify")


if __name__ == "__main__":
    unittest.main()
