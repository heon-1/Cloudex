"""Owner-only account controls using Codex-managed persistent credentials."""
import asyncio
import contextlib
import time
from urllib.parse import urlsplit


class Accounts:
    def __init__(self, bot):
        self.bot = bot
        self.lock = asyncio.Lock()
        self.login = None

    @property
    def pending(self):
        return bool(self.login and self.login["status"] in ("starting", "pending"))

    @property
    def busy(self):
        return any(rt.active or rt.queue._unfinished_tasks for rt in self.bot.runtimes.values())

    def notification(self, event):
        if event["method"] == "cloudex/disconnected" and self.pending:
            self.login = {"status": "failed", "message": "연결이 종료됐습니다. 다시 로그인해주세요."}
        elif event["method"] == "account/login/completed" and self.pending:
            params = event.get("params", {})
            if self.login.get("loginId") and params.get("loginId") != self.login["loginId"]:
                return
            self.login = {"status": "completed" if params.get("success") else "failed",
                          "message": "로그인 완료" if params.get("success") else "로그인이 완료되지 않았습니다. 다시 시도해주세요."}

    async def read(self, refresh=False):
        try:
            await self.bot.codex.start()
            result = await self.bot.codex.call("account/read", {"refreshToken": refresh})
        except Exception:
            raise ValueError("계정 정보를 확인하지 못했습니다. 잠시 후 다시 시도해주세요.") from None
        raw = result.get("account")
        # Never forward credentials or unexpected future RPC fields to the WebView.
        account = ({key: raw[key] for key in ("type", "email", "planType", "workspaceName") if key in raw}
                   if isinstance(raw, dict) else None)
        return {"account": account, "requiresOpenaiAuth": bool(result.get("requiresOpenaiAuth")),
                "savedProfile": self.bot.state.get("account_profile"),
                "login": dict(self.login) if self.login else None,
                "canLogin": not self.busy and not self.pending}

    async def save(self):
        async with self.lock:
            if self.pending:
                raise ValueError("로그인을 완료한 뒤 계정을 저장하세요.")
            result = await self.read(refresh=True)
            if not result["account"]:
                raise ValueError("먼저 ChatGPT 계정으로 로그인해주세요.")
            previous = self.bot.state.get("account_profile")
            self.bot.state["account_profile"] = {**result["account"], "savedAt": int(time.time())}
            try:
                self.bot.persist()
            except Exception:
                if previous is None:
                    self.bot.state.pop("account_profile", None)
                else:
                    self.bot.state["account_profile"] = previous
                raise ValueError("계정 저장에 실패했습니다. 다시 시도해주세요.") from None
            return {**result, "savedProfile": self.bot.state["account_profile"]}

    async def start_login(self):
        async with self.lock:
            if self.pending:
                return {"login": dict(self.login)}
            if self.busy:
                raise ValueError("모든 작업과 대기열이 끝난 뒤 다시 로그인해주세요.")
            self.login = {"status": "starting"}
            login_id = None
            try:
                await self.bot.codex.start()
                result = await self.bot.codex.call("account/login/start", {"type": "chatgptDeviceCode"})
                login_id = result.get("loginId")
                url = urlsplit(result.get("verificationUrl", ""))
                code = result.get("userCode")
                if (result.get("type") != "chatgptDeviceCode" or not isinstance(login_id, str)
                        or not login_id or len(login_id) > 256 or not isinstance(code, str) or not 1 <= len(code) <= 64
                        or url.scheme != "https" or url.netloc != "auth.openai.com"
                        or url.path != "/codex/device" or url.query or url.fragment):
                    raise ValueError("Invalid device login response")
                # A completion notification can arrive before the RPC response.
                if self.login["status"] == "starting":
                    self.login = {"status": "pending", "loginId": login_id,
                                  "verificationUrl": result["verificationUrl"], "userCode": code}
                return {"login": dict(self.login)}
            except (Exception, asyncio.CancelledError) as exc:
                if login_id:
                    with contextlib.suppress(Exception):
                        await self.bot.codex.call("account/login/cancel", {"loginId": login_id})
                self.login = {"status": "failed", "message": "로그인을 시작하지 못했습니다. 다시 시도해주세요."}
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise ValueError("로그인을 시작하지 못했습니다. ChatGPT 보안 설정의 기기 코드 로그인을 확인하고 다시 시도해주세요.") from None

    async def cancel_login(self):
        async with self.lock:
            if self.pending and self.login.get("loginId"):
                await self.bot.codex.call("account/login/cancel", {"loginId": self.login["loginId"]})
            self.login = {"status": "cancelled", "message": "로그인을 취소했습니다."}
            return {"login": dict(self.login)}
