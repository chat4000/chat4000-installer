#!/usr/bin/env python3
"""installer.py — ONE installer for the chat4000 plugin across BOTH agent hosts.

It scans the system for every Hermes (Python agent) and OpenClaw (Node agent)
instance, reports what it found, lets you choose where to install when there's
more than one, then installs the plugin with the right toolchain for that host:

  • Hermes   → pip/uv git-install from the gh `stable` tag into Hermes' venv,
               then `chat4000 wizard`
  • OpenClaw → `openclaw plugins install github:chat4000/chat4000-openclaw-plugin#stable`
               (gh tag, NOT the npm registry), then
               `openclaw chat4000 setup --self-redeem` + gateway restart

Runs in the system Python (stdlib only) — no third-party deps, because the
`rich`/SDK code only exists AFTER the plugin is installed.

────────────────────────────────────────────────────────────────────────
A note about the telemetry in this file, from us at chat4000:

We send anonymous events to PostHog (product analytics) and Sentry (uncaught
crashes) FROM THE INSTALLER ITSELF, so we can see what % of installs succeed,
which step fails most, and get a real stack trace when it crashes.

Things we NEVER send: your message content, prompts, command arguments, env
vars, pairing codes, group keys, usernames, or your file paths (home/username
path segments are scrubbed before send).

What WE send is bounded to: which install step ran/failed + the error class;
python + agent version, OS platform; the merged-environment scan described
below; and an anonymous UUID (~/.config/chat4000/install-id).

The merged scan additionally reports, per detected agent (so we can size the
installed base and prioritise the right host): the agent's install DATE, the
names + COUNT of channels/plugins it has, and how many SESSIONS live on it.
These are counts and public package names — never content.

Opt out any of three ways:
  • CHAT4000_TELEMETRY_DISABLED=1 in your env
  • pass --no-telemetry on the curl|bash line
  • after install: `chat4000 telemetry disable` (Hermes) /
                   `openclaw chat4000 telemetry disable` (OpenClaw)

Privacy policy: https://chat4000.com/privacy
Love, chat4000 ❤️
────────────────────────────────────────────────────────────────────────

Every event carries a `mode` prop ("agent" when run with --agent, else "human");
Sentry events carry a matching `mode` tag.

PostHog events fired by this file (routed to the matching host's project):
  - installer_agent_invoked              (only in --agent mode; dedicated marker)
  - installer_started                    {selected_kind?}
  - installer_environment_scan           {hermes_count, openclaw_count, total}  (→ both)
  - installer_agent_detected             {kind, agent_version, install_date,
                                          age_days, channels, channel_count,
                                          session_count, agent_count,
                                          plugin_installed, plugin_version}
  - installer_hermes_detected            {hermes_layout, hermes_path}
  - installer_openclaw_detected          {openclaw_version, openclaw_path}
  - installer_pkg_installed              {...}
  - installer_gateway_restarted          {method}            (openclaw)
  - installer_handing_off_to_wizard      (hermes)
  - installer_handing_off_to_setup       {paired}            (openclaw)
  - installer_succeeded / installer_failed / installer_cancelled / installer_crashed
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import random
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Optional

# Line-buffer stdout/stderr so subprocess output stays interleaved in order
# when piped through `docker exec` / `ssh`.
with contextlib.suppress(Exception):
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    sys.stderr.reconfigure(line_buffering=True)  # type: ignore[attr-defined]

# ─── Constants ────────────────────────────────────────────────────────────

# Both plugins install from their GitHub repo's `stable` tag (NOT from a package
# registry — no PyPI, no npm registry). `--ref` overrides this tag for both
# hosts; `--latest` is shorthand for the repo's default branch (newest code).
DEFAULT_REF = "stable"
LATEST_REF = "main"

# Hermes — Python plugin, git-installed into Hermes' venv from the gh tag.
HERMES_REPO_URL = "https://github.com/chat4000/chat4000-hermes-plugin"
# Prebuilt Matrix-E2EE wheels (not on PyPI); pip resolves the chat4000-pyvodozemac
# hard dep from here, per platform, so users never need a Rust toolchain.
PYVODOZEMAC_FIND_LINKS = (
    "https://github.com/chat4000/chat4000-pyvodozemac/releases/expanded_assets/v0.1.0"
)
HERMES_PKG = "chat4000-hermes-plugin"

# OpenClaw — Node plugin, installed from the gh tag via an npm git spec
# (github:owner/repo#ref). The repo ships TS source with no build-on-install
# step, so a git install delivers the same bytes as the registry tarball.
# OPENCLAW_PKG is the package *identity* (its package.json name) — still used to
# uninstall and to talk to the `openclaw chat4000` subcommands once installed.
OPENCLAW_REPO_SLUG = "chat4000/chat4000-openclaw-plugin"
OPENCLAW_PKG = "@chat4000/openclaw-plugin"


def openclaw_gh_spec(ref: str) -> str:
    """npm git spec for the OpenClaw plugin at a GitHub ref (tag/branch/SHA)."""
    return f"github:{OPENCLAW_REPO_SLUG}#{ref}"

# Public PostHog credentials — one project per host (same projects the plugins
# + the iOS/Mac apps use), so each host's install funnel stays correlated with
# that host's runtime events. Public keys; safe to embed.
POSTHOG = {
    "hermes": {
        "key": "phc_s49DnTamyFDnEC6MyumNmmjjf7p455LXCVzPE94hPemZ",
        "url": "https://us.i.posthog.com/capture/",
    },
    "openclaw": {
        "key": "phc_wNRtzk3h5FTw2X6h4CvieEoxdSdqUd42eUqbgW6nD7B4",
        "url": "https://posthog.chat4000.com/capture/",
    },
}

# Sentry DSNs — one per host, matching each plugin's runtime telemetry project.
# Public-by-design (write-only ingestion endpoints, not secrets).
SENTRY_DSN = {
    "hermes": "https://ac3dabffdf2c91c9c90a87cd9b258908@o4511305222193152.ingest.us.sentry.io/4511433133129728",
    "openclaw": "https://ca71dd0ea0a2740ec9ced9774c780197@o4511305222193152.ingest.us.sentry.io/4511305367289856",
}
INSTALLER_RELEASE = "chat4000-installer@1.0.0"
INSTALLER_VERSION = "1.0.0"

_STARTED_AT_MS = int(time.time() * 1000)

# ─── ANSI ─────────────────────────────────────────────────────────────────

if sys.stdout.isatty():
    C_RED = "\033[1;31m"
    C_GRN = "\033[1;32m"
    C_YEL = "\033[1;33m"
    C_BLU = "\033[1;34m"
    C_MAG = "\033[1;35m"
    C_CYN = "\033[1;36m"
    C_DIM = "\033[2m"
    C_BOLD = "\033[1m"
    C_RST = "\033[0m"
else:
    C_RED = C_GRN = C_YEL = C_BLU = C_MAG = C_CYN = C_DIM = C_BOLD = C_RST = ""


# Agent mode: terse, machine-addressed output for when an AGENT (not a human)
# runs the installer — e.g. an OpenClaw/Hermes agent driving it over Telegram.
# When on, the human-pretty helpers below are SILENCED and everything the caller
# sees goes through agent_success() / agent_error(). Set in main().
_AGENT_MODE = False


def say(msg: str) -> None:
    if _AGENT_MODE:
        return
    print(f"{C_CYN}>{C_RST} {msg}")


def ok(msg: str) -> None:
    if _AGENT_MODE:
        return
    print(f"{C_GRN}✓{C_RST} {msg}")


def warn(msg: str) -> None:
    if _AGENT_MODE:
        return
    print(f"{C_YEL}⚠{C_RST} {msg}")


def err(msg: str) -> None:
    if _AGENT_MODE:
        return
    print(f"{C_RED}✗{C_RST} {msg}", file=sys.stderr)


def hdr(msg: str) -> None:
    if _AGENT_MODE:
        return
    line = "━" * 63
    print(f"\n{C_MAG}{line}{C_RST}\n{C_MAG}{C_BOLD}{msg}{C_RST}\n{C_MAG}{line}{C_RST}\n")


def banner() -> None:
    if _AGENT_MODE:
        return
    print(f"\n{C_MAG}┌─────────────────────────────────────────────────────────────┐{C_RST}")
    print(
        f"{C_MAG}│{C_RST}  {C_MAG}{C_BOLD}🔐 chat4000{C_RST}  ·  {C_BLU}{C_BOLD}plugin installer{C_RST}  {C_DIM}(Hermes + OpenClaw){C_RST}            {C_MAG}│{C_RST}"  # noqa: E501
    )
    print(
        f"{C_MAG}│{C_RST}  {C_DIM}Native iPhone / Mac / CLI app for your coding agent{C_RST}        {C_MAG}│{C_RST}"  # noqa: E501
    )
    print(f"{C_MAG}└─────────────────────────────────────────────────────────────┘{C_RST}\n")


# ─── install_id (matches what each plugin reuses later) ───────────────────


def resolve_install_id() -> str:
    cfg = Path.home() / ".config" / "chat4000"
    path = cfg / "install-id"
    try:
        if path.exists():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        new_id = str(uuid.uuid4())
        cfg.mkdir(parents=True, exist_ok=True)
        path.write_text(new_id + "\n", encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
        return new_id
    except OSError:
        # Read-only / sandboxed fs — fall back to a process-local anonymous id.
        return str(uuid.uuid4())


# ─── Scrubbing ────────────────────────────────────────────────────────────


def _scrub_path(s):
    if not isinstance(s, str):
        return s
    home = str(Path.home())
    if home and home in s:
        s = s.replace(home, "~")
    return re.sub(r"/(Users|home)/[^/]+", r"/\1/<user>", s)


def _scrub_props_value(v):
    """Recursively scrub username/home paths out of a telemetry prop value."""
    if isinstance(v, str):
        return _scrub_path(v)
    if isinstance(v, list):
        return [_scrub_props_value(x) for x in v]
    if isinstance(v, dict):
        return {k: _scrub_props_value(x) for k, x in v.items()}
    return v


def _scrub_secrets(s: str) -> str:
    if not isinstance(s, str):
        return s
    s = re.sub(r"sk-[A-Za-z0-9]{20,}", "[REDACTED_API_KEY]", s)
    s = re.sub(r"phc_[A-Za-z0-9]{30,}", "[REDACTED_POSTHOG_KEY]", s)
    s = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._-]+", "Bearer [REDACTED]", s)
    return s


# ─── PostHog (stdlib HTTPS, no SDK) ───────────────────────────────────────

_SESSION_ID = str(uuid.uuid4())
_TELEMETRY_DISABLED = (
    os.environ.get("CHAT4000_TELEMETRY_DISABLED", "").strip().lower() in ("1", "true", "yes")
    or "--no-telemetry" in sys.argv
)


def _base_props() -> dict:
    enriched = {
        "source": "chat4000-installer",
        # Stamp EVERY event with how the installer was invoked, so agent-driven
        # installs (--agent) are cleanly separable from human ones in analytics.
        "mode": "agent" if _AGENT_MODE else "human",
        "installer_version": INSTALLER_VERSION,
        "python_version": (
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        ),
        "os_platform": sys.platform,
        "session_id": _SESSION_ID,
        "arch": platform.machine() or "unknown",
        "cpu_count": os.cpu_count() or 0,
        "locale": (os.environ.get("LANG") or "").split(".")[0] or "unknown",
        "since_start_ms": int(time.time() * 1000) - _STARTED_AT_MS,
        "is_root": hasattr(os, "geteuid") and os.geteuid() == 0,
    }
    try:
        sysname = platform.system()
        if sysname == "Linux":
            os_rel = f"Linux {platform.release()}"
            try:
                for line in Path("/etc/os-release").read_text(errors="ignore").splitlines():
                    if line.startswith("PRETTY_NAME="):
                        os_rel = line.split("=", 1)[1].strip().strip('"')
                        break
            except OSError:
                pass
            enriched["os_release"] = os_rel
        elif sysname == "Darwin":
            mv = platform.mac_ver()[0]
            enriched["os_release"] = f"macOS {mv}" if mv else f"Darwin {platform.release()}"
        elif sysname == "Windows":
            wv = platform.win32_ver()[0]
            enriched["os_release"] = f"Windows {wv}" if wv else "Windows"
        else:
            enriched["os_release"] = f"{sysname} {platform.release()}".strip()
    except (OSError, ValueError, IndexError):
        enriched["os_release"] = "unknown"
    try:
        in_container = False
        if Path("/.dockerenv").exists() or os.environ.get("KUBERNETES_SERVICE_HOST"):
            in_container = True
        else:
            cgroup = Path("/proc/1/cgroup").read_text(errors="ignore")
            in_container = any(s in cgroup for s in ("docker", "kubepods", "containerd", "podman"))
        enriched["is_container"] = in_container
    except OSError:
        enriched["is_container"] = False
    # Redacted, scrubbed argv.
    argv_out, skip_next = [], False
    for a in sys.argv[1:]:
        if skip_next:
            argv_out.append("<redacted>")
            skip_next = False
            continue
        if "=" in a:
            k = a.partition("=")[0]
            if any(s in k.lower() for s in ("token", "key", "secret", "pass", "dsn")):
                argv_out.append(f"{k}=<redacted>")
                continue
        if a.startswith(("sk-", "phc_", "ghp_", "Bearer")):
            argv_out.append("<redacted>")
            continue
        if a in ("--token", "--api-key", "--secret", "--password", "--dsn", "--service-token"):
            argv_out.append(a)
            skip_next = True
            continue
        argv_out.append(a)
    enriched["flags"] = argv_out
    return enriched


def _post_posthog(dest: str, event: str, props: dict) -> None:
    cred = POSTHOG.get(dest)
    if not cred:
        return
    body = json.dumps(
        {
            "api_key": cred["key"],
            "event": event,
            "distinct_id": resolve_install_id(),
            "properties": props,
        }
    ).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310  # our own PostHog https ingestion endpoint
        cred["url"],
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # Best-effort: a network failure must never break the install.
    with contextlib.suppress(urllib.error.URLError, OSError, TimeoutError, ValueError):
        urllib.request.urlopen(req, timeout=3).read()  # noqa: S310


def _emit(event: str, props: Optional[dict] = None, dest: str = "both") -> None:
    """Fire a PostHog event to one host's project ("hermes"/"openclaw") or
    "both". Best-effort, never raises, scrubs home/username paths from props."""
    if _TELEMETRY_DISABLED:
        return
    enriched = _base_props()
    if props:
        enriched.update(props)
    enriched = {k: _scrub_props_value(v) for k, v in enriched.items()}
    targets = ("hermes", "openclaw") if dest == "both" else (dest,)
    for t in targets:
        _post_posthog(t, event, enriched)


# ─── Sentry (stdlib envelope POST, no SDK) ────────────────────────────────


def send_sentry_envelope(exc: BaseException, *, kind: str = "both", tags: Optional[dict] = None) -> None:
    """Post a Sentry envelope describing `exc` to one host's DSN (or both).
    Stdlib only; best-effort; strips home paths + obvious secrets first."""
    if _TELEMETRY_DISABLED:
        return
    kinds = ("hermes", "openclaw") if kind == "both" else (kind,)
    for k in kinds:
        _send_sentry_one(SENTRY_DSN.get(k, ""), exc, kind=k, tags=tags)


def _send_sentry_one(dsn: str, exc: BaseException, *, kind: str, tags: Optional[dict]) -> None:
    if not dsn:
        return
    try:
        import datetime
        from urllib.parse import urlparse

        parsed = urlparse(dsn)
        public_key = parsed.username or ""
        project_id = (parsed.path or "").lstrip("/")
        if not public_key or not project_id or not parsed.hostname:
            return
        envelope_url = f"{parsed.scheme}://{parsed.hostname}/api/{project_id}/envelope/"

        frames = []
        tb = exc.__traceback__
        while tb is not None:
            co = tb.tb_frame.f_code
            frames.append(
                {
                    "filename": _scrub_path(co.co_filename),
                    "function": co.co_name,
                    "lineno": tb.tb_lineno,
                    "module": co.co_name,
                    "in_app": "installer.py" in co.co_filename,
                }
            )
            tb = tb.tb_next

        event = {
            "event_id": uuid.uuid4().hex,
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
            "platform": "python",
            "level": "error",
            "release": INSTALLER_RELEASE,
            "environment": os.environ.get("CHAT4000_ENV") or os.environ.get("HERMES_ENV") or "production",
            "tags": {
                "installer": "merged",
                "mode": "agent" if _AGENT_MODE else "human",
                "host_kind": kind,
                "python_version": (
                    f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
                ),
                "os_platform": sys.platform,
                **(tags or {}),
            },
            "exception": {
                "values": [
                    {
                        "type": type(exc).__name__,
                        "value": _scrub_secrets(str(exc))[:500],
                        "stacktrace": {"frames": frames},
                    }
                ]
            },
            "user": {"id": resolve_install_id()},
            "sdk": {"name": "chat4000-installer", "version": INSTALLER_VERSION},
        }

        envelope_header = json.dumps({"dsn": dsn, "event_id": event["event_id"]})
        item_header = json.dumps({"type": "event"})
        item_payload = json.dumps(event)
        body = (envelope_header + "\n" + item_header + "\n" + item_payload + "\n").encode("utf-8")

        req = urllib.request.Request(  # noqa: S310  # our own Sentry https ingestion endpoint
            envelope_url,
            data=body,
            headers={
                "Content-Type": "application/x-sentry-envelope",
                "X-Sentry-Auth": (
                    f"Sentry sentry_version=7, sentry_key={public_key}, "
                    f"sentry_client=chat4000-installer/1.0"
                ),
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5).read()  # noqa: S310
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        # This IS the crash-reporting path — must never raise back into _entry().
        pass


# ─── Detection: Hermes ────────────────────────────────────────────────────

HERMES_LAYOUTS = [
    ("~/.hermes/hermes-agent/venv/bin", "curl-installer"),
    ("/usr/local/lib/hermes-agent/venv/bin", "fhs-source"),
    ("/opt/hermes/.venv/bin", "docker"),
    ("/opt/homebrew/Cellar/hermes-agent/*/libexec/bin", "homebrew-arm64"),
    ("/usr/local/Cellar/hermes-agent/*/libexec/bin", "homebrew-intel"),
    ("/home/linuxbrew/.linuxbrew/Cellar/hermes-agent/*/libexec/bin", "linuxbrew"),
    ("~/.local/share/pipx/venvs/hermes-agent/bin", "pipx-modern"),
    ("~/.local/pipx/venvs/hermes-agent/bin", "pipx-legacy"),
    ("~/.local/share/uv/tools/hermes-agent/bin", "uv-tool"),
    ("/opt/venvs/hermes-agent/bin", "dh-virtualenv"),
    ("/usr/share/hermes-agent/venv/bin", "deb-alt"),
    ("/usr/lib/hermes-agent/venv/bin", "rpm"),
    ("/usr/libexec/hermes-agent/venv/bin", "rpm-libexec"),
    ("/opt/hermes-agent/venv/bin", "rpm-opt"),
    ("~/.local/lib/hermes-agent/venv/bin", "user-prefix"),
    ("~/.local/share/hermes-agent/venv/bin", "xdg-data"),
    ("~/Library/Application Support/Hermes Agent/venv/bin", "macos-app-support"),
]


def _layout_label(path: str) -> str:
    if "/nix/store/" in path:
        return "nix"
    for pattern, label in HERMES_LAYOUTS:
        expanded = str(Path(pattern).expanduser())
        if "*" in expanded:
            rx = re.escape(expanded).replace(r"\*", "[^/]+")
            if re.fullmatch(rx, path):
                return label
        elif path == expanded:
            return label
    return "unknown"


def detect_hermes_all() -> list:
    """Return a list of (venv_bin_path, layout_label) for EVERY Hermes venv we
    can find — env overrides, `hermes` on PATH, and all known layouts (glob-aware
    for Homebrew Cellar). Deduplicated by resolved path, ordered by priority."""
    found: list = []
    seen: set = set()

    def add(bin_path: str, label: str) -> None:
        try:
            key = str(Path(bin_path).resolve())
        except OSError:
            key = bin_path
        if key in seen:
            return
        seen.add(key)
        found.append((bin_path, label))

    # 1. Env-var overrides (project-owned). Highest priority.
    install_dir = (os.environ.get("HERMES_INSTALL_DIR") or "").strip()
    if install_dir:
        p = str(Path(install_dir).expanduser() / "venv" / "bin")
        if Path(f"{p}/python").exists():
            add(p, "env:HERMES_INSTALL_DIR")
    hermes_home = (os.environ.get("HERMES_HOME") or "").strip()
    if hermes_home:
        p = str(Path(hermes_home).expanduser() / "hermes-agent" / "venv" / "bin")
        if Path(f"{p}/python").exists():
            add(p, "env:HERMES_HOME")
    venv = (os.environ.get("VIRTUAL_ENV") or "").strip()
    if venv:
        p = str(Path(venv).expanduser() / "bin")
        if Path(f"{p}/hermes").exists() and Path(f"{p}/python").exists():
            add(p, "env:VIRTUAL_ENV")

    # 2. `hermes` on PATH — wrapper-grep, then resolve as symlink.
    hermes_cmd = shutil.which("hermes")
    if hermes_cmd:
        try:
            content = Path(hermes_cmd).read_text(errors="ignore")
            for pat in (
                r"/[^\"'\s]+/\.?venv/bin",
                r"/nix/store/[^\"'\s]+-hermes-agent-env-[^/]+/bin",
            ):
                m = re.search(pat, content)
                if m:
                    bin_path = m.group(0)
                    if Path(f"{bin_path}/python").exists() or Path(f"{bin_path}/hermes").exists():
                        add(bin_path, _layout_label(bin_path))
        except OSError:
            pass
        try:
            real = Path(hermes_cmd).resolve()
            bin_path = str(real.parent)
            if Path(f"{bin_path}/python").exists():
                add(bin_path, _layout_label(bin_path))
        except OSError:
            pass

    # 3. Known layouts with glob support (collect ALL matches).
    for pattern, label in HERMES_LAYOUTS:
        expanded = str(Path(pattern).expanduser())
        if "*" in expanded:
            try:
                matches = sorted(Path("/").glob(expanded.lstrip("/")))
                for match in reversed(matches):
                    if (match / "python").exists():
                        add(str(match), label)
            except OSError:
                continue
        else:
            if (Path(expanded) / "python").exists():
                add(expanded, label)
    return found


# ─── Detection: OpenClaw ──────────────────────────────────────────────────

OPENCLAW_LOCATIONS = [
    "/usr/local/bin/openclaw",
    "/opt/homebrew/bin/openclaw",
    "/home/linuxbrew/.linuxbrew/bin/openclaw",
    "~/.openclaw/bin/openclaw",
    "~/.local/bin/openclaw",
    "~/.npm-global/bin/openclaw",
    "/usr/bin/openclaw",
    "/Applications/OpenClaw.app/Contents/Resources/cli/openclaw",
    "~/.nvm/versions/node/*/bin/openclaw",
    "~/.nix-profile/bin/openclaw",
    "/run/current-system/sw/bin/openclaw",
]


def _openclaw_version(path: str) -> str:
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10)
        blob = (out.stdout or out.stderr).strip()
        line = blob.splitlines()[0] if blob else "unknown"
        m = re.search(r"\b(\d+\.\d+\.\d+\S*)", line)
        return m.group(1) if m else line
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def detect_openclaw_all() -> list:
    """Return a list of (openclaw_path, version) for EVERY openclaw binary we
    can find. Deduplicated by resolved path, ordered by priority."""
    found: list = []
    seen: set = set()

    def add(path: str) -> None:
        try:
            key = str(Path(path).resolve())
        except OSError:
            key = path
        if key in seen:
            return
        seen.add(key)
        found.append((path, _openclaw_version(path)))

    which = shutil.which("openclaw")
    if which:
        add(which)
    for pattern in OPENCLAW_LOCATIONS:
        expanded = str(Path(pattern).expanduser())
        if "*" in expanded:
            try:
                for match in sorted(Path("/").glob(expanded.lstrip("/")), reverse=True):
                    if match.exists() and os.access(match, os.X_OK):
                        add(str(match))
            except OSError:
                continue
        else:
            if Path(expanded).exists() and os.access(expanded, os.X_OK):
                add(expanded)
    return found


def detect_uv() -> Optional[str]:
    p = shutil.which("uv")
    if p:
        return p
    for cand in (
        Path.home() / ".local" / "bin" / "uv",
        Path("/usr/local/bin/uv"),
        Path("/opt/homebrew/bin/uv"),
    ):
        if cand.exists() and os.access(cand, os.X_OK):
            return str(cand)
    return None


# ─── Stats: install date / channels / sessions (the new analytics) ────────


def _install_date(p: Path) -> tuple:
    """(iso_date|None, age_days|None) for a path — best-effort. Prefers birth
    time (macOS) else the earlier of ctime/mtime."""
    try:
        st = p.stat()
    except OSError:
        return (None, None)
    ts = getattr(st, "st_birthtime", None)
    if not ts:
        ts = min(st.st_ctime, st.st_mtime)
    try:
        import datetime

        iso = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except (OSError, ValueError, OverflowError):
        iso = None
    age_days = max(0, int((time.time() - ts) / 86400)) if ts else None
    return (iso, age_days)


def _count_json_entries(text: str) -> Optional[int]:
    """Count session-like entries in a sessions store JSON blob. Handles a top
    array, a `{sessions|entries|items: [...|{}]}` wrapper, or a direct id→entry
    map (excluding obvious metadata keys)."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        for key in ("sessions", "entries", "items"):
            v = data.get(key)
            if isinstance(v, list):
                return len(v)
            if isinstance(v, dict):
                return len(v)
        meta = {"version", "updatedat", "schema", "schemaversion"}
        keys = [k for k in data.keys() if str(k).lower() not in meta]
        return len(keys)
    return None


def _openclaw_home() -> Path:
    explicit = (os.environ.get("OPENCLAW_STATE_DIR") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    home = (os.environ.get("OPENCLAW_HOME") or "").strip()
    base = Path(home).expanduser() if home else Path.home()
    return base / ".openclaw"


def collect_openclaw_stats(openclaw_path: str) -> dict:
    """install date + channels/plugins + session/agent counts for an OpenClaw."""
    stats: dict = {
        "install_date": None,
        "age_days": None,
        "channels": [],
        "channel_count": None,
        "session_count": None,
        "agent_count": None,
        "plugin_installed": False,
        "plugin_version": None,
    }
    # install date — stat the (resolved) binary.
    try:
        stats["install_date"], stats["age_days"] = _install_date(Path(openclaw_path).resolve())
    except OSError:
        pass

    home = _openclaw_home()

    # Sessions: ~/.openclaw/agents/<id>/sessions/sessions.json (per agent).
    try:
        agents_dir = home / "agents"
        if agents_dir.is_dir():
            agents = [d for d in agents_dir.iterdir() if d.is_dir()]
            stats["agent_count"] = len(agents)
            total = 0
            counted = False
            for a in agents:
                sj = a / "sessions" / "sessions.json"
                if sj.is_file():
                    n = _count_json_entries(sj.read_text(errors="ignore"))
                    if n is not None:
                        total += n
                        counted = True
            if counted:
                stats["session_count"] = total
    except OSError:
        pass

    # Channels / plugins: prefer the CLI, fall back to the plugins dir.
    names = _openclaw_plugins_via_cli(openclaw_path)
    if names is None:
        names = _openclaw_plugins_via_dir(home)
    if names is not None:
        stats["channels"] = names[:50]
        stats["channel_count"] = len(names)

    # Is our plugin already there + at which version?
    installed, cur, _latest, _newer = detect_plugin_state(openclaw_path)
    stats["plugin_installed"] = bool(installed)
    stats["plugin_version"] = cur
    return stats


def _openclaw_plugins_via_cli(openclaw_path: str) -> Optional[list]:
    for cmd in (
        [openclaw_path, "plugins", "list", "--json"],
        [openclaw_path, "plugins", "list"],
    ):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        out = (r.stdout or "").strip()
        if r.returncode != 0 or not out:
            continue
        # JSON form?
        start = out.find("[")
        obj_start = out.find("{")
        with contextlib.suppress(ValueError, TypeError):
            if start >= 0:
                data = json.loads(out[start:])
                names = [_plugin_name(x) for x in data]
                names = [n for n in names if n]
                if names:
                    return names
            elif obj_start >= 0:
                data = json.loads(out[obj_start:])
                if isinstance(data, dict):
                    plugins = data.get("plugins", data)
                    if isinstance(plugins, dict):
                        return list(plugins.keys())
                    if isinstance(plugins, list):
                        names = [n for n in (_plugin_name(x) for x in plugins) if n]
                        if names:
                            return names
        # Plain text: one plugin per line, strip bullets/status decorations.
        names = []
        for line in out.splitlines():
            t = line.strip().lstrip("•-*◦ ").strip()
            t = t.split()[0] if t else ""
            if t and not t.lower().startswith(("name", "plugin", "installed", "no ")):
                names.append(t)
        if names:
            return names
    return None


def _plugin_name(x) -> Optional[str]:
    if isinstance(x, str):
        return x.strip() or None
    if isinstance(x, dict):
        for k in ("name", "id", "package", "pkg"):
            v = x.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def _openclaw_plugins_via_dir(home: Path) -> Optional[list]:
    pdir = home / "plugins"
    try:
        if pdir.is_dir():
            return [d.name for d in pdir.iterdir() if d.is_dir() and not d.name.startswith(".")]
    except OSError:
        pass
    return None


def _hermes_home() -> Path:
    home = (os.environ.get("HERMES_HOME") or "").strip()
    return Path(home).expanduser() if home else Path.home() / ".hermes"


def collect_hermes_stats(venv_bin: str) -> dict:
    """install date + channels/plugins + session count for a Hermes venv."""
    stats: dict = {
        "install_date": None,
        "age_days": None,
        "channels": [],
        "channel_count": None,
        "session_count": None,
        "agent_count": None,
        "plugin_installed": False,
        "plugin_version": None,
        "agent_version": None,
    }
    # install date — stat the venv dir (parent of .../bin).
    try:
        venv_dir = Path(venv_bin).resolve().parent
        stats["install_date"], stats["age_days"] = _install_date(venv_dir)
    except OSError:
        pass
    # agent version
    hermes_bin = Path(venv_bin) / "hermes"
    if hermes_bin.exists():
        try:
            out = subprocess.run([str(hermes_bin), "--version"], capture_output=True, text=True, timeout=10)
            blob = (out.stdout or out.stderr).strip()
            if blob:
                m = re.search(r"\b(\d+\.\d+\.\d+\S*)", blob.splitlines()[0])
                stats["agent_version"] = m.group(1) if m else blob.splitlines()[0]
        except (OSError, subprocess.SubprocessError):
            pass

    home = _hermes_home()

    # Channels + enabled plugins from config.yaml (yaml if available).
    names = _hermes_channels_plugins(home)
    if names is not None:
        stats["channels"] = names[:50]
        stats["channel_count"] = len(names)

    # Sessions — probe known session stores under the Hermes home.
    stats["session_count"] = _count_hermes_sessions(home)

    # Is our plugin importable in this venv + at which version?
    vp = f"{venv_bin}/python"
    try:
        chk = subprocess.run(
            [vp, "-c", "import chat4000_hermes_plugin"], capture_output=True, timeout=15
        )
        if chk.returncode == 0:
            stats["plugin_installed"] = True
            ver = subprocess.run(
                [
                    vp,
                    "-c",
                    "from chat4000_hermes_plugin.package_info import read_package_version;"
                    "print(read_package_version())",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if ver.returncode == 0 and ver.stdout.strip():
                stats["plugin_version"] = ver.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return stats


def _hermes_channels_plugins(home: Path) -> Optional[list]:
    names: list = []
    cfg_path = home / "config.yaml"
    parsed_yaml = False
    if cfg_path.is_file():
        try:
            import yaml  # type: ignore[import-not-found]

            cfg = yaml.safe_load(cfg_path.read_text(errors="ignore"))
            parsed_yaml = True
            if isinstance(cfg, dict):
                plugins = cfg.get("plugins")
                if isinstance(plugins, dict):
                    enabled = plugins.get("enabled")
                    if isinstance(enabled, list):
                        names += [str(p) for p in enabled]
                channels = cfg.get("channels")
                if isinstance(channels, dict):
                    names += [str(k) for k in channels.keys()]
                elif isinstance(channels, list):
                    names += [str(c) for c in channels]
        except ImportError:
            parsed_yaml = False
        except (OSError, ValueError):
            parsed_yaml = True  # file existed but unparseable; don't double-count via dir
    # Fallback (no yaml available): enumerate installed plugin dirs.
    if not parsed_yaml:
        pdir = home / "plugins"
        try:
            if pdir.is_dir():
                names += [d.name for d in pdir.iterdir() if d.is_dir() and not d.name.startswith(".")]
        except OSError:
            pass
    # De-dup, preserve order.
    seen: set = set()
    deduped = []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            deduped.append(n)
    return deduped or None


def _count_hermes_sessions(home: Path) -> Optional[int]:
    """Best-effort session count for a Hermes home. Counts entries in any
    `sessions.json` index and files/dirs in any `sessions/` dir we find under
    the home (direct, per-agent, or under state/). Returns None if no session
    store is discoverable (reported as 'unknown')."""
    total = 0
    found = False
    index_candidates = [
        home / "sessions.json",
        home / "state" / "sessions.json",
        home / "sessions" / "sessions.json",
    ]
    for idx in index_candidates:
        try:
            if idx.is_file():
                n = _count_json_entries(idx.read_text(errors="ignore"))
                if n is not None:
                    total += n
                    found = True
        except OSError:
            continue
    dir_candidates = [home / "sessions", home / "state" / "sessions"]
    try:
        agents_dir = home / "agents"
        if agents_dir.is_dir():
            for a in agents_dir.iterdir():
                if a.is_dir():
                    dir_candidates.append(a / "sessions")
    except OSError:
        pass
    for sdir in dir_candidates:
        try:
            if sdir.is_dir():
                entries = [
                    e
                    for e in sdir.iterdir()
                    if (e.is_dir() and not e.name.startswith("."))
                    or (e.is_file() and e.suffix == ".json" and e.name != "sessions.json")
                ]
                if entries:
                    total += len(entries)
                    found = True
        except OSError:
            continue
    return total if found else None


# ─── Hermes install steps ─────────────────────────────────────────────────


def hermes_install_via_uv(uv: str, venv_python: str, ref: str, *, capture: bool = False) -> None:
    subprocess.run(
        [
            uv, "pip", "install", "--python", venv_python,
            "--find-links", PYVODOZEMAC_FIND_LINKS,
            f"git+{HERMES_REPO_URL}@{ref}",
        ],
        check=True, capture_output=capture, text=capture,
    )


def hermes_install_via_pip(venv_python: str, ref: str, *, capture: bool = False) -> None:
    has_pip = (
        subprocess.run([venv_python, "-c", "import pip"], capture_output=True).returncode == 0
    )
    if not has_pip:
        say("Bootstrapping pip via ensurepip…")
        if subprocess.run([venv_python, "-m", "ensurepip", "--upgrade"], capture_output=True).returncode != 0:
            say("ensurepip failed — fetching get-pip.py")
            with urllib.request.urlopen("https://bootstrap.pypa.io/get-pip.py", timeout=20) as resp:  # noqa: S310
                bootstrap = resp.read()
            subprocess.run([venv_python], input=bootstrap, check=True)
    subprocess.run(
        [
            venv_python, "-m", "pip", "install", "--upgrade",
            "--find-links", PYVODOZEMAC_FIND_LINKS,
            f"git+{HERMES_REPO_URL}@{ref}",
        ],
        check=True, capture_output=capture, text=capture,
    )


def hermes_uninstall(venv_python: str, uv: Optional[str]) -> None:
    if uv:
        subprocess.run([uv, "pip", "uninstall", "--python", venv_python, HERMES_PKG], check=False)
    else:
        subprocess.run([venv_python, "-m", "pip", "uninstall", "-y", HERMES_PKG], check=False)


def hermes_reset_local_state() -> None:
    state_dir = _hermes_home() / "plugins" / "chat4000"
    if state_dir.exists():
        warn(
            f"Removing {state_dir} (key + ack store) — already-paired devices "
            "will fail to decrypt until re-paired."
        )
        ans = input(f"{C_YEL}Continue? [y/N]:{C_RST} ").strip().lower()
        if ans not in ("y", "yes"):
            say("Reset cancelled.")
            return
        shutil.rmtree(state_dir, ignore_errors=True)
        ok(f"Removed {state_dir}")


def symlink_chat4000_onto_path(venv_bin: str) -> None:
    src = Path(venv_bin) / "chat4000"
    if not src.exists():
        return
    path_dirs = os.environ.get("PATH", "").split(os.pathsep)
    for d in (Path("/usr/local/bin"), Path.home() / ".local" / "bin"):
        try:
            d.mkdir(parents=True, exist_ok=True)
            if not os.access(d, os.W_OK):
                continue
            link = d / "chat4000"
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(src)
            ok(
                f"Linked {C_CYN}chat4000{C_RST} -> {link}  "
                f"{C_DIM}(run `chat4000 status` from anywhere){C_RST}"
            )
            if str(d) not in path_dirs:
                warn(f"{d} isn't on your PATH — add it, or use {link} directly.")
            return
        except OSError:
            continue
    warn(f"Couldn't symlink chat4000 onto PATH; run it via {src}")


# ─── OpenClaw install steps ───────────────────────────────────────────────


def detect_plugin_state(openclaw: str) -> tuple:
    """(installed, current, latest, newer) via the plugin's own read-only
    `openclaw chat4000 update --check --json`. Best-effort ⇒ not installed."""
    try:
        r = subprocess.run(
            [openclaw, "chat4000", "update", "--check", "--json"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return (False, None, None, False)
    if r.returncode != 0 or not (r.stdout or "").strip():
        return (False, None, None, False)
    try:
        text = r.stdout
        start = text.find("{")
        data = json.loads(text[start:]) if start >= 0 else {}
    except (ValueError, TypeError):
        return (False, None, None, False)
    cur = data.get("currentVersion")
    latest = data.get("latestVersion")
    newer = bool(data.get("newerAvailable"))
    return (bool(cur), cur, latest, newer)


def openclaw_install_plugin(openclaw: str, ref: str, force: bool = True, *, quiet: bool = False) -> tuple:
    """Install the OpenClaw plugin FROM ITS GITHUB TAG (not the npm registry).

    Tries the npm github shorthand first, then an explicit git+https URL, each
    against the canonical `plugins install` and the legacy `plugin install` CLI
    forms. Returns (success, used_spec, output_tail) — tail is the last ~512
    chars of output on total failure."""
    specs = [
        openclaw_gh_spec(ref),                                  # github:owner/repo#ref
        f"git+https://github.com/{OPENCLAW_REPO_SLUG}.git#{ref}",  # explicit git url
    ]
    cmd_forms = [
        [openclaw, "plugins", "install"],   # canonical (2026.4+)
        [openclaw, "plugin", "install"],    # legacy
    ]
    last_tail = ""
    for spec in specs:
        for form in cmd_forms:
            cmd = list(form) + (["--force"] if force else []) + [spec]
            say(f"$ {' '.join(cmd)}")
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            buf: list = []
            if proc.stdout is not None:
                for line in proc.stdout:
                    if not quiet:
                        sys.stdout.write(line)
                        sys.stdout.flush()
                    buf.append(line)
            if proc.wait() == 0:
                return True, spec, ""
            last_tail = "".join(buf)[-512:]
    return False, None, _scrub_secrets(last_tail) if last_tail else ""


def detect_restart_method() -> Optional[str]:
    docker = shutil.which("docker")
    if docker:
        try:
            r = subprocess.run(
                [docker, "ps", "--filter", "name=openclaw-gateway", "--format", "{{.Names}}"],
                capture_output=True, text=True, timeout=5,
            )
            if "openclaw-gateway" in (r.stdout or ""):
                return "docker"
        except (OSError, subprocess.SubprocessError):
            pass
    openclaw = shutil.which("openclaw") or "openclaw"
    try:
        r = subprocess.run([openclaw, "gateway", "status"], capture_output=True, text=True, timeout=5)
        out = (r.stdout or "") + (r.stderr or "")
        if "service disabled" in out.lower() or "service is not installed" in out.lower():
            return "foreground"
        if r.returncode == 0 and out.strip():
            return "openclaw-supervised"
    except (OSError, subprocess.SubprocessError):
        pass
    return "foreground"


def restart_gateway(method: str) -> bool:
    openclaw = shutil.which("openclaw") or "openclaw"
    if method == "docker":
        docker = shutil.which("docker")
        if not docker:
            return False
        say("$ docker restart openclaw-gateway")
        r = subprocess.run([docker, "restart", "openclaw-gateway"], capture_output=True, text=True)
        if r.returncode != 0:
            warn(f"docker restart failed: {r.stderr.strip()[:200]}")
            return False
        return True
    if method == "openclaw-supervised":
        say(f"$ {openclaw} gateway restart")
        r = subprocess.run([openclaw, "gateway", "restart"], capture_output=True, text=True)
        out = (r.stdout or "") + (r.stderr or "")
        if "service disabled" in out.lower():
            warn("Gateway service is not installed under a supervisor — starting in foreground.")
            return restart_gateway("foreground")
        if r.returncode == 0:
            return True
        if out.strip():
            warn(out.strip()[:500])
        return False
    if method == "foreground":
        log_path = "/tmp/openclaw-gateway.log"
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(["pkill", "-9", "-f", "openclaw gateway run"], capture_output=True, timeout=5)
        time.sleep(1)
        try:
            logf = open(log_path, "ab")
            subprocess.Popen(
                [openclaw, "gateway", "run"],
                stdout=logf, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True,
            )
            say(f"Started gateway in background. Log: {C_CYN}{log_path}{C_RST}")
            time.sleep(4)
            return True
        except (OSError, subprocess.SubprocessError) as exc:
            warn(f"Could not start gateway: {exc}")
            return False
    return False


def openclaw_reset_local_state() -> None:
    state_dir = _openclaw_home() / "plugins" / "chat4000"
    if state_dir.exists():
        warn(f"Removing {state_dir} (key + ack store) — already-paired devices will fail to decrypt until re-paired.")
        ans = input(f"{C_YEL}Continue? [y/N]:{C_RST} ").strip().lower()
        if ans not in ("y", "yes"):
            say("Reset cancelled.")
            return
        shutil.rmtree(state_dir, ignore_errors=True)
        ok(f"Removed {state_dir}")


SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


def wait_for_chat4000_connected(timeout: float = 120.0) -> bool:
    runtime_log = _openclaw_home() / "plugins" / "chat4000" / "logs" / "runtime.log"
    gateway_log = Path("/tmp/openclaw-gateway.log")
    deadline = time.time() + timeout
    started = time.time()
    frame_idx = 0
    is_tty = sys.stdout.isatty()
    last_status = ""
    print()
    while time.time() < deadline:
        if runtime_log.exists():
            try:
                if "runtime.hello_ok" in runtime_log.read_text(errors="ignore"):
                    if is_tty:
                        sys.stdout.write("\r" + " " * 100 + "\r")
                        sys.stdout.flush()
                    return True
            except OSError:
                pass
        status = "starting gateway"
        if gateway_log.exists():
            try:
                gw = gateway_log.read_text(errors="ignore")
                if "[gateway] ready" in gw or "starting channels and sidecars" in gw:
                    status = "loading channels"
                if "[chat4000]" in gw and "Starting chat4000" in gw:
                    status = "chat4000 channel starting"
                if runtime_log.exists():
                    status = "chat4000 connecting to gateway"
            except OSError:
                pass
        if is_tty:
            elapsed = int(time.time() - started)
            frame = SPINNER_FRAMES[frame_idx % len(SPINNER_FRAMES)]
            line = f"\r{C_CYN}{frame}{C_RST}  {C_BOLD}{status}{C_RST}{C_DIM}  ({elapsed}s){C_RST}"
            pad = max(0, len(last_status) - len(line))
            sys.stdout.write(line + (" " * pad))
            sys.stdout.flush()
            last_status = line
        time.sleep(0.1)
        frame_idx += 1
    if is_tty:
        sys.stdout.write("\r" + " " * 100 + "\r")
        sys.stdout.flush()
    return False


# ─── Targets: model, scan report, selection ───────────────────────────────


def build_targets(args) -> list:
    """Discover every install target on this host (every Hermes venv + every
    OpenClaw binary), honoring --hermes-bin / --openclaw-bin overrides. Each
    target is a dict with a `kind` and the per-kind paths. Stats are NOT
    collected here (that happens in the scan)."""
    targets: list = []

    # Explicit overrides short-circuit detection for that kind.
    if args.hermes_bin:
        cand = str(Path(args.hermes_bin.rstrip("/")).expanduser())
        if Path(f"{cand}/python").exists():
            targets.append(_mk_hermes(cand, "user-override"))
        else:
            err(f"--hermes-bin {cand}: no `python` found there.")
    if args.openclaw_bin:
        cand = str(Path(args.openclaw_bin).expanduser())
        if Path(cand).exists() and os.access(cand, os.X_OK):
            targets.append(_mk_openclaw(cand, _openclaw_version(cand), "user-override"))
        else:
            err(f"--openclaw-bin {cand}: not an executable file.")
    if args.hermes_bin or args.openclaw_bin:
        return targets

    for venv_bin, layout in detect_hermes_all():
        targets.append(_mk_hermes(venv_bin, layout))
    for path, version in detect_openclaw_all():
        targets.append(_mk_openclaw(path, version, "detected"))
    return targets


def _mk_hermes(venv_bin: str, layout: str) -> dict:
    return {
        "kind": "hermes",
        "venv_bin": venv_bin,
        "venv_python": f"{venv_bin}/python",
        "layout": layout,
        "display": venv_bin,
        "version": None,
        "stats": None,
    }


def _mk_openclaw(path: str, version: str, layout: str) -> dict:
    return {
        "kind": "openclaw",
        "bin": path,
        "layout": layout,
        "display": path,
        "version": version,
        "stats": None,
    }


def scan_and_report(targets: list) -> None:
    """Collect stats for every detected target, print a table, and emit the
    per-agent + summary analytics."""
    hdr("🔎 Scanning this machine for Hermes / OpenClaw")
    h = sum(1 for t in targets if t["kind"] == "hermes")
    o = sum(1 for t in targets if t["kind"] == "openclaw")
    if not targets:
        warn("No Hermes or OpenClaw install detected.")
    for idx, t in enumerate(targets, 1):
        if t["kind"] == "hermes":
            t["stats"] = collect_hermes_stats(t["venv_bin"])
            t["version"] = t["stats"].get("agent_version") or t["version"]
        else:
            t["stats"] = collect_openclaw_stats(t["bin"])
        _print_target_row(idx, t)
        _emit_agent_detected(t)
    _emit(
        "installer_environment_scan",
        {"hermes_count": h, "openclaw_count": o, "total": len(targets)},
        dest="both",
    )


def _print_target_row(idx: int, t: dict) -> None:
    st = t["stats"] or {}
    kind = t["kind"].upper()
    ver = t.get("version") or "?"
    date = st.get("install_date") or "?"
    age = st.get("age_days")
    age_s = f"{age}d ago" if age is not None else "?"
    chans = st.get("channel_count")
    chans_s = str(chans) if chans is not None else "?"
    sess = st.get("session_count")
    sess_s = str(sess) if sess is not None else "?"
    plug = "yes" if st.get("plugin_installed") else "no"
    pv = st.get("plugin_version")
    plug_s = f"{plug} ({pv})" if pv else plug
    print(f"  {C_BOLD}[{idx}]{C_RST} {C_BLU}{kind}{C_RST}  {C_CYN}{t['display']}{C_RST}")
    print(
        f"      {C_DIM}agent{C_RST} {ver}  ·  {C_DIM}installed{C_RST} {date} ({age_s})  ·  "
        f"{C_DIM}channels{C_RST} {chans_s}  ·  {C_DIM}sessions{C_RST} {sess_s}  ·  "
        f"{C_DIM}chat4000 plugin{C_RST} {plug_s}"
    )
    ch = st.get("channels") or []
    if ch:
        shown = ", ".join(ch[:8]) + (" …" if len(ch) > 8 else "")
        print(f"      {C_DIM}↳ {shown}{C_RST}")


def _emit_agent_detected(t: dict) -> None:
    st = t["stats"] or {}
    _emit(
        "installer_agent_detected",
        {
            "kind": t["kind"],
            "agent_version": t.get("version"),
            "layout": t.get("layout"),
            "install_date": st.get("install_date"),
            "age_days": st.get("age_days"),
            "channels": st.get("channels"),
            "channel_count": st.get("channel_count"),
            "session_count": st.get("session_count"),
            "agent_count": st.get("agent_count"),
            "plugin_installed": st.get("plugin_installed"),
            "plugin_version": st.get("plugin_version"),
        },
        dest=t["kind"],
    )


def select_targets(targets: list, args) -> Optional[list]:
    """Resolve which target(s) to install into. Honors --target/--all and
    prompts when there's more than one and the choice is ambiguous. Returns a
    list (usually length 1) or None to abort."""
    pool = targets
    if args.target:
        pool = [t for t in targets if t["kind"] == args.target]
        if not pool:
            err(f"--target {args.target}: no {args.target} install detected.")
            return None

    if not pool:
        return None
    if args.all:
        return pool
    if len(pool) == 1:
        return pool

    # More than one instance — ask where to install.
    hdr("Where should we install the chat4000 plugin?")
    for idx, t in enumerate(pool, 1):
        ver = t.get("version") or "?"
        print(f"  {C_BOLD}{idx}{C_RST}) {C_BLU}{t['kind']}{C_RST}  {C_CYN}{t['display']}{C_RST}  {C_DIM}({ver}){C_RST}")
    print(f"  {C_BOLD}a{C_RST}) all of them")
    print(f"  {C_BOLD}q{C_RST}) cancel")
    if not sys.stdin.isatty():
        err("Multiple instances found but this is a non-interactive shell.")
        err("Re-run interactively, or narrow it with --target hermes|openclaw, --hermes-bin/--openclaw-bin, or --all.")
        _emit("installer_failed", {"stage": "select_target", "error_class": "Ambiguous", "error_msg": f"{len(pool)} targets, non-interactive"}, dest="both")
        return None
    while True:
        try:
            choice = input(f"{C_CYN}? Pick a target [1-{len(pool)}/a/q]:{C_RST} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            warn("Cancelled.")
            return None
        if choice in ("q", ""):
            warn("Cancelled.")
            return None
        if choice == "a":
            return pool
        if choice.isdigit() and 1 <= int(choice) <= len(pool):
            return [pool[int(choice) - 1]]
        warn("Invalid choice — type a number, 'a', or 'q'.")


# ─── Per-kind install flows ───────────────────────────────────────────────


def install_into_hermes(t: dict, args, *, interactive: bool) -> int:
    venv_bin = t["venv_bin"]
    venv_python = t["venv_python"]
    ok(f"Hermes venv:  {C_CYN}{venv_bin}{C_RST}  {C_DIM}({t['layout']}){C_RST}")
    _emit("installer_hermes_detected", {"hermes_layout": t["layout"], "hermes_path": venv_bin}, dest="hermes")

    if args.uninstall:
        hdr("Uninstall mode (Hermes)")
        hermes_uninstall(venv_python, detect_uv())
        ok("Plugin uninstalled. Local key + state at ~/.hermes/plugins/chat4000 NOT removed (use --reset).")
        return 0
    if args.reset:
        hdr("Reset mode (Hermes, destructive)")
        hermes_reset_local_state()

    hdr(f"📦 Installing chat4000 plugin from {HERMES_REPO_URL}@{args.ref}")
    uv = detect_uv()
    try:
        if uv:
            ok(f"Using uv at {C_CYN}{uv}{C_RST}")
            _emit("installer_uv_detected", {"uv_path": uv}, dest="hermes")
            hermes_install_via_uv(uv, venv_python, args.ref)
            installer_used = "uv"
        else:
            warn("uv not found — falling back to venv pip")
            hermes_install_via_pip(venv_python, args.ref)
            installer_used = "pip"
    except subprocess.CalledProcessError as exc:
        err(f"Install failed: {exc}")
        _emit("installer_failed", {"stage": "pip_install", "error_class": type(exc).__name__, "error_msg": str(exc)[:200], "installer_used": uv and "uv" or "pip"}, dest="hermes")
        return 1
    ok("Plugin installed.")

    check = subprocess.run(
        [venv_python, "-c", "import chat4000_hermes_plugin; print(chat4000_hermes_plugin.__name__)"],
        capture_output=True, text=True,
    )
    if check.returncode != 0:
        err("Plugin installed but import failed:")
        err(check.stderr.strip())
        _emit("installer_failed", {"stage": "import_check", "error_msg": check.stderr.strip()[:200]}, dest="hermes")
        return 1

    ver = subprocess.run(
        [venv_python, "-c", "from chat4000_hermes_plugin.package_info import read_package_version; print(read_package_version())"],
        capture_output=True, text=True,
    )
    plugin_version = ver.stdout.strip() if ver.returncode == 0 else "unknown"
    ok(f"Installed version: {C_GRN}{plugin_version}{C_RST}")
    _emit("installer_pkg_installed", {"installer_used": installer_used, "plugin_ref": args.ref, "plugin_version": plugin_version}, dest="hermes")

    symlink_chat4000_onto_path(venv_bin)

    if args.no_wizard or not interactive:
        if not interactive:
            warn("Multiple targets selected — skipping the interactive wizard for this one.")
        else:
            warn("Skipping wizard (--no-wizard).")
        print(f"  Pair it any time:  {C_CYN}{venv_bin}/chat4000 wizard{C_RST}")
        return 0

    hdr("🪄 Running install wizard")
    _emit("installer_handing_off_to_wizard", dest="hermes")
    # exec so the wizard owns the tty for Ctrl-C handling during pair. Only safe
    # for a single selected target (it replaces this process).
    try:
        os.execv(f"{venv_bin}/chat4000", [f"{venv_bin}/chat4000", "wizard"])  # noqa: S606
    except OSError as exc:
        err(f"Could not exec wizard: {exc}")
        _emit("installer_failed", {"stage": "wizard_exec", "error_class": type(exc).__name__, "error_msg": str(exc)[:200]}, dest="hermes")
        return 1
    return 0  # unreachable after execv


def install_into_openclaw(t: dict, args, *, interactive: bool) -> int:
    openclaw_path = t["bin"]
    ok(f"OpenClaw:  {C_CYN}{openclaw_path}{C_RST}  {C_DIM}({t.get('version')}){C_RST}")
    _emit("installer_openclaw_detected", {"openclaw_version": t.get("version"), "openclaw_path": openclaw_path}, dest="openclaw")

    if args.uninstall:
        hdr("Uninstall mode (OpenClaw)")
        subprocess.run([openclaw_path, "plugins", "uninstall", OPENCLAW_PKG], check=False)
        ok("Plugin uninstalled. Local key + state at ~/.openclaw/plugins/chat4000 NOT removed (use --reset).")
        return 0
    if args.reset:
        hdr("Reset mode (OpenClaw, destructive)")
        openclaw_reset_local_state()

    # Install from the GitHub tag (NOT npm). --plugin-version overrides the gh
    # ref for OpenClaw only; otherwise the shared --ref (default: stable) is used.
    oc_ref = args.plugin_version or args.ref
    installed, cur_ver, _latest, _newer = detect_plugin_state(openclaw_path)
    if installed:
        hdr(f"⬆️  Reinstalling {OPENCLAW_PKG} from GitHub @{oc_ref}  {C_DIM}(have {cur_ver}){C_RST}")
    else:
        hdr(f"📦 Installing {OPENCLAW_PKG} from GitHub @{oc_ref}")

    success, used_spec, output_tail = openclaw_install_plugin(openclaw_path, oc_ref, force=True)
    if not success:
        err("Installing the OpenClaw plugin from GitHub failed.")
        err("Common causes:")
        err("  - GitHub / network unreachable from this host (proxy / offline)")
        err(f"  - This OpenClaw's `plugins install` doesn't accept git specs — try `{openclaw_path} --help`")
        err(f"  - The `{oc_ref}` tag doesn't exist on github.com/{OPENCLAW_REPO_SLUG} yet")
        err("  - Permissions on the OpenClaw plugins directory")
        _emit("installer_failed", {"stage": "plugin_install", "error_class": "InstallFailed", "error_msg": output_tail[:200] or "no output", "output_tail": output_tail, "ref": oc_ref}, dest="openclaw")
        return 1
    ok(f"chat4000 plugin ready (GitHub @{oc_ref}).")
    _emit("installer_pkg_installed", {"plugin_package": OPENCLAW_PKG, "source": "github", "ref": oc_ref, "spec": used_spec, "from_version": cur_ver, "was_installed": installed}, dest="openclaw")

    # Onboard + pair.
    setup_cmd = [openclaw_path, "chat4000", "setup", "--self-redeem"]
    if args.stage:
        setup_cmd.append("--stage")
    elif args.env:
        setup_cmd += ["--env", args.env]
    if args.service_token:
        setup_cmd += ["--service-token", args.service_token]

    do_pair = interactive and not args.no_pair
    if not do_pair:
        setup_cmd.append("--no-pair")
        hdr("🔑 Onboarding the plugin's Matrix identity")
        if not interactive:
            warn("Multiple targets selected — onboarding identity only (pair each later).")
        _emit("installer_handing_off_to_setup", {"paired": False}, dest="openclaw")
    else:
        hdr("📱 Onboarding + pairing a device")
        print(f"{C_DIM}Scan the QR with the chat4000 iOS/macOS app. Ctrl-C to cancel.{C_RST}\n")
        _emit("installer_handing_off_to_setup", {"paired": True}, dest="openclaw")
    try:
        pair_rc = subprocess.run(setup_cmd).returncode
    except KeyboardInterrupt:
        warn("Onboarding cancelled.")
        _emit("installer_cancelled", {"stage": "setup"}, dest="openclaw")
        return 130
    if pair_rc != 0:
        err(f"Setup exited {pair_rc}.")
        err("If this is a token error, pass --service-token <TOKEN> and select --env prod|stage.")
        _emit("installer_failed", {"stage": "setup", "exit_code": pair_rc}, dest="openclaw")
        return pair_rc
    if not do_pair:
        ok("Identity onboarded. Pair a device any time with:")
        print(f"  {C_CYN}{openclaw_path} chat4000 pair{C_RST}")
    else:
        _emit("pairing_completed_via_installer", {}, dest="openclaw")

    # (Re)start gateway.
    if args.no_restart:
        warn("Skipping gateway restart (--no-restart).")
        print(f"  {C_CYN}{openclaw_path} gateway run{C_RST}")
        return 0

    hdr("🔁 Starting OpenClaw gateway")
    method = detect_restart_method()
    if method is not None and restart_gateway(method):
        ok(f"Gateway started (method: {method}).")
        _emit("installer_gateway_restarted", {"method": method}, dest="openclaw")
    else:
        warn("Could not auto-start the gateway.")
        warn("Docker: docker restart openclaw-gateway · terminal: openclaw gateway run · service: openclaw gateway start")
        _emit("installer_failed", {"stage": "gateway_restart", "error_class": "RestartUnavailable", "error_msg": f"no working method (probed: {method or 'none'})"}, dest="openclaw")
        return 1

    if not interactive:
        ok("Installed. Pair + verify each target individually when ready.")
        return 0

    print(f"{C_DIM}First install can take a couple of minutes while OpenClaw loads plugins{C_RST}")
    print(f"{C_DIM}and the chat4000 channel connects. Grab a coffee — we'll tell you when ready.{C_RST}")
    if wait_for_chat4000_connected(timeout=120):
        ok("chat4000 connected. Send a message from your iOS/Mac app — your OpenClaw agent will reply.")
        _emit("installer_succeeded", {}, dest="openclaw")
        _emit("installer_chat4000_relay_connected", {}, dest="openclaw")
        return 0
    warn("chat4000 didn't connect within 120s.")
    warn(f"Watch logs: {C_CYN}tail -f ~/.openclaw/plugins/chat4000/logs/runtime.log{C_RST}")
    _emit("installer_failed", {"stage": "relay_handshake", "error_class": "Timeout", "error_msg": "no runtime.hello_ok within 120s"}, dest="openclaw")
    return 1


def prompt_manual_target(args) -> Optional[list]:
    """Nothing detected — let the user point us at a Hermes venv or OpenClaw bin."""
    print()
    err("We couldn't find Hermes or OpenClaw on this machine.")
    print()
    print("We looked for:")
    print(f"  · {C_CYN}hermes{C_RST} / {C_CYN}openclaw{C_RST} on PATH, env overrides, and all known install layouts")
    print()
    print(f"{C_BOLD}Point us at one, or cancel:{C_RST}")
    print("  · a Hermes venv-bin dir (contains `python` and `hermes`), or")
    print("  · the full path to an `openclaw` executable")
    print(f"  · or press {C_CYN}Ctrl+C{C_RST} to cancel")
    print()
    print(f"{C_BOLD}Or re-run non-interactively:{C_RST}")
    print(f"  {C_CYN}curl ... | bash -s -- --hermes-bin /path/to/venv/bin{C_RST}")
    print(f"  {C_CYN}curl ... | bash -s -- --openclaw-bin /path/to/openclaw{C_RST}")
    print()
    if not sys.stdin.isatty():
        err("(non-interactive shell — cannot prompt. Re-run interactively or pass --hermes-bin/--openclaw-bin.)")
        _emit("installer_failed", {"stage": "detect", "error_class": "NotFound", "error_msg": "nothing detected; non-interactive"}, dest="both")
        return None
    try:
        user_input = input(f"{C_CYN}? Path to a Hermes venv-bin or an openclaw binary:{C_RST} ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        warn("Cancelled.")
        return None
    if not user_input:
        err("Empty path. Bailing.")
        return None
    cand = str(Path(user_input).expanduser())
    # Hermes venv-bin?
    if Path(f"{cand}/python").exists():
        ok(f"Hermes venv:  {C_CYN}{cand}{C_RST}  {C_DIM}(user input){C_RST}")
        _emit("installer_hermes_path_via_user_input", {"hermes_path": cand}, dest="hermes")
        t = _mk_hermes(cand.rstrip("/"), "user-input")
        t["stats"] = collect_hermes_stats(t["venv_bin"])
        return [t]
    # OpenClaw binary?
    if Path(cand).exists() and os.access(cand, os.X_OK):
        ver = _openclaw_version(cand)
        ok(f"OpenClaw:  {C_CYN}{cand}{C_RST}  {C_DIM}({ver}, user input){C_RST}")
        _emit("installer_openclaw_path_via_user_input", {"openclaw_path": cand}, dest="openclaw")
        t = _mk_openclaw(cand, ver, "user-input")
        t["stats"] = collect_openclaw_stats(cand)
        return [t]
    err(f"{cand} is neither a Hermes venv-bin (no `python`) nor an executable openclaw. Bailing.")
    _emit("installer_failed", {"stage": "detect", "error_class": "InvalidUserInput", "error_msg": "unrecognized path"}, dest="both")
    return None


# ─── Agent mode (terse, machine-addressed, detached pairing) ──────────────
#
# When an AGENT runs the installer (not a human at a terminal), we don't want a
# banner/QR/TUI. We want: install quietly, hand the agent the pairing CODE + QR
# so it can relay them to its user, and run the actual pair-and-wait DETACHED so
# this process exits immediately. On any failure: one terse error the agent can
# pass on. Everything the caller sees goes through agent_success/agent_error.

PAIR_EMIT_TIMEOUT = 60.0  # seconds to wait for the detached pair child to print its code
_QR_RE = re.compile(r"(chat4000://pair\?\S+)")
_CODE_IN_URI_RE = re.compile(r"[?&]code=(\d+)")
_CODE_LINE_RE = re.compile(r"[Pp]airing code:\s*([0-9][0-9 ]{3,})")


def _agent_print(lines: list) -> None:
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def agent_error(stage: str, detail: str) -> int:
    detail = _scrub_secrets((detail or "").strip())[-800:] or "(no further detail)"
    _agent_print(
        [
            "[chat4000 installer → agent]  SCRIPT ERROR",
            f"I'm the chat4000 plugin installer and I failed while {stage}.",
            "Relay this to your user, then stop — there's no pairing code this run:",
            "",
            detail,
        ]
    )
    return 1


def agent_success(
    kind: str,
    code: str,
    qr: Optional[str],
    logpath: str,
    note: Optional[str] = None,
    qr_ascii: Optional[str] = None,
) -> int:
    qr_display = qr or "(unavailable — the code or pair link is enough)"
    code_disp = f"{code[:3]}-{code[3:]}" if (code and len(code) == 6 and code.isdigit()) else code
    pair_url = f"https://chat4000.com/pair?code={code}" if code else None
    lines = [
        "[chat4000 installer → agent]",
        "I'm the chat4000 plugin installer. I install the plugin that lets your user",
        "reach you from the chat4000 iPhone / Mac app — like another one of your plugins.",
        f"I installed the files into this {kind} host and started pairing in the background.",
        "",
        "Tell your user the installer ran, then give them ALL THREE ways to pair:",
        "  • SCAN the QR below with the chat4000 app, or",
        "  • TYPE the pairing code into the app, or",
        "  • if they're reading this ON the device they want to pair, just TAP the link.",
        "",
        f"  PAIRING CODE:  {code_disp}",
    ]
    if pair_url:
        lines.append(f"  TAP-TO-PAIR LINK:  {pair_url}")
    lines += [
        "",
        "👉 You MUST show the QR below to the user — display it IN FULL so they can scan",
        "   it. Do not skip it, summarize it, or replace it with just the code/link.",
        "👉 Send the tap-to-pair link as PLAIN TEXT with NO link preview/unfurl (turn the",
        "   preview card OFF). Keep the FULL code in the URL exactly as given — it's a",
        "   short-lived pairing code, NOT a secret, so never mask, star, or shorten it",
        "   (never send 'code=***').",
    ]
    if qr_ascii:
        lines += ["", "  QR — SHOW THIS TO THE USER (scannable; paste it in full):", qr_ascii]
    elif qr:
        lines += ["", f"  QR / LINK — render this as a QR for the user:  {qr_display}"]
    lines += [
        "",
        "Valid for ~5 minutes. After they scan, type, or tap, pairing completes on its",
        "own — you don't need to wait or run anything else.",
    ]
    if note:
        lines += ["", f"NOTE: {note}"]
    lines += [f"(background pairing log: {logpath})"]
    _agent_print(lines)
    return 0


def _render_ascii_qr(uri: Optional[str], python_bins: list) -> Optional[str]:
    """Render the pairing URI as an ASCII QR using whichever python has `qrcode`
    (the Hermes venv ships it; system python usually doesn't). Returns the ASCII
    block or None — best-effort. A missing lib just means the agent falls back to
    rendering the link itself or showing the 6-digit code."""
    if not uri:
        return None
    snippet = (
        "import sys,io,qrcode\n"
        "q=qrcode.QRCode(border=1)\n"
        "q.add_data(sys.argv[1]); q.make(fit=True)\n"
        "b=io.StringIO(); q.print_ascii(out=b, invert=True); sys.stdout.write(b.getvalue())\n"
    )
    for py in python_bins:
        if not py:
            continue
        try:
            r = subprocess.run([py, "-c", snippet, uri], capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.rstrip("\n")
    return None


def _pair_env() -> dict:
    env = dict(os.environ)
    # Force the (python) Hermes pair child to flush its code line to the log
    # immediately instead of block-buffering it because stdout isn't a tty.
    env["PYTHONUNBUFFERED"] = "1"
    return env


def spawn_detached_pair(cmd: list, env: dict, post_success_sh: Optional[str] = None) -> tuple:
    """Start the pair command DETACHED (own session, output → a /tmp log), then
    tail the log until it prints the pairing code + QR. Returns
    (code, qr_uri, logpath, error). On success the child keeps running (polling
    the registrar for the rest of its TTL) after we return — we never wait on it,
    which is the whole point: this process exits while pairing continues.

    If `post_success_sh` is given, the detached child becomes a wrapper that runs
    the pair command and, ONLY if it exits 0 (the device actually paired), runs
    that shell. On Hermes that shell (re)starts the gateway so it loads chat4000
    and invites the just-paired user. Crucially the restart fires AFTER a redeem,
    which can only happen after the user got the code we hand back — so it can
    never race the agent's relay of that code."""
    logpath = f"/tmp/chat4000-pair-{uuid.uuid4().hex[:8]}.log"
    try:
        logf = open(logpath, "ab")  # noqa: SIM115  # handed to the child; we close our copy below
    except OSError as exc:
        return (None, None, None, f"could not open pairing log {logpath}: {exc}")

    child_cmd = cmd
    if post_success_sh:
        # Stage the post-success shell as a TEMP FILE rather than inlining it, so
        # the wrapper's own command line never contains the gateway match-pattern
        # — otherwise `pkill -f 'hermes gateway'` inside it could match and kill
        # the wrapper itself. A file's contents are not in any process's argv.
        quoted = " ".join(shlex.quote(c) for c in cmd)
        sh_path = f"/tmp/chat4000-postpair-{uuid.uuid4().hex[:8]}.sh"
        try:
            Path(sh_path).write_text("#!/usr/bin/env bash\n" + post_success_sh + '\nrm -f "$0"\n', encoding="utf-8")
            os.chmod(sh_path, 0o700)
            child_cmd = ["bash", "-c", f'{quoted}\nrc=$?\nif [ "$rc" -eq 0 ]; then bash {shlex.quote(sh_path)}; fi\n']
        except OSError:
            child_cmd = cmd  # couldn't stage the restart; still run the pairing itself

    try:
        proc = subprocess.Popen(
            child_cmd,
            stdout=logf,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # detach: survives our exit, no SIGHUP/SIGINT from our tty
            close_fds=True,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logf.close()
        return (None, None, logpath, f"could not start pairing: {exc}")
    logf.close()  # the child holds its own copy of the fd

    deadline = time.time() + PAIR_EMIT_TIMEOUT
    last_code = None
    while time.time() < deadline:
        try:
            text = Path(logpath).read_text(errors="ignore")
        except OSError:
            text = ""
        qr = None
        qm = _QR_RE.search(text)
        if qm:
            qr = qm.group(1).rstrip(").,")
        code = None
        if qr:
            cm = _CODE_IN_URI_RE.search(qr)
            if cm:
                code = cm.group(1)
        # Prefer the full QR+code (the QR line carries the code). The plain
        # "Pairing code:" line is a fallback if the child exits/times out first.
        if qr and code:
            return (code, qr, logpath, None)
        lm = _CODE_LINE_RE.search(text)
        if lm:
            last_code = lm.group(1).replace(" ", "")
        rc = proc.poll()
        if rc is not None:
            if last_code:
                return (last_code, qr, logpath, None)
            tail = _scrub_secrets(text.strip())[-800:]
            return (None, qr, logpath, tail or f"pairing exited ({rc}) before printing a code")
        time.sleep(0.2)
    if last_code:
        return (last_code, None, logpath, None)
    return (None, None, logpath, f"pairing didn't print a code within {int(PAIR_EMIT_TIMEOUT)}s (log: {logpath})")


# 20 fun facts to keep the user company while the install runs. Kept short,
# safe, and broadly delightful — the ticker / preamble hands the agent a random
# one to relay.
FUN_FACTS = [
    "Octopuses have three hearts and blue blood.",
    "Honey never spoils — archaeologists have eaten 3,000-year-old honey.",
    "A day on Venus is longer than a year on Venus.",
    "Bananas are berries, but strawberries aren't.",
    "Wombat poop is cube-shaped.",
    "The Eiffel Tower can be ~15 cm taller in summer as the metal expands.",
    "Sharks are older than trees — by about 50 million years.",
    "There are more possible games of chess than atoms in the observable universe.",
    "A group of flamingos is called a 'flamboyance'.",
    "Hot water can freeze faster than cold water (the Mpemba effect).",
    "Sea otters hold hands while sleeping so they don't drift apart.",
    "The shortest war in history lasted about 38 minutes.",
    "Your stomach grows a new lining every few days so it doesn't digest itself.",
    "Cleopatra lived closer in time to the Moon landing than to the Great Pyramid.",
    "A bolt of lightning is about five times hotter than the surface of the Sun.",
    "Cows have best friends and get stressed when they're separated.",
    "The dot over a lowercase 'i' or 'j' is called a tittle.",
    "Scotland's national animal is the unicorn.",
    "Bubble wrap was originally invented as wallpaper.",
    "Hummingbirds are the only birds that can fly backwards.",
]


def _agent_preamble() -> None:
    """Printed FIRST, before the slow install, so that — if the agent streams our
    output — it can tell the user something's happening and keep them company.
    Best-effort: if the agent only reads our output once we exit, this just
    precedes the final block."""
    fact = random.choice(FUN_FACTS)
    _agent_print(
        [
            "[chat4000 installer → agent]",
            "Heads up — I'm setting up the chat4000 plugin now. This can take up to a",
            "minute (download + install). RIGHT NOW, before I finish, send your user a",
            "short message so they're not left hanging — for example:",
            '   "Installing chat4000 — about a minute. I\'ll send you a QR to pair as soon',
            '    as it\'s ready 🛠"',
            "While they wait, keep them company: every ~15s send a quick 'still on it ⏳'",
            "with a fun fact. Here's one to start with:",
            f"   • {fact}",
            "(More progress + facts follow below as I work. Don't link-preview any URLs.)",
            "",
        ]
    )


class _AgentTicker:
    """While a slow step runs, print a progress note + a fresh fun fact every ~15s
    for the agent to relay. Only surfaces to the user mid-wait if the agent streams
    our stdout; harmless (just precedes the final block) otherwise."""

    def __init__(self, interval: float = 15.0) -> None:
        self.interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._facts = random.sample(FUN_FACTS, len(FUN_FACTS))

    def _run(self) -> None:
        i = 0
        elapsed = 0
        while not self._stop.wait(self.interval):
            elapsed += int(self.interval)
            fact = self._facts[i % len(self._facts)]
            i += 1
            _agent_print(
                [
                    f"[chat4000 installer → agent]  still working ({elapsed}s) — send your user",
                    "a quick 'still on it ⏳' with this fun fact:",
                    f"   • {fact}",
                    "",
                ]
            )

    def __enter__(self) -> "_AgentTicker":
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)


def _hermes_gateway_reload_sh(hermes_bin: str) -> str:
    """Shell run by the detached pair wrapper ONLY after a successful pair: make
    the gateway load chat4000 so it invites the freshly-paired user. Hermes has
    no hot-reload, so a gateway that booted BEFORE chat4000 was enabled must
    restart to pick it up. We capture the running gateway's EXACT argv from
    /proc and relaunch it identically (the live gateway runs as `hermes gateway`,
    not `hermes gateway run`, so we don't guess the command), detached so it
    outlives this wrapper. If a supervisor respawns it first, we don't double
    start. The match pattern never appears in the wrapper's own argv (it's in a
    temp file), so pkill can't kill the wrapper."""
    hb = shlex.quote(hermes_bin)
    return f"""sleep 3
gpid=$(pgrep -f 'hermes gateway' 2>/dev/null | head -n1)
if [ -n "$gpid" ] && [ -r "/proc/$gpid/cmdline" ]; then cp "/proc/$gpid/cmdline" /tmp/chat4000-gw-argv.bin 2>/dev/null; fi
pkill -9 -f 'hermes gateway' 2>/dev/null || true
for _ in $(seq 1 12); do pgrep -f 'hermes gateway' >/dev/null 2>&1 || break; sleep 1; done
sleep 2
if pgrep -f 'hermes gateway' >/dev/null 2>&1; then exit 0; fi
if [ -s /tmp/chat4000-gw-argv.bin ]; then
  mapfile -d '' gargv < /tmp/chat4000-gw-argv.bin
  [ -n "${{gargv[-1]:-}}" ] || unset 'gargv[-1]'
  setsid "${{gargv[@]}}" >/tmp/chat4000-gateway.log 2>&1 </dev/null &
else
  setsid {hb} gateway run >/tmp/chat4000-gateway.log 2>&1 </dev/null &
fi
"""


def install_openclaw_agent(t: dict, args) -> int:
    oc = t["bin"]
    oc_ref = args.plugin_version or args.ref
    _agent_preamble()
    # Install + onboard are the slow part — run them under the progress ticker, and
    # only report a failure AFTER the ticker stops (so output never interleaves).
    fail = None  # (stage, detail) of the first failing step
    with _AgentTicker():
        success, _spec, tail = openclaw_install_plugin(oc, oc_ref, force=True, quiet=True)
        if not success:
            fail = (f"installing the OpenClaw plugin from GitHub @{oc_ref}", tail or "no output")
        if fail is None:
            # Onboard the bot identity — no phone needed (--self-redeem), no pairing yet.
            setup_cmd = [oc, "chat4000", "setup", "--self-redeem", "--no-pair"]
            if args.stage:
                setup_cmd.append("--stage")
            elif args.env:
                setup_cmd += ["--env", args.env]
            if args.service_token:
                setup_cmd += ["--service-token", args.service_token]
            r = subprocess.run(setup_cmd, capture_output=True, text=True)
            if r.returncode != 0:
                fail = ("onboarding the plugin identity", ((r.stdout or "") + (r.stderr or "")).strip())
    if fail is not None:
        return agent_error(*fail)
    # 3. Start device pairing DETACHED and capture the code.
    pair_cmd = [oc, "chat4000", "pair"]
    if args.stage:
        pair_cmd.append("--stage")
    elif args.env:
        pair_cmd += ["--env", args.env]
    code, qr, logpath, perr = spawn_detached_pair(pair_cmd, _pair_env())
    if not code:
        return agent_error("starting device pairing", perr or "no pairing code produced")
    # 4. (Re)start the gateway so the channel goes live. It's a separate process,
    #    so this can't kill the agent running us. Soft-fail — the code is already
    #    valid; the user just needs the gateway up for messages to flow.
    note = None
    method = detect_restart_method()
    if not (method and restart_gateway(method)):
        note = ("the OpenClaw gateway didn't auto-start — have the user run "
                "`openclaw gateway run` (or `docker restart openclaw-gateway`) so messages flow")
    _emit("installer_pkg_installed", {"plugin_package": OPENCLAW_PKG, "source": "github", "ref": oc_ref, "mode": "agent"}, dest="openclaw")
    qr_ascii = _render_ascii_qr(qr, [shutil.which("python3"), "python3"])
    return agent_success("OpenClaw", code, qr, logpath, note, qr_ascii=qr_ascii)


def install_hermes_agent(t: dict, args) -> int:
    venv_bin = t["venv_bin"]
    venv_python = t["venv_python"]
    chat4000 = f"{venv_bin}/chat4000"
    _agent_preamble()
    # Install + import-check + prepare are the slow part — run under the progress
    # ticker, and report any failure AFTER it stops so output never interleaves.
    fail = None  # (stage, detail) of the first failing step
    with _AgentTicker():
        uv = detect_uv()
        try:
            if uv:
                hermes_install_via_uv(uv, venv_python, args.ref, capture=True)
            else:
                hermes_install_via_pip(venv_python, args.ref, capture=True)
        except subprocess.CalledProcessError as exc:
            out = getattr(exc, "stderr", None) or getattr(exc, "stdout", None) or ""
            if isinstance(out, bytes):
                out = out.decode("utf-8", "ignore")
            fail = (f"installing the Hermes plugin from GitHub @{args.ref}", out or str(exc))
        if fail is None:
            chk = subprocess.run([venv_python, "-c", "import chat4000_hermes_plugin"], capture_output=True, text=True)
            if chk.returncode != 0:
                fail = ("verifying the installed plugin imports", (chk.stderr or "").strip())
        if fail is None:
            symlink_chat4000_onto_path(venv_bin)
            # `prepare` is pre-restart prep — it does NOT restart the gateway, so it
            # can't kill an agent running us.
            prep_cmd = [chat4000, "prepare"]
            if args.stage:
                prep_cmd.append("--stage")
            r = subprocess.run(prep_cmd, capture_output=True, text=True)
            if r.returncode != 0:
                fail = ("preparing the Hermes plugin (enable + onboard)", ((r.stdout or "") + (r.stderr or "")).strip())
    if fail is not None:
        return agent_error(*fail)
    # 4. Start device pairing DETACHED. When it COMPLETES (the user redeems), the
    #    detached wrapper (re)starts the gateway so it loads chat4000 and invites
    #    the just-paired user. This can't race the relay: a redeem only happens
    #    after the user received the code we hand back below. And because the
    #    restart is gated on pair SUCCESS, an expired/abandoned code never bounces
    #    the gateway.
    pair_cmd = [chat4000, "pair"]
    if args.stage:
        pair_cmd.append("--stage")
    reload_sh = _hermes_gateway_reload_sh(f"{venv_bin}/hermes")
    code, qr, logpath, perr = spawn_detached_pair(pair_cmd, _pair_env(), post_success_sh=reload_sh)
    if not code:
        return agent_error("starting device pairing", perr or "no pairing code produced")
    note = ("once the user enters the code, pairing finishes and I auto-(re)start the Hermes gateway "
            "so it loads chat4000 and invites them — the bot may blip for a few seconds during that restart")
    _emit("installer_pkg_installed", {"plugin_ref": args.ref, "mode": "agent"}, dest="hermes")
    qr_ascii = _render_ascii_qr(qr, [venv_python])
    return agent_success("Hermes", code, qr, logpath, note, qr_ascii=qr_ascii)


def run_agent_mode(args) -> int:
    """`--agent`: terse, machine-addressed install for an agent caller."""
    # Dedicated, easy-to-funnel marker that this run was agent-driven (every other
    # event also carries mode="agent" via _base_props, but this one is explicit).
    _emit("installer_agent_invoked", {"env": os.environ.get("CHAT4000_ENV", "production")}, dest="both")
    _emit("installer_started", {"env": os.environ.get("CHAT4000_ENV", "production")}, dest="both")
    if args.uninstall or args.reset:
        return agent_error("starting", "--uninstall / --reset aren't supported in --agent mode; run the installer normally for those.")

    targets = build_targets(args)
    # Silent scan: collect stats + fire analytics, print nothing.
    for t in targets:
        try:
            if t["kind"] == "hermes":
                t["stats"] = collect_hermes_stats(t["venv_bin"])
                t["version"] = t["stats"].get("agent_version") or t["version"]
            else:
                t["stats"] = collect_openclaw_stats(t["bin"])
            _emit_agent_detected(t)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass  # scanning is best-effort; it must never break the install
    _emit(
        "installer_environment_scan",
        {
            "hermes_count": sum(1 for x in targets if x["kind"] == "hermes"),
            "openclaw_count": sum(1 for x in targets if x["kind"] == "openclaw"),
            "total": len(targets),
            "mode": "agent",
        },
        dest="both",
    )

    pool = targets
    if args.target:
        pool = [t for t in targets if t["kind"] == args.target]
    if not pool:
        return agent_error(
            "detecting an agent host",
            "no Hermes or OpenClaw install found on this machine. Re-run with --hermes-bin <venv/bin> or --openclaw-bin <path>.",
        )
    if len(pool) > 1:
        listed = "; ".join(f"{x['kind']} {x['display']}" for x in pool)
        return agent_error(
            "choosing where to install",
            f"found {len(pool)} hosts ({listed}). Re-run with --target hermes|openclaw, or --hermes-bin/--openclaw-bin to pick one.",
        )

    t = pool[0]
    if args.scan_only:
        st = t.get("stats") or {}
        _agent_print(
            [
                "[chat4000 installer → agent]  scan only — nothing installed.",
                f"Detected: {t['kind']} at {t['display']} "
                f"(chat4000 plugin {'present' if st.get('plugin_installed') else 'absent'}).",
            ]
        )
        return 0
    if t["kind"] == "hermes":
        return install_hermes_agent(t, args)
    return install_openclaw_agent(t, args)


# ─── Main ─────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="chat4000 plugin installer (Hermes + OpenClaw)", add_help=True)
    # Selection
    parser.add_argument("--target", choices=["hermes", "openclaw"], help="only consider this host kind")
    parser.add_argument("--all", action="store_true", help="install into EVERY detected target (no interactive pair)")
    parser.add_argument("--scan-only", action="store_true", help="scan + report + emit analytics, then exit (install nothing)")
    parser.add_argument("--agent", action="store_true", help="agent mode: terse machine-readable output, install quietly, hand back the pairing code + QR, and run pairing DETACHED so this process exits immediately")
    parser.add_argument("--hermes-bin", default=None, metavar="PATH", help="use this Hermes venv-bin dir directly (contains python + hermes)")
    parser.add_argument("--openclaw-bin", default=None, metavar="PATH", help="use this openclaw executable directly")
    # Hermes flow
    parser.add_argument("--no-wizard", action="store_true", help="(hermes) install only, don't run the wizard")
    parser.add_argument("--ref", default=DEFAULT_REF, help=f"GitHub tag/branch/SHA to install for BOTH hosts (default: {DEFAULT_REF})")
    parser.add_argument("--branch", default=None, metavar="NAME", help="install the plugin from this GitHub branch (both hosts) — alias for --ref <branch>")
    parser.add_argument("--latest", action="store_true", help=f"install the LATEST code (the repo's default branch '{LATEST_REF}') instead of the '{DEFAULT_REF}' tag")
    # OpenClaw flow
    parser.add_argument("--no-pair", action="store_true", help="(openclaw) install + restart only, don't pair")
    parser.add_argument("--no-restart", action="store_true", help="(openclaw) install only, don't touch the gateway")
    parser.add_argument("--force", action="store_true", help="(openclaw) force-reinstall in place (gh installs are always forced)")
    parser.add_argument("--plugin-version", default=None, metavar="REF", help="(openclaw) override the GitHub ref for OpenClaw only (tag/branch/SHA)")
    parser.add_argument("--env", default=None, metavar="NAME", help="(openclaw) backend environment: prod | stage")
    parser.add_argument("--service-token", default=None, metavar="TOKEN", help="(openclaw) registrar SERVICE_TOKEN for self-onboard")
    # Common
    parser.add_argument("--reset", action="store_true", help="wipe local key + ack store for the chosen target (destructive)")
    parser.add_argument("--uninstall", action="store_true", help="remove the plugin from the chosen target")
    parser.add_argument("--stage", action="store_true", help="use the chat4000 stage servers")
    parser.add_argument("--no-telemetry", action="store_true", help="disable PostHog + Sentry for this run")
    parser.add_argument("--installer-ref", default=None, help="(internal) ref install.sh fetched this installer from")
    parser.add_argument("--verbose", action="store_true", help="echo every subprocess command")
    args = parser.parse_args()

    global _AGENT_MODE
    _AGENT_MODE = args.agent

    if args.stage:
        os.environ["CHAT4000_ENV"] = "stage"
        say("Stage mode: onboarding/pairing will use the stage servers.")
    # Resolve the GitHub ref for the plugin install (both hosts). Precedence:
    # explicit --ref > --branch > --latest (default branch) > the 'stable' tag.
    if "--ref" not in sys.argv:
        if args.branch:
            args.ref = args.branch
            say(f"Installing from branch {C_CYN}{args.branch}{C_RST} (GitHub @{args.branch}).")
        elif args.latest:
            args.ref = LATEST_REF
            say(f"Installing the LATEST code (GitHub @{LATEST_REF}) — not the '{DEFAULT_REF}' tag.")
    # NOTE: --installer-ref pins only WHICH installer.py was fetched (this repo,
    # chat4000-installer). It deliberately does NOT set the Hermes plugin ref —
    # the plugin lives in a DIFFERENT repo (chat4000-hermes-plugin), so an
    # installer SHA is not a valid plugin ref. To pin the Hermes plugin to a
    # dev build, pass --ref <hermes-plugin-ref> explicitly (default: stable).

    if _AGENT_MODE:
        return run_agent_mode(args)

    banner()
    _emit(
        "installer_started",
        {"env": os.environ.get("CHAT4000_ENV", "production"), "installer_ref": args.installer_ref},
        dest="both",
    )

    # 1. Discover every target, scan + report + emit the new analytics.
    targets = build_targets(args)
    scan_and_report(targets)

    if args.scan_only:
        ok("Scan complete (--scan-only). Nothing installed.")
        return 0

    # 2. Resolve which target(s) to install into.
    if not targets:
        chosen = prompt_manual_target(args)
    else:
        chosen = select_targets(targets, args)
    if not chosen:
        return 130

    # 3. Install. Interactive pairing/wizard only when exactly one target.
    interactive = len(chosen) == 1
    rc = 0
    for t in chosen:
        if t["kind"] == "hermes":
            rc = install_into_hermes(t, args, interactive=interactive) or rc
        else:
            rc = install_into_openclaw(t, args, interactive=interactive) or rc
    return rc


def _entry() -> int:
    try:
        return main()
    except KeyboardInterrupt:
        print()
        warn("Install cancelled.")
        _emit("installer_cancelled", {"stage": "uncaught"}, dest="both")
        return 130
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001  # installer top-level boundary: reports to its own sinks, then exits
        err(f"Installer crashed unexpectedly: {type(exc).__name__}: {exc}")
        _emit("installer_crashed", {"error_class": type(exc).__name__, "error_msg": str(exc)[:200]}, dest="both")
        send_sentry_envelope(exc, kind="both", tags={"crash_stage": "uncaught"})
        err("Crash report sent. If this keeps happening, please open an issue at:")
        err("  https://github.com/chat4000/chat4000-installer/issues")
        return 1


if __name__ == "__main__":
    sys.exit(_entry())
