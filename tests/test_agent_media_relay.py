#!/usr/bin/env python3
"""Unit tests for the agent-mode media relay (D1): Hermes delivers the GIF + QR
as LOCAL-file `MEDIA:` directives (downloaded to /tmp), because Hermes' default
streaming reply path drops remote ![](url) images. OpenClaw keeps ![](url).

Stdlib-only (`python3 -m unittest`) to match the installer's no-deps ethos.

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
    spec = importlib.util.spec_from_file_location("installer_under_test_media", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


installer = _load_installer()

CODE = "403743"
CELEB = installer.CELEBRATION_GIF_URL
# The exact remote-markdown forms the OLD (broken-on-Hermes) path emitted. The
# celebration URL also appears inside a "don't do this" example in the relay
# block, so it is NOT a reliable absence signal — the QR registrar markdown is
# (it never appears in any example), so we key the "broken path gone" checks on
# it. A REAL downloaded media line starts "MEDIA:/tmp/chat4000-"; the HOW-TO
# instructional text uses the ellipsis form "MEDIA:/tmp/…", so the real-prefix
# substring cleanly distinguishes an actual directive from the explanation.
QR_REMOTE_MD = "![](https://registrar.chat4000.com/codes/403743/qr.png)"
GIF_REMOTE_MD = f"![]({CELEB})"
REAL_MEDIA_PREFIX = "MEDIA:/tmp/chat4000-"


def _capture_success(kind, *, download):
    """Run agent_success(kind=…) with _emit silenced, _download_to_tmp stubbed to
    `download` (a callable taking (url, prefix, ext)), and _agent_print captured.
    Returns the full printed relay block as one string."""
    captured = {"text": ""}
    with mock.patch.object(installer, "_emit", lambda *a, **k: None), \
         mock.patch.object(installer, "_download_to_tmp", side_effect=download), \
         mock.patch.object(installer, "_agent_print",
                           side_effect=lambda lines: captured.__setitem__("text", "\n".join(lines))):
        rc = installer.agent_success(kind, CODE, None, "/tmp/pair.log")
    return rc, captured["text"]


class TestHermesMediaRelay(unittest.TestCase):
    def test_hermes_uses_local_media_lines_when_download_succeeds(self):
        # Each download returns a deterministic local /tmp path.
        def dl(url, prefix, ext):
            return f"/tmp/{prefix}-deadbeef{ext}"

        rc, text = _capture_success("Hermes", download=dl)
        self.assertEqual(rc, 0)
        # Both assets ride as real MEDIA: local-file directives…
        self.assertIn("MEDIA:/tmp/chat4000-celebration-deadbeef.gif", text)
        self.assertIn("MEDIA:/tmp/chat4000-qr-deadbeef.png", text)
        # …and the QR is NOT delivered as the broken remote image-markdown.
        self.assertNotIn(QR_REMOTE_MD, text)

    def test_hermes_falls_back_to_url_markdown_when_download_fails(self):
        # Network down → _download_to_tmp returns None for both.
        rc, text = _capture_success("Hermes", download=lambda *a, **k: None)
        self.assertEqual(rc, 0)
        self.assertIn(GIF_REMOTE_MD, text)              # GIF falls back to URL md
        self.assertIn(QR_REMOTE_MD, text)               # QR falls back to URL md
        self.assertNotIn(REAL_MEDIA_PREFIX, text)       # no real local-file directive

    def test_hermes_downloads_both_assets(self):
        calls = []

        def dl(url, prefix, ext):
            calls.append((url, prefix, ext))
            return f"/tmp/{prefix}{ext}"

        _capture_success("Hermes", download=dl)
        urls = {c[0] for c in calls}
        self.assertIn(CELEB, urls)
        self.assertTrue(any("registrar" in u and u.endswith(".png") for u in urls),
                        f"QR png url not downloaded; got {urls}")


class TestOpenClawKeepsUrlMarkdown(unittest.TestCase):
    def test_openclaw_never_downloads_and_keeps_url_markdown(self):
        downloaded = {"v": False}

        def dl(*a, **k):
            downloaded["v"] = True
            return "/tmp/should-not-happen"

        rc, text = _capture_success("OpenClaw", download=dl)
        self.assertEqual(rc, 0)
        self.assertFalse(downloaded["v"], "OpenClaw must NOT download assets (uses remote ![](url))")
        self.assertIn(GIF_REMOTE_MD, text)
        self.assertIn(QR_REMOTE_MD, text)
        self.assertNotIn(REAL_MEDIA_PREFIX, text)


if __name__ == "__main__":
    unittest.main()
