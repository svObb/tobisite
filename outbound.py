"""Экстренный стоп исходящего (1.26): один флаг, переживающий рестарт.

Флаг живёт в базе, а не в памяти процесса. «Остановить всё» — решение, которое
нельзя потерять при деплое, а деплой случается ровно тогда, когда что-то пошло
не так: в памяти он продержался бы до первого перезапуска и тихо снялся сам.

Проверять его обязана каждая точка, из которой письмо может уйти наружу.
Сегодня таких точек нет вовсе — конвейер кончается одобрением, — поэтому
проверка стоит на входе очереди и на самом одобрении: дальше по течению
ничего нет. Появится отправка — stopped() будет первой строкой её кода.
"""
import logging

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from models import Session, Setting

log = logging.getLogger(__name__)

KEY = "outbound_stop"
ON, OFF = "1", "0"
# Причина отказа — одна на все точки: человек должен узнавать её с первого
# взгляда, в какой бы части бота он на неё ни наткнулся.
REASON = "исходящее остановлено командой /stop_all"


async def stopped(session=None) -> bool:
    """Стоп включён. Чужую сессию принимает, чтобы не открывать вторую."""
    if session is not None:
        return await _read(session)
    async with Session() as s:
        return await _read(s)


async def state() -> tuple[bool, Setting | None]:
    """(включён, строка флага): кто трогал последним и когда."""
    async with Session() as s:
        row = await s.get(Setting, KEY)
    return bool(row and row.value == ON), row


async def set_stopped(on: bool, actor_tg_id: int) -> bool:
    """Поставить или снять стоп. True — состояние действительно изменилось."""
    value = ON if on else OFF
    async with Session() as s, s.begin():
        row = await s.get(Setting, KEY)
        was = bool(row and row.value == ON)
        # upsert, а не «нет строки — вставим»: двое админов могут нажать
        # кнопку одновременно, и вторая вставка упала бы на первичном ключе
        await s.execute(
            pg_insert(Setting)
            .values(key=KEY, value=value, actor_tg_id=actor_tg_id)
            .on_conflict_do_update(
                index_elements=["key"],
                set_={"value": value, "actor_tg_id": actor_tg_id,
                      "updated_at": func.now()},
            )
        )
    log.warning("исходящее %s, tg=%s", "остановлено" if on else "разрешено",
                actor_tg_id)
    return was != on


async def _read(session) -> bool:
    row = await session.get(Setting, KEY)
    return bool(row and row.value == ON)
