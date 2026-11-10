#!/usr/bin/env python3
"""Interactive secret configuration. Token input is hidden."""
import getpass
import json
import secrets
from bot import BASE, save_json

path = BASE / "config.json"
config = json.loads(path.read_text()) if path.exists() else {}
token = getpass.getpass("Telegram bot token (hidden; Enter keeps current): ").strip()
if token:
    config["bot_token"] = token
if not config.get("bot_token"):
    raise SystemExit("Bot token is required.")
config.setdefault("pairing_code", secrets.token_urlsafe(18))
save_json(path, config)
print("Saved private configuration. Pair in Telegram with:")
print("/pair " + config["pairing_code"])
