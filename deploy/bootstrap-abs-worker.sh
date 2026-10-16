#!/bin/bash
# Bootstrap the virtual worker on this account (run as abs-worker, e.g. via
# `ssh abs-worker@localhost 'bash -s' < deploy/bootstrap-abs-worker.sh` after
# deploy/sync.sh). Idempotent.
set -e
cd ~/virtual-worker

echo "==> venv + python deps"
python3 -m venv ~/venv
~/venv/bin/pip install -q --upgrade pip
~/venv/bin/pip install -q -r requirements.txt

echo "==> playwright chromium (runs on Xvfb, not headless)"
~/venv/bin/python3 -m playwright install chromium

echo "==> directories"
mkdir -p ~/vw-data/run ~/vw-data/db ~/vw-data/logs ~/vw-data/config ~/.config/systemd/user

echo "==> env file (edit ~/worker.env to add GOOGLE_* / OPENROUTER_API_KEY)"
if [ ! -f ~/vw-data/config/worker.env ]; then
  sed -e "s|^VW_SOCKET=.*|VW_SOCKET=$HOME/run/agent.sock|" \
      -e "s|^VW_DB=.*|VW_DB=$HOME/data/kb.db|" \
      deploy/env.template > ~/vw-data/config/worker.env
  chmod 600 ~/vw-data/config/worker.env
fi

echo "==> systemd user units"
cp deploy/systemd-user/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now vw-display.service vw-audio.service \
                            vw-backend.service vw-agent.service

echo "==> CLI on PATH"
mkdir -p ~/.local/bin
ln -sf ~/virtual-worker/deploy/vw-wrapper.sh ~/.local/bin/vw
ln -sf ~/virtual-worker/deploy/vw-wrapper.sh ~/.local/bin/vw-admin
grep -q '.local/bin' ~/.bashrc || echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc

systemctl --user --no-pager --plain list-units 'vw-*'
echo "Done. Try: vw selftest"