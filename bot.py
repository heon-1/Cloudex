#!/usr/bin/env python3
"""Cloudex: private Telegram client for the local Codex app server (Python 3.12)."""
import asyncio
from collections import deque
import contextlib
import contextvars
import fcntl
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import signal
import time
import urllib.error
import urllib.request

from accounts import Accounts
from queueing import QueuedRequests, WorkQueue

BASE = Path(__file__).resolve().parent
MAX_DOWNLOAD = 20 * 1024 * 1024
MAX_UPLOAD = 50 * 1024 * 1024
LOG = logging.getLogger("cloudex")
VERSION = "0.2.0"
APPROVAL_METHODS = {
    "item/commandExecution/requestApproval", "item/fileChange/requestApproval",
    "item/permissions/requestApproval", "mcpServer/elicitation/request",
    "applyPatchApproval", "execCommandApproval",
}


def approval_result(event, decision):
    """Only return the permissions actually requested, scoped to this turn."""
    method = event["method"]
    if method not in APPROVAL_METHODS:
        raise ValueError("이 요청은 승인 버튼으로 응답할 수 없습니다.")
    if decision not in ("accept", "acceptForSession", "decline", "cancel"):
        raise ValueError("잘못된 승인 응답")
    if decision == "acceptForSession" and method in ("item/permissions/requestApproval", "mcpServer/elicitation/request"):
        raise ValueError("이 요청에는 세션 승인을 사용할 수 없습니다.")
    if method == "item/permissions/requestApproval":
        return {"permissions": event["params"]["permissions"] if decision == "accept" else {}, "scope": "turn"}
    if method == "mcpServer/elicitation/request":
        if decision == "accept" and event["params"].get("mode") != "url":
            raise ValueError("MCP 양식에는 /form 코드 JSON 으로 응답하세요.")
        return {"action": decision, "content": None}
    if method in ("applyPatchApproval", "execCommandApproval"):
        return {"decision": {"accept": "approved", "acceptForSession": "approved_for_session",
                             "decline": "denied", "cancel": "abort"}[decision]}
    return {"decision": decision}


def policy_changes(data, policy):
    if policy == "full-access":
        return {"approvalPolicy": "never", "sandbox": "danger-full-access"}
    if policy not in ("on-request", "untrusted", "never"):
        raise ValueError("on-request / untrusted / never / full-access 중 선택하세요.")
    changes = {"approvalPolicy": policy}
    if data.get("sandbox") == "danger-full-access":
        changes["sandbox"] = "workspace-write"
    return changes


def fast_service_tier(model):
    tiers = [tier.get("id") for tier in model.get("serviceTiers", []) if isinstance(tier, dict)]
    for tier in tiers:
        if tier in ("fast", "priority"):
            return tier
    if not tiers and "fast" in model.get("additionalSpeedTiers", []):
        return "priority"
    return None


def sandbox_policy(mode):
    return {"read-only": {"type": "readOnly"},
            "workspace-write": {"type": "workspaceWrite"},
            "danger-full-access": {"type": "dangerFullAccess"}}.get(mode)


def validate_form(value, schema):
    """Validate Telegram JSON input against the MCP-provided form schema."""
    if not isinstance(schema, dict):
        return
    for union in ("oneOf", "anyOf"):
        if union in schema:
            successes = 0
            for branch in schema[union]:
                try:
                    validate_form(value, branch)
                    successes += 1
                except ValueError:
                    pass
            if successes == 0 or (union == "oneOf" and successes != 1):
                raise ValueError("MCP 양식의 선택 항목 형식과 맞지 않습니다.")
    kind = schema.get("type")
    checks = {"object": isinstance(value, dict), "array": isinstance(value, list),
        "string": isinstance(value, str), "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool), "null": value is None}
    if kind and not any(checks.get(k, False) for k in (kind if isinstance(kind, list) else [kind])):
        raise ValueError(f"MCP 양식 값의 자료형이 맞지 않습니다: {kind}")
    if "const" in schema and value != schema["const"]:
        raise ValueError("MCP 양식의 선택 항목을 확인하세요.")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("MCP 양식의 허용된 값이 아닙니다.")
    if isinstance(value, dict):
        missing = set(schema.get("required") or []) - value.keys()
        if missing:
            raise ValueError("필수 항목: " + ", ".join(missing))
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(value) - props.keys():
            raise ValueError("MCP 양식에 없는 항목이 포함돼 있습니다.")
        for key, entry in value.items():
            if key in props:
                validate_form(entry, props[key])
    elif isinstance(value, (str, list)):
        lower, upper = ("minLength", "maxLength") if isinstance(value, str) else ("minItems", "maxItems")
        if schema.get(lower) is not None and len(value) < schema[lower]:
            raise ValueError("MCP 양식 값이 너무 짧습니다.")
        if schema.get(upper) is not None and len(value) > schema[upper]:
            raise ValueError("MCP 양식 값이 너무 깁니다.")
        if isinstance(value, list) and isinstance(schema.get("items"), dict):
            for item in value:
                validate_form(item, schema["items"])
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if schema.get("minimum") is not None and value < schema["minimum"]:
            raise ValueError("MCP 양식 값이 최소값보다 작습니다.")
        if schema.get("maximum") is not None and value > schema["maximum"]:
            raise ValueError("MCP 양식 값이 최대값보다 큽니다.")


def save_json(path, value):
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(value, out, ensure_ascii=False, indent=2)
        out.flush()
        os.fsync(out.fileno())
    tmp.replace(path)


def safe_file(root, name):
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("작업 폴더 안의 파일만 보낼 수 있습니다.")
    return path


class TelegramRateLimit(RuntimeError):
    def __init__(self, delay):
        super().__init__("Telegram 전송 제한으로 잠시 대기합니다.")
        self.delay = max(1, min(float(delay), 120))


class Telegram:
    def __init__(self, token):
        self.base = "https://api.telegram.org/bot" + token + "/"
        self.filebase = "https://api.telegram.org/file/bot" + token + "/"
        self.send_lock = asyncio.Lock()
        self.last_send = 0.0

    def request(self, method, data=None, timeout=45, body=None, headers=None):
        if body is None:
            body = json.dumps(data or {}).encode()
            headers = {"Content-Type": "application/json"}
        try:
            req = urllib.request.Request(self.base + method, body, headers or {})
            with urllib.request.urlopen(req, timeout=timeout) as res:
                result = json.load(res)
            if not result.get("ok"):
                raise RuntimeError("Telegram API 요청 실패")
            return result["result"]
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                try:
                    delay = json.loads(exc.read(8192)).get("parameters", {}).get("retry_after", 3)
                except (ValueError, OSError):
                    delay = 3
                raise TelegramRateLimit(delay) from None
            raise RuntimeError(f"Telegram API HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise RuntimeError("Telegram 연결 실패: 서버의 인터넷/DNS를 확인하세요.") from None

    async def api(self, method, data=None, **kwargs):
        if method in ("sendMessage", "sendDocument"):
            async with self.send_lock:
                wait = 1.05 - (time.monotonic() - self.last_send)
                if wait > 0:
                    await asyncio.sleep(wait)
                self.last_send = time.monotonic()
        for attempt in range(4):
            try:
                return await asyncio.to_thread(self.request, method, data, **kwargs)
            except TelegramRateLimit as exc:
                if attempt == 3:
                    raise
                await asyncio.sleep(exc.delay + 0.1)

    async def say(self, chat, text, **extra):
        text = str(text) or "(내용 없음)"
        for offset in range(0, len(text), 3000):
            await self.api("sendMessage", {"chat_id": chat, "text": text[offset:offset+3000], **extra})

    async def send_file(self, chat, path):
        if path.stat().st_size > MAX_UPLOAD:
            raise ValueError("전송 파일은 50MB 이하여야 합니다.")
        data = await asyncio.to_thread(path.read_bytes)
        boundary = secrets.token_hex(16)
        filename = path.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{filename}\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n").encode()
        body += data + f"\r\n--{boundary}--\r\n".encode()
        await self.api("sendDocument", timeout=120, body=body,
                       headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})

    async def download(self, attachment, root):
        if attachment.get("file_size", 0) > MAX_DOWNLOAD:
            raise ValueError("수신 파일은 Telegram 기본 API 기준 20MB 이하여야 합니다.")
        info = await self.api("getFile", {"file_id": attachment["file_id"]})
        remote = info["file_path"]
        if remote.startswith("/") or ".." in remote.split("/"):
            raise ValueError("잘못된 파일 경로")
        name = Path(attachment.get("file_name") or remote).name
        name = "".join(c for c in name if c.isprintable() and c not in '/\\')[:180] or "file"
        inbox = root / "inbox"
        if inbox.is_symlink():
            raise ValueError("수신 폴더는 심볼릭 링크일 수 없습니다.")
        inbox.mkdir(exist_ok=True)
        # Never overwrite an earlier attachment, even if Telegram reuses its filename.
        destination = inbox / (secrets.token_hex(6) + "-" + name)

        def transfer():
            temp = destination.with_suffix(destination.suffix + ".part")
            try:
                req = urllib.request.Request(self.filebase + remote)
                with urllib.request.urlopen(req, timeout=120) as src, temp.open("xb") as out:
                    total = 0
                    while chunk := src.read(65536):
                        total += len(chunk)
                        if total > MAX_DOWNLOAD:
                            raise ValueError("수신 제한 20MB 초과")
                        out.write(chunk)
                temp.replace(destination)
            except (urllib.error.URLError, OSError):
                raise RuntimeError("Telegram 파일 다운로드 실패") from None
            finally:
                temp.unlink(missing_ok=True)
        await asyncio.to_thread(transfer)
        return destination


class Codex:
    def __init__(self, notify):
        self.notify = notify
        self.proc = None
        self.pending = {}
        self.seq = 0
        self.writer_lock = asyncio.Lock()
        self.start_lock = asyncio.Lock()
        self.reader_task = None
        self.stderr_task = None

    async def send(self, value):
        async with self.writer_lock:
            self.proc.stdin.write((json.dumps(value) + "\n").encode())
            await self.proc.stdin.drain()

    async def call(self, method, params, timeout=90):
        self.seq += 1
        ident = self.seq
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            await self.send({"id": ident, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(ident, None)

    async def start(self):
        async with self.start_lock:
            if self.proc and self.proc.returncode is None:
                return
            self.proc = await asyncio.create_subprocess_exec(
                "codex", "app-server", "--listen", "stdio://",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=8 * 1024 * 1024)
            self.reader_task = asyncio.create_task(self.read())
            self.stderr_task = asyncio.create_task(self.stderr())
            try:
                await self.call("initialize", {"clientInfo": {"name": "cloudex", "version": VERSION},
                                                "capabilities": {"experimentalApi": True}})
                await self.send({"method": "initialized"})
            except BaseException:
                await self.close()
                raise

    async def stderr(self):
        while line := await self.proc.stderr.readline():
            LOG.info("codex: %s", line.decode(errors="replace").strip())

    async def read(self):
        try:
            while line := await self.proc.stdout.readline():
                msg = json.loads(line)
                if "method" not in msg and msg.get("id") in self.pending:
                    future = self.pending[msg["id"]]
                    if not future.done():
                        if "error" in msg:
                            future.set_exception(RuntimeError(msg["error"].get("message", "Codex 오류")))
                        else:
                            future.set_result(msg.get("result", {}))
                elif "method" in msg:
                    await self.notify(msg)
        except Exception as exc:
            LOG.error("Codex 이벤트 수신 실패: %s", type(exc).__name__)
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("Codex 연결 종료"))
            await self.notify({"method": "cloudex/disconnected", "params": {}})

    async def close(self):
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()


class SessionRuntime:
    def __init__(self, base, sid, data):
        self.data = data
        self.root = base / "workspace" if sid == "main" else base / "workspaces" / sid
        self.root.mkdir(parents=True, exist_ok=True)
        self.queue = WorkQueue(maxsize=20)
        self.queue_lock = asyncio.Lock()
        for text in data.get("pending", []):
            self.queue.put_nowait(text)
        pending = data.setdefault("pending", [])
        ids = data.get("pending_ids", [])
        if len(ids) != len(pending) or any(not isinstance(ident, str) or not ident for ident in ids) or len(set(ids)) != len(ids):
            ids = [secrets.token_hex(8) for _ in pending]
        data["pending_ids"] = ids
        if data.get("inflight"):
            data["interrupted_job"] = data.pop("inflight")
        self.active = None
        self.deleting = False
        self.transfers = 0
        self.turn_done = asyncio.Event()
        self.turn_ready = asyncio.Event()
        self.session_ready = False
        self.effective = {}
        self.chunks = {}


class Bot:
    def __init__(self, config, base=BASE):
        self.base = base
        self.config = config
        self.statepath = base / "state.json"
        self.state = json.loads(self.statepath.read_text()) if self.statepath.exists() else {}
        # Upgrade the single-session format without dropping pairing or history.
        if "sessions" not in self.state:
            old = {k: self.state.pop(k) for k in ("thread_id", "model", "approvalPolicy") if k in self.state}
            self.state["sessions"] = {"main": {"name": "기본", **old}}
            self.state["selected_session"] = "main"
        self.session_context = contextvars.ContextVar("cloudex_session", default=None)
        self.runtimes = {sid: SessionRuntime(base, sid, data) for sid, data in self.state["sessions"].items()}
        self.worker_tasks = {}
        self.deletion_lock = asyncio.Lock()
        self.deleted_threads = set(self.state.get("deleted_threads", []))
        self.started = False
        self.telegram = Telegram(config["bot_token"])
        self.codex = Codex(self.event)
        self.accounts = Accounts(self)
        self.queued = QueuedRequests(self)
        self.model_defaults = {}
        self.events = asyncio.Queue()
        self.session_events = {}
        self.event_tasks = {}
        self.approvals = {}
        self.tasks = set()
        self.closing = False
        self.server_id = secrets.token_hex(8)
        self.feed = deque(maxlen=600)
        self.feed_seq = 0
        self.feed_changed = asyncio.Event()
        self.quick_ready = False

    @property
    def current_sid(self):
        return self.session_context.get() or self.state["selected_session"]

    @property
    def runtime(self):
        return self.runtimes[self.current_sid]

    @property
    def session_state(self):
        return self.runtime.data

    root = property(lambda self: self.runtime.root)
    queue = property(lambda self: self.runtime.queue)
    turn_done = property(lambda self: self.runtime.turn_done)
    active = property(lambda self: self.runtime.active, lambda self, value: setattr(self.runtime, "active", value))
    session_ready = property(lambda self: self.runtime.session_ready, lambda self, value: setattr(self.runtime, "session_ready", value))
    effective = property(lambda self: self.runtime.effective, lambda self, value: setattr(self.runtime, "effective", value))
    chunks = property(lambda self: self.runtime.chunks)

    @property
    def owner(self):
        return self.state.get("owner_id")

    def event_sid(self, event):
        params = event.get("params", {})
        thread = params.get("threadId") or params.get("conversationId") or (params.get("thread") or {}).get("id")
        fallback = event.get("cloudex_session")
        if fallback is None:
            fallback = self.current_sid if self.current_sid in self.runtimes else self.state["selected_session"]
        return next((sid for sid, rt in self.runtimes.items() if thread and rt.data.get("thread_id") == thread), fallback)

    async def tell(self, text, session_id=None, **extra):
        sid = session_id or self.current_sid
        rt = self.runtimes.get(sid)
        if rt is None or rt.deleting:
            return
        label = rt.data["name"]
        await self.telegram.say(self.owner, f"[{label} · {sid}]\n{text}", **extra)

    def ensure_worker(self, sid):
        if sid not in self.runtimes or self.runtimes[sid].deleting:
            return
        if sid not in self.worker_tasks or self.worker_tasks[sid].done():
            self.worker_tasks[sid] = asyncio.create_task(self.work(sid))

    def fresh_session_record(self, name="새 대화"):
        sid = secrets.token_hex(3)
        while sid in self.runtimes or (self.base / "workspaces" / sid).exists():
            sid = secrets.token_hex(3)
        defaults = self.state.get("session_defaults", {})
        data = {"name": name[:60] or "새 대화",
                **{key: defaults[key] for key in ("approvalPolicy", "sandbox") if key in defaults}}
        return sid, data

    def create_session(self, name="새 대화"):
        if len(self.runtimes) >= 50:
            raise ValueError("세션은 최대 50개입니다.")
        sid, data = self.fresh_session_record(name)
        self.state["sessions"][sid] = data
        self.runtimes[sid] = SessionRuntime(self.base, sid, data)
        self.state["selected_session"] = sid
        self.persist()
        if self.started:
            self.ensure_worker(sid)
        return sid

    async def delete_session(self, sid):
        async with self.deletion_lock:
            rt = self.runtimes.get(sid)
            if rt is None:
                raise ValueError("세션을 찾을 수 없습니다.")
            if rt.deleting:
                raise ValueError("이미 삭제 중인 세션입니다.")
            if rt.active or rt.queue._unfinished_tasks or rt.queue_lock.locked():
                raise ValueError("작업을 중단하고 대기열을 비운 뒤 삭제하세요.")
            if rt.transfers:
                raise ValueError("파일 첨부가 끝난 뒤 삭제하세요.")
            rt.deleting = True
            thread = rt.data.get("thread_id")
            archived_here = False
            try:
                if thread and not rt.data.get("archived"):
                    await self.codex.start()
                    await self.codex.call("thread/archive", {"threadId": thread})
                    archived_here = True
                sessions = {key: value for key, value in self.state["sessions"].items() if key != sid}
                available = [key for key in sessions if not sessions[key].get("archived")
                             and not self.runtimes[key].deleting]
                replacement = None
                selected = self.state["selected_session"]
                if not available:
                    replacement_id, data = self.fresh_session_record()
                    replacement = SessionRuntime(self.base, replacement_id, data)
                    sessions[replacement_id] = data
                    available = [replacement_id]
                if selected == sid or selected not in available:
                    selected = available[0]
                deleted_threads = self.deleted_threads | ({thread} if thread else set())
                next_state = {**self.state, "sessions": sessions, "selected_session": selected,
                              "deleted_threads": sorted(deleted_threads)}
                try:
                    save_json(self.statepath, next_state)
                except Exception:
                    if archived_here:
                        with contextlib.suppress(Exception):
                            await self.codex.call("thread/unarchive", {"threadId": thread})
                    raise
                # Commit without yielding: other workers always see matching state/runtime maps.
                self.state = next_state
                self.deleted_threads = deleted_threads
                self.runtimes.pop(sid)
                if replacement is not None:
                    self.runtimes[replacement_id] = replacement
                workers = [task for task in (self.worker_tasks.pop(sid, None), self.event_tasks.pop(sid, None)) if task]
                for task in workers:
                    task.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                events = self.session_events.pop(sid, None)
                if events is not None:
                    while not events.empty():
                        events.get_nowait()
                        events.task_done()
                        self.events.task_done()
                for key, request in list(self.approvals.items()):
                    params = request.get("params", {})
                    if request.get("cloudex_session") == sid or (thread and (params.get("threadId") or params.get("conversationId")) == thread):
                        self.approvals.pop(key, None)
                self.publish({"method": "cloudex/sessionDeleted", "cloudex_session": sid, "params": {"session": sid}})
                if replacement is not None and self.started:
                    self.ensure_worker(replacement_id)
                return selected
            finally:
                rt.deleting = False

    def select_session(self, sid):
        if sid not in self.runtimes or self.runtimes[sid].data.get("archived") or self.runtimes[sid].deleting:
            raise ValueError("사용 가능한 세션 ID를 /sessions 에서 확인하세요.")
        self.state["selected_session"] = sid
        self.persist()

    def persist(self):
        save_json(self.statepath, self.state)

    def background(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        def finished(done):
            self.tasks.discard(done)
            if not done.cancelled() and (exc := done.exception()) is not None:
                LOG.warning("백그라운드 알림 실패: %s", type(exc).__name__)
        task.add_done_callback(finished)

    def publish(self, event):
        self.feed_seq += 1
        self.feed.append({**event, "seq": self.feed_seq})
        self.feed_changed.set()

    async def event(self, event):
        # Never block JSON-RPC response reading on Telegram's network latency.
        method, params = event["method"], event.get("params", {})
        if method.startswith("account/") and "id" not in event:
            self.accounts.notification(event)
            self.publish({"method": "cloudex/accountChanged", "params": {
                "authChanged": method in ("account/updated", "account/login/completed"),
                **({"usage": self.accounts.usage} if method == "account/rateLimits/updated" else {})}})
            return
        if method == "cloudex/disconnected":
            self.accounts.notification(event)
        sid = self.event_sid(event)
        thread = params.get("threadId") or params.get("conversationId") or (params.get("thread") or {}).get("id")
        if thread in self.deleted_threads or sid not in self.runtimes:
            if "id" in event:
                await self.codex.send({"id": event["id"], "error": {"code": -32600, "message": "Session was deleted"}})
            return
        event["cloudex_session"] = sid
        self.publish(event)
        rt = self.runtimes[sid]
        if method == "turn/started":
            rt.active = params["turn"]["id"]
            rt.turn_ready.set()
        if method == "turn/completed":
            rt.active = None
            rt.turn_done.set()
            rt.turn_ready.set()
        if method == "model/rerouted":
            rt.effective["model"] = params.get("toModel")
        if method == "cloudex/disconnected":
            for runtime in self.runtimes.values():
                runtime.active = None
                runtime.session_ready = False
                runtime.turn_done.set()
                runtime.turn_ready.set()
            self.approvals.clear()
            for runtime in self.runtimes.values():
                runtime.chunks.clear()
        self.events.put_nowait(event)

    async def event_worker(self):
        # Preserve event order inside each session without waiting on another's
        # Telegram output or approval notification.
        while True:
            event = await self.events.get()
            sid = event["cloudex_session"]
            if sid not in self.runtimes or self.runtimes[sid].deleting:
                self.events.task_done()
                continue
            queue = self.session_events.setdefault(sid, asyncio.Queue())
            queue.put_nowait(event)
            if sid not in self.event_tasks or self.event_tasks[sid].done():
                self.event_tasks[sid] = asyncio.create_task(self.deliver_session_events(sid, queue))

    async def deliver_session_events(self, sid, queue):
        token = self.session_context.set(sid)
        try:
            while True:
                event = await queue.get()
                try:
                    if self.owner:
                        await self.deliver_event(event)
                except Exception as exc:
                    LOG.error("이벤트 전달 실패 [%s]: %s", sid, type(exc).__name__)
                finally:
                    queue.task_done()
                    self.events.task_done()
        finally:
            self.session_context.reset(token)

    async def deliver_event(self, event):
        method, params = event["method"], event.get("params", {})
        if "id" in event:
            key = secrets.token_hex(8)
            self.approvals[key] = event
            if method in APPROVAL_METHODS:
                await self.show_approval(key, event)
            elif method == "item/tool/requestUserInput":
                await self.tell("Codex 질문\n" + json.dumps(params.get("questions", []), ensure_ascii=False)
                                        + f"\n/answer {key} 질문ID 답변")
            else:
                # Unknown interactive methods must not silently hang the agent or grant access.
                self.approvals.pop(key)
                await self.codex.send({"id": event["id"], "error": {"code": -32601,
                                        "message": "Cloudex does not support this interactive request"}})
                await self.tell(f"지원되지 않는 승인 요청을 거절했습니다: {method}")
        elif method == "item/agentMessage/delta":
            ident = params.get("itemId", "message")
            self.chunks[ident] = self.chunks.get(ident, "") + params.get("delta", "")
        elif method == "item/completed":
            item = params.get("item", {})
            if item.get("type") == "agentMessage":
                text = item.get("text") or self.chunks.get(item.get("id"), "")
                self.chunks.pop(item.get("id"), None)
                if text:
                    await self.tell(text)
            elif item.get("type") == "commandExecution":
                status = item.get("status", "")
                await self.tell(f"명령 {status}: {item.get('command', '')}"[:3000])
            elif item.get("type") == "fileChange":
                changes = item.get("changes", [])
                names = [change.get("path", "") for change in changes]
                await self.tell("변경 파일\n" + "\n".join(names) + f"\n/use {self.current_sid} 후 /get 파일경로 로 받으세요.")
        elif method == "turn/completed":
            self.chunks.clear()
            turn = params.get("turn", {})
            await self.tell(f"작업 {turn.get('status', '종료')}"
                                    + ("\n" + str(turn["error"]) if turn.get("error") else ""))
            for key, request in list(self.approvals.items()):
                if self.event_sid(request) == self.current_sid:
                    self.approvals.pop(key, None)
        elif method == "cloudex/disconnected":
            await self.tell("Codex 연결이 종료됐습니다. 다음 요청 때 재연결합니다.")
        elif method == "error":
            await self.tell("Codex 오류\n" + str(params.get("error", params)))

    async def show_approval(self, key, event):
        method, params = event["method"], event.get("params", {})
        # Include cwd, command, reason, permission scope and available decisions in the review.
        details = json.dumps(params, ensure_ascii=False, indent=2)
        await self.tell(f"승인 요청 {key}\n{method}\n{details}", session_id=self.event_sid(event))
        buttons = []
        is_form = method == "mcpServer/elicitation/request" and params.get("mode") != "url"
        if is_form:
            await self.tell(f'/form {key} {{"필드":"값"}} 로 입력하세요.', session_id=self.event_sid(event))
        else:
            buttons.append({"text": "이번 요청 승인", "callback_data": f"a:{key}:accept"})
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            available = params.get("availableDecisions")
            if available is None or "acceptForSession" in available:
                buttons.append({"text": "세션 승인", "callback_data": f"a:{key}:acceptForSession"})
        buttons.append({"text": "거절", "callback_data": f"a:{key}:decline"})
        buttons.append({"text": "요청 취소", "callback_data": f"a:{key}:cancel"})
        await self.tell("처리 방법을 선택하세요. 추가 권한은 이번 턴에만 적용됩니다.", session_id=self.event_sid(event),
                                reply_markup={"inline_keyboard": [buttons[i:i+2] for i in range(0, len(buttons), 2)]})

    async def decide(self, key, decision):
        event = self.approvals.get(key)
        if not event:
            raise ValueError("이미 처리됐거나 만료된 요청입니다.")
        if event["method"] == "item/tool/requestUserInput" and decision in ("decline", "cancel"):
            result = {"answers": {q["id"]: {"answers": []} for q in event["params"].get("questions", [])}}
        else:
            result = approval_result(event, decision)
        await self.codex.send({"id": event["id"], "result": result})
        self.approvals.pop(key, None)
        self.publish({"method": "cloudex/approvalResolved", "cloudex_session": self.event_sid(event), "params": {"key": key}})
        LOG.info("approval method=%s request=%s decision=%s", event["method"], key, decision)
        await self.tell("Codex에 전달했습니다: " + decision, session_id=self.event_sid(event))

    async def model_options(self, cursor=None):
        cwd = str(self.root)
        await self.codex.start()
        if not cursor:
            response = await self.codex.call("config/read", {"cwd": cwd, "includeLayers": False})
            config = response.get("config") or {}
            self.model_defaults[self.current_sid] = {"defaultModel": config.get("model"),
                                                     "defaultServiceTier": config.get("service_tier")}
        response = await self.codex.call("model/list", {"limit": 100, "cursor": cursor})
        defaults = dict(self.model_defaults.get(self.current_sid, {}))
        if not defaults.get("defaultModel"):
            default = next((model for model in response.get("data", []) if model.get("isDefault")), {})
            defaults["defaultModel"] = default.get("model") or default.get("id")
        return {**response, **defaults}

    async def fast_tier_for_model(self, model=None):
        cursor = None
        while True:
            response = await self.model_options(cursor)
            model = model or self.session_state.get("model") or self.effective.get("model") or response.get("defaultModel")
            entry = next((entry for entry in response.get("data", []) if model and model in (entry.get("model"), entry.get("id"))), None)
            if entry is not None:
                tier = fast_service_tier(entry)
                if tier:
                    return tier
                break
            cursor = response.get("nextCursor")
            if not cursor:
                break
        raise ValueError("선택한 모델은 Fast 모드를 지원하지 않습니다. OFF를 선택해주세요.")

    async def session(self):
        await self.codex.start()
        if self.session_ready:
            return
        # Let Codex resolve its own configured defaults and managed requirements.
        args = {"cwd": str(self.root)}
        for name in ("approvalPolicy", "model", "sandbox", "serviceTier"):
            if self.session_state.get(name):
                args[name] = self.session_state[name]
        if self.session_state.get("thread_id"):
            # Surface resume errors; never silently discard the user's previous session.
            result = await self.codex.call("thread/resume", {**args, "threadId": self.session_state["thread_id"]})
        else:
            result = await self.codex.call("thread/start", args)
            self.session_state["thread_id"] = result["thread"]["id"]
            self.persist()
        self.effective = {k: result.get(k) for k in ("model", "modelProvider", "reasoningEffort", "serviceTier", "approvalPolicy", "approvalsReviewer", "sandbox", "cwd")}
        self.session_ready = True

    async def work(self, sid=None):
        self.session_context.set(sid or self.current_sid)
        while True:
            await self.queue.available.wait()
            async with self.runtime.queue_lock:
                if self.queue.empty():
                    continue
                text = self.queue.get_nowait()
                pending = self.session_state.setdefault("pending", [])
                if pending:
                    pending.pop(0)
                    self.session_state["pending_ids"].pop(0)
                self.session_state["inflight"] = text
            try:
                self.runtime.turn_ready.clear()
                self.persist()
                await self.session()
                self.turn_done.clear()
                self.background(self.tell("작업을 시작합니다.", session_id=self.current_sid))
                args = {"threadId": self.session_state["thread_id"],
                        "input": [{"type": "text", "text": text, "text_elements": []}]}
                for name in ("approvalPolicy", "model", "serviceTier"):
                    if self.session_state.get(name):
                        args[name] = self.session_state[name]
                if self.session_state.get("effort"):
                    args["effort"] = self.session_state["effort"]
                sandbox = sandbox_policy(self.session_state.get("sandbox"))
                if sandbox:
                    args["sandboxPolicy"] = sandbox
                result = await self.codex.call("turn/start", args)
                if sandbox:
                    self.effective["sandbox"] = sandbox
                for key in ("model", "approvalPolicy", "serviceTier"):
                    if key in args:
                        self.effective[key] = args[key]
                if "effort" in args:
                    self.effective["reasoningEffort"] = args["effort"]
                if result.get("turn", {}).get("status") not in ("completed", "interrupted", "failed"):
                    if not self.turn_done.is_set():
                        self.active = result.get("turn", {}).get("id") or self.active
                    self.runtime.turn_ready.set()
                    await self.turn_done.wait()
            except asyncio.CancelledError:
                self.session_state["interrupted_job"] = self.session_state.get("inflight", text)
                raise
            except Exception as exc:
                LOG.error("작업 실패: %s", type(exc).__name__)
                with contextlib.suppress(RuntimeError):
                    await self.tell("작업 오류: " + str(exc)[:1500])
            finally:
                self.active = None
                self.runtime.turn_ready.set()
                self.session_state.pop("inflight", None)
                self.persist()
                self.queue.task_done()

    async def enqueue(self, text):
        if self.accounts.pending:
            raise ValueError("계정 로그인을 완료하거나 취소한 뒤 작업을 보내주세요.")
        if self.runtime.deleting:
            raise ValueError("삭제 중인 세션입니다.")
        async with self.runtime.queue_lock:
            if self.queue.full():
                raise ValueError("대기 작업이 20개입니다. 완료 후 다시 보내주세요.")
            was_idle = not self.runtime.active and not self.queue._unfinished_tasks
            self.queued.write([*self.session_state.get("pending", []), text],
                              [*self.session_state.get("pending_ids", []), secrets.token_hex(8)])
            self.queue.put_nowait(text)
            if was_idle:
                self.runtime.turn_ready.clear()
        if self.started:
            self.ensure_worker(self.current_sid)
        self.background(self.tell(f"접수했습니다. 대기 {self.queue.qsize()}개 · /status · /stop",
                                  session_id=self.current_sid))

    async def submit_message(self, text, mode="queue", expected_turn_id=None):
        if self.accounts.pending:
            raise ValueError("계정 로그인을 완료하거나 취소한 뒤 작업을 보내주세요.")
        if self.runtime.deleting:
            raise ValueError("삭제 중인 세션입니다.")
        if mode not in ("queue", "steer"):
            raise ValueError("대기열에 추가 또는 바로 적용을 선택하세요.")
        if expected_turn_id is not None and (not isinstance(expected_turn_id, str)
                or not expected_turn_id or len(expected_turn_id) > 200):
            raise ValueError("실행 중인 작업 정보를 확인하세요.")
        if mode == "queue":
            await self.enqueue(text)
            return {"delivery": "queue"}
        rt = self.runtime
        if expected_turn_id and rt.active != expected_turn_id:
            raise ValueError("작업이 이미 끝났거나 바뀌었어요. 상태를 확인하고 다시 전송하세요.")
        if not rt.active and rt.queue._unfinished_tasks:
            if self.started:
                self.ensure_worker(self.current_sid)
            try:
                await asyncio.wait_for(rt.turn_ready.wait(), 10)
            except asyncio.TimeoutError:
                raise ValueError("작업을 준비 중이에요. 잠시 후 바로 적용을 다시 눌러주세요.") from None
        if not rt.active:
            if expected_turn_id or rt.queue._unfinished_tasks:
                raise ValueError("작업 상태가 바뀌었어요. 상태를 확인하고 다시 전송하세요.")
            await self.enqueue(text)
            return {"delivery": "start"}
        turn_id = rt.active
        if expected_turn_id and turn_id != expected_turn_id:
            raise ValueError("작업이 바뀌었어요. 상태를 확인하고 다시 전송하세요.")
        result = await self.codex.call("turn/steer", {
            "threadId": rt.data["thread_id"], "expectedTurnId": turn_id,
            "input": [{"type": "text", "text": text, "text_elements": []}]})
        if rt.active == turn_id and rt.data.get("inflight"):
            rt.data["inflight"] += "\n\n추가 지시:\n" + text
            self.persist()
        self.publish({"method": "cloudex/messageSteered", "cloudex_session": self.current_sid,
                      "params": {"turnId": result.get("turnId", turn_id)}})
        return {"delivery": "steer", "turnId": result.get("turnId", turn_id)}

    async def receive(self, update):
        update_id = update.get("update_id")
        if update_id is not None and update_id <= self.state.get("last_received_update", -1):
            return
        token = self.session_context.set(self.state["selected_session"])
        try:
            await self.receive_in_session(update)
        finally:
            self.session_context.reset(token)
            if update_id is not None:
                self.state["last_received_update"] = update_id
                self.persist()

    async def receive_in_session(self, update):
        callback = update.get("callback_query")
        msg = update.get("message")
        if callback:
            if callback.get("from", {}).get("id") != self.owner:
                return
            await self.telegram.api("answerCallbackQuery", {"callback_query_id": callback["id"]})
            parts = callback.get("data", "").split(":")
            if len(parts) == 2 and parts[0] == "u":
                self.select_session(parts[1])
                await self.tell("세션을 전환했습니다.", session_id=parts[1])
                return
            if len(parts) != 3 or parts[0] != "a":
                return
            try:
                await self.decide(parts[1], parts[2])
            except ValueError as exc:
                await self.telegram.say(self.owner, str(exc))
            return
        if not msg or msg.get("chat", {}).get("type") != "private":
            return
        chat = msg["chat"]["id"]
        sender = msg.get("from", {}).get("id")
        text = msg.get("text", "").strip()
        if not self.owner:
            expected = "/pair " + self.config["pairing_code"]
            if sender == chat and hmac.compare_digest(text.encode(), expected.encode()):
                self.state["owner_id"] = sender
                self.persist()
                await self.telegram.say(chat, "Cloudex 연결 완료. 메시지로 작업을 요청하거나 파일을 첨부하세요. /help")
            elif text == "/start":
                await self.telegram.say(chat, "서버에서 발급한 /pair 코드로 연결해주세요.")
            return
        if sender != self.owner or chat != self.owner:
            return
        if text in ("/start", "/help"):
            await self.telegram.say(chat, "Cloudex · Codex 비동기 작업\n"
                "메시지: 작업 접수\n파일/사진 첨부: 작업 폴더에 저장 (설명은 작업 요청)\n"
                "/status 상태\n/stop 현재 작업 중단\n/clear 대기 작업 비우기\n"
                "/app 미니앱 열기\n/sessions 세션 목록\n/new 이름 — 새 세션\n/use ID — 세션 전환\n"
                "/rename 이름\n/archive ID · /restore ID\n"
                "/files 파일 목록\n/get 경로 — 파일 받기\n"
                "/pending 승인/질문 목록\n/answer 코드 질문ID 답변\n/form 코드 JSON\n"
                "/settings 적용 설정\n/models 모델 목록\n/model 모델ID\n/effort 추론수준\n"
                "/retry 재시작으로 중단된 요청 재접수\n"
                "/policy on-request|untrusted|never (다음 작업부터)\n"
                "권한 설정과 관리 정책은 Codex가 최종 적용합니다.")
        elif text == "/app":
            if self.config.get("public_url") and (not self.config.get("quick_tunnel") or self.quick_ready):
                await self.telegram.say(chat, "Cloudex 미니앱", reply_markup={"inline_keyboard": [[{
                    "text": "Cloudex 열기", "web_app": {"url": self.config["public_url"]}}]]})
            else:
                await self.telegram.say(chat, "HTTPS 주소를 준비 중입니다. 잠시 후 /app 을 다시 보내주세요."
                                        if self.config.get("quick_tunnel") else "미니앱 서버는 준비됐습니다. 공개 HTTPS 주소 연결이 필요합니다.")
        elif text in ("/sessions", "/sessions all"):
            lines, buttons = [], []
            for sid, rt in self.runtimes.items():
                data = rt.data
                if data.get("archived") and text != "/sessions all":
                    continue
                status = "보관" if data.get("archived") else "실행" if rt.queue._unfinished_tasks > rt.queue.qsize() else "대기"
                selected = "● " if sid == self.state["selected_session"] else ""
                lines.append(f"{selected}{sid} · {data['name']} · {status} · 대기 {rt.queue.qsize()} · {data.get('model', 'Codex 기본')}")
                if not data.get("archived"):
                    buttons.append([{"text": f"{sid} · {data['name']}", "callback_data": f"u:{sid}"}])
            await self.telegram.say(chat, "세션 목록\n" + "\n".join(lines), reply_markup={"inline_keyboard": buttons})
        elif text.startswith("/use "):
            sid = text[5:].strip()
            self.select_session(sid)
            self.background(self.tell("세션을 전환했습니다. 기존 작업은 계속 실행됩니다.", session_id=sid))
        elif text == "/new" or text.startswith("/new "):
            sid = self.create_session(text[5:].strip() or "새 대화")
            self.background(self.tell("새 세션을 만들었습니다. 세션별 작업 폴더와 모델 설정을 사용합니다.", session_id=sid))
        elif text.startswith("/rename "):
            name = text[8:].strip()
            if not name or len(name) > 60:
                raise ValueError("세션 이름은 1~60자로 입력하세요.")
            self.session_state["name"] = name
            self.persist()
            await self.tell("세션 이름을 변경했습니다.")
        elif text == "/archive" or text.startswith("/archive "):
            sid = text[9:].strip() or self.current_sid
            rt = self.runtimes.get(sid)
            if not rt or rt.data.get("archived"):
                raise ValueError("보관할 세션 ID를 확인하세요.")
            if rt.queue._unfinished_tasks or rt.active:
                raise ValueError("작업을 중단하고 대기를 비운 뒤 보관하세요.")
            available = [other for other, other_rt in self.runtimes.items() if other != sid and not other_rt.data.get("archived")]
            if not available:
                raise ValueError("마지막 세션입니다. /new 로 다른 세션을 먼저 만드세요.")
            if rt.data.get("thread_id"):
                await self.codex.start()
                await self.codex.call("thread/archive", {"threadId": rt.data["thread_id"]})
            rt.data["archived"] = True
            rt.session_ready = False
            if self.state["selected_session"] == sid:
                self.state["selected_session"] = available[0]
            self.persist()
            await self.telegram.say(chat, f"{sid} 세션을 보관했습니다. /restore {sid} 로 복원할 수 있습니다.")
        elif text.startswith("/restore "):
            sid = text[9:].strip()
            rt = self.runtimes.get(sid)
            if not rt or not rt.data.get("archived"):
                raise ValueError("보관된 세션 ID를 /sessions all 에서 확인하세요.")
            if rt.data.get("thread_id"):
                await self.codex.start()
                await self.codex.call("thread/unarchive", {"threadId": rt.data["thread_id"]})
            rt.data["archived"] = False
            self.select_session(sid)
            await self.tell("세션을 복원하고 선택했습니다.", session_id=sid)
        elif text == "/retry":
            request = self.session_state.get("interrupted_job")
            if not request:
                raise ValueError("재시작으로 중단된 요청이 없습니다.")
            if self.queue.full():
                raise ValueError("대기 작업이 가득 찼습니다.")
            await self.enqueue(request)
            self.session_state.pop("interrupted_job", None)
            self.persist()
        elif text == "/pending":
            requests = [(key, event) for key, event in self.approvals.items() if self.event_sid(event) == self.current_sid]
            if not requests:
                await self.telegram.say(chat, "대기 중인 승인/질문이 없습니다.")
            for key, event in requests:
                if event["method"] in APPROVAL_METHODS:
                    await self.show_approval(key, event)
                else:
                    await self.telegram.say(chat, f"{key}\n" + json.dumps(event["params"], ensure_ascii=False))
        elif text == "/settings":
            await self.codex.start()
            response = await self.codex.call("config/read", {"cwd": str(self.root), "includeLayers": False})
            config = response.get("config", {})
            defaults = {k: config.get(k) for k in ("model", "model_reasoning_effort", "approval_policy", "sandbox_mode")}
            await self.telegram.say(chat, "Codex 설정\n" + json.dumps({"기본": defaults, "현재 대화": self.effective,
                "세션": self.current_sid, "다음 작업": {k: self.session_state[k] for k in ("model", "approvalPolicy", "effort") if k in self.session_state}}, ensure_ascii=False, indent=2))
        elif text == "/models":
            await self.codex.start()
            result = await self.codex.call("model/list", {})
            await self.telegram.say(chat, "\n".join(f"{m.get('model') or m.get('id')} · {m.get('displayName', '')}\n"
                f"추론: {', '.join(e.get('reasoningEffort', '') for e in m.get('supportedReasoningEfforts', []))}" for m in result.get("data", [])))
        elif text.startswith("/model "):
            model = text[7:].strip()
            if not model or len(model) > 128 or any(c.isspace() for c in model):
                raise ValueError("/models 에 표시된 모델 ID를 입력하세요.")
            self.session_state["model"] = model
            self.persist()
            await self.tell(f"다음 작업 모델: {model}. 사용 가능 여부는 Codex가 확인합니다.")
        elif text.startswith("/effort "):
            effort = text[8:].strip()
            if not effort or len(effort) > 40 or any(c.isspace() for c in effort):
                raise ValueError("/models 에 표시된 추론 수준을 입력하세요.")
            # Values evolve with models; leave support validation to Codex.
            self.session_state["effort"] = effort
            self.persist()
            await self.tell(f"다음 작업 추론 수준: {effort}. 모델 지원 여부는 Codex가 확인합니다.")
        elif text.startswith("/policy "):
            policy = text[8:].strip()
            self.session_state.update(policy_changes(self.session_state, policy))
            self.persist()
            label = "항상 승인 · 전체 액세스" if policy == "full-access" else policy
            await self.tell(f"다음 작업 승인 정책: {label}. Codex 관리 정책 제한이 우선 적용됩니다.")
        elif text == "/status":
            await self.tell(f"상태: {'실행 중' if self.queue._unfinished_tasks > self.queue.qsize() else '대기'}\n"
                f"대기 작업: {self.queue.qsize()}\n승인/답변 대기: {sum(self.event_sid(e) == self.current_sid for e in self.approvals.values())}\n"
                f"대화: {self.session_state.get('thread_id', '없음')}\n"
                f"중단된 이전 요청: {'있음 — /retry' if self.session_state.get('interrupted_job') else '없음'}")
        elif text == "/stop":
            if self.active:
                await self.codex.call("turn/interrupt", {"threadId": self.session_state["thread_id"], "turnId": self.active})
                await self.telegram.say(chat, "중단 요청을 보냈습니다. 대기 작업도 취소하려면 /clear")
            else:
                await self.telegram.say(chat, "실행 중인 작업이 없습니다.")
        elif text == "/clear":
            async with self.runtime.queue_lock:
                count = self.queue.qsize()
                self.queued.write([], [])
                while not self.queue.empty():
                    self.queue.get_nowait()
                    self.queue.task_done()
            await self.telegram.say(chat, f"대기 작업 {count}개를 취소했습니다.")
        elif text == "/files":
            files = []
            for path in self.root.rglob("*"):
                if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(self.root.resolve()):
                    files.append(str(path.relative_to(self.root)))
                    if len(files) == 100:
                        break
            await self.telegram.say(chat, "파일 목록 (최대 100개)\n" + ("\n".join(files) or "없음") + "\n/get 경로")
        elif text.startswith("/get "):
            self.background(self.send_file(safe_file(self.root, text[5:].strip())))
        elif text.startswith("/answer "):
            parts = text.split(" ", 3)
            event = self.approvals.get(parts[1]) if len(parts) == 4 else None
            if not event or event["method"] != "item/tool/requestUserInput":
                await self.telegram.say(chat, "유효한 질문 코드가 필요합니다: /answer 코드 질문ID 답변")
            else:
                ids = {q["id"] for q in event["params"].get("questions", [])}
                if parts[2] not in ids:
                    raise ValueError("질문 ID를 확인하세요: " + ", ".join(ids))
                answers = event.setdefault("cloudex_answers", {})
                answers[parts[2]] = {"answers": [parts[3]]}
                if ids <= answers.keys():
                    await self.codex.send({"id": event["id"], "result": {"answers": answers}})
                    self.approvals.pop(parts[1])
                    await self.telegram.say(chat, "답변을 전달했습니다.")
                else:
                    await self.telegram.say(chat, "남은 질문: " + ", ".join(ids - answers.keys()))
        elif text.startswith("/form "):
            parts = text.split(" ", 2)
            event = self.approvals.get(parts[1]) if len(parts) == 3 else None
            if not event or event["method"] != "mcpServer/elicitation/request":
                raise ValueError("유효한 MCP 양식 코드가 필요합니다.")
            content = json.loads(parts[2])
            if not isinstance(content, dict):
                raise ValueError("JSON 객체를 입력하세요.")
            schema = event["params"].get("requestedSchema", {})
            validate_form(content, schema)
            await self.codex.send({"id": event["id"], "result": {"action": "accept", "content": content}})
            self.approvals.pop(parts[1])
            LOG.info("MCP form accepted request=%s", parts[1])
            await self.telegram.say(chat, "MCP 양식 답변을 전달했습니다.")
        elif text.startswith("/"):
            await self.telegram.say(chat, "알 수 없는 명령입니다. /help")
        else:
            attachment = next((msg[k] for k in ("document", "video", "audio", "voice", "animation") if k in msg), None)
            if msg.get("photo"):
                attachment = msg["photo"][-1]
            if attachment:
                if self.runtime.deleting:
                    raise ValueError("삭제 중인 세션입니다.")
                self.runtime.transfers += 1
                self.background(self.receive_file(msg, attachment))
            elif text:
                await self.enqueue(text)

    async def receive_file(self, msg, attachment):
        rt = self.runtime
        try:
            path = await self.telegram.download(attachment, rt.root)
            relative = str(path.relative_to(rt.root))
            await self.tell(f"파일 저장 완료: {relative}")
            if msg.get("caption"):
                await self.enqueue(f"사용자 첨부 파일: {relative}\n요청: {msg['caption']}")
        except Exception as exc:
            await self.tell("파일 수신 실패: " + str(exc))
        finally:
            rt.transfers -= 1

    async def send_file(self, path):
        try:
            await self.telegram.send_file(self.owner, path)
        except Exception as exc:
            await self.tell("파일 전송 실패: " + str(exc))

    async def run(self):
        LOG.info("Cloudex %s · multi-session", VERSION)
        web = None
        if self.config.get("web_enabled", True):
            from web import MiniApp
            web = MiniApp(self, asyncio.get_running_loop())
            try:
                web.start()
                LOG.info("Mini App ready on 127.0.0.1:%s", self.config.get("web_port", 8787))
            except OSError as exc:
                LOG.error("Mini App 시작 실패 (%s). 봇 채팅은 계속 실행합니다.", type(exc).__name__)
                web = None
        identity = await self.telegram.api("getMe")
        LOG.info("@%s 연결", identity["username"])
        commands = [("help", "사용법"), ("status", "작업 상태"), ("stop", "현재 작업 중단"),
            ("clear", "현재 세션 대기 작업 취소"), ("sessions", "세션 목록과 전환"),
            ("new", "새 세션: /new 이름"), ("use", "세션 전환: /use ID"),
            ("rename", "세션 이름 변경"), ("archive", "세션 보관"), ("restore", "세션 복원"),
            ("retry", "재시작으로 중단된 작업 재접수"), ("files", "작업 파일 목록"),
            ("get", "파일 받기: /get 경로"), ("pending", "대기 중인 승인·질문"),
            ("settings", "Codex 적용 설정"), ("models", "사용 가능한 모델"),
            ("model", "현재 세션 모델 선택"), ("effort", "현재 세션 추론 수준"),
            ("policy", "현재 세션 승인 정책"), ("app", "Cloudex 미니앱 열기")]
        await self.telegram.api("setMyCommands", {"commands": [{"command": k, "description": v} for k, v in commands]})
        webhook = await self.telegram.api("getWebhookInfo")
        if webhook.get("url"):
            await self.telegram.api("deleteWebhook", {"drop_pending_updates": False})
        menu = await self.telegram.api("getChatMenuButton")
        if menu.get("type") == "web_app":
            self.state["previous_menu_url"] = menu.get("web_app", {}).get("url")
        if self.config.get("public_url") and not self.config.get("quick_tunnel"):
            targets = [{}]
            if self.owner:
                targets.append({"chat_id": self.owner})
            for target in targets:
                await self.telegram.api("setChatMenuButton", {**target, "menu_button": {"type": "web_app",
                    "text": "Cloudex", "web_app": {"url": self.config["public_url"]}}})
        LOG.info("PAIR: /pair %s", self.config["pairing_code"] if not self.owner else "(already paired)")
        self.started = True
        self.persist()
        for sid in self.runtimes:
            self.ensure_worker(sid)
        events = asyncio.create_task(self.event_worker())
        tunnel = tunnel_task = None
        if self.config.get("quick_tunnel") and web:
            from tunnel import QuickTunnel
            tunnel = QuickTunnel(self)
            tunnel_task = asyncio.create_task(tunnel.run())
        try:
            while not self.closing:
                try:
                    updates = await self.telegram.api("getUpdates", {"offset": self.state.get("offset", 0),
                        "timeout": 25, "allowed_updates": ["message", "callback_query"]})
                    for update in updates:
                        try:
                            await self.receive(update)
                        except Exception as exc:
                            LOG.error("메시지 처리 실패: %s", type(exc).__name__)
                            if self.owner and (update.get("message", {}).get("from", {}).get("id") == self.owner):
                                await self.telegram.say(self.owner, "요청 실패: " + str(exc)[:1500])
                        self.state["offset"] = update["update_id"] + 1
                        self.persist()
                except RuntimeError as exc:
                    LOG.warning("%s", exc)
                    await asyncio.sleep(5)
        finally:
            self.started = False
            for worker in self.worker_tasks.values():
                worker.cancel()
            events.cancel()
            for task in self.event_tasks.values():
                task.cancel()
            for task in self.tasks:
                task.cancel()
            if tunnel:
                await tunnel.close()
                tunnel_task.cancel()
                await asyncio.gather(tunnel_task, return_exceptions=True)
            await self.codex.close()
            if web:
                await web.close()
            await asyncio.gather(*self.worker_tasks.values(), events, *self.event_tasks.values(), *self.tasks, return_exceptions=True)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config_path = BASE / "config.json"
    if not config_path.exists():
        raise SystemExit("먼저 python3 setup.py 를 실행하세요.")
    config = json.loads(config_path.read_text())
    bot = Bot(config)
    async def run():
        task = asyncio.create_task(bot.run())
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, task.cancel)
        try:
            await task
        except asyncio.CancelledError:
            pass
    with (BASE / ".cloudex.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Cloudex가 이미 실행 중입니다.")
        asyncio.run(run())


if __name__ == "__main__":
    main()
