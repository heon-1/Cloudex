"""Supervise a temporary HTTPS endpoint; refresh Telegram only after health passes."""
import asyncio
import json
import logging
import re
import urllib.error
import urllib.request

LOG = logging.getLogger("cloudex.tunnel")
URL_PATTERN = re.compile(r"https://([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)\.trycloudflare\.com\b")


def extract_url(line):
    for match in URL_PATTERN.finditer(line):
        if match.group(1) not in ("api", "www", "developers"):
            return match.group(0)
    return None


def healthy(url):
    try:
        with urllib.request.urlopen(url + "/healthz", timeout=10) as response:
            return response.status == 200 and json.loads(response.read(1024)).get("ok") is True
    except (urllib.error.URLError, OSError, ValueError):
        return False


class QuickTunnel:
    def __init__(self, bot):
        self.bot = bot
        self.process = None
        self.activation = None
        self.generation = 0
        self.closed = False

    async def activate(self, url, generation):
        for attempt in range(30):
            if self.closed or generation != self.generation:
                return False
            ready = await asyncio.to_thread(healthy, url)
            if ready:
                if self.closed or generation != self.generation:
                    return False
                try:
                    targets = [{}]
                    if self.bot.owner:
                        targets.append({"chat_id": self.bot.owner})
                    for target in targets:
                        if self.closed or generation != self.generation:
                            return False
                        await self.bot.telegram.api("setChatMenuButton", {**target, "menu_button": {
                            "type": "web_app", "text": "Cloudex", "web_app": {"url": url}}})
                except RuntimeError:
                    LOG.warning("미니앱 메뉴 등록을 재시도합니다.")
                    await asyncio.sleep(2)
                    continue
                if self.closed or generation != self.generation:
                    return False
                self.bot.config["public_url"] = url
                self.bot.quick_ready = True
                self.bot.state["tunnel_url"] = url
                self.bot.persist()
                LOG.info("Mini App HTTPS ready: %s", url)
                if self.bot.owner:
                    try:
                        await self.bot.telegram.say(self.bot.owner, "Cloudex 미니앱이 준비됐습니다. 아래 버튼으로 열어주세요.",
                            reply_markup={"inline_keyboard": [[{"text": "Cloudex 열기", "web_app": {"url": url}}]]})
                    except RuntimeError:
                        LOG.warning("준비 안내를 보내지 못했습니다. 봇 메뉴 또는 /app 을 사용하세요.")
                return True
            await asyncio.sleep(2)
        LOG.warning("임시 HTTPS 주소의 응답을 확인하지 못했습니다. 봇 채팅은 계속 사용할 수 있습니다.")
        return False

    async def terminate(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()

    async def activate_until_ready(self, url, generation):
        while not self.closed and generation == self.generation:
            if await self.activate(url, generation):
                return True
            if not self.closed and generation == self.generation:
                await asyncio.sleep(10)
        return False

    async def run(self):
        try:
            while not self.closed:
                self.generation += 1
                generation = self.generation
                self.bot.quick_ready = False
                self.bot.state.pop("tunnel_url", None)
                self.bot.persist()
                try:
                    self.process = await asyncio.create_subprocess_exec(
                        "cloudflared", "tunnel", "--url", f"http://127.0.0.1:{self.bot.config.get('web_port', 8787)}",
                        "--protocol", "http2", stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT)
                    discovered = None
                    while line := await self.process.stdout.readline():
                        text = line.decode(errors="replace").strip()
                        LOG.info("%s", text)
                        url = extract_url(text)
                        if url and url != discovered:
                            discovered = url
                            if self.activation:
                                self.activation.cancel()
                            self.activation = asyncio.create_task(self.activate_until_ready(url, generation))
                    await self.process.wait()
                except FileNotFoundError:
                    LOG.error("cloudflared가 없습니다. install-quick-tunnel.sh 를 실행하세요.")
                    return
                except OSError as exc:
                    LOG.error("터널 실행 실패: %s", type(exc).__name__)
                finally:
                    self.generation += 1
                    self.bot.quick_ready = False
                    if self.activation:
                        self.activation.cancel()
                        await asyncio.gather(self.activation, return_exceptions=True)
                        self.activation = None
                    await self.terminate()
                if not self.closed:
                    await asyncio.sleep(10)
        finally:
            self.bot.quick_ready = False
            await self.terminate()


    async def close(self):
        self.closed = True
        self.generation += 1
        if self.activation:
            self.activation.cancel()
        await self.terminate()
