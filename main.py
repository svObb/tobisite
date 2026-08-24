import asyncio
import logging
import sys

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import (
    BotCommand, BotCommandScopeChat, CallbackQuery, ErrorEvent, Message,
)
from sqlalchemy import select

import config
import handlers_admin
import handlers_review
import handlers_worker
import keyboards as kb
from fsm_storage import PgStorage, purge_stale_fsm
from models import Session, Worker, ensure_admin_worker

log = logging.getLogger("tobisite")


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

        is_admin = config.is_admin(user.id)
        worker = None
        if is_admin:
            # админ тоже заносит компании, а у лида worker_id обязателен. Один
            # запрос по уникальному tg_id на апдейт — ровно столько же платит
            # каждый работник строкой ниже
            worker = await ensure_admin_worker(user.id)
        else:
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
    await cb.message.answer("Отменено.", reply_markup=kb.menu(is_admin))


async def main():
    setup_logging()
    bot = Bot(
        config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    # FSM в Postgres: MemoryStorage терял недозаполненные формы у всех
    # работников при каждом деплое (см. fsm_storage.py)
    dp = Dispatcher(storage=PgStorage())
    dp.message.outer_middleware(Auth())
    dp.callback_query.outer_middleware(Auth())

    is_admin = F.from_user.id.in_(config.ADMIN_IDS)
    handlers_admin.router.message.filter(is_admin)
    handlers_admin.router.callback_query.filter(is_admin)
    handlers_worker.router.message.filter(~is_admin)
    handlers_worker.router.callback_query.filter(~is_admin)

    dp.include_router(common)
    # очередь одобрения — обеим ролям, без ролевого фильтра: доступ
    # проверяется внутри, как у общих edit_router / add_router
    dp.include_router(handlers_review.router)
    dp.include_router(handlers_admin.router)
    dp.include_router(handlers_worker.edit_router)
    dp.include_router(handlers_worker.router)
    # add_router общий для обеих ролей и обязан идти последним: в нём есть
    # message(Add.name), который иначе перехватил бы «/start» посреди формы и
    # записал его в название компании. /start — заявленный выход из залипания.
    dp.include_router(handlers_worker.add_router)

    @dp.errors()
    async def on_error(event: ErrorEvent):
        log.exception("handler error: %s", event.exception)
        # без ответа падение хендлера выглядит как зависший бот: у callback'а
        # крутятся часики, на сообщение просто нет реакции
        cb = event.update.callback_query
        target = event.update.message or (cb.message if cb else None)
        try:
            if cb:
                await cb.answer()
            if target:
                await target.answer(
                    "⚠️ Что-то пошло не так. Попробуйте ещё раз или нажмите /start."
                )
        except Exception as e:
            log.warning("error notify failed: %s", e)
        return True

    me = await bot.get_me()
    # Список команд у бота пустой по умолчанию: без него в чате нет кнопки
    # «Меню», и /start приходится набирать руками. Не критично для запуска,
    # поэтому отказ только логируется.
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Начать заново"),
            BotCommand(command="queue", description="Очередь писем"),
        ])
        # /costs видит только админ: работникам команда всё равно не ответит
        # (роутер отфильтрован), нечего ей делать и в их меню
        for admin_id in config.ADMIN_IDS:
            await bot.set_my_commands(
                [
                    BotCommand(command="start", description="Начать заново"),
                    BotCommand(command="queue", description="Очередь писем"),
                    BotCommand(command="costs", description="Расходы на ИИ за месяц"),
                    BotCommand(command="subs", description="Подписки на доп-услуги"),
                    BotCommand(command="scout", description="Скаут: страна ниша город"),
                    BotCommand(command="scout_paste",
                               description="Скаут: домены из Ads Transparency"),
                ],
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
    except Exception as e:
        log.warning("set_my_commands failed: %s", e)
    try:
        purged = await purge_stale_fsm()
        if purged:
            log.info("purged %s stale fsm rows", purged)
    except Exception as e:
        log.warning("fsm purge failed: %s", e)
    log.info(
        "bot started: @%s, режим %s",
        me.username, "ТЕСТОВЫЙ" if config.TEST_MODE else "боевой",
    )
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
