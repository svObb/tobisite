"""FSM-хранилище aiogram в Postgres вместо MemoryStorage.

MemoryStorage терял состояние у всех при каждом деплое: работник на шаге 11/12
формы получал молчание, а следующее его сообщение падало в пустоту. Redis ради
одного словаря — лишний контейнер на CX22; Postgres у бота уже есть, и формы
переживают перезапуск бесплатно.

Строка на пользователя (таблица fsm_states): state — имя шага, data — JSON
накопленных полей. Всё, что кладётся в FSM, обязано быть JSON-сериализуемым —
сейчас это строки, числа и списки словарей (contacts, фильтры админа).
"""
import json

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StorageKey, StateType
from sqlalchemy import delete, func, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from models import FsmState, Session

# отличить «колонку не трогаем» от «записать NULL»
_UNSET = object()


def _key(key: StorageKey) -> str:
    """StorageKey → строковый первичный ключ.

    bot_id в ключе обязателен: тестовый и боевой боты в одной базе не должны
    видеть формы друг друга (сейчас базы разные, но ключ этого не знает).
    """
    return ":".join(str(p) for p in (
        key.bot_id, key.chat_id, key.user_id, key.thread_id or 0, key.destiny,
    ))


async def _upsert(k: str, *, state_value=_UNSET, data_value=_UNSET):
    values = {"key": k}
    if state_value is not _UNSET:
        values["state"] = state_value
    if data_value is not _UNSET:
        values["data"] = data_value
    # updated_at руками: onupdate модели не срабатывает в ветке ON CONFLICT
    changed = {c: values[c] for c in values if c != "key"}
    changed["updated_at"] = func.now()
    stmt = pg_insert(FsmState).values(**values).on_conflict_do_update(
        index_elements=[FsmState.key], set_=changed
    )
    async with Session() as s, s.begin():
        await s.execute(stmt)


class PgStorage(BaseStorage):
    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        value = state.state if isinstance(state, State) else state
        await _upsert(_key(key), state_value=value)

    async def get_state(self, key: StorageKey) -> str | None:
        async with Session() as s:
            row = await s.get(FsmState, _key(key))
        return row.state if row else None

    async def set_data(self, key: StorageKey, data: dict) -> None:
        value = json.dumps(data, ensure_ascii=False) if data else None
        await _upsert(_key(key), data_value=value)

    async def get_data(self, key: StorageKey) -> dict:
        async with Session() as s:
            row = await s.get(FsmState, _key(key))
        return json.loads(row.data) if row and row.data else {}

    async def close(self) -> None:
        # соединениями владеет models.engine, закрывать здесь нечего
        pass


async def purge_stale_fsm(days: int = 30) -> int:
    """Убирает формы, к которым никто не прикасался days дней. Зовётся на старте.

    Брошенная на середине форма месячной давности уже никому не нужна, а таблица
    без чистки растёт на каждого написавшего боту.
    """
    async with Session() as s, s.begin():
        result = await s.execute(
            delete(FsmState).where(
                FsmState.updated_at < func.now() - text(f"interval '{int(days)} days'")
            )
        )
    return result.rowcount or 0
