#!/bin/bash
set -euo pipefail
if [ "$(id -u)" != 0 ]; then
  echo 'Run this installer as root.' >&2
  exit 1
fi
project_dir=$(cd -- "$(dirname -- "$0")" && pwd)
if [ ! -s "$project_dir/config.json" ]; then
  echo 'Configure the bot token first.' >&2
  exit 1
fi
if ! command -v cloudflared >/dev/null; then
  if ! command -v curl >/dev/null; then
    apt-get update
    apt-get install -y curl ca-certificates
  fi
  package_arch=$(dpkg --print-architecture)
  case "$package_arch" in
    amd64|arm64|armhf) download_arch=$package_arch ;;
    i386) download_arch=386 ;;
    *) echo "Unsupported architecture: $package_arch" >&2; exit 1 ;;
  esac
  download_dir=$(mktemp -d)
  trap 'rm -rf -- "$download_dir"' EXIT
  release_url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$download_arch.deb"
  curl --fail --location --retry 3 --max-time 180 \
    --output "$download_dir/cloudflared.deb" "$release_url"
  # apt's _apt user needs to be able to read this public package.
  chmod 0755 "$download_dir"
  apt-get install -y "$download_dir/cloudflared.deb"
fi
cloudflared --version
python3 - "$project_dir" <<'PY'
from pathlib import Path
import json, os, sys
sys.path.insert(0, sys.argv[1])
from bot import save_json
p=Path(sys.argv[1])/'config.json'
metadata=p.stat()
config=json.loads(p.read_text())
config['quick_tunnel']=True
config['web_enabled']=True
save_json(p,config)
if p.stat().st_uid!=metadata.st_uid:
 os.chown(p,metadata.st_uid,metadata.st_gid)
print('Temporary HTTPS tunnel enabled. Bot token was preserved.')
PY
if [ ! -f /etc/systemd/system/cloudex.service ]; then
  bash "$project_dir/install-service.sh"
fi
systemctl restart cloudex.service
systemctl --no-pager status cloudex.service
echo 'The bot will send an Open Cloudex button after HTTPS health verification.'
echo 'Temporary URLs change on tunnel restart; the bot menu updates automatically.'
