# Virtual Worker

A virtual worker that joins an online meeting, listens quietly, answers aloud
when called by name, remembers the discussion, delivers reminders, and only
exposes meeting information to authorized users.

- Requirements: [`docs/requirements.md`](docs/requirements.md)
- Design: [`docs/design.md`](docs/design.md)

## Quick start (isolated environment: `ssh abs-worker@localhost`)

The worker runs as Linux user `abs-worker` inside the `abs-workspace`
container (host port 2222). First-time setup from the repo root:

```bash
mkdir -p deploy/keys && ssh-keygen -t ed25519 -f deploy/keys/abs-worker_key -N ''
cp deploy/keys/abs-worker_key.pub deploy/keys/authorized_keys
# pass OPENROUTER_API_KEY into the container (optional, enables the brain)
OPENROUTER_API_KEY=... docker compose -f docker/docker-compose.yml up -d --build
```

Make `ssh abs-worker@localhost` work from your own account — add to
`~/.ssh/config`:

```
Match host localhost user abs-worker
    HostName 127.0.0.1
    Port 2222
    IdentityFile ~/Workspace/virtual-worker/deploy/keys/abs-worker_key
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new
```

Then:

```bash
ssh abs-worker@localhost          # isolated shell as the worker user
vw selftest                       # end-to-end environment self test
vw demo                           # scripted meeting: listen -> answer -> reminder
vw join https://meet.google.com/xxx-xxxx-xxx   # real meeting (req 1)
vw-admin ask --user alice "What did we agree about the launch?"
```

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
docker/                 abs-workspace image + compose + entrypoint
deploy/                 systemd units (host), setup-user.sh, env template
tests/                  ACL, reminder dedup, parsing, protocol tests
```

## Host deployment (real separate Linux user, with root)

```bash
sudo deploy/setup-user.sh
# install units from deploy/systemd-units.md, secrets in /etc/virtual-worker/env
sudo systemctl enable --now virtual-worker-backend.service virtual-worker.service
```

## Tests

```bash
python3 -m unittest discover -s tests -v   # pure-python: KB/ACL, parsing, protocol
vw selftest                                # full environment (inside abs-workspace)
```