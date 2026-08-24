"""Уведомления бота: админам о новом лиде (6.13), работнику о его лиде (6.14).

Telegram имитирует FakeBot — сети нет, наружу ничего не уходит. Проверяется и
то, о чём бот молчать обязан: о своих же действиях и о внутренних шагах
конвейера.
"""
from datetime import datetime
from types import SimpleNamespace

import config
import notify
from conftest import TEST_TG_BASE
from models import Contact, Session, Worker

SECOND = TEST_TG_BASE + 888_001


class FakeBot:
    """Отправка в Telegram. blocked — чаты, где бот получает отказ."""

    def __init__(self, blocked=()):
        self.sent = []
        self.blocked = set(blocked)

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        if chat_id in self.blocked:
            raise RuntimeError("бота заблокировали")
        self.sent.append((chat_id, text))


def two_admins(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_IDS", [config.ADMIN_TG_ID, SECOND])


# --- 6.13: новый лид ----------------------------------------------------------

async def test_new_lead_reaches_every_admin_but_the_author(monkeypatch, make_lead):
    two_admins(monkeypatch)
    async with Session() as s, s.begin():
        lead = await make_lead(s, name="Клініка Тест", niche="Стоматология")
        s.add(Contact(lead_id=lead.id, ctype="phone", value="+380000000001"))
        lid, name = lead.id, lead.name

    bot = FakeBot()
    assert await notify.new_lead(bot, lid, skip_tg_id=config.ADMIN_TG_ID) == 1
    (chat_id, text), = bot.sent
    assert chat_id == SECOND
    assert name in text and "Стоматология" in text and "Контактов: 1" in text
    # наблюдение видно сразу: по нему решают, годится ли лид под письмо
    assert "Наблюдение" in text


async def test_new_lead_notifies_both_when_author_is_a_worker(monkeypatch,
                                                              make_lead):
    two_admins(monkeypatch)
    async with Session() as s, s.begin():
        lid = (await make_lead(s)).id

    bot = FakeBot()
    assert await notify.new_lead(bot, lid, skip_tg_id=TEST_TG_BASE + 5) == 2
    assert [chat for chat, _ in bot.sent] == [config.ADMIN_TG_ID, SECOND]


async def test_blocked_admin_does_not_break_the_rest(monkeypatch, make_lead):
    two_admins(monkeypatch)
    async with Session() as s, s.begin():
        lid = (await make_lead(s)).id

    bot = FakeBot(blocked={config.ADMIN_TG_ID})
    assert await notify.new_lead(bot, lid) == 1
    assert [chat for chat, _ in bot.sent] == [SECOND]


async def test_switch_off_silences_new_lead(monkeypatch, make_lead):
    monkeypatch.setattr(config, "NOTIFY_NEW_LEAD", False)
    async with Session() as s, s.begin():
        lid = (await make_lead(s)).id

    bot = FakeBot()
    assert await notify.new_lead(bot, lid) == 0
    assert bot.sent == []


async def test_deleted_lead_is_not_announced(make_lead):
    async with Session() as s, s.begin():
        lead = await make_lead(s)
        lead.deleted_at = datetime.now(config.TZ)
        lid = lead.id

    bot = FakeBot()
    assert await notify.new_lead(bot, lid) == 0


# --- 6.14: смена статуса ------------------------------------------------------

def lead_of(lead_id=7, name="Клініка Тест"):
    return SimpleNamespace(id=lead_id, name=name)


async def worker_of(worker_id) -> Worker:
    async with Session() as s:
        return await s.get(Worker, worker_id)


async def test_worker_learns_about_the_sale(worker_id):
    worker = await worker_of(worker_id)
    bot = FakeBot()
    assert await notify.lead_status(bot, lead_of(), worker, "sent", "sold",
                                    actor_tg_id=config.ADMIN_TG_ID)
    (chat_id, text), = bot.sent
    assert chat_id == worker.tg_id
    assert "Продано" in text and "Клініка Тест" in text


async def test_rejected_carries_the_reason(worker_id):
    worker = await worker_of(worker_id)
    bot = FakeBot()
    await notify.lead_status(bot, lead_of(), worker, "new", "rejected",
                             reason="no_contact",
                             actor_tg_id=config.ADMIN_TG_ID)
    (_, text), = bot.sent
    assert config.LEAD_REJECT_LABELS["no_contact"] in text


async def test_internal_steps_stay_silent(worker_id):
    worker = await worker_of(worker_id)
    bot = FakeBot()
    for step in ("draft_ready", "sent"):
        assert not await notify.lead_status(bot, lead_of(), worker, "verified",
                                            step,
                                            actor_tg_id=config.ADMIN_TG_ID)
    assert bot.sent == []


async def test_own_action_is_not_echoed(worker_id):
    worker = await worker_of(worker_id)
    bot = FakeBot()
    assert not await notify.lead_status(bot, lead_of(), worker, "new",
                                        "verified",
                                        actor_tg_id=worker.tg_id)
    assert bot.sent == []


async def test_deleted_worker_is_not_notified(worker_id):
    async with Session() as s, s.begin():
        worker = await s.get(Worker, worker_id)
        worker.deleted_at = datetime.now(config.TZ)

    bot = FakeBot()
    assert not await notify.lead_status(bot, lead_of(), worker, "new", "sold",
                                        actor_tg_id=config.ADMIN_TG_ID)
    assert bot.sent == []
