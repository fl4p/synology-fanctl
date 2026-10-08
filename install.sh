#!/bin/sh
# Idempotent (re)install of fanctl. Run as root; safe as a DSM boot-up task,
# which re-creates the service after a DSM update wipes /etc/systemd/system.
set -e
here=$(cd "$(dirname "$0")" && pwd)
install -m 755 "$here/fanctl.py" /usr/local/bin/fanctl.py
install -m 644 "$here/fanctl.service" /etc/systemd/system/fanctl.service
systemctl daemon-reload
systemctl enable fanctl.service
systemctl restart fanctl.service
systemctl is-active fanctl.service
