import asyncio
import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bot import Bot, Telegram, approval_result, safe_file, validate_form


class FakeTelegram:
    def __init__(self):
        self.messages = []
        self.calls = []
        self.files = []

    async def say(self, chat, text, **kwargs):
        self.messages.append((chat, text, kwargs))

    async def api(self, method, data=None):
        self.calls.append((method, data))
        return {}

    async def send_file(self, chat, path):
        self.files.append((chat, path.read_bytes()))


class FakeCodex:
    def __init__(self):
        self.calls = []
        self.sent = []
        self.thread_count = 0

    async def start(self):
        pass

    async def call(self, method, args):
        self.calls.append((method, args))
        if method in ("thread/start", "thread/resume"):
            self.thread_count += method == "thread/start"
            thread = args.get("threadId", f"thread{self.thread_count}")
            return {"thread": {"id": thread}, "model": "configured-model", "approvalPolicy": "untrusted"}
        if method == "turn/start":
            return {"turn": {"id": "turn1", "status": "inProgress"}}
        if method == "turn/steer":
            return {"turnId": args["expectedTurnId"]}
        return {}

    async def send(self, value):
        self.sent.append(value)


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # This executor denies socketpair.send (EPERM), including asyncio's
        # cross-thread wakeup. A timer keeps local mocked I/O tests progressing.
        selector = asyncio.get_running_loop()._selector
        original_select = selector.select
        selector.select = lambda timeout=None: original_select(0.01 if timeout is None else min(timeout, 0.01))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.bot = Bot({"bot_token": "test", "pairing_code": "pair-secret"}, self.base)
        self.bot.telegram = FakeTelegram()
        self.bot.codex = FakeCodex()

    def update(self, text, uid=42):
        return {"message": {"chat": {"id": uid, "type": "private"}, "from": {"id": uid}, "text": text}}

    async def pair(self):
        await self.bot.receive(self.update("/pair pair-secret"))

    async def test_pair_requires_secret_and_owner_persists(self):
        await self.bot.receive(self.update("hello", 7))
        self.assertIsNone(self.bot.owner)
        await self.pair()
        other = Bot(self.bot.config, self.base)
        self.assertEqual(other.owner, 42)
        self.assertEqual(self.bot.statepath.stat().st_mode & 0o777, 0o600)
        await self.bot.receive(self.update("secret task", 7))
        self.assertTrue(self.bot.queue.empty())

    async def test_busy_job_does_not_block_status_or_queued_job(self):
        await self.pair()
        await self.bot.receive(self.update("first job"))
        worker = asyncio.create_task(self.bot.work())
        try:
            for _ in range(100):
                if any(m == "turn/start" for m, _ in self.bot.codex.calls):
                    break
                await asyncio.sleep(0)
            self.assertEqual(self.bot.queue._unfinished_tasks, 1)
            self.bot.active = "turn1"
            await asyncio.wait_for(self.bot.receive(self.update("second job")), 0.5)
            await asyncio.wait_for(self.bot.receive(self.update("/status")), 0.5)
            self.assertEqual(self.bot.queue.qsize(), 1)
            self.assertIn("실행 중", self.bot.telegram.messages[-1][1])
            self.assertEqual(sum(m == "turn/start" for m, _ in self.bot.codex.calls), 1)
            await self.bot.event({"method": "turn/completed", "params": {"turn": {"status": "completed"}}})
            for _ in range(100):
                if sum(m == "turn/start" for m, _ in self.bot.codex.calls) == 2:
                    break
                await asyncio.sleep(0)
            self.assertEqual(sum(m == "turn/start" for m, _ in self.bot.codex.calls), 2)
        finally:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker

    async def test_native_defaults_are_not_overridden(self):
        await self.pair()
        await self.bot.session()
        args = self.bot.codex.calls[0][1]
        self.assertNotIn("sandbox", args)
        self.assertNotIn("approvalPolicy", args)
        self.assertEqual(self.bot.effective["model"], "configured-model")

    async def test_full_access_is_sent_when_starting_or_resuming_threads(self):
        await self.pair()
        await self.bot.receive(self.update("/policy full-access"))
        await self.bot.session()
        self.assertEqual(self.bot.codex.calls[-1][1]["approvalPolicy"], "never")
        self.assertEqual(self.bot.codex.calls[-1][1]["sandbox"], "danger-full-access")
        self.bot.session_ready = False
        await self.bot.session()
        self.assertEqual(self.bot.codex.calls[-1][0], "thread/resume")
        self.assertEqual(self.bot.codex.calls[-1][1]["sandbox"], "danger-full-access")
        await self.bot.receive(self.update("/policy on-request"))
        self.assertEqual(self.bot.session_state["sandbox"], "workspace-write")

    async def test_full_access_can_be_enabled_and_revoked_on_ready_thread(self):
        await self.pair()
        await self.bot.session()
        worker = asyncio.create_task(self.bot.work())
        try:
            for policy, sandbox in [("full-access", "dangerFullAccess"), ("on-request", "workspaceWrite")]:
                await self.bot.receive(self.update("/policy " + policy))
                count = sum(m == "turn/start" for m, _ in self.bot.codex.calls)
                await self.bot.receive(self.update("task"))
                for _ in range(100):
                    if sum(m == "turn/start" for m, _ in self.bot.codex.calls) > count:
                        break
                    await asyncio.sleep(0)
                args = next(args for method, args in reversed(self.bot.codex.calls) if method == "turn/start")
                self.assertEqual(args["sandboxPolicy"], {"type": sandbox})
                self.assertEqual(args["approvalPolicy"], "never" if policy == "full-access" else "on-request")
                await self.bot.event({"method": "turn/completed", "params": {"turn": {"status": "completed"}}})
                await asyncio.wait_for(self.bot.queue.join(), .5)
        finally:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker

    async def test_new_sessions_inherit_only_explicit_application_defaults(self):
        await self.pair()
        self.bot.state["session_defaults"] = {"approvalPolicy": "never", "sandbox": "danger-full-access"}
        sid = self.bot.create_session("next")
        self.assertEqual(self.bot.runtimes[sid].data["approvalPolicy"], "never")
        self.assertEqual(self.bot.runtimes[sid].data["sandbox"], "danger-full-access")
        self.assertTrue(self.bot.session_state["name"] == "next")

    async def test_approval_requires_owner_and_is_consumed_once(self):
        await self.pair()
        self.bot.approvals["key"] = {"id": 9, "method": "item/commandExecution/requestApproval", "params": {}}
        callback = {"callback_query": {"id": "cb", "from": {"id": 7}, "data": "a:key:accept"}}
        await self.bot.receive(callback)
        self.assertFalse(self.bot.codex.sent)
        callback["callback_query"]["from"]["id"] = 42
        await self.bot.receive(callback)
        self.assertEqual(self.bot.codex.sent, [{"id": 9, "result": {"decision": "accept"}}])
        await self.bot.receive(callback)
        self.assertEqual(len(self.bot.codex.sent), 1)

    async def test_questions_collect_each_answer(self):
        await self.pair()
        self.bot.approvals["key"] = {"id": 9, "method": "item/tool/requestUserInput",
            "params": {"questions": [{"id": "a"}, {"id": "b"}]}}
        await self.bot.receive(self.update("/answer key a first"))
        self.assertFalse(self.bot.codex.sent)
        await self.bot.receive(self.update("/answer key b second"))
        self.assertEqual(self.bot.codex.sent[0]["result"]["answers"],
                         {"a": {"answers": ["first"]}, "b": {"answers": ["second"]}})

    async def test_mcp_form_rejects_missing_required_fields(self):
        await self.pair()
        self.bot.approvals["key"] = {"id": 9, "method": "mcpServer/elicitation/request",
            "params": {"mode": "form", "requestedSchema": {"required": ["name"]}}}
        with self.assertRaises(ValueError):
            await self.bot.receive(self.update('/form key {}'))
        await self.bot.receive(self.update('/form key {"name":"hi"}'))
        self.assertEqual(self.bot.codex.sent[0]["result"], {"action": "accept", "content": {"name": "hi"}})

    async def test_file_export_rejects_traversal_and_symlinks(self):
        await self.pair()
        (self.base / "secret").write_text("private")
        (self.bot.root / "link").symlink_to(self.base / "secret")
        for name in ("../secret", "link", str(self.base / "secret")):
            with self.assertRaises(ValueError):
                safe_file(self.bot.root, name)
        (self.bot.root / "result.txt").write_text("result")
        await self.bot.receive(self.update("/get result.txt"))
        await asyncio.gather(*self.bot.tasks)
        self.assertEqual(self.bot.telegram.files, [(42, b"result")])

    async def test_sending_large_file_does_not_block_receiving_commands(self):
        await self.pair()
        started, finish = asyncio.Event(), asyncio.Event()
        async def send_file(*args):
            started.set()
            await finish.wait()
        self.bot.telegram.send_file = send_file
        (self.bot.root / "large.bin").write_bytes(b"data")
        await asyncio.wait_for(self.bot.receive(self.update("/get large.bin")), 0.5)
        await started.wait()
        await asyncio.wait_for(self.bot.receive(self.update("/status")), 0.5)
        finish.set()
        await asyncio.gather(*self.bot.tasks)

    async def test_received_file_never_overwrites_and_paths_are_sanitized(self):
        telegram = Telegram("test")
        async def api(method, data):
            return {"file_path": "documents/test.txt"}
        telegram.api = api
        import io
        with patch("urllib.request.urlopen", side_effect=lambda *a, **k: io.BytesIO(b"incoming")):
            first = await telegram.download({"file_id": "1", "file_name": "../../file.txt"}, self.bot.root)
            second = await telegram.download({"file_id": "1", "file_name": "../../file.txt"}, self.bot.root)
        self.assertNotEqual(first, second)
        self.assertEqual(first.parent, self.bot.root / "inbox")
        self.assertEqual(first.read_bytes(), b"incoming")

    async def test_upload_directory_symlink_is_rejected(self):
        telegram = Telegram("test")
        async def api(*args):
            return {"file_path": "file.txt"}
        telegram.api = api
        (self.bot.root / "inbox").symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(ValueError):
            await telegram.download({"file_id": "1"}, self.bot.root)

    async def test_permission_grant_cannot_broaden_requested_access(self):
        event = {"method": "item/permissions/requestApproval", "params": {"permissions": {"network": {"enabled": True}}}}
        self.assertEqual(approval_result(event, "accept"),
                         {"permissions": {"network": {"enabled": True}}, "scope": "turn"})
        self.assertEqual(approval_result(event, "decline"), {"permissions": {}, "scope": "turn"})
        form = {"method": "mcpServer/elicitation/request", "params": {"mode": "form"}}
        with self.assertRaises(ValueError):
            approval_result(form, "accept")

    async def test_mcp_schema_type_enum_and_range_checks(self):
        schema = {"type": "object", "required": ["age"], "additionalProperties": False,
                  "properties": {"age": {"type": "integer", "minimum": 18},
                                 "choice": {"type": "string", "enum": ["yes", "no"]}}}
        validate_form({"age": 20, "choice": "yes"}, schema)
        for value in ({"age": "20"}, {"age": True}, {"age": 17}, {"age": 20, "choice": "other"}, {"age": 20, "extra": 1}):
            with self.assertRaises(ValueError):
                validate_form(value, schema)

    async def test_session_models_and_directories_are_independent(self):
        await self.pair()
        await self.bot.receive(self.update('/model model-a'))
        await self.bot.receive(self.update('/new second'))
        second = self.bot.state['selected_session']
        await self.bot.receive(self.update('/model model-b'))
        await self.bot.receive(self.update('/effort high'))
        self.assertEqual(self.bot.runtimes['main'].data['model'], 'model-a')
        self.assertEqual(self.bot.runtimes[second].data['model'], 'model-b')
        self.assertNotEqual(self.bot.runtimes['main'].root, self.bot.runtimes[second].root)
        await self.bot.receive(self.update('/use main'))
        self.assertEqual(self.bot.current_sid, 'main')
        self.assertNotIn('effort', self.bot.session_state)

    async def test_two_sessions_run_concurrently_without_switching_jobs(self):
        await self.pair()
        await self.bot.receive(self.update('/model model-a'))
        await self.bot.receive(self.update('job a'))
        second = self.bot.create_session('second')
        await self.bot.receive(self.update('/model model-b'))
        await self.bot.receive(self.update('job b'))
        workers = [asyncio.create_task(self.bot.work(sid)) for sid in ('main', second)]
        try:
            for _ in range(100):
                if sum(m == 'turn/start' for m, _ in self.bot.codex.calls) == 2:
                    break
                await asyncio.sleep(0)
            starts = [args for method, args in self.bot.codex.calls if method == 'turn/start']
            self.assertEqual(len(starts), 2)
            self.assertEqual({s['input'][0]['text']: s['model'] for s in starts}, {'job a': 'model-a', 'job b': 'model-b'})
            self.assertNotEqual(starts[0]['threadId'], starts[1]['threadId'])
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)


    async def test_new_and_use_session_do_not_wait_for_telegram(self):
        await self.pair()
        gate = asyncio.Event()
        async def stalled(*args, **kwargs):
            await gate.wait()
        self.bot.telegram.say = stalled
        try:
            await asyncio.wait_for(self.bot.receive(self.update("/new parallel")), .5)
            self.assertNotEqual(self.bot.state["selected_session"], "main")
            await asyncio.wait_for(self.bot.receive(self.update("/use main")), .5)
            self.assertEqual(self.bot.state["selected_session"], "main")
            self.assertFalse(gate.is_set())
        finally:
            gate.set()
            await asyncio.gather(*self.bot.tasks, return_exceptions=True)

    async def test_parallel_workers_start_even_when_telegram_notifications_stall(self):
        await self.pair()
        second = self.bot.create_session("second")
        gate = asyncio.Event()
        async def blocked_notification(*args, **kwargs):
            await gate.wait()
        self.bot.telegram.say = blocked_notification
        self.bot.started = True
        try:
            for sid in ("main", second):
                token = self.bot.session_context.set(sid)
                try:
                    await asyncio.wait_for(self.bot.enqueue("job " + sid), .5)
                finally:
                    self.bot.session_context.reset(token)
            for _ in range(100):
                if sum(method == "turn/start" for method, _ in self.bot.codex.calls) == 2:
                    break
                await asyncio.sleep(0)
            starts = [args for method, args in self.bot.codex.calls if method == "turn/start"]
            self.assertEqual(len(starts), 2)
            self.assertNotEqual(starts[0]["threadId"], starts[1]["threadId"])
            self.assertTrue(all(not task.done() for task in self.bot.worker_tasks.values()))
        finally:
            gate.set()
            for task in self.bot.worker_tasks.values():
                task.cancel()
            await asyncio.gather(*self.bot.worker_tasks.values(), *self.bot.tasks, return_exceptions=True)
            self.bot.started = False

    async def test_slow_session_output_does_not_delay_other_session_approval(self):
        await self.pair()
        second = self.bot.create_session("second")
        for sid, thread in [("main", "thread-a"), (second, "thread-b")]:
            self.bot.runtimes[sid].data["thread_id"] = thread
        blocked = asyncio.Event()
        release = asyncio.Event()
        async def notification(chat, text, **kwargs):
            if "slow output" in text:
                blocked.set()
                await release.wait()
        self.bot.telegram.say = notification
        dispatcher = asyncio.create_task(self.bot.event_worker())
        try:
            await self.bot.event({"method": "item/completed", "params": {
                "threadId": "thread-a", "item": {"type": "agentMessage", "text": "slow output"}}})
            await asyncio.wait_for(blocked.wait(), .5)
            await self.bot.event({"id": 90, "method": "item/commandExecution/requestApproval",
                                  "params": {"threadId": "thread-b", "command": "pwd"}})
            for _ in range(100):
                if self.bot.approvals:
                    break
                await asyncio.sleep(0)
            self.assertEqual(len(self.bot.approvals), 1)
            self.assertEqual(self.bot.event_sid(next(iter(self.bot.approvals.values()))), second)
            self.assertFalse(release.is_set())
            release.set()
            await asyncio.wait_for(self.bot.events.join(), .5)
        finally:
            release.set()
            dispatcher.cancel()
            for task in self.bot.event_tasks.values():
                task.cancel()
            await asyncio.gather(dispatcher, *self.bot.event_tasks.values(), return_exceptions=True)

    async def test_session_completion_and_stop_do_not_release_another_session_queue(self):
        await self.pair()
        second = self.bot.create_session("second")
        self.bot.started = True
        try:
            for sid, jobs in [("main", ["a1", "a2"]), (second, ["b1"])]:
                token = self.bot.session_context.set(sid)
                try:
                    for job in jobs:
                        await self.bot.enqueue(job)
                finally:
                    self.bot.session_context.reset(token)
            for _ in range(100):
                if sum(m == "turn/start" for m, _ in self.bot.codex.calls) == 2:
                    break
                await asyncio.sleep(0)
            main_thread = self.bot.runtimes["main"].data["thread_id"]
            other_thread = self.bot.runtimes[second].data["thread_id"]
            await self.bot.event({"method": "turn/started", "params": {"threadId": main_thread, "turn": {"id": "turn-a"}}})
            await self.bot.event({"method": "turn/started", "params": {"threadId": other_thread, "turn": {"id": "turn-b"}}})
            await self.bot.receive(self.update("/stop"))
            interrupt = next(args for m, args in self.bot.codex.calls if m == "turn/interrupt")
            self.assertEqual(interrupt, {"threadId": other_thread, "turnId": "turn-b"})
            await self.bot.event({"method": "turn/completed", "params": {"threadId": other_thread, "turn": {"status": "interrupted"}}})
            await asyncio.wait_for(self.bot.runtimes[second].queue.join(), .5)
            self.assertEqual(sum(m == "turn/start" for m, _ in self.bot.codex.calls), 2)
            self.assertEqual(self.bot.runtimes["main"].queue.qsize(), 1)
            self.assertFalse(self.bot.runtimes["main"].turn_done.is_set())
            await self.bot.event({"method": "turn/completed", "params": {"threadId": main_thread, "turn": {"status": "completed"}}})
            for _ in range(100):
                if sum(m == "turn/start" for m, _ in self.bot.codex.calls) == 3:
                    break
                await asyncio.sleep(0)
            starts = [args for m, args in self.bot.codex.calls if m == "turn/start"]
            self.assertEqual(starts[-1]["input"][0]["text"], "a2")
            self.assertEqual(starts[-1]["threadId"], main_thread)
        finally:
            for task in self.bot.worker_tasks.values():
                task.cancel()
            await asyncio.gather(*self.bot.worker_tasks.values(), *self.bot.tasks, return_exceptions=True)
            self.bot.started = False


    async def test_queue_mode_defers_without_steering_active_turn(self):
        await self.pair()
        self.bot.session_state["thread_id"] = "thread-a"
        self.bot.active = "active-a"
        result = await self.bot.submit_message("later", "queue")
        self.assertEqual(result, {"delivery": "queue"})
        self.assertEqual(self.bot.queue.qsize(), 1)
        self.assertFalse(any(m == "turn/steer" for m, _ in self.bot.codex.calls))
        await asyncio.gather(*self.bot.tasks)

    async def test_immediate_mode_steers_without_consuming_queue(self):
        await self.pair()
        self.bot.session_state.update(thread_id="thread-a", inflight="original")
        self.bot.active = "active-a"
        await self.bot.enqueue("already queued")
        result = await self.bot.submit_message("change direction", "steer", "active-a")
        self.assertEqual(result, {"delivery": "steer", "turnId": "active-a"})
        self.assertEqual(self.bot.queue.qsize(), 1)
        method, args = self.bot.codex.calls[-1]
        self.assertEqual(method, "turn/steer")
        self.assertEqual(args["threadId"], "thread-a")
        self.assertEqual(args["expectedTurnId"], "active-a")
        self.assertEqual(args["input"][0]["text"], "change direction")
        self.assertIn("change direction", self.bot.session_state["inflight"])
        await asyncio.gather(*self.bot.tasks)

    async def test_immediate_mode_starts_when_idle(self):
        await self.pair()
        result = await self.bot.submit_message("start now", "steer")
        self.assertEqual(result, {"delivery": "start"})
        self.assertEqual(self.bot.queue.get_nowait(), "start now")
        self.assertFalse(any(m == "turn/steer" for m, _ in self.bot.codex.calls))
        self.bot.queue.task_done()
        await asyncio.gather(*self.bot.tasks)

    async def test_changed_turn_does_not_receive_correction_or_queue_it(self):
        await self.pair()
        self.bot.session_state["thread_id"] = "thread-a"
        for current in (None, "replacement"):
            self.bot.active = current
            with self.assertRaises(ValueError):
                await self.bot.submit_message("old correction", "steer", "old-turn")
        self.assertEqual(self.bot.queue.qsize(), 0)
        self.assertFalse(self.bot.codex.calls)

    async def test_failed_steer_is_not_requeued_and_preserves_original(self):
        await self.pair()
        self.bot.session_state.update(thread_id="thread-a", inflight="original")
        self.bot.active = "active-a"
        async def failure(method, args):
            raise RuntimeError("turn completed")
        self.bot.codex.call = failure
        with self.assertRaises(RuntimeError):
            await self.bot.submit_message("correction", "steer", "active-a")
        self.assertEqual(self.bot.queue.qsize(), 0)
        self.assertEqual(self.bot.session_state["inflight"], "original")

    async def test_immediate_mode_waits_for_a_starting_turn(self):
        await self.pair()
        preparing, release = asyncio.Event(), asyncio.Event()
        original = self.bot.codex.call
        async def slow_start(method, args):
            if method == "thread/start":
                preparing.set()
                await release.wait()
            return await original(method, args)
        self.bot.codex.call = slow_start
        self.bot.started = True
        correction = None
        try:
            await self.bot.enqueue("first")
            await asyncio.wait_for(preparing.wait(), .5)
            correction = asyncio.create_task(self.bot.submit_message("correction", "steer"))
            await asyncio.sleep(0)
            self.assertFalse(correction.done())
            release.set()
            result = await asyncio.wait_for(correction, .5)
            self.assertEqual(result["delivery"], "steer")
            self.assertEqual(self.bot.queue.qsize(), 0)
            self.assertEqual(sum(m == "turn/start" for m, _ in self.bot.codex.calls), 1)
            self.assertEqual(sum(m == "turn/steer" for m, _ in self.bot.codex.calls), 1)
        finally:
            release.set()
            if correction and not correction.done():
                correction.cancel()
            for task in self.bot.worker_tasks.values():
                task.cancel()
            await asyncio.gather(*self.bot.worker_tasks.values(), *self.bot.tasks, *([correction] if correction else []), return_exceptions=True)
            self.bot.started = False

    async def test_full_queue_rejects_extra_message(self):
        await self.pair()
        for index in range(20):
            await self.bot.enqueue(str(index))
        with self.assertRaises(ValueError):
            await self.bot.submit_message("extra", "queue")
        self.assertEqual(self.bot.queue.qsize(), 20)
        self.assertEqual(len(self.bot.session_state["pending"]), 20)
        await asyncio.gather(*self.bot.tasks)

    async def test_one_session_completion_preserves_other_approvals(self):
        await self.pair()
        second = self.bot.create_session('second')
        for sid, thread in [('main', 'thread-a'), (second, 'thread-b')]:
            self.bot.runtimes[sid].data['thread_id'] = thread
        self.bot.approvals['b'] = {'id': 9, 'method': 'item/commandExecution/requestApproval',
            'params': {'threadId': 'thread-b'}, 'cloudex_session': second}
        token = self.bot.session_context.set('main')
        try:
            await self.bot.deliver_event({'method': 'turn/completed', 'params': {'threadId': 'thread-a', 'turn': {}}})
        finally:
            self.bot.session_context.reset(token)
        self.assertIn('b', self.bot.approvals)
        await self.bot.decide('b', 'accept')
        self.assertIn(second, self.bot.telegram.messages[-1][1])

    async def test_files_keep_the_original_session_during_upload(self):
        await self.pair()
        started, finish = asyncio.Event(), asyncio.Event()
        async def download(attachment, root):
            started.set()
            await finish.wait()
            path = root / 'input.txt'
            path.write_text('data')
            return path
        self.bot.telegram.download = download
        update = self.update('')
        update['message']['document'] = {'file_id': 'x'}
        update['message']['caption'] = 'process file'
        await self.bot.receive(update)
        await started.wait()
        second = self.bot.create_session('second')
        finish.set()
        await asyncio.gather(*self.bot.tasks)
        self.assertTrue((self.bot.runtimes['main'].root / 'input.txt').exists())
        self.assertFalse((self.bot.runtimes[second].root / 'input.txt').exists())
        self.assertEqual(self.bot.runtimes['main'].queue.qsize(), 1)
        self.assertEqual(self.bot.runtimes[second].queue.qsize(), 0)

    async def test_old_state_migrates_and_pending_jobs_survive_restart(self):
        from bot import save_json
        save_json(self.bot.statepath, {'owner_id': 42, 'thread_id': 'old-thread', 'model': 'old-model', 'offset': 100})
        migrated = Bot(self.bot.config, self.base)
        self.assertEqual(migrated.owner, 42)
        self.assertEqual(migrated.session_state['thread_id'], 'old-thread')
        self.assertEqual(migrated.session_state['model'], 'old-model')
        migrated.telegram = FakeTelegram()
        await migrated.enqueue('pending work')
        migrated.session_state['inflight'] = 'partially completed work'
        migrated.persist()
        restarted = Bot(self.bot.config, self.base)
        self.assertEqual(restarted.queue.get_nowait(), 'pending work')
        self.assertEqual(restarted.session_state['interrupted_job'], 'partially completed work')


    async def test_delete_session_preserves_files_and_other_session(self):
        await self.pair()
        second = self.bot.create_session("remove")
        rt = self.bot.runtimes[second]
        rt.data["thread_id"] = "deleted-thread"
        (rt.root / "keep.txt").write_text("keep")
        self.bot.runtimes["main"].data["model"] = "keep-model"
        await self.bot.delete_session(second)
        self.assertNotIn(second, self.bot.runtimes)
        self.assertNotIn(second, self.bot.state["sessions"])
        self.assertEqual(self.bot.state["selected_session"], "main")
        self.assertEqual(self.bot.runtimes["main"].data["model"], "keep-model")
        self.assertEqual((rt.root / "keep.txt").read_text(), "keep")
        self.assertIn(("thread/archive", {"threadId": "deleted-thread"}), self.bot.codex.calls)
        saved = json.loads(self.bot.statepath.read_text())
        self.assertNotIn(second, saved["sessions"])
        self.assertIn("deleted-thread", saved["deleted_threads"])

    async def test_deleting_last_session_creates_empty_replacement_with_defaults(self):
        await self.pair()
        self.bot.state["session_defaults"] = {"approvalPolicy": "never", "sandbox": "danger-full-access"}
        original_root = self.bot.root
        (original_root / "keep.txt").write_text("original")
        await self.bot.delete_session("main")
        sid = self.bot.state["selected_session"]
        self.assertNotEqual(sid, "main")
        self.assertEqual(list(self.bot.runtimes), [sid])
        self.assertEqual(self.bot.runtimes[sid].data["approvalPolicy"], "never")
        self.assertNotIn("thread_id", self.bot.runtimes[sid].data)
        self.assertEqual((original_root / "keep.txt").read_text(), "original")

    async def test_busy_and_uploading_sessions_cannot_be_deleted(self):
        await self.pair()
        self.bot.create_session("other")
        rt = self.bot.runtimes["main"]
        rt.active = "active"
        with self.assertRaises(ValueError):
            await self.bot.delete_session("main")
        rt.active = None
        rt.transfers = 1
        with self.assertRaises(ValueError):
            await self.bot.delete_session("main")
        rt.transfers = 0
        token = self.bot.session_context.set("main")
        try:
            await self.bot.enqueue("pending")
        finally:
            self.bot.session_context.reset(token)
        with self.assertRaises(ValueError):
            await self.bot.delete_session("main")
        self.assertIn("main", self.bot.runtimes)
        self.assertFalse(rt.deleting)
        await asyncio.gather(*self.bot.tasks)

    async def test_failed_delete_keeps_metadata_and_unlocks_session(self):
        await self.pair()
        rt = self.bot.runtime
        rt.data["thread_id"] = "native-thread"
        async def failure(*args):
            raise RuntimeError("archive unavailable")
        self.bot.codex.call = failure
        with self.assertRaises(RuntimeError):
            await self.bot.delete_session("main")
        self.assertIs(self.bot.runtimes["main"], rt)
        self.assertFalse(rt.deleting)
        self.assertNotIn("native-thread", self.bot.deleted_threads)

    async def test_failed_delete_persistence_restores_native_archive(self):
        await self.pair()
        rt = self.bot.runtime
        rt.data["thread_id"] = "native-thread"
        with patch("bot.save_json", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                await self.bot.delete_session("main")
        self.assertIs(self.bot.runtimes["main"], rt)
        self.assertFalse(rt.deleting)
        self.assertEqual(self.bot.codex.calls[-1], ("thread/unarchive", {"threadId": "native-thread"}))

    async def test_deleted_session_events_cannot_change_surviving_session(self):
        await self.pair()
        self.bot.session_state["thread_id"] = "old-thread"
        second = self.bot.create_session("keep")
        await self.bot.delete_session("main")
        rt = self.bot.runtimes[second]
        rt.data["thread_id"] = "keep-thread"
        rt.active = "keep-turn"
        await self.bot.event({"method": "turn/completed", "params": {"threadId": "old-thread", "turn": {"status": "completed"}}})
        self.assertEqual(rt.active, "keep-turn")
        self.assertFalse(rt.turn_done.is_set())
        await self.bot.event({"id": 777, "method": "item/commandExecution/requestApproval", "params": {"threadId": "old-thread"}})
        self.assertEqual(self.bot.codex.sent[-1]["id"], 777)
        self.assertIn("error", self.bot.codex.sent[-1])
        token = self.bot.session_context.set("main")
        try:
            await self.bot.event({"method": "cloudex/disconnected", "params": {}})
        finally:
            self.bot.session_context.reset(token)
        self.assertIsNone(rt.active)
        self.assertTrue(rt.turn_done.is_set())

    async def test_deletion_stops_old_event_delivery_without_blocking_other_sessions(self):
        await self.pair()
        second = self.bot.create_session("remove")
        self.bot.runtimes[second].data["thread_id"] = "remove-thread"
        blocked, release = asyncio.Event(), asyncio.Event()
        async def notification(*args, **kwargs):
            blocked.set()
            await release.wait()
        self.bot.telegram.say = notification
        dispatcher = asyncio.create_task(self.bot.event_worker())
        try:
            await self.bot.event({"method": "item/completed", "params": {"threadId": "remove-thread", "item": {"type": "agentMessage", "text": "old"}}})
            await asyncio.wait_for(blocked.wait(), .5)
            await self.bot.event({"method": "item/completed", "params": {"threadId": "remove-thread", "item": {"type": "agentMessage", "text": "queued"}}})
            await self.bot.delete_session(second)
            await asyncio.wait_for(self.bot.events.join(), .5)
            self.assertNotIn(second, self.bot.event_tasks)
            self.assertNotIn(second, self.bot.session_events)
        finally:
            release.set()
            dispatcher.cancel()
            for task in self.bot.event_tasks.values():
                task.cancel()
            await asyncio.gather(dispatcher, *self.bot.event_tasks.values(), return_exceptions=True)

    async def test_concurrent_deletes_leave_one_valid_session_and_never_reuse_files(self):
        await self.pair()
        second = self.bot.create_session("other")
        root = self.bot.runtimes[second].root
        (root / "keep.txt").write_text("private")
        await asyncio.gather(self.bot.delete_session("main"), self.bot.delete_session(second))
        self.assertEqual(len(self.bot.runtimes), 1)
        self.assertIn(self.bot.state["selected_session"], self.bot.runtimes)
        self.assertEqual((root / "keep.txt").read_text(), "private")
        with patch("bot.secrets.token_hex", side_effect=[second, "abcdef"]):
            sid = self.bot.create_session("new")
        self.assertEqual(sid, "abcdef")
        self.assertNotEqual(self.bot.runtimes[sid].root, root)

    async def test_archive_refuses_busy_or_last_session(self):
        await self.pair()
        with self.assertRaises(ValueError):
            await self.bot.receive(self.update('/archive'))
        self.bot.create_session('second')
        await self.bot.receive(self.update('/use main'))
        await self.bot.enqueue('busy')
        with self.assertRaises(ValueError):
            await self.bot.receive(self.update('/archive main'))


if __name__ == "__main__":
    unittest.main()
