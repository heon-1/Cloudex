"""Stable queued-request identities and serialized session queue edits."""
import asyncio
import logging

LOG = logging.getLogger("cloudex")


class WorkQueue(asyncio.Queue):
    def __init__(self, maxsize=20):
        super().__init__(maxsize)
        self.available = asyncio.Event()

    def _put(self, item):
        super()._put(item)
        self.available.set()

    def _get(self):
        item = super()._get()
        if not self._queue:
            self.available.clear()
        return item

    def remove_at(self, index):
        del self._queue[index]
        if not self._queue:
            self.available.clear()
        self.task_done()
        self._wakeup_next(self._putters)

    def insert_at(self, index, text):
        self.put_nowait(text)
        self._queue.insert(index, self._queue.pop())

    def move_first(self, index):
        text = self._queue[index]
        del self._queue[index]
        self._queue.appendleft(text)


class QueuedRequests:
    def __init__(self, bot):
        self.bot = bot

    def snapshot(self):
        rt = self.bot.runtime
        return {"session": self.bot.current_sid, "activeTurnId": rt.active,
                "items": [{"id": ident, "text": text} for ident, text in
                          zip(rt.data.get("pending_ids", []), rt.data.get("pending", []))]}

    async def read(self):
        async with self.bot.runtime.queue_lock:
            return self.snapshot()

    def position(self, ident):
        rt = self.bot.runtime
        if not isinstance(ident, str) or not ident or len(ident) > 128:
            raise ValueError("대기 요청을 선택해주세요.")
        ids, pending = rt.data.get("pending_ids", []), rt.data.get("pending", [])
        if ident not in ids:
            raise ValueError("이미 실행되거나 삭제된 요청입니다. 대기열을 새로 확인해주세요.")
        if len(ids) != len(pending) or list(rt.queue._queue) != pending:
            raise ValueError("대기열 상태가 바뀌었습니다. 다시 확인해주세요.")
        return ids.index(ident)

    def write(self, pending, ids):
        rt = self.bot.runtime
        old_pending, old_ids = rt.data.get("pending", []), rt.data.get("pending_ids", [])
        rt.data.update(pending=pending, pending_ids=ids)
        try:
            self.bot.persist()
        except Exception:
            rt.data.update(pending=old_pending, pending_ids=old_ids)
            raise

    def remove(self, index):
        rt = self.bot.runtime
        self.write(rt.data["pending"][:index] + rt.data["pending"][index + 1:],
                   rt.data["pending_ids"][:index] + rt.data["pending_ids"][index + 1:])
        rt.queue.remove_at(index)

    async def delete(self, ident):
        async with self.bot.runtime.queue_lock:
            self.remove(self.position(ident))
            return self.snapshot()

    async def edit(self, ident, text, expected_text):
        if not isinstance(text, str) or not text.strip() or len(text.strip()) > 30000:
            raise ValueError("메시지는 1~30,000자로 입력하세요.")
        if not isinstance(expected_text, str):
            raise ValueError("수정할 요청을 다시 열어주세요.")
        rt = self.bot.runtime
        async with rt.queue_lock:
            index = self.position(ident)
            if rt.data.get("archived"):
                raise ValueError("보관된 세션은 먼저 복원해주세요.")
            if rt.data["pending"][index] != expected_text:
                raise ValueError("다른 곳에서 수정된 요청입니다. 최신 내용을 확인한 뒤 다시 수정해주세요.")
            text = text.strip()
            pending = list(rt.data["pending"])
            pending[index] = text
            self.write(pending, list(rt.data["pending_ids"]))
            rt.queue._queue[index] = text
            return self.snapshot()

    async def reorder(self, ids, expected_ids):
        for values in (ids, expected_ids):
            if (not isinstance(values, list) or len(values) > 20 or
                    any(not isinstance(ident, str) or not ident or len(ident) > 128 for ident in values) or
                    len(set(values)) != len(values)):
                raise ValueError("대기 요청의 순서를 확인해주세요.")
        rt = self.bot.runtime
        async with rt.queue_lock:
            if rt.data.get("archived"):
                raise ValueError("보관된 세션은 먼저 복원해주세요.")
            current_ids = rt.data.get("pending_ids", [])
            pending = rt.data.get("pending", [])
            if expected_ids != current_ids:
                raise ValueError("대기열이 바뀌었습니다. 최신 순서를 확인한 뒤 다시 이동해주세요.")
            if set(ids) != set(current_ids) or len(pending) != len(current_ids) or list(rt.queue._queue) != pending:
                raise ValueError("대기열 상태가 바뀌었습니다. 다시 확인해주세요.")
            if ids != current_ids:
                texts = dict(zip(current_ids, pending))
                pending = [texts[ident] for ident in ids]
                self.write(pending, list(ids))
                rt.queue._queue.clear()
                rt.queue._queue.extend(pending)
            return self.snapshot()

    async def apply(self, ident, expected_turn_id=None):
        rt = self.bot.runtime
        async with rt.queue_lock:
            index = self.position(ident)
            if rt.data.get("archived"):
                raise ValueError("보관된 세션은 먼저 복원해주세요.")
            if self.bot.accounts.pending:
                raise ValueError("계정 로그인을 완료하거나 취소해주세요.")
            if expected_turn_id is not None and (not isinstance(expected_turn_id, str) or not expected_turn_id or len(expected_turn_id) > 200):
                raise ValueError("실행 중인 작업 정보를 확인해주세요.")
            if expected_turn_id and rt.active != expected_turn_id:
                raise ValueError("실행 중인 작업이 바뀌었습니다. 대기열을 새로 확인해주세요.")
            if not rt.active and rt.queue._unfinished_tasks > rt.queue.qsize():
                try:
                    await asyncio.wait_for(rt.turn_ready.wait(), 10)
                except asyncio.TimeoutError:
                    raise ValueError("작업을 준비 중입니다. 잠시 후 다시 적용해주세요.") from None
                if not rt.active:
                    raise ValueError("작업 상태가 바뀌었습니다. 다시 적용해주세요.")
            if not rt.active:
                pending, ids = list(rt.data["pending"]), list(rt.data["pending_ids"])
                pending.insert(0, pending.pop(index)); ids.insert(0, ids.pop(index))
                self.write(pending, ids)
                rt.queue.move_first(index)
                if self.bot.started:
                    self.bot.ensure_worker(self.bot.current_sid)
                return {**self.snapshot(), "delivery": "start"}
            text, turn_id = rt.data["pending"][index], rt.active
            # Persist removal before sending; an accepted steer must not run again after restart.
            self.remove(index)
            try:
                result = await self.bot.codex.call("turn/steer", {
                    "threadId": rt.data["thread_id"], "expectedTurnId": turn_id,
                    "input": [{"type": "text", "text": text, "text_elements": []}]})
            except (Exception, asyncio.CancelledError):
                pending, ids = list(rt.data["pending"]), list(rt.data["pending_ids"])
                pending.insert(index, text); ids.insert(index, ident)
                try:
                    self.write(pending, ids)
                finally:
                    rt.data.update(pending=pending, pending_ids=ids)
                    rt.queue.insert_at(index, text)
                raise
            if rt.active == turn_id and rt.data.get("inflight"):
                rt.data["inflight"] += "\n\n추가 지시:\n" + text
                try:
                    self.bot.persist()
                except OSError:
                    LOG.warning("적용된 요청의 작업 내역 저장을 다음 상태 갱신 때 다시 시도합니다.")
            self.bot.publish({"method": "cloudex/messageSteered", "cloudex_session": self.bot.current_sid,
                              "params": {"turnId": result.get("turnId", turn_id)}})
            return {**self.snapshot(), "delivery": "steer"}
