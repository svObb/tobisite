"""PgStorage: формы переживают перезапуск бота (дефект 6.6)."""
import itertools

from aiogram.fsm.storage.base import StorageKey

from conftest import FSM_BOT_ID
from fsm_storage import PgStorage

_seq = itertools.count(1)


def _key() -> StorageKey:
    n = next(_seq)
    return StorageKey(bot_id=FSM_BOT_ID, chat_id=n, user_id=n)


async def test_state_and_data_roundtrip():
    st = PgStorage()
    key = _key()
    assert await st.get_state(key) is None
    assert await st.get_data(key) == {}

    await st.set_state(key, "Add:city")
    data = {"name": "Тест ООО", "contacts": [
        {"ctype": "phone", "ctype_other": None, "value": "+380501112233"},
    ]}
    await st.set_data(key, data)

    assert await st.get_state(key) == "Add:city"
    assert await st.get_data(key) == data


async def test_survives_restart_and_clears():
    st = PgStorage()
    key = _key()
    await st.set_state(key, "Add:niche")
    await st.set_data(key, {"city": "Ужгород"})

    # «деплой»: новый экземпляр хранилища видит то же состояние
    fresh = PgStorage()
    assert await fresh.get_state(key) == "Add:niche"
    assert await fresh.get_data(key) == {"city": "Ужгород"}

    # update_data (дефолт BaseStorage) сливает, а не затирает
    await fresh.update_data(key, {"niche": "Юрист"})
    assert await fresh.get_data(key) == {"city": "Ужгород", "niche": "Юрист"}

    await fresh.set_state(key, None)
    assert await fresh.get_state(key) is None
    # данные живут отдельно от state — как в MemoryStorage
    assert (await fresh.get_data(key))["city"] == "Ужгород"

    await fresh.set_data(key, {})
    assert await fresh.get_data(key) == {}
