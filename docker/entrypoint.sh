#!/bin/bash
# abs-workspace entrypoint: root starts sshd + dirs, then launches the
# virtual worker stack as the `abs-worker` Linux user.
set -e

# --- dirs / ownership (the socket + KB live here, mode-checked by backend) ---
mkdir -p /run/virtual-worker /var/lib/virtual-worker
chown abs-worker:abs-worker /run/virtual-worker /var/lib/virtual-worker
chmod 750 /run/virtual-worker

# --- ssh keys for password-less access ---------------------------------------
KEYS=/opt/virtual-worker/deploy/keys/authorized_keys
if [ -f "$KEYS" ]; then
  mkdir -p /home/abs-worker/.ssh
  cp "$KEYS" /home/abs-worker/.ssh/authorized_keys
  chown -R abs-worker:abs-worker /home/abs-worker/.ssh
  chmod 700 /home/abs-worker/.ssh
  chmod 600 /home/abs-worker/.ssh/authorized_keys
fi

# --- sshd ---------------------------------------------------------------------
/usr/sbin/sshd

# --- run the worker stack as abs-worker ---------------------------------------
# PWD for services; env comes from the container environment (VW_*, keys).
su - abs-worker -c '
  set -a; [ -f /opt/virtual-worker/deploy/worker.env ] && . /opt/virtual-worker/deploy/worker.env; set +a
  export DISPLAY=:99
  export PATH="/opt/venv/bin:/usr/local/bin:$PATH"
  export PYTHONPATH="/opt/virtual-worker/common:/opt/virtual-worker/backend:/opt/virtual-worker/agent"
  export XDG_RUNTIME_DIR=/run/user/$(id -u)
  mkdir -p "$XDG_RUNTIME_DIR" && chmod 700 "$XDG_RUNTIME_DIR"

  # Xvfb virtual display (design: display.service)
  if [ ! -e /tmp/.X99-lock ]; then
    Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp &
    sleep 1
  fi

  # per-user PulseAudio (design: audio.service)
  pulseaudio --start --exit-idiom-time=-1 2>/dev/null || true
  sleep 1

  # backend owns the socket (design: backend)
  python3 -m vw_backend.server >> /home/abs-worker/backend.log 2>&1 &

  # worker agent (design: worker-agent.service)
  python3 -m vw_agent.agent >> /home/abs-worker/agent.log 2>&1 &

  echo "abs-workspace up: ssh abs-worker@localhost, then: vw status"
  wait
' &
CHILD=$!

# forward signals to the service tree
trap 'kill -TERM $CHILD 2>/dev/null' TERM INT
wait $CHILD