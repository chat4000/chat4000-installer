#!/usr/bin/env bash
#
# install.sh — ONE installer for the chat4000 plugin, for BOTH agent hosts:
#   • Hermes  (Python agent — plugin installed into Hermes' venv via uv/pip)
#   • OpenClaw (Node agent  — plugin installed via `openclaw plugins install`)
#
# This is a minimal Python bootstrap: it finds a working Python ≥ 3.8 and hands
# off to scripts/installer.py, which does EVERYTHING ELSE — scans the system for
# every Hermes/OpenClaw instance, lets you pick where to install when there's
# more than one, installs the plugin with the right toolchain, runs the wizard /
# pairing, restarts the gateway, and fires analytics (including, for every
# detected agent: its install date, the channels/plugins it has, and how many
# sessions live on it).
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/chat4000/chat4000-installer/main/install.sh | bash
#   curl -fsSL .../install.sh | bash -s -- --target hermes        # force a host kind
#   curl -fsSL .../install.sh | bash -s -- --scan-only            # just report + emit, install nothing
#   curl -fsSL .../install.sh | bash -s -- --no-telemetry         # no PostHog/Sentry this run
#
# Pin the installer (and, for Hermes, the plugin git ref) to a branch/tag/SHA:
#   curl -fsSL .../<SHA>/install.sh | CHAT4000_INSTALL_REF=<SHA> bash
#
# Stage backend:  pass --stage  (or CHAT4000_ENV=stage) — inherited by wizard/pair.
#
# All flags pass through to installer.py. See `bash install.sh --help`
# (after fetching) for the full list.

set -euo pipefail

# Ref the bootstrap fetches installer.py FROM (a branch, tag, or commit SHA).
# Also passed to installer.py as --installer-ref so the Hermes plugin git
# install matches the installer you just ran (unless you pass your own --ref).
REF="${CHAT4000_INSTALL_REF:-main}"
REPO_RAW="https://raw.githubusercontent.com/chat4000/chat4000-installer/${REF}"

# Find a usable Python interpreter (≥ 3.8).
find_python() {
  for cand in python3.13 python3.12 python3.11 python3.10 python3.9 python3.8 python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
      if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)' 2>/dev/null; then
        printf "%s" "$cand"
        return 0
      fi
    fi
  done
  return 1
}

PY="$(find_python || true)"
if [[ -z "$PY" ]]; then
  printf "\033[1;31m\xe2\x9c\x97\033[0m Need Python \xe2\x89\xa5 3.8 on PATH. Install Python first, then re-run.\n" >&2
  exit 1
fi

# Download + run installer.py. Use a temp file so argv is preserved
# (`bash -c "curl ... | python"` would lose them).
TMP="$(mktemp -t chat4000-installer.XXXXXX.py)"
trap 'rm -f "$TMP"' EXIT
curl -fsSL "$REPO_RAW/scripts/installer.py" -o "$TMP"

# Pass the same ref to installer.py (so a Hermes git install matches the
# installer we just fetched) — unless the caller passed their own --installer-ref.
case " $* " in
  *" --installer-ref "*) exec "$PY" "$TMP" "$@" ;;
  *)                     exec "$PY" "$TMP" --installer-ref "$REF" "$@" ;;
esac
