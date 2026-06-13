#!/usr/bin/env python3
"""Unit tests for installer agent-mode detection (BUG 1).

Stdlib-only (`python3 -m unittest`) to match the installer's no-deps ethos
(pytest is not installed and the installer itself imports only stdlib).

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path


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


class TestOpenclawGatewayServing(unittest.TestCase):
    """BUG 3: reuse an already-serving gateway instead of a false health-fail."""

    def test_lost_port_detects_address_in_use(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            f.write("...\nError: listen EADDRINUSE: address already in use :::18789\n")
            path = f.name
        try:
            self.assertTrue(installer._foreground_gateway_lost_port(path))
        finally:
            os.unlink(path)

    def test_lost_port_false_on_clean_log(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            f.write("[gateway] ready\nstarting channels and sidecars\n")
            path = f.name
        try:
            self.assertFalse(installer._foreground_gateway_lost_port(path))
        finally:
            os.unlink(path)

    def test_lost_port_false_on_missing_log(self):
        self.assertFalse(installer._foreground_gateway_lost_port("/nonexistent/x.log"))

    def _run_serving_probe(self, *, status_rc, status_out, hello_ok):
        """Drive _openclaw_gateway_serving_chat4000 with mocked status + log."""
        import tempfile

        orig_run = installer.subprocess.run
        orig_home = installer._openclaw_home

        class _R:
            returncode = status_rc
            stdout = status_out
            stderr = ""

        def fake_run(cmd, *a, **k):
            if cmd[1:] == ["gateway", "status"]:
                return _R()
            return orig_run(cmd, *a, **k)

        with tempfile.TemporaryDirectory() as td:
            logdir = Path(td) / "plugins" / "chat4000" / "logs"
            logdir.mkdir(parents=True)
            if hello_ok is not None:
                (logdir / "runtime.log").write_text(
                    "runtime.hello_ok\n" if hello_ok else "still connecting\n"
                )
            installer.subprocess.run = fake_run
            installer._openclaw_home = lambda: Path(td)
            try:
                return installer._openclaw_gateway_serving_chat4000()
            finally:
                installer.subprocess.run = orig_run
                installer._openclaw_home = orig_home

    def test_serving_true_when_status_ok_and_hello_ok(self):
        self.assertTrue(
            self._run_serving_probe(status_rc=0, status_out="gateway connected", hello_ok=True)
        )

    def test_serving_false_when_status_ok_but_no_hello_ok(self):
        # Gateway up but chat4000 not loaded yet (fresh install) → must restart.
        self.assertFalse(
            self._run_serving_probe(status_rc=0, status_out="gateway connected", hello_ok=False)
        )

    def test_serving_false_when_status_disabled(self):
        self.assertFalse(
            self._run_serving_probe(status_rc=0, status_out="service disabled", hello_ok=True)
        )

    def test_serving_false_when_status_nonzero(self):
        self.assertFalse(
            self._run_serving_probe(status_rc=1, status_out="", hello_ok=True)
        )

    def test_serving_false_when_no_runtime_log(self):
        self.assertFalse(
            self._run_serving_probe(status_rc=0, status_out="gateway connected", hello_ok=None)
        )


if __name__ == "__main__":
    unittest.main()
