import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from accounts import usage_buckets
import test_bot as fixtures
from web import dispatch


class AccountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.BotTests.asyncSetUp(self)
        self.account = {"type": "chatgpt", "email": "owner@example.test", "planType": "plus",
                        "workspaceName": "Personal", "accessToken": "private-access-token"}
        self.device = {"type": "chatgptDeviceCode", "loginId": "login-one", "userCode": "TEST-1234",
                       "verificationUrl": "https://auth.openai.com/codex/device"}
        self.limits = {"rateLimits": {"limitId": "codex", "primary": {
            "usedPercent": 25, "windowDurationMins": 300, "resetsAt": 1900000000},
            "secondary": {"usedPercent": 60, "windowDurationMins": 10080, "resetsAt": 1900600000}}}
        original = self.bot.codex.call

        async def call(method, params, **kwargs):
            await original(method, params)
            if method == "account/read":
                return {"account": self.account, "requiresOpenaiAuth": True}
            if method == "account/login/start":
                return self.device
            if method == "account/rateLimits/read":
                return self.limits
            return {}
        self.bot.codex.call = call

    async def test_account_read_is_global_and_excludes_credentials(self):
        result = await dispatch(self.bot, "/api/account", {"session": "deleted-session"})
        self.assertEqual(result["account"]["email"], "owner@example.test")
        self.assertNotIn("private-access-token", json.dumps(result))
        self.assertIn(("account/read", {"refreshToken": False}), self.bot.codex.calls)

    async def test_save_refreshes_native_credentials_and_persists_only_profile(self):
        result = await dispatch(self.bot, "/api/account/save", {})
        saved = json.loads(self.bot.statepath.read_text())["account_profile"]
        self.assertEqual(saved["email"], "owner@example.test")
        self.assertIn("savedAt", saved)
        self.assertEqual(result["savedProfile"], saved)
        self.assertNotIn("private-access-token", self.bot.statepath.read_text())
        self.assertIn(("account/read", {"refreshToken": True}), self.bot.codex.calls)

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

    async def test_remaining_percentage_and_real_reset_windows(self):
        result = await self.bot.accounts.read()
        bucket = result["usage"]["buckets"][0]
        self.assertEqual(bucket["primary"], {"remainingPercent": 75, "windowDurationMins": 300, "resetsAt": 1900000000})
        self.assertEqual(bucket["secondary"]["remainingPercent"], 40)
        self.assertEqual(bucket["secondary"]["windowDurationMins"], 10080)

    def test_multi_bucket_view_has_no_legacy_duplicate_or_extra_fields(self):
        self.limits["rateLimitsByLimitId"] = {"codex": self.limits["rateLimits"], "other": {
            "limitName": "Other model", "privateToken": "secret", "primary": {"usedPercent": 100}}}
        buckets = usage_buckets(self.limits)
        self.assertEqual(len(buckets), 2)
        self.assertEqual(buckets[1]["primary"]["remainingPercent"], 0)
        self.assertNotIn("secret", json.dumps(buckets))
        self.assertIsNone(buckets[1]["primary"]["resetsAt"])

    def test_unknown_percent_is_not_shown_as_full_and_percentages_are_clamped(self):
        for value, expected in [(None, None), (True, None), (float('nan'), None), (float('inf'), None), (-5, 100), (120, 0), (25.5, 74.5)]:
            with self.subTest(value=value):
                buckets = usage_buckets({"rateLimits": {"primary": {"usedPercent": value, "resetsAt": -1}}})
                self.assertEqual(buckets[0]["primary"]["remainingPercent"], expected)
                self.assertIsNone(buckets[0]["primary"]["resetsAt"])

    async def test_usage_cache_and_explicit_refresh(self):
        await self.bot.accounts.read()
        await self.bot.accounts.read()
        self.assertEqual(sum(method == "account/rateLimits/read" for method, _ in self.bot.codex.calls), 1)
        self.limits["rateLimits"]["primary"]["usedPercent"] = 70
        result = await dispatch(self.bot, "/api/account", {"refreshUsage": "1"})
        self.assertEqual(result["usage"]["buckets"][0]["primary"]["remainingPercent"], 30)
        self.assertEqual(sum(method == "account/rateLimits/read" for method, _ in self.bot.codex.calls), 2)

    async def test_usage_is_cached_for_five_minutes(self):
        now = [1000]
        clock = SimpleNamespace(time=lambda: 1900000000, monotonic=lambda: now[0])
        with patch("accounts.time", clock):
            await self.bot.accounts.read()
            now[0] = 1299
            await self.bot.accounts.read()
            self.assertEqual(sum(method == "account/rateLimits/read" for method, _ in self.bot.codex.calls), 1)
            now[0] = 1300
            await self.bot.accounts.read()
            self.assertEqual(sum(method == "account/rateLimits/read" for method, _ in self.bot.codex.calls), 2)
            now[0] = 1301
            await self.bot.accounts.read(refresh_usage=True)
            self.assertEqual(sum(method == "account/rateLimits/read" for method, _ in self.bot.codex.calls), 3)

    async def test_sparse_notification_preserves_other_window(self):
        await self.bot.accounts.read()
        await self.bot.event({"method": "account/rateLimits/updated", "params": {"rateLimits": {
            "limitId": "codex", "primary": {"usedPercent": 80, "windowDurationMins": 300, "resetsAt": 1900000000}}}})
        result = await self.bot.accounts.read()
        self.assertEqual(result["usage"]["buckets"][0]["primary"]["remainingPercent"], 20)
        self.assertEqual(result["usage"]["buckets"][0]["secondary"]["remainingPercent"], 40)
        self.assertFalse(self.bot.feed[-1]["params"]["authChanged"])
        self.assertEqual(self.bot.feed[-1]["params"]["usage"]["buckets"][0]["primary"]["remainingPercent"], 20)

    async def test_usage_failure_keeps_account_and_marks_previous_snapshot_stale(self):
        previous = await self.bot.accounts.read()
        original = self.bot.codex.call
        async def failing(method, params, **kwargs):
            if method == "account/rateLimits/read":
                raise RuntimeError("private-token-from-upstream")
            return await original(method, params, **kwargs)
        self.bot.codex.call = failing
        result = await self.bot.accounts.read(refresh_usage=True)
        self.assertEqual(result["account"]["email"], "owner@example.test")
        self.assertEqual(result["usage"]["buckets"], previous["usage"]["buckets"])
        self.assertTrue(result["usage"]["error"])
        self.assertNotIn("private-token-from-upstream", json.dumps(result))

    async def test_account_change_clears_previous_accounts_usage(self):
        await self.bot.accounts.read()
        await self.bot.event({"method": "account/updated", "params": {"authMode": "chatgpt"}})
        self.assertIsNone(self.bot.accounts.usage)
        self.limits = {}
        result = await self.bot.accounts.read()
        self.assertEqual(result["usage"]["buckets"], [])

    async def test_api_key_and_logged_out_accounts_have_no_fake_quota(self):
        for account in ({"type": "apiKey"}, None):
            self.account = account
            result = await self.bot.accounts.read()
            self.assertEqual(result["usage"]["buckets"], [])
            self.assertTrue(result["usage"]["error"])
        self.assertFalse(any(method == "account/rateLimits/read" for method, _ in self.bot.codex.calls))

    async def test_inflight_usage_response_cannot_restore_old_account_data(self):
        original = self.bot.codex.call
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(method, params, **kwargs):
            if method == "account/rateLimits/read":
                entered.set()
                await release.wait()
            return await original(method, params, **kwargs)
        self.bot.codex.call = delayed
        task = asyncio.create_task(self.bot.accounts.read())
        try:
            await asyncio.wait_for(entered.wait(), .5)
            await self.bot.event({"method": "account/updated", "params": {"authMode": "chatgpt"}})
        finally:
            release.set()
        result = await task
        self.assertEqual(result["usage"]["buckets"], [])


if __name__ == "__main__":
    unittest.main()
