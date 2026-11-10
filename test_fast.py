import asyncio
import unittest
from unittest.mock import patch

from bot import fast_service_tier
import test_bot as fixtures
from web import dispatch


class FastTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.BotTests.asyncSetUp(self)
        self.config = {"model": "fast-model", "service_tier": None}
        self.models = [{"id": "fast-model", "model": "fast-model", "isDefault": True,
                        "serviceTiers": [{"id": "priority", "name": "Fast"}]},
                       {"id": "slow-model", "model": "slow-model", "serviceTiers": []}]
        original = self.bot.codex.call
        async def call(method, params):
            result = await original(method, params)
            if method == "config/read":
                return {"config": self.config}
            if method == "model/list":
                return {"data": self.models, "nextCursor": None}
            return result
        self.bot.codex.call = call

    async def asyncTearDown(self):
        await asyncio.gather(*self.bot.tasks, return_exceptions=True)

    def test_catalog_tiers_and_legacy_support(self):
        self.assertEqual(fast_service_tier(self.models[0]), "priority")
        self.assertEqual(fast_service_tier({"serviceTiers": [{"id": "fast"}]}), "fast")
        self.assertEqual(fast_service_tier({"additionalSpeedTiers": ["fast"]}), "priority")
        self.assertIsNone(fast_service_tier({"serviceTiers": [{"id": "ultrafast"}]}))
        self.assertIsNone(fast_service_tier(self.models[1]))

    async def test_models_include_configured_defaults_for_target_session(self):
        other = self.bot.create_session("other")
        self.config["service_tier"] = "priority"
        result = await dispatch(self.bot, "/api/models", {"session": "main"})
        self.assertEqual(result["defaultModel"], "fast-model")
        self.assertEqual(result["defaultServiceTier"], "priority")
        config_call = next(params for method, params in self.bot.codex.calls if method == "config/read")
        self.assertEqual(config_call["cwd"], str(self.bot.runtimes["main"].root))
        self.assertEqual(self.bot.state["selected_session"], other)

    async def test_on_is_saved_per_session_and_sent_to_thread_and_turn(self):
        result = await dispatch(self.bot, "/api/settings", {"fastMode": True})
        self.assertEqual(result["serviceTier"], "priority")
        self.assertEqual(self.bot.session_state["serviceTier"], "priority")
        other = self.bot.create_session("other")
        self.assertNotIn("serviceTier", self.bot.runtimes[other].data)
        token = self.bot.session_context.set("main")
        worker = None
        try:
            await self.bot.enqueue("work")
            worker = asyncio.create_task(self.bot.work("main"))
            for _ in range(100):
                if self.bot.runtimes["main"].active:
                    break
                await asyncio.sleep(.001)
            calls = dict(self.bot.codex.calls)
            self.assertEqual(calls["thread/start"]["serviceTier"], "priority")
            self.assertEqual(calls["turn/start"]["serviceTier"], "priority")
            self.assertEqual(self.bot.runtimes["main"].effective["serviceTier"], "priority")
            status = await dispatch(self.bot, "/api/status", {})
            self.assertEqual(next(session for session in status["sessions"] if session["id"] == "main")["serviceTier"], "priority")
        finally:
            if worker:
                worker.cancel(); await asyncio.gather(worker, return_exceptions=True)
            self.bot.session_context.reset(token)

    async def test_off_explicitly_overrides_global_fast_on_resumed_thread(self):
        self.config["service_tier"] = "priority"
        self.bot.session_state["thread_id"] = "existing-thread"
        result = await dispatch(self.bot, "/api/settings", {"fastMode": False})
        self.assertEqual(result["serviceTier"], "default")
        await self.bot.session()
        self.assertEqual(self.bot.codex.calls[-1], ("thread/resume", {
            "cwd": str(self.bot.root), "threadId": "existing-thread", "serviceTier": "default"}))

    async def test_unsupported_on_rejects_all_changes(self):
        with self.assertRaises(ValueError):
            await dispatch(self.bot, "/api/settings", {"model": "slow-model", "approvalPolicy": "full-access", "fastMode": True})
        for key in ("model", "serviceTier", "sandbox", "approvalPolicy"):
            self.assertNotIn(key, self.bot.session_state)

    async def test_non_boolean_fast_values_are_rejected(self):
        for value in ("true", 1, None, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                await dispatch(self.bot, "/api/settings", {"fastMode": value})
        self.assertNotIn("serviceTier", self.bot.session_state)

    async def test_unsupported_model_change_requires_explicit_off(self):
        await dispatch(self.bot, "/api/settings", {"fastMode": True})
        with self.assertRaises(ValueError):
            await dispatch(self.bot, "/api/settings", {"model": "slow-model"})
        result = await dispatch(self.bot, "/api/settings", {"model": "slow-model", "fastMode": False})
        self.assertEqual(result["serviceTier"], "default")
        self.assertEqual(self.bot.session_state["model"], "slow-model")

    async def test_untouched_setting_preserves_inherited_service_tier(self):
        self.config["service_tier"] = "priority"
        await dispatch(self.bot, "/api/settings", {"effort": "high"})
        self.assertNotIn("serviceTier", self.bot.session_state)
        await self.bot.session()
        self.assertNotIn("serviceTier", self.bot.codex.calls[-1][1])

    async def test_failed_save_does_not_enable_fast_in_memory(self):
        with patch.object(self.bot, "persist", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                await dispatch(self.bot, "/api/settings", {"fastMode": True})
        self.assertNotIn("serviceTier", self.bot.session_state)

    async def test_model_lookup_supports_pagination(self):
        original = self.bot.codex.call
        async def paged(method, params):
            result = await original(method, params)
            if method == "model/list":
                return {"data": [self.models[0] if params.get("cursor") else self.models[1]],
                        "nextCursor": None if params.get("cursor") else "next"}
            return result
        self.bot.codex.call = paged
        result = await dispatch(self.bot, "/api/settings", {"fastMode": True})
        self.assertEqual(result["serviceTier"], "priority")
        self.assertEqual(sum(method == "config/read" for method, _ in self.bot.codex.calls), 1)

    async def test_changing_fast_does_not_modify_current_turn(self):
        await dispatch(self.bot, "/api/settings", {"fastMode": True})
        await self.bot.enqueue("first")
        worker = asyncio.create_task(self.bot.work("main"))
        try:
            for _ in range(100):
                if self.bot.active:
                    break
                await asyncio.sleep(.001)
            await dispatch(self.bot, "/api/settings", {"fastMode": False})
            turns = [params for method, params in self.bot.codex.calls if method == "turn/start"]
            self.assertEqual(len(turns), 1)
            self.assertEqual(turns[0]["serviceTier"], "priority")
            self.assertEqual(self.bot.effective["serviceTier"], "priority")
            await self.bot.enqueue("second")
            await self.bot.event({"method": "turn/completed", "params": {"threadId": self.bot.session_state["thread_id"], "turn": {"status": "completed"}}})
            for _ in range(100):
                if sum(method == "turn/start" for method, _ in self.bot.codex.calls) == 2:
                    break
                await asyncio.sleep(.001)
            turns = [params for method, params in self.bot.codex.calls if method == "turn/start"]
            self.assertEqual([turn["serviceTier"] for turn in turns], ["priority", "default"])
        finally:
            worker.cancel(); await asyncio.gather(worker, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
