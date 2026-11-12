import asyncio
import json
import unittest
from unittest.mock import patch

from bot import Bot
import test_bot as fixtures
from web import dispatch


class QueueTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.BotTests.asyncSetUp

    async def asyncTearDown(self):
        await asyncio.gather(*self.bot.tasks, return_exceptions=True)

    async def fill(self, *texts):
        for text in texts:
            await self.bot.enqueue(text)
        return (await dispatch(self.bot, "/api/queue", {}))["items"]

    async def test_stable_ids_distinguish_duplicates_and_survive_restart(self):
        items = await self.fill("same", "same")
        self.assertNotEqual(items[0]["id"], items[1]["id"])
        restarted = Bot(self.bot.config, self.base)
        self.assertEqual(restarted.session_state["pending_ids"], [item["id"] for item in items])
        await dispatch(self.bot, "/api/queue/delete", {"id": items[1]["id"]})
        self.assertEqual(self.bot.session_state["pending_ids"], [items[0]["id"]])
        self.assertEqual(list(self.bot.queue._queue), ["same"])

    async def test_legacy_pending_text_is_migrated_without_loss(self):
        self.bot.state["sessions"]["main"]["pending"] = ["first", "second"]
        self.bot.state["sessions"]["main"].pop("pending_ids", None)
        self.bot.persist()
        restarted = Bot(self.bot.config, self.base)
        self.assertEqual(list(restarted.queue._queue), ["first", "second"])
        self.assertEqual(len(restarted.session_state["pending_ids"]), 2)

    async def test_delete_preserves_running_job_and_queue_accounting(self):
        items = await self.fill("first", "second")
        self.bot.runtime.active = "running"
        self.bot.session_state["inflight"] = "original"
        result = await dispatch(self.bot, "/api/queue/delete", {"id": items[0]["id"]})
        self.assertEqual([item["text"] for item in result["items"]], ["second"])
        self.assertEqual(self.bot.runtime.active, "running")
        self.assertEqual(self.bot.session_state["inflight"], "original")
        self.assertEqual(self.bot.queue._unfinished_tasks, 1)
        await dispatch(self.bot, "/api/queue/delete", {"id": items[1]["id"]})
        await asyncio.wait_for(self.bot.queue.join(), .5)

    async def test_stale_id_never_removes_a_different_request(self):
        items = await self.fill("first", "second")
        await self.bot.queued.delete(items[0]["id"])
        with self.assertRaises(ValueError):
            await self.bot.queued.delete(items[0]["id"])
        self.assertEqual(list(self.bot.queue._queue), ["second"])

    async def test_delete_persistence_failure_leaves_request_unchanged(self):
        items = await self.fill("keep")
        with patch.object(self.bot, "persist", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                await self.bot.queued.delete(items[0]["id"])
        self.assertEqual(self.bot.session_state["pending"], ["keep"])
        self.assertEqual(list(self.bot.queue._queue), ["keep"])
        self.assertEqual(self.bot.queue._unfinished_tasks, 1)

    async def test_edit_preserves_identity_order_accounting_and_survives_restart(self):
        items = await self.fill("same", "same", "last")
        self.bot.runtime.active = "running"
        self.bot.session_state["inflight"] = "original"
        result = await dispatch(self.bot, "/api/queue/edit", {
            "id": items[1]["id"], "text": "  revised\nrequest  ", "expectedText": "same"})
        self.assertEqual([row["id"] for row in result["items"]], [row["id"] for row in items])
        self.assertEqual([row["text"] for row in result["items"]], ["same", "revised\nrequest", "last"])
        self.assertEqual(list(self.bot.queue._queue), ["same", "revised\nrequest", "last"])
        self.assertEqual(self.bot.queue._unfinished_tasks, 3)
        self.assertTrue(self.bot.queue.available.is_set())
        self.assertEqual(self.bot.runtime.active, "running")
        self.assertEqual(self.bot.session_state["inflight"], "original")
        restarted = Bot(self.bot.config, self.base)
        self.assertEqual(list(restarted.queue._queue), ["same", "revised\nrequest", "last"])
        self.assertEqual(restarted.session_state["pending_ids"], [row["id"] for row in items])

    async def test_invalid_or_stale_edit_does_not_change_waiting_requests(self):
        items = await self.fill("keep", "other")
        for text, expected in [(None, "keep"), (42, "keep"), (" \n ", "keep"),
                               ("x" * 30001, "keep"), ("new", None), ("new", "old")]:
            with self.assertRaises(ValueError):
                await dispatch(self.bot, "/api/queue/edit", {
                    "id": items[0]["id"], "text": text, "expectedText": expected})
        await self.bot.queued.delete(items[0]["id"])
        with self.assertRaises(ValueError):
            await self.bot.queued.edit(items[0]["id"], "new", "keep")
        self.assertEqual(list(self.bot.queue._queue), ["other"])

    async def test_edit_persistence_failure_leaves_both_copies_unchanged(self):
        items = await self.fill("keep")
        with patch.object(self.bot, "persist", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                await self.bot.queued.edit(items[0]["id"], "new", "keep")
        self.assertEqual(self.bot.session_state["pending"], ["keep"])
        self.assertEqual(list(self.bot.queue._queue), ["keep"])
        self.assertEqual(self.bot.queue._unfinished_tasks, 1)

    async def test_edit_is_session_scoped_and_archived_edits_are_rejected(self):
        items = await self.fill("main job")
        other = self.bot.create_session("other")
        await self.fill("other job")
        await dispatch(self.bot, "/api/queue/edit", {
            "session": "main", "id": items[0]["id"], "text": "main revised", "expectedText": "main job"})
        self.assertEqual(self.bot.state["selected_session"], other)
        self.assertEqual(list(self.bot.queue._queue), ["other job"])
        self.assertEqual(list(self.bot.runtimes["main"].queue._queue), ["main revised"])
        self.bot.runtimes["main"].data["archived"] = True
        with self.assertRaises(ValueError):
            await dispatch(self.bot, "/api/queue/edit", {
                "session": "main", "id": items[0]["id"], "text": "new", "expectedText": "main revised"})

    async def test_concurrent_edit_rejects_overwrite_and_worker_executes_saved_content(self):
        items = await self.fill("original")
        edits = await asyncio.gather(
            self.bot.queued.edit(items[0]["id"], "first revision", "original"),
            self.bot.queued.edit(items[0]["id"], "second revision", "original"),
            return_exceptions=True)
        self.assertEqual(sum(isinstance(result, ValueError) for result in edits), 1)
        saved = self.bot.session_state["pending"][0]
        worker = asyncio.create_task(self.bot.work("main"))
        try:
            for _ in range(100):
                if self.bot.active:
                    break
                await asyncio.sleep(.001)
            turns = [args for method, args in self.bot.codex.calls if method == "turn/start"]
            self.assertEqual([args["input"][0]["text"] for args in turns], [saved])
            with self.assertRaises(ValueError):
                await self.bot.queued.edit(items[0]["id"], "too late", saved)
            await self.bot.event({"method": "turn/completed", "params": {
                "threadId": self.bot.session_state["thread_id"], "turn": {"status": "completed"}}})
            await asyncio.wait_for(self.bot.queue.join(), .5)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_apply_to_running_turn_removes_only_selected_request(self):
        items = await self.fill("later", "apply me")
        self.bot.runtime.active = "turn-a"
        self.bot.session_state.update(thread_id="thread-a", inflight="original")
        result = await dispatch(self.bot, "/api/queue/apply", {"id": items[1]["id"], "expectedTurnId": "turn-a"})
        self.assertEqual(result["delivery"], "steer")
        self.assertEqual(list(self.bot.queue._queue), ["later"])
        self.assertEqual(self.bot.codex.calls[-1][0], "turn/steer")
        self.assertEqual(self.bot.codex.calls[-1][1]["input"][0]["text"], "apply me")
        self.assertIn("apply me", self.bot.session_state["inflight"])
        restarted = Bot(self.bot.config, self.base)
        self.assertEqual(list(restarted.queue._queue), ["later"])

    async def test_reorder_preserves_ids_text_and_accounting_across_restart(self):
        items = await self.fill("same", "same", "last")
        ids = [item["id"] for item in items]
        self.bot.runtime.active = "running"
        self.bot.session_state["inflight"] = "original"
        result = await dispatch(self.bot, "/api/queue/reorder", {"ids": [ids[2], ids[0], ids[1]], "expectedIds": ids})
        self.assertEqual([row["id"] for row in result["items"]], [ids[2], ids[0], ids[1]])
        self.assertEqual(list(self.bot.queue._queue), ["last", "same", "same"])
        self.assertEqual(self.bot.queue._unfinished_tasks, 3)
        self.assertTrue(self.bot.queue.available.is_set())
        self.assertEqual(self.bot.runtime.active, "running")
        self.assertEqual(self.bot.session_state["inflight"], "original")
        restarted = Bot(self.bot.config, self.base)
        self.assertEqual(restarted.session_state["pending_ids"], [ids[2], ids[0], ids[1]])
        self.assertEqual(list(restarted.queue._queue), ["last", "same", "same"])

    async def test_reorder_rejects_invalid_permutations_and_stale_orders(self):
        items = await self.fill("first", "second")
        ids = [item["id"] for item in items]
        for values in [None, "bad", [ids[0]], [ids[0], ids[0]], [ids[0], "missing"], [None, ids[1]],
                       [1, ids[1]], [[]], ["x" * 129], [str(i) for i in range(21)]]:
            for new_ids, expected_ids in [(values, ids), (ids, values)]:
                with self.assertRaises(ValueError):
                    await self.bot.queued.reorder(new_ids, expected_ids)
        await self.bot.enqueue("new request")
        with self.assertRaisesRegex(ValueError, "대기열이 바뀌었습니다"):
            await self.bot.queued.reorder(ids[::-1], ids)
        self.assertEqual(list(self.bot.queue._queue), ["first", "second", "new request"])
        current_ids = list(self.bot.session_state["pending_ids"])
        await self.bot.queued.delete(ids[0])
        with self.assertRaises(ValueError):
            await self.bot.queued.reorder(current_ids[::-1], current_ids)
        self.assertEqual(list(self.bot.queue._queue), ["second", "new request"])

    async def test_reorder_persistence_failure_keeps_original_order(self):
        items = await self.fill("first", "second")
        ids = [item["id"] for item in items]
        with patch.object(self.bot, "persist", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                await self.bot.queued.reorder(ids[::-1], ids)
        self.assertEqual(self.bot.session_state["pending_ids"], ids)
        self.assertEqual(self.bot.session_state["pending"], ["first", "second"])
        self.assertEqual(list(self.bot.queue._queue), ["first", "second"])
        self.assertEqual(self.bot.queue._unfinished_tasks, 2)

    async def test_reorder_uses_latest_edited_text_and_is_session_scoped(self):
        items = await self.fill("first", "second")
        ids = [item["id"] for item in items]
        await self.bot.queued.edit(ids[0], "edited", "first")
        other = self.bot.create_session("other")
        await self.fill("other job")
        await dispatch(self.bot, "/api/queue/reorder", {"session": "main", "ids": ids[::-1], "expectedIds": ids})
        self.assertEqual(list(self.bot.runtimes["main"].queue._queue), ["second", "edited"])
        self.assertEqual(self.bot.state["selected_session"], other)
        self.assertEqual(list(self.bot.queue._queue), ["other job"])
        self.bot.runtimes["main"].data["archived"] = True
        with self.assertRaises(ValueError):
            await dispatch(self.bot, "/api/queue/reorder", {"session": "main", "ids": ids, "expectedIds": ids[::-1]})

    async def test_worker_executes_new_order_and_stale_drag_cannot_restore_running_item(self):
        items = await self.fill("first", "second")
        ids = [item["id"] for item in items]
        await self.bot.queued.reorder(ids[::-1], ids)
        worker = asyncio.create_task(self.bot.work("main"))
        try:
            for text in ["second", "first"]:
                for _ in range(100):
                    if self.bot.active:
                        break
                    await asyncio.sleep(.001)
                turns = [args for method, args in self.bot.codex.calls if method == "turn/start"]
                self.assertEqual(turns[-1]["input"][0]["text"], text)
                with self.assertRaises(ValueError):
                    await self.bot.queued.reorder(ids, ids[::-1])
                await self.bot.event({"method": "turn/completed", "params": {
                    "threadId": self.bot.session_state["thread_id"], "turn": {"status": "completed"}}})
                await asyncio.sleep(.005)
            await asyncio.wait_for(self.bot.queue.join(), .5)
            self.assertEqual(self.bot.session_state["pending_ids"], [])
            self.assertFalse(self.bot.queue.available.is_set())
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_idle_apply_prioritizes_without_duplicating_queue_entries(self):
        items = await self.fill("first", "second", "chosen")
        result = await dispatch(self.bot, "/api/queue/apply", {"id": items[2]["id"]})
        self.assertEqual(result["delivery"], "start")
        self.assertEqual(list(self.bot.queue._queue), ["chosen", "first", "second"])
        self.assertEqual(self.bot.queue._unfinished_tasks, 3)
        self.assertEqual(result["items"][0]["id"], items[2]["id"])

    async def test_changed_turn_is_rejected_without_queue_mutation(self):
        items = await self.fill("keep")
        self.bot.runtime.active = "new-turn"
        with self.assertRaises(ValueError):
            await self.bot.queued.apply(items[0]["id"], "old-turn")
        self.assertEqual(list(self.bot.queue._queue), ["keep"])
        self.assertFalse(any(method == "turn/steer" for method, _ in self.bot.codex.calls))

    async def test_failed_apply_restores_original_order_and_persisted_ids(self):
        items = await self.fill("first", "chosen", "last")
        self.bot.runtime.active = "turn-a"
        self.bot.session_state["thread_id"] = "thread-a"
        async def reject(*args):
            raise RuntimeError("rejected")
        self.bot.codex.call = reject
        with self.assertRaises(RuntimeError):
            await self.bot.queued.apply(items[1]["id"], "turn-a")
        self.assertEqual(list(self.bot.queue._queue), ["first", "chosen", "last"])
        self.assertEqual(self.bot.queue._unfinished_tasks, 3)
        saved = json.loads(self.bot.statepath.read_text())["sessions"]["main"]
        self.assertEqual(saved["pending_ids"], [item["id"] for item in items])

    async def test_post_accept_save_failure_does_not_requeue_applied_request(self):
        items = await self.fill("chosen")
        self.bot.runtime.active = "turn-a"
        self.bot.session_state.update(thread_id="thread-a", inflight="original")
        original, calls = self.bot.persist, 0
        def persist():
            nonlocal calls
            calls += 1
            if calls > 1:
                raise OSError("disk")
            original()
        with patch.object(self.bot, "persist", side_effect=persist), self.assertLogs("cloudex", level="WARNING"):
            result = await self.bot.queued.apply(items[0]["id"], "turn-a")
        self.assertEqual(result["delivery"], "steer")
        self.assertTrue(self.bot.queue.empty())
        self.assertEqual(json.loads(self.bot.statepath.read_text())["sessions"]["main"]["pending"], [])

    async def test_session_target_is_pinned_and_clear_removes_ids(self):
        main = await self.fill("main job")
        other = self.bot.create_session("other")
        result = await dispatch(self.bot, "/api/queue", {"session": "main"})
        self.assertEqual(result["items"], main)
        await dispatch(self.bot, "/api/queue/delete", {"session": "main", "id": main[0]["id"]})
        self.assertEqual(self.bot.state["selected_session"], other)
        self.assertTrue(self.bot.runtimes[other].queue.empty())
        await self.fill("clear me")
        self.bot.state["owner_id"] = 42
        await self.bot.receive_in_session(fixtures.BotTests.update(self, "/clear"))
        self.assertEqual(self.bot.session_state["pending_ids"], [])
        self.assertFalse(self.bot.queue.available.is_set())
        await asyncio.wait_for(self.bot.queue.join(), .5)

    async def test_worker_cannot_start_waiting_jobs_during_apply(self):
        await self.fill("original")
        worker = asyncio.create_task(self.bot.work("main"))
        entered, release = asyncio.Event(), asyncio.Event()
        try:
            for _ in range(100):
                if self.bot.active:
                    break
                await asyncio.sleep(.001)
            items = await self.fill("later", "chosen")
            original = self.bot.codex.call
            async def delayed(method, params):
                if method == "turn/steer":
                    entered.set()
                    await release.wait()
                return await original(method, params)
            self.bot.codex.call = delayed
            applying = asyncio.create_task(dispatch(self.bot, "/api/queue/apply", {"id": items[1]["id"], "expectedTurnId": self.bot.active}))
            await asyncio.wait_for(entered.wait(), .5)
            await self.bot.event({"method": "turn/completed", "params": {"threadId": self.bot.session_state["thread_id"], "turn": {"status": "completed"}}})
            await asyncio.sleep(.01)
            self.assertEqual(sum(method == "turn/start" for method, _ in self.bot.codex.calls), 1)
            release.set(); await applying
            for _ in range(100):
                if sum(method == "turn/start" for method, _ in self.bot.codex.calls) == 2:
                    break
                await asyncio.sleep(.001)
            turns = [params for method, params in self.bot.codex.calls if method == "turn/start"]
            self.assertEqual([params["input"][0]["text"] for params in turns], ["original", "later"])
            await self.bot.event({"method": "turn/completed", "params": {"threadId": self.bot.session_state["thread_id"], "turn": {"status": "completed"}}})
            await asyncio.wait_for(self.bot.queue.join(), .5)
        finally:
            release.set()
            worker.cancel(); await asyncio.gather(worker, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
