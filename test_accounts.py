import asyncio
import json
import unittest
from unittest.mock import patch

import test_bot as fixtures
from web import dispatch


class AccountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.BotTests.asyncSetUp(self)
        self.account = {"type": "chatgpt", "email": "owner@example.test", "planType": "plus",
                        "workspaceName": "Personal", "accessToken": "private-access-token"}
        self.device = {"type": "chatgptDeviceCode", "loginId": "login-one", "userCode": "TEST-1234",
                       "verificationUrl": "https://auth.openai.com/codex/device"}
        original = self.bot.codex.call

        async def call(method, params, **kwargs):
            await original(method, params)
            if method == "account/read":
                return {"account": self.account, "requiresOpenaiAuth": True}
            if method == "account/login/start":
                return self.device
            return {}
        self.bot.codex.call = call

    async def test_account_read_is_global_and_excludes_credentials(self):
        result = await dispatch(self.bot, "/api/account", {"session": "deleted-session"})
        self.assertEqual(result["account"]["email"], "owner@example.test")
        self.assertNotIn("private-access-token", json.dumps(result))
        self.assertEqual(self.bot.codex.calls[-1], ("account/read", {"refreshToken": False}))

    async def test_save_refreshes_native_credentials_and_persists_only_profile(self):
        result = await dispatch(self.bot, "/api/account/save", {})
        saved = json.loads(self.bot.statepath.read_text())["account_profile"]
        self.assertEqual(saved["email"], "owner@example.test")
        self.assertIn("savedAt", saved)
        self.assertEqual(result["savedProfile"], saved)
        self.assertNotIn("private-access-token", self.bot.statepath.read_text())
        self.assertEqual(self.bot.codex.calls[-1], ("account/read", {"refreshToken": True}))

    async def test_unsigned_account_cannot_be_saved(self):
        self.account = None
        with self.assertRaises(ValueError):
            await self.bot.accounts.save()
        self.assertNotIn("account_profile", self.bot.state)

    async def test_failed_save_restores_previous_profile(self):
        self.bot.state["account_profile"] = {"email": "previous@example.test"}
        with patch.object(self.bot, "persist", side_effect=OSError("disk")):
            with self.assertRaises(ValueError):
                await self.bot.accounts.save()
        self.assertEqual(self.bot.state["account_profile"], {"email": "previous@example.test"})

    async def test_device_login_is_reused_and_cancelled(self):
        result = await dispatch(self.bot, "/api/account/login", {})
        self.assertEqual(result["login"]["userCode"], "TEST-1234")
        self.assertEqual(result, await self.bot.accounts.start_login())
        starts = [method for method, _ in self.bot.codex.calls if method == "account/login/start"]
        self.assertEqual(len(starts), 1)
        self.assertTrue(self.bot.accounts.pending)
        await dispatch(self.bot, "/api/account/cancel", {})
        self.assertFalse(self.bot.accounts.pending)
        self.assertEqual(self.bot.codex.calls[-1], ("account/login/cancel", {"loginId": "login-one"}))

    async def test_login_rejected_when_any_session_has_work(self):
        sid = self.bot.create_session("other")
        self.bot.runtimes[sid].active = "active-turn"
        with self.assertRaises(ValueError):
            await self.bot.accounts.start_login()
        self.bot.runtimes[sid].active = None
        self.bot.runtimes[sid].queue.put_nowait("queued")
        with self.assertRaises(ValueError):
            await self.bot.accounts.start_login()
        self.assertFalse(self.bot.accounts.pending)

    async def test_pending_login_blocks_jobs_and_saving(self):
        await self.bot.accounts.start_login()
        for operation in (self.bot.enqueue("new"), self.bot.submit_message("new", "steer"), self.bot.accounts.save()):
            with self.assertRaises(ValueError):
                await operation
        self.assertEqual(self.bot.queue.qsize(), 0)

    async def test_completion_is_global_and_never_exposes_raw_error(self):
        await self.bot.accounts.start_login()
        await self.bot.event({"method": "account/login/completed", "params": {
            "loginId": "login-one", "success": False, "error": "private-token-in-error"}})
        self.assertFalse(self.bot.accounts.pending)
        self.assertEqual(self.bot.accounts.login["status"], "failed")
        self.assertNotIn("private-token", json.dumps(list(self.bot.feed)))
        self.assertNotIn("private-token", json.dumps(await self.bot.accounts.read()))

    async def test_unrelated_completion_does_not_end_pending_login(self):
        await self.bot.accounts.start_login()
        await self.bot.event({"method": "account/login/completed", "params": {"loginId": "other", "success": True}})
        self.assertTrue(self.bot.accounts.pending)
        await self.bot.event({"method": "account/login/completed", "params": {"loginId": "login-one", "success": True}})
        self.assertEqual(self.bot.accounts.login, {"status": "completed", "message": "로그인 완료"})

    async def test_early_completion_does_not_leave_login_pending(self):
        original = self.bot.codex.call
        async def call(method, params):
            result = await original(method, params)
            if method == "account/login/start":
                await self.bot.event({"method": "account/login/completed", "params": {"loginId": "login-one", "success": True}})
            return result
        self.bot.codex.call = call
        result = await self.bot.accounts.start_login()
        self.assertEqual(result["login"]["status"], "completed")

    async def test_untrusted_login_url_is_cancelled_and_rejected(self):
        self.device["verificationUrl"] = "https://attacker.example/codex/device"
        with self.assertRaises(ValueError):
            await self.bot.accounts.start_login()
        self.assertFalse(self.bot.accounts.pending)
        self.assertEqual(self.bot.codex.calls[-1], ("account/login/cancel", {"loginId": "login-one"}))

    async def test_starting_login_blocks_jobs_before_rpc_finishes(self):
        original = self.bot.codex.call
        entered, release = asyncio.Event(), asyncio.Event()
        async def call(method, params):
            if method == "account/login/start":
                entered.set()
                await release.wait()
            return await original(method, params)
        self.bot.codex.call = call
        task = asyncio.create_task(self.bot.accounts.start_login())
        try:
            await asyncio.wait_for(entered.wait(), .5)
            with self.assertRaises(ValueError):
                await self.bot.enqueue("new job")
        finally:
            release.set()
            await task


if __name__ == "__main__":
    unittest.main()
