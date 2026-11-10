import asyncio
import base64
import hashlib
import hmac
import json
import unittest
from urllib.parse import urlencode

import test_bot as fixtures
from web import dispatch, validate_init_data


def sign(fields, token="test-token"):
    check = "\n".join(k + "=" + v for k, v in sorted(fields.items()))
    secret = hmac.digest(b"WebAppData", token.encode(), "sha256")
    signature = hmac.digest(secret, check.encode(), "sha256").hex()
    return urlencode({**fields, "hash": signature})


class AuthenticationTests(unittest.TestCase):
    def test_signed_owner_is_accepted(self):
        fields = {"auth_date": "1000", "user": '{"id":42}', "query_id": "test"}
        self.assertEqual(validate_init_data(sign(fields), "test-token", 42, now=1100), 42)

    def test_wrong_owner_tampering_expiration_and_future_are_rejected(self):
        fields = {"auth_date": "1000", "user": '{"id":42}'}
        valid = sign(fields)
        for raw, owner, now in [(valid, 7, 1100), (valid.replace("1000", "1001"), 42, 1100),
                                (valid, 42, 5000), (valid, 42, 900), (valid, None, 1100)]:
            with self.assertRaises(PermissionError):
                validate_init_data(raw, "test-token", owner, now=now)

    def test_duplicate_fields_are_rejected(self):
        valid = sign({"auth_date": "1000", "user": '{"id":42}'})
        with self.assertRaises(PermissionError):
            validate_init_data(valid + '&auth_date=1000', "test-token", 42, now=1100)


class WebApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.BotTests.asyncSetUp
    update = fixtures.BotTests.update
    pair = fixtures.BotTests.pair

    async def test_stale_client_selection_does_not_block_global_reads(self):
        await self.pair()
        status = await dispatch(self.bot, '/api/status', {'session': 'deleted-session'})
        self.assertEqual(status['selected'], 'main')
        self.bot.publish({'method': 'test', 'cloudex_session': 'main'})
        events = await dispatch(self.bot, '/api/events', {'session': 'deleted-session'})
        self.assertEqual(events['events'][0]['method'], 'test')
        await dispatch(self.bot, '/api/models', {'session': 'deleted-session'})
        self.assertEqual(self.bot.codex.calls[-1][0], 'model/list')
        with self.assertRaises(ValueError):
            await dispatch(self.bot, '/api/settings', {'session': 'deleted-session', 'approvalPolicy': 'full-access'})

    async def test_web_message_is_pinned_to_its_session(self):
        await self.pair()
        second = self.bot.create_session('second')
        await dispatch(self.bot, '/api/command', {'session': 'main', 'text': 'job for main'})
        self.assertEqual(self.bot.runtimes['main'].queue.qsize(), 1)
        self.assertEqual(self.bot.runtimes[second].queue.qsize(), 0)
        self.assertEqual(self.bot.state['selected_session'], second)

    async def test_settings_are_saved_only_for_target_session(self):
        await self.pair()
        second = self.bot.create_session('second')
        await dispatch(self.bot, '/api/settings', {'session': 'main', 'model': 'model-a', 'effort': 'high', 'approvalPolicy': 'untrusted'})
        self.assertEqual(self.bot.runtimes['main'].data['model'], 'model-a')
        self.assertNotIn('model', self.bot.runtimes[second].data)
        with self.assertRaises(ValueError):
            await dispatch(self.bot, '/api/settings', {'session': 'main', 'approvalPolicy': 'approve-everything'})

    async def test_full_access_is_scoped_to_target_and_exported(self):
        await self.pair()
        other = self.bot.create_session("other")
        await dispatch(self.bot, "/api/settings", {"session": "main", "approvalPolicy": "full-access"})
        self.assertEqual(self.bot.runtimes["main"].data["approvalPolicy"], "never")
        self.assertEqual(self.bot.runtimes["main"].data["sandbox"], "danger-full-access")
        self.assertNotIn("sandbox", self.bot.runtimes[other].data)
        status = await dispatch(self.bot, "/api/status", {"session": "main"})
        main = next(s for s in status["sessions"] if s["id"] == "main")
        self.assertEqual((main["policy"], main["sandbox"]), ("never", "danger-full-access"))
        await dispatch(self.bot, "/api/settings", {"session": "main", "approvalPolicy": "never"})
        self.assertEqual(self.bot.runtimes["main"].data["sandbox"], "workspace-write")

    async def test_invalid_settings_do_not_partially_enable_full_access(self):
        await self.pair()
        with self.assertRaises(ValueError):
            await dispatch(self.bot, "/api/settings", {"approvalPolicy": "full-access", "model": "bad model"})
        self.assertNotIn("sandbox", self.bot.session_state)

    async def test_web_upload_isolated_and_exported_in_file_list(self):
        await self.pair()
        second = self.bot.create_session('second')
        result = await dispatch(self.bot, '/api/upload', {'session': 'main', 'name': '../hello.txt',
                        'content': base64.b64encode(b'hello').decode()})
        files = await dispatch(self.bot, '/api/files', {'session': 'main'})
        other = await dispatch(self.bot, '/api/files', {'session': second})
        self.assertEqual(files['files'][0]['path'], result['path'])
        self.assertEqual(other['files'], [])
        self.assertTrue(result['path'].startswith('inbox/'))

    async def test_event_feed_keeps_session_identity(self):
        await self.pair()
        self.bot.session_state['thread_id'] = 'thread-main'
        second = self.bot.create_session('second')
        await self.bot.event({'method': 'item/agentMessage/delta', 'params': {'threadId': 'thread-main', 'itemId': 'm', 'delta': 'hello'}})
        result = await dispatch(self.bot, '/api/events', {'session': second, 'after': 0, 'server': self.bot.server_id})
        self.assertEqual(result['events'][0]['cloudex_session'], 'main')


    async def test_api_command_ack_does_not_wait_for_telegram_delivery(self):
        await self.pair()
        gate = asyncio.Event()
        async def stalled(*args, **kwargs):
            await gate.wait()
        self.bot.telegram.say = stalled
        try:
            result = await asyncio.wait_for(dispatch(self.bot, "/api/command", {"session": "main", "text": "job"}), .5)
            self.assertTrue(result["ok"])
            self.assertEqual(self.bot.runtimes["main"].queue.qsize(), 1)
            self.assertFalse(gate.is_set())
        finally:
            gate.set()
            await asyncio.gather(*self.bot.tasks, return_exceptions=True)

    async def test_failed_telegram_ack_does_not_enqueue_the_same_update_twice(self):
        await self.pair()
        async def failure(*a, **kw):
            raise RuntimeError('offline')
        self.bot.telegram.say = failure
        update = {**self.update('job'), 'update_id': 123}
        with self.assertLogs("cloudex", level="WARNING"):
            await self.bot.receive(update)
            await self.bot.receive(update)
            await asyncio.gather(*self.bot.tasks, return_exceptions=True)
        self.assertEqual(self.bot.queue.qsize(), 1)


if __name__ == '__main__':
    unittest.main()
