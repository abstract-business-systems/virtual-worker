# Virtual Worker services for a real host deployment (with root).
# The Docker container (docker/) mirrors this layout inside abs-workspace.
# See deploy/setup-user.sh for account creation.

# /etc/systemd/system/virtual-worker-display.service
[Unit]
Description=Virtual worker virtual display (Xvfb :99)
[Service]
User=abs-worker
Environment=DISPLAY=:99
ExecStartPre=/bin/sh -c '[ ! -e /tmp/.X99-lock ]'
ExecStart=/usr/bin/Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp
Restart=always
[Install]
WantedBy=multi-user.target

# /etc/systemd/system/virtual-worker-audio.service
[Unit]
Description=Virtual worker per-user PulseAudio
Requires=virtual-worker-display.service
[Service]
User=abs-worker
Environment=DISPLAY=:99
Environment=XDG_RUNTIME_DIR=/run/user/1500
ExecStartPre=/usr/bin/mkdir -p /run/user/1500
ExecStartPre=/usr/bin/chown abs-worker:abs-worker /run/user/1500
ExecStart=/usr/bin/pulseaudio --exit-idiom-time=-1
Restart=on-failure
[Install]
WantedBy=multi-user.target

# /etc/systemd/system/virtual-worker-backend.service
[Unit]
Description=Virtual worker backend (Pi brain, KB, scheduler)
Requires=virtual-worker-display.service virtual-worker-audio.service
After=network-online.target
[Service]
User=abs-worker
EnvironmentFile=-/etc/virtual-worker/env
Environment=VW_SOCKET=/run/virtual-worker/agent.sock
Environment=VW_DB=/var/lib/virtual-worker/kb.db
Environment=PYTHONPATH=/opt/virtual-worker/common:/opt/virtual-worker/backend:/opt/virtual-worker/agent
ExecStart=/usr/bin/python3 -m vw_backend.server
Restart=on-failure
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/run/virtual-worker /var/lib/virtual-worker
PrivateTmp=yes
[Install]
WantedBy=multi-user.target

# /etc/systemd/system/virtual-worker.service (the agent)
[Unit]
Description=Virtual worker agent (browser, audio, screen loops)
Requires=virtual-worker-backend.service
[Service]
User=abs-worker
Environment=DISPLAY=:99
Environment=VW_SOCKET=/run/virtual-worker/agent.sock
Environment=VW_DB=/var/lib/virtual-worker/kb.db
EnvironmentFile=-/etc/virtual-worker/env
Environment=PYTHONPATH=/opt/virtual-worker/common:/opt/virtual-worker/backend:/opt/virtual-worker/agent
ExecStart=/usr/bin/python3 -m vw_agent.agent
Restart=on-failure
# --- sandboxing ---
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/home/abs-worker /run/virtual-worker
PrivateTmp=yes
ProtectHome=read-only
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=
[Install]
WantedBy=multi-user.target