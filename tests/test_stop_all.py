"""Экстренный стоп исходящего (1.26): флаг в базе, а не в памяти процесса.

Хендлеры зовутся напрямую, Telegram имитируют FakeMsg/FakeCb. Проверяется не
то, что кнопка нажалась, а то, что при включённом флаге письмо не собирается и
не одобряется, и что бот, поднятый заново, застаёт стоп на месте.
"""
import asyncio

import pytest
from sqlalchemy import delete, select

import config
import email_gen
import keyboards as kb
import outbound
import queue_service as qs
from conftest import wipe_cards
from handlers_admin import stop_all_cmd, stop_all_set
from models import MessageDraft, Session, Setting
from test_email_gen import UK_DRAFT, UK_JSON
from test_reject_reason import ADMIN, FakeCb, FakeMsg, FakeState

REVIEWER = 700_101


@pytest.fixture(autouse=True)
def clean_switch():
    """Флаг и очередь — общие на всю базу: свой мусор убираем с обеих сторон."""
    asyncio.run(_wipe())
    yield
    asyncio.run(_wipe())


async def _wipe():
    async with Session() as s, s.begin():
        await s.execute(delete(Setting).where(Setting.key == outbound.KEY))
    await wipe_cards()


async def _queued(gap_lead) -> tuple[int, int]:
    lead = await gap_lead(status="verified")
    result = await qs.enqueue(lead.id, actor_tg_id=REVIEWER,
                              draft_summary=UK_DRAFT)
    assert result.ok, result.reason
    async with Session() as s:
        draft = await s.get(MessageDraft, result.draft_id)
    return draft.id, draft.shown_version_id


def _buttons(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


# --- флаг ---------------------------------------------------------------------

async def test_the_switch_is_written_down():
    await outbound.set_stopped(True, ADMIN)

    async with Session() as s:
        row = await s.get(Setting, outbound.KEY)
    assert row.value == outbound.ON and row.actor_tg_id == ADMIN
    assert await outbound.stopped()


async def test_a_stop_set_before_the_start_still_holds():
    """Рестарт: строку поставил прошлый процесс, этот её не ставил ни разу."""
    async with Session() as s, s.begin():
        s.add(Setting(key=outbound.KEY, value=outbound.ON, actor_tg_id=ADMIN))

    assert await outbound.stopped()


async def test_lifting_the_stop_is_written_down_too():
    await outbound.set_stopped(True, ADMIN)

    changed = await outbound.set_stopped(False, ADMIN)

    assert changed and not await outbound.stopped()


async def test_the_same_switch_twice_changes_nothing():
    assert await outbound.set_stopped(True, ADMIN)
    assert not await outbound.set_stopped(True, ADMIN)


# --- очередь при включённом стопе ---------------------------------------------

async def test_a_stopped_bot_builds_no_letter(model, gap_lead):
    fake = model(UK_JSON)
    lead = await gap_lead(status="verified")
    await outbound.set_stopped(True, ADMIN)

    result = await qs.enqueue(lead.id, actor_tg_id=REVIEWER,
                              draft_summary=UK_DRAFT)

    assert not result.ok and result.reason == outbound.REASON
    # отказ стоит до генерации: ни карточки, ни вызова модели
    assert result.draft_id is None and fake.messages.calls == []


async def test_a_stop_during_generation_is_still_in_time(model, gap_lead,
                                                         monkeypatch):
    """Стоп пришёл, пока модель писала письмо: карточки всё равно не будет."""
    model(UK_JSON)
    lead = await gap_lead(status="verified")
    build = email_gen.build_email

    async def _slow_build(*args, **kw):
        result = await build(*args, **kw)
        await outbound.set_stopped(True, ADMIN)
        return result

    monkeypatch.setattr(email_gen, "build_email", _slow_build)

    result = await qs.enqueue(lead.id, actor_tg_id=REVIEWER,
                              draft_summary=UK_DRAFT)

    assert not result.ok and result.reason == outbound.REASON
    async with Session() as s:
        cards = list(await s.scalars(
            select(MessageDraft).where(MessageDraft.lead_id == lead.id)))
    assert cards == []


async def test_a_stopped_bot_approves_nothing(model, gap_lead):
    model(UK_JSON)
    draft_id, version_id = await _queued(gap_lead)
    await qs.claim_next(REVIEWER)
    await outbound.set_stopped(True, ADMIN)

    decision = await qs.approve(draft_id, version_id, REVIEWER)

    assert not decision.ok and decision.reason == outbound.REASON
    async with Session() as s:
        draft = await s.get(MessageDraft, draft_id)
    assert draft.status == "claimed"


async def test_the_queue_opens_again_after_the_stop_is_lifted(model, gap_lead):
    model(UK_JSON)
    lead = await gap_lead(status="verified")
    await outbound.set_stopped(True, ADMIN)
    await stop_all_set(FakeCb("sal:off"))

    result = await qs.enqueue(lead.id, actor_tg_id=REVIEWER,
                              draft_summary=UK_DRAFT)

    assert result.ok, result.reason


# --- команда ------------------------------------------------------------------

async def test_the_command_only_offers_the_switch():
    msg = FakeMsg()

    await stop_all_cmd(msg, FakeState())

    assert "разрешено" in msg.sent[0]
    assert _buttons(msg.markups[0]) == ["sal:on", kb.CANCEL_CB]
    # первый шаг ничего не переключает: стоп ставит только подтверждение
    assert not await outbound.stopped()


async def test_the_command_names_who_switched_it_last():
    await outbound.set_stopped(True, 4242)
    msg = FakeMsg()

    await stop_all_cmd(msg, FakeState())

    assert "остановлено" in msg.sent[0] and "4242" in msg.sent[0]
    assert _buttons(msg.markups[0]) == ["sal:off", kb.CANCEL_CB]


async def test_the_confirmation_stops_everything():
    cb = FakeCb("sal:on")

    await stop_all_set(cb)

    assert await outbound.stopped()
    assert "остановлено" in cb.message.sent[0]
    # кнопку снимают: второе нажатие того же сообщения ничего не значит
    assert cb.message.edits == [None]


async def test_the_second_admin_learns_about_the_stop(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_IDS", [ADMIN, 4242])
    cb = FakeCb("sal:on")

    await stop_all_set(cb)

    assert [chat for chat, _ in cb.message.notified] == [4242]


async def test_pressing_stop_twice_says_so(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_IDS", [ADMIN, 4242])
    await stop_all_set(FakeCb("sal:on"))

    again = FakeCb("sal:on")
    await stop_all_set(again)

    assert "и так остановлено" in again.message.sent[0]
    assert again.message.notified == [] and await outbound.stopped()
