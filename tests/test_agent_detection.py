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


class TestDetectRestartMethod(unittest.TestCase):
    """detect_restart_method is now docker-vs-not, nothing more: a container
    gateway lives in its own PID namespace (a host-side kill can't reach it) so it
    MUST go through `docker restart`; everything else collapses to one "local"
    kill-and-see path that needs no supervisor-type detection up front. The
    disabled-service handling that used to live here now lives inside
    restart_gateway's local path (see TestRestartGatewayLocalKillAndSee)."""

    def _detect(self, *, container_up, docker_on_path=True):
        class _R:
            returncode = 0
            stdout = "openclaw-gateway\n" if container_up else ""
            stderr = ""

        def fake_run(cmd, *a, **k):
            return _R()

        def fake_which(name):
            if name == "docker":
                return "/usr/bin/docker" if docker_on_path else None
            return "/usr/bin/openclaw"

        with mock.patch.object(installer.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(installer.shutil, "which", side_effect=fake_which):
            return installer.detect_restart_method()

    def test_running_container_is_docker(self):
        self.assertEqual(self._detect(container_up=True), "docker")

    def test_no_container_is_local(self):
        self.assertEqual(self._detect(container_up=False), "local")

    def test_no_docker_on_path_is_local(self):
        self.assertEqual(self._detect(container_up=False, docker_on_path=False), "local")

    def test_never_returns_none(self):
        # The local path always has a kill-and-see strategy, so detection never
        # comes up empty — callers can rely on a non-None method.
        self.assertIsNotNone(self._detect(container_up=False))


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


class TestRestartGatewayLocalKillAndSee(unittest.TestCase):
    """The single "local" kill-and-see path (mirrors the Hermes restart), driven
    step-by-step:
      1. graceful `openclaw gateway restart` that PROVES a new live pid → success;
      2. F1/F2: a disabled/no-op rc-0 restart is NOT trusted → fall through to kill;
      3/4. kill by lockfile pid, then SEE if a supervisor revives it → success;
      5. no revival → foreground start, then VERIFY a new pid (META).
    plus the no-pid-but-running loud-failure guard."""

    def _run(self, restart_out="Gateway restarted.", restart_rc=0):
        class _R:
            returncode = restart_rc
            stdout = restart_out
            stderr = ""
        return _R()

    def test_graceful_native_restart_with_new_pid_is_success(self):
        # Step 1: native restart, output is benign, verify sees a new pid → done.
        # We must NOT proceed to kill/spawn.
        killed = {"v": False}
        with mock.patch.object(installer, "_openclaw_gateway_pid", lambda: 513), \
             mock.patch.object(installer.subprocess, "run", lambda *a, **k: self._run()), \
             mock.patch.object(installer, "_verify_gateway_restarted", lambda pre, **k: True), \
             mock.patch.object(installer, "_kill_openclaw_gateway",
                               lambda: killed.__setitem__("v", True)), \
             mock.patch.object(installer.shutil, "which", lambda n: "/usr/bin/openclaw"):
            self.assertTrue(installer.restart_gateway("local"))
        self.assertFalse(killed["v"], "a verified native restart must not proceed to kill")

    def test_supervisor_revives_within_grace_is_success_without_spawn(self):
        # Step 4: after the kill, a NEW live pid (!= pre) appears within GRACE → a
        # supervisor revived it; we must succeed WITHOUT spawning a foreground one.
        pids = iter([513, 513, 9001])  # pre_pid, native-verify miss, then revived
        spawned = {"v": False}

        def fake_pid():
            try:
                return next(pids)
            except StopIteration:
                return 9001

        with mock.patch.object(installer, "_openclaw_gateway_pid", side_effect=fake_pid), \
             mock.patch.object(installer.subprocess, "run", lambda *a, **k: self._run("disabled")), \
             mock.patch.object(installer, "_kill_openclaw_gateway", lambda: 513), \
             mock.patch.object(installer.subprocess, "Popen",
                               lambda *a, **k: spawned.__setitem__("v", True)), \
             mock.patch.object(installer.time, "sleep", lambda *_: None), \
             mock.patch.object(installer.shutil, "which", lambda n: "/usr/bin/openclaw"):
            self.assertTrue(installer.restart_gateway("local"))
        self.assertFalse(spawned["v"], "supervisor revival must not trigger a foreground spawn")

    def test_no_supervisor_foreground_start_verified_is_success(self):
        # Step 5: native no-ops, kill, no revival within GRACE → spawn foreground,
        # then verify a NEW pid. Disabled output routes straight past native.
        spawned = {"v": False}
        with mock.patch.object(installer, "_openclaw_gateway_pid", lambda: 513), \
             mock.patch.object(installer.subprocess, "run",
                               lambda *a, **k: self._run("Service: systemd user (disabled)")), \
             mock.patch.object(installer, "_kill_openclaw_gateway", lambda: 513), \
             mock.patch.object(installer.subprocess, "Popen",
                               lambda *a, **k: spawned.__setitem__("v", True) or mock.MagicMock()), \
             mock.patch.object(installer, "_verify_gateway_restarted", lambda pre, **k: True), \
             mock.patch.object(installer.time, "sleep", lambda *_: None), \
             mock.patch("builtins.open", mock.mock_open()), \
             mock.patch.object(installer.shutil, "which", lambda n: "/usr/bin/openclaw"):
            self.assertTrue(installer.restart_gateway("local"))
        self.assertTrue(spawned["v"], "no revival must spawn a foreground gateway")

    def test_foreground_unverified_is_failure(self):
        # META: spawned but a new pid never appears → must return False, not a
        # phantom success.
        with mock.patch.object(installer, "_openclaw_gateway_pid", lambda: 513), \
             mock.patch.object(installer.subprocess, "run",
                               lambda *a, **k: self._run("(disabled)")), \
             mock.patch.object(installer, "_kill_openclaw_gateway", lambda: 513), \
             mock.patch.object(installer.subprocess, "Popen",
                               lambda *a, **k: mock.MagicMock()), \
             mock.patch.object(installer, "_verify_gateway_restarted", lambda *a, **k: False), \
             mock.patch.object(installer.time, "sleep", lambda *_: None), \
             mock.patch("builtins.open", mock.mock_open()), \
             mock.patch.object(installer.shutil, "which", lambda n: "/usr/bin/openclaw"):
            self.assertFalse(installer.restart_gateway("local"))

    def test_no_lockfile_pid_but_gateway_running_fails_loudly(self):
        # No authoritative pid AND a gateway is otherwise running (argv match): we
        # can't identify/kill/verify it by pid — must FAIL, never silently succeed.
        spawned = {"v": False}
        with mock.patch.object(installer, "_openclaw_gateway_pid", lambda: None), \
             mock.patch.object(installer.subprocess, "run",
                               lambda *a, **k: self._run("(disabled)")), \
             mock.patch.object(installer, "_openclaw_gateway_argv_alive", lambda: True), \
             mock.patch.object(installer.subprocess, "Popen",
                               lambda *a, **k: spawned.__setitem__("v", True)), \
             mock.patch.object(installer.shutil, "which", lambda n: "/usr/bin/openclaw"):
            self.assertFalse(installer.restart_gateway("local"))
        self.assertFalse(spawned["v"], "must not spawn when it can't anchor on a pid")

    def test_fresh_install_no_pid_no_running_starts_foreground(self):
        # Clean box: no pre pid, no argv match → proceed to foreground start and
        # verify the new gateway (pre_pid None means any new live pid passes).
        spawned = {"v": False}
        with mock.patch.object(installer, "_openclaw_gateway_pid", lambda: None), \
             mock.patch.object(installer.subprocess, "run",
                               lambda *a, **k: self._run("(disabled)")), \
             mock.patch.object(installer, "_openclaw_gateway_argv_alive", lambda: False), \
             mock.patch.object(installer, "_kill_openclaw_gateway", lambda: None), \
             mock.patch.object(installer.subprocess, "Popen",
                               lambda *a, **k: spawned.__setitem__("v", True) or mock.MagicMock()), \
             mock.patch.object(installer, "_verify_gateway_restarted", lambda pre, **k: True), \
             mock.patch.object(installer.time, "sleep", lambda *_: None), \
             mock.patch("builtins.open", mock.mock_open()), \
             mock.patch.object(installer.shutil, "which", lambda n: "/usr/bin/openclaw"):
            self.assertTrue(installer.restart_gateway("local"))
        self.assertTrue(spawned["v"], "a clean box must start a foreground gateway")


class TestRestartGatewayDocker(unittest.TestCase):
    """The docker branch MUST go through `docker restart` (container PID namespace
    is unreachable from a host kill) AND verify a live gateway came back — rc 0
    alone is NOT proof (the bug this fixes)."""

    def test_docker_restart_verified_is_success(self):
        with mock.patch.object(installer.shutil, "which", lambda n: "/usr/bin/" + n), \
             mock.patch.object(installer.subprocess, "run",
                               lambda *a, **k: mock.MagicMock(returncode=0, stderr="", stdout="")), \
             mock.patch.object(installer, "_verify_docker_gateway_restarted", lambda *a, **k: True):
            self.assertTrue(installer.restart_gateway("docker"))

    def test_docker_restart_rc0_but_no_gateway_is_failure(self):
        # The old bug: returned True on rc 0 with no proof. Now it must fail.
        with mock.patch.object(installer.shutil, "which", lambda n: "/usr/bin/" + n), \
             mock.patch.object(installer.subprocess, "run",
                               lambda *a, **k: mock.MagicMock(returncode=0, stderr="", stdout="")), \
             mock.patch.object(installer, "_verify_docker_gateway_restarted", lambda *a, **k: False):
            self.assertFalse(installer.restart_gateway("docker"))

    def test_docker_restart_nonzero_is_failure(self):
        with mock.patch.object(installer.shutil, "which", lambda n: "/usr/bin/" + n), \
             mock.patch.object(installer.subprocess, "run",
                               lambda *a, **k: mock.MagicMock(returncode=1, stderr="boom", stdout="")):
            self.assertFalse(installer.restart_gateway("docker"))


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
