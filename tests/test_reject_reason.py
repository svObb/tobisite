"""Причина отклонения лида (6.17) и решение, дошедшее до работника (6.14).

Хендлеры зовутся напрямую — декоратор aiogram возвращает функцию как есть.
Telegram имитируют FakeMsg/FakeCb: сети нет, база настоящая (проверяется в том
числе CHECK-констрейнт причины).
"""
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

import config
from handlers_admin import reject_reason, status_set
from handlers_worker import fmt_lead
from models import Lead, LeadEvent, Session, Worker
from test_gap_form import card

ADMIN = 1


class FakeState:
    def __init__(self, **data):
        self.data = data
        self.state = None

    async def set_state(self, state):
        self.state = state

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return dict(self.data)

    async def clear(self):
        self.data = {}
        self.state = None


class FakeMsg:
    def __init__(self, tg_id=ADMIN):
        self.from_user = SimpleNamespace(id=tg_id)
        self.sent = []
        self.markups = []
        self.notified = []
        self.bot = SimpleNamespace(send_message=self._send)

    async def answer(self, text, reply_markup=None, **kw):
        self.sent.append(text)
        self.markups.append(reply_markup)

    async def _send(self, chat_id, text, **kw):
        self.notified.append((chat_id, text))


class FakeCb:
    def __init__(self, data, tg_id=ADMIN):
        self.data = data
        self.from_user = SimpleNamespace(id=tg_id)
        self.alerts = []
        self.message = FakeMsg(tg_id=tg_id)
        self.bot = self.message.bot

    async def answer(self, text=None, show_alert=False):
        self.alerts.append(text)


async def _lead(lead_id) -> Lead:
    async with Session() as s:
        return await s.get(Lead, lead_id)


async def _events(lead_id, event) -> int:
    async with Session() as s:
        return await s.scalar(
            select(func.count()).select_from(LeadEvent)
            .where(LeadEvent.lead_id == lead_id, LeadEvent.event == event)
        )


def _buttons(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


async def test_rejected_asks_for_the_reason_before_writing(make_lead):
    async with Session() as s, s.begin():
        lid = (await make_lead(s)).id

    cb = FakeCb(f"stv:{lid}:rejected")
    await status_set(cb, FakeState())

    assert (await _lead(lid)).status == "new"  # статус ещё не тронут
    assert "Почему отклоняем?" in cb.message.sent[0]
    assert f"rjr:{lid}:no_contact" in _buttons(cb.message.markups[0])


async def test_reason_lands_in_the_lead_and_in_history(make_lead):
    async with Session() as s, s.begin():
        lid = (await make_lead(s)).id

    cb = FakeCb(f"rjr:{lid}:no_contact")
    await reject_reason(cb, FakeState())

    lead = await _lead(lid)
    assert (lead.status, lead.reject_reason) == ("rejected", "no_contact")
    assert await _events(lid, "reject_reason") == 1
    assert config.LEAD_REJECT_LABELS["no_contact"] in cb.message.sent[0]


async def test_worker_learns_why_his_lead_was_rejected(make_lead, worker_id):
    async with Session() as s, s.begin():
        lead = await make_lead(s)
        lid, name = lead.id, lead.name
        tg_id = (await s.get(Worker, worker_id)).tg_id

    cb = FakeCb(f"rjr:{lid}:not_our_niche")
    await reject_reason(cb, FakeState())

    (chat_id, text), = cb.message.notified
    assert chat_id == tg_id
    assert name in text and config.LEAD_REJECT_LABELS["not_our_niche"] in text


async def test_unknown_reason_is_refused(make_lead):
    async with Session() as s, s.begin():
        lid = (await make_lead(s)).id

    cb = FakeCb(f"rjr:{lid}:потому_что")
    await reject_reason(cb, FakeState())

    assert cb.alerts == ["Неизвестная причина"]
    assert (await _lead(lid)).status == "new"


async def test_base_refuses_a_reason_outside_the_enum(make_lead):
    with pytest.raises(IntegrityError):
        async with Session() as s, s.begin():
            await make_lead(s, status="rejected", reject_reason="выдуманная")


async def test_next_status_clears_the_reason(make_lead):
    async with Session() as s, s.begin():
        lid = (await make_lead(s)).id
    await reject_reason(FakeCb(f"rjr:{lid}:closed"), FakeState())

    await status_set(FakeCb(f"stv:{lid}:replied"), FakeState())

    lead = await _lead(lid)
    assert (lead.status, lead.reject_reason) == ("replied", None)


def test_card_shows_the_reason():
    text = fmt_lead(card(status="rejected", reject_reason="site_is_fine"), [])
    assert config.LEAD_REJECT_LABELS["site_is_fine"] in text
    assert "Причина отклонения" in text


def test_card_without_rejection_says_nothing_about_it():
    assert "Причина отклонения" not in fmt_lead(card(), [])
