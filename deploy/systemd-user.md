# systemd **user** units for the abs-worker account.
#
# The actual unit files live in this directory (deploy/systemd-user/*.service).
# deploy/bootstrap-abs-worker.sh copies them to ~/.config/systemd/user/ and
# enables them with `systemctl --user`. Linger is enabled for abs-worker, so
# they run without an active ssh session.
#
# Host deployment with a real root-owned system manager is documented in
# docs/design.md section 4 (system units + /etc/virtual-worker/env).
