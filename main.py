import asyncio
import logging
import sys

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, ErrorEvent, Message
from sqlalchemy import select

import config
import handlers_admin
import handlers_worker
import keyboards as kb
from models import Session, Worker

log = logging.getLogger("qdif")


def setup_logging():
    handlers = [logging.StreamHandler(sys.stdout)]
    if config.LOG_FILE:
        handlers.append(logging.FileHandler(config.LOG_FILE, encoding="utf-8"))
    logging.basicConfig(
        level=config.LOG_LEVEL,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


class Auth(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        is_admin = user.id == config.ADMIN_TG_ID
        worker = None
        if not is_admin:
            async with Session() as s:
                worker = await s.scalar(
                    select(Worker).where(
                        Worker.tg_id == user.id, Worker.deleted_at.is_(None)
                    )
                )
            if worker and not worker.is_active:
                await self.reject(event, "Доступ отключён.")
                return
            if worker is None and not await self.registering(event, data):
                await self.reject(event, "Отправьте /start, чтобы начать.")
                return

        data["worker"] = worker
        data["is_admin"] = is_admin
        return await handler(event, data)

    @staticmethod
    async def registering(event, data) -> bool:
        text = getattr(event, "text", None) or ""
        if text.startswith("/start"):
            return True
        state = data.get("state")
        return bool(state and await state.get_state())

    @staticmethod
    async def reject(event, text):
        if isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        elif isinstance(event, Message):
            await event.answer(text)


common = Router()


@common.callback_query(F.data == kb.CANCEL_CB)
async def cancel_any(cb: CallbackQuery, state, is_admin: bool):
    await state.clear()
    await cb.answer("Отменено")
    await cb.message.answer(
        "Отменено.", reply_markup=kb.admin_menu() if is_admin else kb.worker_menu()
    )


async def main():
    setup_logging()
    bot = Bot(
        config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(Auth())
    dp.callback_query.outer_middleware(Auth())

    is_admin = F.from_user.id == config.ADMIN_TG_ID
    handlers_admin.router.message.filter(is_admin)
    handlers_admin.router.callback_query.filter(is_admin)
    handlers_worker.router.message.filter(~is_admin)
    handlers_worker.router.callback_query.filter(~is_admin)

    dp.include_router(common)
    dp.include_router(handlers_admin.router)
    dp.include_router(handlers_worker.edit_router)
    dp.include_router(handlers_worker.router)

    @dp.errors()
    async def on_error(event: ErrorEvent):
        log.exception("handler error: %s", event.exception)
        return True

    log.info("bot started")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
