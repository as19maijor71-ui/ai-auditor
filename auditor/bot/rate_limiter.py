import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, TelegramObject


_RATE_LIMITED_STATES = {
    "AuditFlow:waiting_url",
    "AuditFlow:collecting_paste",
    "AuditFlow:collecting_screenshots",
    "AuditFlow:supplementing_export",
}


class RateLimiter:
    # NOTE: _buckets grows unbounded with unique user_ids.
    # For MVP (<50 users) this is fine — dict with empty lists is negligible.
    # Production: add periodic cleanup of stale buckets.
    def __init__(self, max_requests: int = 3, window_seconds: int = 60) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._buckets: dict[int, list[float]] = {}

    def is_allowed(self, user_id: int) -> bool:
        now = time.monotonic()
        timestamps = self._buckets.get(user_id, [])
        cutoff = now - self._window
        timestamps = [t for t in timestamps if t > cutoff]
        if len(timestamps) >= self._max:
            self._buckets[user_id] = timestamps
            return False
        timestamps.append(now)
        self._buckets[user_id] = timestamps
        return True


class RateLimitMiddleware(BaseMiddleware):
    def __init__(self, limiter: RateLimiter) -> None:
        self._limiter = limiter
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or event.from_user is None:
            return await handler(event, data)

        if event.text == "🔄 Начать заново":
            return await handler(event, data)

        state: FSMContext = data.get("state")
        if state is None:
            return await handler(event, data)

        current_state = await state.get_state()
        if current_state not in _RATE_LIMITED_STATES:
            return await handler(event, data)

        if not self._limiter.is_allowed(event.from_user.id):
            await event.answer(
                "\u26a0\ufe0f \u0421\u043b\u0438\u0448\u043a\u043e\u043c "
                "\u043c\u043d\u043e\u0433\u043e \u0437\u0430\u043f\u0440\u043e\u0441\u043e\u0432. "
                "\u041f\u043e\u0434\u043e\u0436\u0434\u0438 60 \u0441\u0435\u043a\u0443\u043d\u0434."
            )
            return None

        return await handler(event, data)
