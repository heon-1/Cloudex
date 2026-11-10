#!/usr/bin/env python3
"""Set an HTTPS origin without printing or replacing the bot token."""
import json
import os
import sys
from urllib.parse import urlsplit
from bot import BASE, save_json

if len(sys.argv) != 2:
    raise SystemExit("Usage: python3 configure-web.py https://your-domain")
origin = sys.argv[1].rstrip("/")
parts = urlsplit(origin)
if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
        or parts.path or parts.query or parts.fragment or any(c.isspace() for c in origin)):
    raise SystemExit("Provide an HTTPS origin with no path, query, or credentials.")
path = BASE / "config.json"
metadata = path.stat()
config = json.loads(path.read_text())
config["public_url"] = origin
config["quick_tunnel"] = False
save_json(path, config)
if path.stat().st_uid != metadata.st_uid:
    os.chown(path, metadata.st_uid, metadata.st_gid)
print("Mini App URL configured:", origin)
print("Reverse proxy this origin to 127.0.0.1:8787, then restart cloudex.service.")
