#!/bin/bash
# Create the isolated Linux user + directories for a real host deployment.
# (Inside the abs-workspace container the user is created by the Dockerfile.)
set -e
if ! id abs-worker &>/dev/null; then
  useradd --create-home --shell /bin/bash --uid 1500 abs-worker
fi
mkdir -p /run/virtual-worker /var/lib/virtual-worker /etc/virtual-worker
chown abs-worker:abs-worker /run/virtual-worker /var/lib/virtual-worker
chmod 750 /run/virtual-worker
cp -n "$(dirname "$0")/env.template" /etc/virtual-worker/env 2>/dev/null || true
chown root:abs-worker /etc/virtual-worker/env
chmod 640 /etc/virtual-worker/env
echo "user 'abs-worker' ready. Install the units from deploy/systemd-units.md,"
echo "put secrets in /etc/virtual-worker/env (mode 640 root:abs-worker), then:"
echo "  systemctl enable --now virtual-worker-backend.service virtual-worker.service"