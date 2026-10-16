# Virtual Worker

A virtual worker that joins an online meeting, listens quietly, answers aloud
when called by name, remembers the discussion, delivers reminders, and only
exposes meeting information to authorized users.

- Requirements: [`docs/requirements.md`](docs/requirements.md)
- Design: [`docs/design.md`](docs/design.md)

## Quick start (isolated environment: `ssh abs-worker@localhost`)

The worker runs directly on this host as the separate Linux user `abs-worker`
(its own home, credentials, display and audio server — never inside your
session). Passwordless ssh from your account is already set up.

First-time setup from the repo root:

```bash
deploy/sync.sh                                   # copy the tree to abs-worker
ssh abs-worker@localhost 'bash -s' < deploy/bootstrap-abs-worker.sh
```

Bootstrap installs a venv + deps (pip only, no root needed), Chromium for
Playwright, systemd **user** units (`vw-display`, `vw-audio`, `vw-backend`,
`vw-agent`) and puts `vw` / `vw-admin` on PATH. Secrets live in
`~/vw-data/config/worker.env` (mode 600, gitignored):
`OPENROUTER_API_KEY` for the brain, `GOOGLE_EMAIL`/`GOOGLE_PASSWORD` for the
Meet account.

Service status: `ssh abs-worker@localhost 'systemctl --user status vw-agent'`

## Commands (as abs-worker)

| Command | What it does |
|---|---|
| `vw join <url>` | join a Google Meet link via the dedicated account (req 1) |
| `vw status` | worker state: joined URL, transcript count, loops alive |
| `vw say "text"` | make the worker speak (debug) |
| `vw selftest` | PASS/FAIL report for Xvfb, PulseAudio buses, STT, TTS, Chromium, backend, ACL fail-closed, reminder scheduling |
| `vw demo` | scripted participant utterances through the real audio pipeline: wake name -> brain -> spoken answer; reminder -> scheduler -> announcement |
| `vw-admin team-add / user-add / meeting-start / meeting-end` | manage teams, users, meetings |
| `vw-admin ask --user <u> "<q>"` | ask as a named user — ACL enforced (req 7/8) |
| `vw-admin put "text"` | store knowledge under the active meeting's team |
| `vw-admin reminders` | pending reminders |

## Layout

```
common/vw_common/       socket protocol helpers (newline-delimited JSON)
backend/vw_backend/     kb.py (SQLite + FTS5 + ACL), brain.py (pi/openrouter),
                        server.py (hub), scheduler.py (reminders), cli.py
agent/vw_agent/         agent.py (orchestrator), audio.py (PulseAudio buses),
                        stt.py (faster-whisper), screen.py (5s diff + OCR),
                        browser.py (Playwright/Meet), parse.py (reminders),
                        config.py
deploy/                 sync.sh, bootstrap-abs-worker.sh, systemd user units,
                        vw-wrapper.sh, env template
                        (root-owned system units: docs/design.md section 4)
tests/                  ACL, reminder dedup, parsing, protocol tests
```

## Host deployment with root (alternative)

If you later want root-owned services instead of user units:
`sudo deploy/setup-user.sh`, then install the system units described in
[`docs/design.md`](docs/design.md) section 4 with secrets in
`/etc/virtual-worker/env` (mode 640 root:abs-worker).`

## Tests

```bash
python3 -m unittest discover -s tests -v   # pure-python: KB/ACL, parsing, protocol
vw selftest                                # full environment (as abs-worker)
```