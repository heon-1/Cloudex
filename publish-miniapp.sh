#!/bin/bash
set -euo pipefail
if [ "$(id -u)" != 0 ]; then
  echo 'Run this installer as root.' >&2
  exit 1
fi
domain=${1:-}
if [[ ! "$domain" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}$ ]]; then
  echo 'Usage: bash publish-miniapp.sh your-domain.example' >&2
  exit 1
fi
project_dir=$(cd -- "$(dirname -- "$0")" && pwd)
if [ ! -s "$project_dir/config.json" ]; then
  echo 'Configure the bot token first.' >&2
  exit 1
fi
if ! command -v caddy >/dev/null; then
  apt-get update
  apt-get install -y caddy
fi
install -d -m 0755 /etc/caddy/sites
site_file=/etc/caddy/sites/cloudex.caddy
if [ -f "$site_file" ]; then
  cp -p "$site_file" "$site_file.previous"
fi
cat > "$site_file" <<EOF
https://$domain {
    reverse_proxy 127.0.0.1:8787
}
EOF
chmod 0644 "$site_file"
if ! python3 - <<'PY'
from pathlib import Path
p=Path('/etc/caddy/Caddyfile')
line='import /etc/caddy/sites/*.caddy'
text=p.read_text() if p.exists() else ''
if line not in text.splitlines():
 p.write_text(text+'\n'+line+'\n')
PY
then
  echo 'Failed to add the Caddy site import.' >&2
  exit 1
fi
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
python3 "$project_dir/configure-web.py" "https://$domain"
systemctl enable --now caddy
systemctl reload caddy
systemctl restart cloudex.service
echo "Cloudex Mini App configured at https://$domain"
echo 'DNS must point to this server; public ports 80 and 443 must be reachable.'
echo 'After HTTPS is available, send /app to the bot.'
