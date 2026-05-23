import asyncio
import logging
import os
import sys

from aiogram import Bot, Dispatcher

from auditor.bot.handlers import router, set_storage
from auditor.bot.rate_limiter import RateLimitMiddleware, RateLimiter
from auditor.bot.storage import SQLiteStorage
from auditor.config import settings


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )

    dirname = os.path.dirname(settings.SQLITE_PATH)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    storage = SQLiteStorage(settings.SQLITE_PATH, fsm_ttl=settings.FSM_STATE_TTL, admin_id=settings.ADMIN_USER_ID)
    set_storage(storage)

    bot = Bot(token=settings.BOT_TOKEN)
    dp = Dispatcher(storage=storage)
    dp.include_router(router)

    rate_limiter = RateLimiter(
        max_requests=settings.RATE_LIMIT_MAX,
        window_seconds=settings.RATE_LIMIT_WINDOW,
    )
    dp.message.middleware(RateLimitMiddleware(rate_limiter))

    logging.info("Bot started")
    try:
        await dp.start_polling(bot)
    finally:
        logging.info("Bot stopped")
        await storage.close()
        await bot.session.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
