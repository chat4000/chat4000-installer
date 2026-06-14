#!/usr/bin/env python3
"""Unit tests for the Hermes --no-pair UPGRADE gate in install_into_hermes.

A resident plugin's version-poller refreshes itself by re-running the installer
with --no-pair: it must install/refresh the plugin + restart the gateway (so the
new plugin code is loaded and running) WITHOUT spawning device pairing.

These tests stub the heavy side-effecting helpers (pip/uv install, prepare,
gateway restart, symlink, analytics) and drive install_into_hermes directly,
asserting on whether `chat4000 pair` is ever launched and whether the gateway is
restarted.

Stdlib-only (`python3 -m unittest`) to match the installer's no-deps ethos.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import importlib.util
import types
import unittest
from pathlib import Path
from unittest import mock


def _load_installer():
    """Import scripts/installer.py as a module without running main()."""
    here = Path(__file__).resolve().parent
    path = here.parent / "scripts" / "installer.py"
    spec = importlib.util.spec_from_file_location("installer_under_test_nopair", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


installer = _load_installer()


def _args(**overrides):
    """A minimal args namespace with the attributes install_into_hermes reads."""
    base = {
        "uninstall": False,
        "reset": False,
        "hermes_branch": None,
        "ref": "v1.0.0",
        "stage": False,
        "no_wizard": False,
        "no_pair": False,
        "pair_ttl": None,
        "reusable": False,
        "qr": False,
    }
    base.update(overrides)
    return types.SimpleNamespace(**base)


class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestHermesNoPairGate(unittest.TestCase):
    def _run(self, args):
        """Drive install_into_hermes with all heavy helpers stubbed.

        Returns (rc, pair_launched: bool, restart_called: bool).
        """
        t = {
            "venv_bin": "/opt/hermes/venv/bin",
            "venv_python": "/opt/hermes/venv/bin/python",
            "layout": "test-layout",
        }
        pair_launched = {"v": False}
        restart_called = {"v": False}

        def fake_subprocess_run(cmd, *a, **kw):
            # The import-check and version probes go through subprocess.run.
            if isinstance(cmd, list) and len(cmd) >= 2 and cmd[1] == "-c":
                src = cmd[2] if len(cmd) > 2 else ""
                if "package_info" in src:
                    return _FakeProc(0, stdout="9.9.9")
                return _FakeProc(0, stdout="chat4000_hermes_plugin")
            # `chat4000 pair` — the thing --no-pair must suppress.
            if isinstance(cmd, list) and cmd[:1] and cmd[0].endswith("/chat4000") and "pair" in cmd:
                pair_launched["v"] = True
                return _FakeProc(0)
            return _FakeProc(0)

        def fake_restart(venv_bin):
            restart_called["v"] = True
            return "native"

        with mock.patch.object(installer.subprocess, "run", side_effect=fake_subprocess_run), \
             mock.patch.object(installer, "detect_uv", return_value=None), \
             mock.patch.object(installer, "hermes_install_via_pip", return_value=None), \
             mock.patch.object(installer, "hermes_install_via_uv", return_value=None), \
             mock.patch.object(installer, "symlink_chat4000_onto_path", return_value=None), \
             mock.patch.object(installer, "_run_streaming", return_value=(0, "")), \
             mock.patch.object(installer, "_hermes_restart_gateway", side_effect=fake_restart), \
             mock.patch.object(installer, "_emit", return_value=None), \
             mock.patch.object(installer, "use_agent_distinct_id", return_value="x"):
            rc = installer.install_into_hermes(t, args, interactive=True)
        return rc, pair_launched["v"], restart_called["v"]

    def test_no_pair_skips_pairing_but_restarts_gateway(self):
        rc, pair_launched, restart_called = self._run(_args(no_pair=True))
        self.assertFalse(pair_launched, "--no-pair must NOT spawn `chat4000 pair`")
        self.assertTrue(restart_called, "--no-pair must still restart the gateway")
        self.assertEqual(rc, 0)

    def test_default_interactive_does_pair(self):
        rc, pair_launched, restart_called = self._run(_args(no_pair=False))
        self.assertTrue(pair_launched, "default interactive install must pair")
        self.assertTrue(restart_called)
        self.assertEqual(rc, 0)

    def test_non_interactive_short_circuits_before_pair(self):
        # Multiple-targets path: returns early, never pairs, never restarts here.
        t = {
            "venv_bin": "/opt/hermes/venv/bin",
            "venv_python": "/opt/hermes/venv/bin/python",
            "layout": "test-layout",
        }
        with mock.patch.object(installer.subprocess, "run", return_value=_FakeProc(0, stdout="chat4000_hermes_plugin")), \
             mock.patch.object(installer, "detect_uv", return_value=None), \
             mock.patch.object(installer, "hermes_install_via_pip", return_value=None), \
             mock.patch.object(installer, "symlink_chat4000_onto_path", return_value=None), \
             mock.patch.object(installer, "_emit", return_value=None), \
             mock.patch.object(installer, "use_agent_distinct_id", return_value="x"):
            rc = installer.install_into_hermes(t, _args(), interactive=False)
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
