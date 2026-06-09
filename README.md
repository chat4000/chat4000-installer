# chat4000-installer

**One installer for the chat4000 plugin, for both agent hosts** — Hermes (Python)
and OpenClaw (Node). It merges the two previous per-plugin installers
(`chat4000-hermes-plugin/scripts/installer.py` and
`chat4000-openclaw-plugin/scripts/installer.py`) into a single entry point.

## What it does

1. **Scans the machine** for *every* Hermes venv and *every* OpenClaw binary
   (env overrides, `hermes`/`openclaw` on PATH, and all known install layouts).
2. **Reports** each detected agent with its install date, channel/plugin count,
   session count, and whether the chat4000 plugin is already on it.
3. **Asks where to install** when more than one instance is found (or pass
   `--target`, `--hermes-bin`, `--openclaw-bin`, or `--all`).
4. **Installs from each plugin's GitHub `stable` tag** (not from PyPI or the npm
   registry) with the right toolchain:
   - Hermes → `uv`/`pip` git-install `git+…/chat4000-hermes-plugin@stable` into
     the venv, then `chat4000 wizard`.
   - OpenClaw → `openclaw plugins install github:chat4000/chat4000-openclaw-plugin#stable`,
     then `openclaw chat4000 setup --self-redeem` + gateway restart + relay wait.

   Override the tag for both hosts with `--ref <tag|branch|sha>`, `--branch
   <name>` (install from a branch), or `--latest` (newest code = default branch
   `main`). OpenClaw-only override: `--plugin-version <ref>`. Precedence:
   `--ref` > `--branch` > `--latest` > `stable`.

## Run it

```bash
curl -fsSL https://raw.githubusercontent.com/chat4000/chat4000-installer/main/install.sh | bash
```

Useful flags (all pass through `install.sh` → `installer.py`):

| Flag | Effect |
|------|--------|
| `--scan-only` | Report + emit analytics, install nothing |
| `--agent` | **Agent mode** — for when an agent (not a human) runs the installer (e.g. over Telegram). No banner/QR/TUI: installs quietly, prints the pairing **code + QR** addressed to the calling agent so it can relay them to its user, and runs pairing **detached** so the process exits immediately (the pair-and-wait keeps polling in the background). Errors come back as one terse `SCRIPT ERROR` block. Auto-detects the host (Hermes or OpenClaw); pass `--target`/`--hermes-bin`/`--openclaw-bin` if more than one is present. On Hermes, when the device actually pairs the detached wrapper auto-(re)starts the gateway so it loads chat4000 and invites the new user (the bot blips briefly); the restart is gated on pair success, so an abandoned code never bounces the gateway. |
| `--target hermes\|openclaw` | Only consider that host kind |
| `--all` | Install into every detected target (no interactive pairing) |
| `--hermes-bin PATH` / `--openclaw-bin PATH` | Skip detection, use this one |
| `--stage` | Use the chat4000 stage servers |
| `--no-telemetry` | Disable PostHog + Sentry for this run |
| `--uninstall` / `--reset` | Remove the plugin / wipe local key+ack store |

`bash install.sh --help` lists them all.

## Analytics

Same stack as the originals: anonymous PostHog (product) + Sentry (crashes) over
stdlib HTTPS, routed to each host's existing project so funnels stay intact.
Beyond the per-step funnel, the merged scan emits, **per detected agent**:
install **date** + age, the **channels/plugins** it has (names + count), and its
**session count** + agent count. Counts and public package names only — never
message content, prompts, credentials, or your file paths (home/username
segments are scrubbed before send). Opt out with `CHAT4000_TELEMETRY_DISABLED=1`,
`--no-telemetry`, or `chat4000 telemetry disable` after install.

Privacy policy: https://chat4000.com/privacy
