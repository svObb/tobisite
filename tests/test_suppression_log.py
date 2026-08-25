"""Стоп-лист и журнал отписок (9.27, 9.34): чем закрыли, когда и по чьей просьбе.

Хендлеры зовутся напрямую — декоратор aiogram возвращает функцию как есть;
Telegram имитирует FakeMsg. Отправки писем в проекте нет, поэтому «исполнение
≤2 дней» из 9.30 проверяется тем, что карточки лида уходят с очереди в той же
транзакции, что и запись в стоп-лист.
"""
import itertools
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

import email_gen
from handlers_admin import stop_cmd, stops_cmd
from models import (
    Contact, LeadEvent, MessageDraft, Session, Suppression, SuppressionEvent,
    company_key, email_key, suppress_lead, suppression_hit,
)
from test_email_gen import UK_DRAFT, UK_JSON
from test_reject_reason import ADMIN, FakeMsg, FakeState

# домен и адрес у каждого лида свои: закрытый в одном тесте ключ иначе
# уменьшил бы счёт закрытых значений в следующем
_seq = itertools.count(1)


def cmd(args: str):
    return SimpleNamespace(args=args)


async def _lead_with_contacts(gap_lead, **kw):
    n = next(_seq)
    lead = await gap_lead(domain_norm=kw.pop("domain_norm", f"stop-{n}.example"),
                          **kw)
    lead.contact_email = f"office@stop-{n}.example"
    async with Session() as s, s.begin():
        s.add(Contact(lead_id=lead.id, ctype="email", value=lead.contact_email))
    return lead


async def _suppressed(want) -> set[tuple[str, str]]:
    async with Session() as s:
        rows = await s.execute(
            select(Suppression.kind, Suppression.value_norm)
            .where(Suppression.value_norm.in_([v for _, v in want]))
        )
    return {tuple(r) for r in rows}


async def _journal(lead_id) -> list[SuppressionEvent]:
    async with Session() as s:
        return list(await s.scalars(
            select(SuppressionEvent)
            .where(SuppressionEvent.lead_id == lead_id)
            .order_by(SuppressionEvent.id)
        ))


async def _draft_status(draft_id) -> str:
    async with Session() as s:
        return (await s.get(MessageDraft, draft_id)).status


# --- механика ----------------------------------------------------------------

async def test_unsubscribe_closes_address_domain_and_company(gap_lead):
    lead = await _lead_with_contacts(gap_lead)

    async with Session() as s, s.begin():
        added = await suppress_lead(s, lead, event="unsubscribe",
                                    source="pytest")

    want = {("company", company_key(lead.name, lead.city)),
            ("domain", lead.domain_norm),
            ("email_hash", email_key(lead.contact_email))}
    assert added == 3
    assert await _suppressed(want) == want


async def test_company_without_site_is_closed_by_name_and_city(gap_lead):
    lead = await gap_lead()

    async with Session() as s, s.begin():
        added = await suppress_lead(s, lead, event="complaint", source="pytest")

    # закрывать нечего, кроме «имя+город» — и этого достаточно, чтобы письмо
    # не собралось: suppression_hit смотрит по всем трём пространствам
    assert added == 1
    async with Session() as s:
        assert await suppression_hit(s, lead)


async def test_repeat_request_adds_nothing_but_lands_in_the_journal(gap_lead):
    lead = await _lead_with_contacts(gap_lead)

    async with Session() as s, s.begin():
        await suppress_lead(s, lead, event="unsubscribe", source="pytest")
    async with Session() as s, s.begin():
        again = await suppress_lead(s, lead, event="complaint", source="pytest")

    assert again == 0  # ключи те же
    assert [e.event for e in await _journal(lead.id)] == ["unsubscribe",
                                                          "complaint"]


async def test_journal_remembers_when_who_and_why(gap_lead):
    lead = await _lead_with_contacts(gap_lead)

    async with Session() as s, s.begin():
        await suppress_lead(s, lead, event="complaint", source="pytest",
                            note="звонил, ругался", actor_tg_id=ADMIN)

    entry, = await _journal(lead.id)
    assert (entry.event, entry.source, entry.actor_tg_id) == (
        "complaint", "pytest", ADMIN)
    assert entry.note == "звонил, ругался" and entry.created_at


async def test_event_outside_the_enum_is_refused(gap_lead):
    lead = await gap_lead()
    with pytest.raises(IntegrityError):
        async with Session() as s, s.begin():
            await suppress_lead(s, lead, event="обиделся", source="pytest")


async def test_suppressed_lead_gets_no_letter(model, gap_lead):
    fake = model(UK_JSON)
    lead = await _lead_with_contacts(gap_lead)
    async with Session() as s, s.begin():
        await suppress_lead(s, lead, event="unsubscribe", source="pytest")

    result = await email_gen.build_email(lead, UK_DRAFT)

    assert result.needs_manual and "стоп-лист" in result.reason
    assert fake.messages.calls == []


# --- команды -----------------------------------------------------------------

async def test_stop_closes_the_lead_and_says_what_it_closed(gap_lead):
    lead = await _lead_with_contacts(gap_lead)
    msg = FakeMsg()

    await stop_cmd(msg, FakeState(), cmd(f"{lead.id} complaint слишком часто"))

    assert "жалоба" in msg.sent[0] and "Закрыто значений: 3" in msg.sent[0]
    entry, = await _journal(lead.id)
    assert entry.event == "complaint" and entry.note == "слишком часто"


async def test_stop_without_a_word_is_an_unsubscribe(gap_lead):
    lead = await gap_lead()
    msg = FakeMsg()

    await stop_cmd(msg, FakeState(), cmd(f"{lead.id} просил больше не писать"))

    entry, = await _journal(lead.id)
    # заметку не съедаем: первое слово не вид события, значит это уже текст
    assert entry.event == "unsubscribe"
    assert entry.note == "просил больше не писать"


async def test_stop_takes_the_queue_card_off(gap_lead):
    lead = await gap_lead()
    async with Session() as s, s.begin():
        draft = MessageDraft(lead_id=lead.id, touch_number=1, lang="uk")
        s.add(draft)
        await s.flush()
        draft_id = draft.id

    await stop_cmd(FakeMsg(), FakeState(), cmd(f"{lead.id} unsub"))

    assert await _draft_status(draft_id) == "cancelled"


async def test_stop_writes_the_lead_history(gap_lead):
    lead = await gap_lead()

    await stop_cmd(FakeMsg(), FakeState(), cmd(f"{lead.id} manual"))

    async with Session() as s:
        count = await s.scalar(
            select(func.count()).select_from(LeadEvent)
            .where(LeadEvent.lead_id == lead.id, LeadEvent.event == "suppressed")
        )
    assert count == 1


async def test_stop_on_an_unknown_lead_says_so():
    msg = FakeMsg()
    await stop_cmd(msg, FakeState(), cmd("999999999"))
    assert "не найден" in msg.sent[0]
    assert not await _journal(999999999)


@pytest.mark.parametrize("args", ["", "лид 42"])
async def test_stop_without_an_id_shows_the_format(args):
    msg = FakeMsg()
    await stop_cmd(msg, FakeState(), cmd(args))
    assert "Формат:" in msg.sent[0]


async def test_stops_shows_the_journal(gap_lead):
    lead = await _lead_with_contacts(gap_lead)
    await stop_cmd(FakeMsg(), FakeState(), cmd(f"{lead.id} unsub ответ на письмо"))

    msg = FakeMsg()
    await stops_cmd(msg, FakeState(), cmd(""))

    assert lead.name in msg.sent[0]
    assert "отписка" in msg.sent[0] and "ответ на письмо" in msg.sent[0]
