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


if __name__ == "__main__":
    unittest.main()
