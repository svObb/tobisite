"""6.9/6.10: кнопки rst:/hst: из старых сообщений на удалённом лиде.

Хендлеры зовутся напрямую (декоратор aiogram возвращает функцию как есть),
Telegram имитирует FakeCb — сеть не нужна, база нужна.
"""
from datetime import datetime
from types import SimpleNamespace

import config
from handlers_admin import history, restore_lead
from handlers_worker import STALE
from models import Lead, Session, log_event


class FakeCb:
    def __init__(self, data):
        self.data = data
        self.from_user = SimpleNamespace(id=1)
        self.alerts = []
        self.message = SimpleNamespace(sent=[], answer=self._answer_msg)

    async def _answer_msg(self, text, **kw):
        self.message.sent.append(text)

    async def answer(self, text=None, show_alert=False):
        self.alerts.append(text)


async def test_history_hidden_for_deleted_lead(make_lead):
    async with Session() as s, s.begin():
        lead = await make_lead(s)
        log_event(s, lead.id, "add", 1)
        lead.deleted_at = datetime.now(config.TZ)
        lid = lead.id

    cb = FakeCb(f"hst:{lid}")
    await history(cb)
    assert cb.alerts == [STALE]
    assert cb.message.sent == []


async def test_history_still_works_for_alive_lead(make_lead):
    async with Session() as s, s.begin():
        lead = await make_lead(s)
        log_event(s, lead.id, "add", 1)
        lid = lead.id

    cb = FakeCb(f"hst:{lid}")
    await history(cb)
    assert cb.alerts == [None]  # обычный cb.answer() без алерта
    assert any("История" in t for t in cb.message.sent)


async def test_restore_refused_for_deleted_lead(make_lead):
    now = datetime.now(config.TZ)
    async with Session() as s, s.begin():
        lead = await make_lead(s)
        lead.cancelled_at = now
        lead.cancelled_by = 1
        lead.deleted_at = now
        lid = lead.id

    cb = FakeCb(f"rst:{lid}")
    await restore_lead(cb)
    assert cb.alerts == [STALE]
    async with Session() as s:
        lead = await s.get(Lead, lid)
        # осталась и отменённой, и удалённой — состояние не «раздвоилось»
        assert lead.cancelled_at is not None and lead.deleted_at is not None


async def test_restore_still_works_for_cancelled_lead(make_lead):
    async with Session() as s, s.begin():
        lead = await make_lead(s)
        lead.cancelled_at = datetime.now(config.TZ)
        lead.cancelled_by = 1
        lid = lead.id

    cb = FakeCb(f"rst:{lid}")
    await restore_lead(cb)
    assert cb.alerts == ["Восстановлено"]
    async with Session() as s:
        assert (await s.get(Lead, lid)).cancelled_at is None
