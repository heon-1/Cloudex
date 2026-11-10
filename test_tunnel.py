import unittest
from unittest.mock import AsyncMock, patch

import test_bot as fixtures
from tunnel import QuickTunnel, extract_url


class UrlTests(unittest.TestCase):
    def test_extracts_only_tunnel_hostnames(self):
        self.assertEqual(extract_url('| https://four-words-green-tree.trycloudflare.com |'),
                         'https://four-words-green-tree.trycloudflare.com')
        self.assertIsNone(extract_url('Post https://api.trycloudflare.com/tunnel failed'))
        self.assertIsNone(extract_url('https://attacker.example.com'))


class TunnelTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.BotTests.asyncSetUp
    update = fixtures.BotTests.update
    pair = fixtures.BotTests.pair

    async def test_late_healthy_tunnel_is_retried_after_initial_window(self):
        await self.pair()
        tunnel = QuickTunnel(self.bot)
        tunnel.activate = AsyncMock(side_effect=[False, True])
        with patch('tunnel.asyncio.sleep', new=AsyncMock()):
            self.assertTrue(await tunnel.activate_until_ready('https://test.trycloudflare.com', 0))
        self.assertEqual(tunnel.activate.await_count, 2)

    async def test_activation_retry_stops_when_generation_changes(self):
        await self.pair()
        tunnel = QuickTunnel(self.bot)
        async def stale(*args):
            tunnel.generation += 1
            return False
        tunnel.activate = AsyncMock(side_effect=stale)
        self.assertFalse(await tunnel.activate_until_ready('https://test.trycloudflare.com', 0))
        self.assertEqual(tunnel.activate.await_count, 1)

    async def test_menu_changes_only_after_health_passes(self):
        await self.pair()
        tunnel = QuickTunnel(self.bot)
        with patch('tunnel.healthy', return_value=True):
            self.assertTrue(await tunnel.activate('https://test.trycloudflare.com', 0))
        self.assertTrue(self.bot.quick_ready)
        self.assertEqual(self.bot.state['tunnel_url'], 'https://test.trycloudflare.com')
        self.assertEqual(self.bot.telegram.calls[-1][0], 'setChatMenuButton')
        self.assertEqual(self.bot.telegram.calls[-1][1]['menu_button']['web_app']['url'], self.bot.state['tunnel_url'])
        menus = [data for method, data in self.bot.telegram.calls if method == "setChatMenuButton"]
        self.assertEqual([data.get("chat_id") for data in menus], [None, self.bot.owner])
        self.assertTrue(all(data["menu_button"]["web_app"]["url"] == self.bot.state["tunnel_url"] for data in menus))

    async def test_unhealthy_and_stale_tunnels_never_update_menu(self):
        await self.pair()
        tunnel = QuickTunnel(self.bot)
        with patch('tunnel.healthy', return_value=False), patch('tunnel.asyncio.sleep', new=AsyncMock()):
            self.assertFalse(await tunnel.activate('https://test.trycloudflare.com', 0))
        self.assertFalse(self.bot.telegram.calls)
        tunnel.generation = 1
        with patch('tunnel.healthy', return_value=True):
            self.assertFalse(await tunnel.activate('https://old.trycloudflare.com', 0))
        self.assertFalse(self.bot.quick_ready)

    async def test_app_does_not_offer_a_stale_tunnel_url(self):
        await self.pair()
        self.bot.config.update(quick_tunnel=True, public_url='https://old.trycloudflare.com')
        await self.bot.receive(self.update('/app'))
        self.assertNotIn('reply_markup', self.bot.telegram.messages[-1][2])
        self.bot.quick_ready = True
        await self.bot.receive(self.update('/app'))
        self.assertIn('reply_markup', self.bot.telegram.messages[-1][2])

    async def test_transient_menu_failure_is_retried(self):
        await self.pair()
        tunnel = QuickTunnel(self.bot)
        self.bot.telegram.api = AsyncMock(side_effect=[RuntimeError('offline'), {}, {}])
        with patch('tunnel.healthy', return_value=True), patch('tunnel.asyncio.sleep', new=AsyncMock()):
            self.assertTrue(await tunnel.activate('https://test.trycloudflare.com', 0))
        self.assertEqual(self.bot.telegram.api.await_count, 3)
        self.assertTrue(self.bot.quick_ready)

    async def test_owner_menu_failure_retries_before_marking_ready(self):
        await self.pair()
        tunnel = QuickTunnel(self.bot)
        self.bot.telegram.api = AsyncMock(side_effect=[{}, RuntimeError("owner menu offline"), {}, {}])
        with patch("tunnel.healthy", return_value=True), patch("tunnel.asyncio.sleep", new=AsyncMock()):
            self.assertTrue(await tunnel.activate("https://new.trycloudflare.com", 0))
        self.assertEqual(self.bot.telegram.api.await_count, 4)
        self.assertEqual(self.bot.telegram.api.call_args_list[-1].args[1]["chat_id"], self.bot.owner)
        self.assertTrue(self.bot.quick_ready)

    async def test_stale_generation_does_not_overwrite_owner_menu(self):
        await self.pair()
        tunnel = QuickTunnel(self.bot)
        async def changed_generation(*args):
            tunnel.generation += 1
            return {}
        self.bot.telegram.api = AsyncMock(side_effect=changed_generation)
        with patch("tunnel.healthy", return_value=True):
            self.assertFalse(await tunnel.activate("https://stale.trycloudflare.com", 0))
        self.assertEqual(self.bot.telegram.api.await_count, 1)
        self.assertFalse(self.bot.quick_ready)


if __name__ == '__main__':
    unittest.main()
