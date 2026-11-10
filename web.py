"""Telegram-authenticated, loopback-only Mini App server. No extra dependencies."""
import asyncio
import base64
from concurrent.futures import TimeoutError as FutureTimeout
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import parse_qs, parse_qsl, urlsplit

from bot import MAX_DOWNLOAD, MAX_UPLOAD, policy_changes, safe_file


def validate_init_data(raw, token, owner, now=None):
    if not owner or not raw or len(raw) > 32768:
        raise PermissionError("텔레그램 봇에서 미니앱을 열어주세요.")
    pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise PermissionError("중복된 인증 필드")
    signature = fields.pop("hash", "")
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, "\n".join(f"{k}={v}" for k, v in sorted(fields.items())).encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise PermissionError("텔레그램 인증에 실패했습니다.")
    age = (time.time() if now is None else now) - int(fields.get("auth_date", 0))
    if not -30 <= age <= 3600:
        raise PermissionError("인증이 만료됐습니다. 미니앱을 닫고 다시 열어주세요.")
    if json.loads(fields.get("user", "{}") or "{}").get("id") != owner:
        raise PermissionError("연결된 소유자만 접근할 수 있습니다.")
    return owner


async def dispatch(bot, path, data):
    # Global reads must work even when a WebView remembers a deleted session.
    sid = (bot.state["selected_session"] if path in ("/api/status", "/api/events", "/api/models")
           else data.get("session") or bot.state["selected_session"])
    if sid not in bot.runtimes:
        raise ValueError("세션을 찾을 수 없습니다.")
    if path not in ("/api/status", "/api/events", "/api/models") and bot.runtimes[sid].deleting:
        raise ValueError("삭제 중인 세션입니다.")
    context = bot.session_context.set(sid)
    try:
        if path == "/api/status":
            return {"selected": bot.state["selected_session"], "server": bot.server_id,
                "messageModes": ["queue", "steer"], "canDeleteSessions": True,
                "sessions": [{"id": key, "name": rt.data["name"], "archived": bool(rt.data.get("archived")),
                    "active": bool(rt.active or rt.queue._unfinished_tasks > rt.queue.qsize()), "queued": rt.queue.qsize(),
                    "model": rt.data.get("model") or rt.effective.get("model"), "effort": rt.data.get("effort"),
                    "policy": rt.data.get("approvalPolicy") or rt.effective.get("approvalPolicy"),
                    "sandbox": rt.data.get("sandbox"),
                    "deleting": rt.deleting, "transferring": bool(rt.transfers),
                    "threadId": rt.data.get("thread_id"), "activeTurnId": rt.active, "interrupted": bool(rt.data.get("interrupted_job")),
                    "effective": rt.effective} for key, rt in bot.runtimes.items()],
                "approvals": [{"key": key, "session": bot.event_sid(event), "method": event["method"],
                    "params": event.get("params", {})} for key, event in bot.approvals.items()]}
        if path == "/api/events":
            after = int(data.get("after", 0))
            if data.get("server") != bot.server_id:
                after = 0
            for attempt in range(2):
                rows = [event for event in bot.feed if event["seq"] > after]
                if rows or attempt:
                    return {"server": bot.server_id, "events": rows, "cursor": bot.feed_seq}
                bot.feed_changed.clear()
                try:
                    await asyncio.wait_for(bot.feed_changed.wait(), 20)
                except asyncio.TimeoutError:
                    pass
        if path == "/api/session/delete":
            if data.get("confirm") is not True:
                raise ValueError("세션 삭제를 확인해주세요.")
            await bot.delete_session(sid)
            status = await dispatch(bot, "/api/status", {})
            return {"ok": True, "deleted": sid, **status}
        if path == "/api/command":
            if bot.runtime.data.get("archived"):
                raise ValueError("보관된 세션은 먼저 복원하세요.")
            text = data.get("text", "")
            if not isinstance(text, str) or not text.strip() or len(text.strip()) > 30000:
                raise ValueError("메시지는 1~30,000자로 입력하세요.")
            text = text.strip()
            mode = data.get("mode", "queue")
            if mode not in ("queue", "steer"):
                raise ValueError("대기열에 추가 또는 바로 적용을 선택하세요.")
            if text.startswith("/"):
                await bot.receive_in_session({"message": {"chat": {"id": bot.owner, "type": "private"},
                    "from": {"id": bot.owner}, "text": text}})
                delivery = {"delivery": "command"}
            else:
                delivery = await bot.submit_message(text, mode, data.get("expectedTurnId"))
            return {"ok": True, "selected": bot.state["selected_session"], **delivery}
        if path == "/api/models":
            await bot.codex.start()
            return await bot.codex.call("model/list", {"limit": 100, "cursor": data.get("cursor")})
        if path == "/api/decision":
            await bot.decide(data.get("key", ""), data.get("decision", ""))
            return {"ok": True}
        if path == "/api/settings":
            changes = {}
            for key, limit in (("model", 128), ("effort", 40), ("approvalPolicy", 30)):
                value = data.get(key)
                if not value:
                    continue
                if not isinstance(value, str) or len(value) > limit or any(c.isspace() for c in value):
                    raise ValueError("설정 값을 확인하세요.")
                if key == "approvalPolicy":
                    changes.update(policy_changes(bot.session_state, value))
                else:
                    changes[key] = value
            bot.session_state.update(changes)
            bot.persist()
            return {"ok": True}
        if path == "/api/history":
            thread = bot.session_state.get("thread_id")
            if not thread:
                return {"data": []}
            await bot.codex.start()
            return await bot.codex.call("thread/items/list", {"threadId": thread,
                "limit": 100, "sortDirection": "desc", "cursor": data.get("cursor")})
        if path == "/api/files":
            files = []
            for path in bot.root.rglob("*"):
                if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(bot.root.resolve()):
                    stat = path.stat()
                    files.append({"path": str(path.relative_to(bot.root)), "size": stat.st_size, "modified": stat.st_mtime})
                    if len(files) == 500:
                        break
            return {"files": files}
        if path == "/api/upload":
            if bot.runtime.data.get("archived"):
                raise ValueError("보관된 세션입니다.")
            content = base64.b64decode(data.get("content", ""), validate=True)
            if len(content) > MAX_DOWNLOAD:
                raise ValueError("업로드는 20MB 이하여야 합니다.")
            filename = str(data.get("name") or "file")
            filename = "".join(c for c in filename if c.isprintable() and c not in '/\\')[:180] or "file"
            inbox = bot.root / "inbox"
            if inbox.is_symlink():
                raise ValueError("수신 폴더 경로가 잘못됐습니다.")
            inbox.mkdir(exist_ok=True)
            destination = inbox / (secrets.token_hex(6) + "-" + filename)
            def write():
                temp = destination.with_suffix(destination.suffix + ".part")
                try:
                    with temp.open("xb") as out:
                        out.write(content)
                    temp.replace(destination)
                finally:
                    temp.unlink(missing_ok=True)
            rt = bot.runtime
            rt.transfers += 1
            try:
                await asyncio.to_thread(write)
            finally:
                rt.transfers -= 1
            relative = str(destination.relative_to(rt.root))
            bot.publish({"method": "cloudex/fileUploaded", "cloudex_session": sid, "params": {"path": relative}})
            return {"path": relative}
        raise ValueError("지원하지 않는 요청입니다.")
    finally:
        bot.session_context.reset(context)


class MiniApp:
    def __init__(self, bot, loop):
        self.bot, self.loop = bot, loop
        self.server = None

    def start(self):
        app = self
        static = Path(__file__).parent / "static"

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(30)

            def log_message(self, *args):
                pass

            def reply(self, value, status=200, content_type="application/json; charset=utf-8", filename=None):
                body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors https://web.telegram.org https://*.telegram.org")
                if filename:
                    from urllib.parse import quote
                    self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(filename))
                self.end_headers()
                self.wfile.write(body)

            def handle_request(self, post=False):
                try:
                    parsed = urlsplit(self.path)
                    path = parsed.path
                    if not post and path in ("/", "/app.js", "/style.css"):
                        file = static / ("index.html" if path == "/" else path[1:])
                        self.reply(file.read_bytes(), content_type=mimetypes.guess_type(file)[0] or "application/octet-stream")
                        return
                    if not post and path == "/healthz":
                        self.reply({"ok": True})
                        return
                    if not path.startswith("/api/"):
                        self.reply({"error": "Not found"}, 404)
                        return
                    validate_init_data(self.headers.get("X-Telegram-Init-Data", ""), app.bot.config["bot_token"], app.bot.owner)
                    if post:
                        if self.headers.get_content_type() != "application/json":
                            raise ValueError("JSON 요청이 필요합니다.")
                        size = int(self.headers.get("Content-Length", 0))
                        if not 0 < size <= 29 * 1024 * 1024:
                            raise ValueError("요청 크기가 허용 범위를 벗어났습니다.")
                        data = json.loads(self.rfile.read(size))
                        if not isinstance(data, dict):
                            raise ValueError("JSON 객체가 필요합니다.")
                    else:
                        data = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
                        if path not in ("/api/status", "/api/events", "/api/files", "/api/history", "/api/models", "/api/download"):
                            raise ValueError("이 요청에는 POST가 필요합니다.")
                    if path == "/api/download":
                        sid = data.get("session") or app.bot.state["selected_session"]
                        if sid not in app.bot.runtimes:
                            raise ValueError("세션이 없습니다.")
                        file = safe_file(app.bot.runtimes[sid].root, data.get("path", ""))
                        if file.stat().st_size > MAX_UPLOAD:
                            raise ValueError("다운로드는 50MB 이하여야 합니다.")
                        self.reply(file.read_bytes(), content_type="application/octet-stream", filename=file.name)
                        return
                    future = asyncio.run_coroutine_threadsafe(dispatch(app.bot, path, data), app.loop)
                    try:
                        result = future.result(timeout=100)
                    except FutureTimeout:
                        future.cancel()
                        raise ValueError("요청 시간이 초과됐습니다.")
                    self.reply(result)
                except PermissionError as exc:
                    self.reply({"error": str(exc)}, 401)
                except (ValueError, KeyError, OSError) as exc:
                    self.reply({"error": str(exc)[:1000]}, 400)
                except Exception:
                    self.reply({"error": "요청 처리 실패. 봇 상태와 서버 로그를 확인하세요."}, 500)

            def do_GET(self):
                self.handle_request()

            def do_POST(self):
                self.handle_request(post=True)

        self.server = ThreadingHTTPServer(("127.0.0.1", int(self.bot.config.get("web_port", 8787))), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    async def close(self):
        if self.server:
            await asyncio.to_thread(self.server.shutdown)
            self.server.server_close()
