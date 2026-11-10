#!/bin/bash
set -euo pipefail
if [ "$(id -u)" != 0 ]; then
  echo 'Run this installer as root.' >&2
  exit 1
fi
project_dir=$(cd -- "$(dirname -- "$0")" && pwd)
project_user=$(stat -c '%U' "$project_dir")
project_home=$(getent passwd "$project_user" | cut -d: -f6)
if [ ! -s "$project_dir/config.json" ]; then
  echo 'Configure the token with setup.py first.' >&2
  exit 1
fi
cat > /etc/systemd/system/cloudex.service <<EOF
[Unit]
Description=Cloudex Telegram Codex bridge
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=120
StartLimitBurst=10

[Service]
Type=simple
User=$project_user
WorkingDirectory=$project_dir
Environment=HOME=$project_home
Environment=PATH=$project_home/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/usr/bin/python3 $project_dir/bot.py
Restart=on-failure
RestartSec=10
UMask=0077
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now cloudex.service
systemctl --no-pager status cloudex.service
