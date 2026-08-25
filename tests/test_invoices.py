"""Счета подписки: цикл, напоминания и предупреждение о следующем
(12.29, 12.16, 12.30).

Хендлеры зовутся напрямую — декоратор aiogram возвращает функцию как есть;
Telegram имитируют FakeMsg/FakeCb и FakeBot. Наружу тут не уходит ничего и
уйти не может: счёт — внутренняя запись, а все адресаты уведомлений это чаты
админа и работника.

База общая, и цикл соседнего теста тоже попадает в заход, поэтому всё
проверяется по своему лиду: счета фильтруются по lead_id, уведомления — по
имени компании в тексте.
"""
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

import billing
import config
from handlers_admin import INVOICE_USAGE, invoice_cmd, invoice_paid
from conftest import TEST_TG_BASE
from models import (
    OPEN_INVOICE_STATUSES, Invoice, Lead, LeadEvent, Sale, Session, Worker,
)
from test_reject_reason import ADMIN, FakeCb, FakeMsg, FakeState
from test_suppression_log import cmd


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


async def _sold(make_lead, worker_id, currency="USD"):
    """Проданный лид со строкой продажи — то, на что подписку вообще включают."""
    async with Session() as s, s.begin():
        lead = await make_lead(s, status="sold")
        sale = Sale(lead_id=lead.id, worker_id=worker_id,
                    deal_amount=Decimal("600.00"), currency=currency,
                    rate_pct=20, amount_due=Decimal("120.00"))
        s.add(sale)
        await s.flush()
    return lead, sale.id


async def _cmd(args: str) -> FakeMsg:
    msg = FakeMsg()
    await invoice_cmd(msg, FakeState(), cmd(args))
    return msg


async def _sale(sale_id: int) -> Sale:
    async with Session() as s:
        return await s.get(Sale, sale_id)


async def _invoices(lead_id: int) -> list[Invoice]:
    async with Session() as s:
        return list(await s.scalars(
            select(Invoice).where(Invoice.lead_id == lead_id)
            .order_by(Invoice.id)
        ))


async def _events(lead_id: int) -> list[str]:
    async with Session() as s:
        return list(await s.scalars(
            select(LeadEvent.event).where(LeadEvent.lead_id == lead_id)
        ))


async def _rewind(sale_id: int, **kw):
    """Отмотать календарь продажи: время в тестах двигается только так."""
    async with Session() as s, s.begin():
        await s.execute(update(Sale).where(Sale.id == sale_id).values(**kw))


async def _rewind_invoice(invoice_id: int, **kw):
    async with Session() as s, s.begin():
        await s.execute(update(Invoice).where(Invoice.id == invoice_id)
                        .values(**kw))


def _mine(bot: FakeBot, lead) -> list[str]:
    return [text for _, text in bot.sent if f"#{lead.id} " in text]


def _now() -> datetime:
    return datetime.now(config.TZ)


# --- включение цикла (12.29) --------------------------------------------------

async def test_cycle_starts_only_on_a_sold_lead(make_lead, worker_id):
    async with Session() as s, s.begin():
        lead = await make_lead(s)

    msg = await _cmd(f"on {lead.id} 40")

    assert "Продажи" in msg.sent[0] and "нет" in msg.sent[0]
    assert await _invoices(lead.id) == []


async def test_cycle_start_sets_the_calendar(make_lead, worker_id):
    lead, sale_id = await _sold(make_lead, worker_id)

    msg = await _cmd(f"on {lead.id} 40.50")

    sale = await _sale(sale_id)
    assert sale.sub_amount == Decimal("40.50")
    assert sale.sub_started_at is not None and sale.sub_cancelled_at is None
    # первый счёт выставляется ближайшим заходом, а не через месяц: подписка
    # начинается тогда, когда её включили
    assert sale.sub_next_at <= _now()
    assert "40.50 USD/мес" in msg.sent[0]
    assert "sub_cycle_on" in await _events(lead.id)


async def test_second_start_does_not_reset_the_cycle(make_lead, worker_id):
    lead, sale_id = await _sold(make_lead, worker_id)
    await _cmd(f"on {lead.id} 40")

    msg = await _cmd(f"on {lead.id} 90")

    assert "уже идёт" in msg.sent[0]
    assert (await _sale(sale_id)).sub_amount == Decimal("40")


@pytest.mark.parametrize("amount", ["0", "-5", "40.505", "много"])
async def test_amount_must_be_a_real_price(make_lead, worker_id, amount):
    lead, sale_id = await _sold(make_lead, worker_id)

    await _cmd(f"on {lead.id} {amount}")

    assert (await _sale(sale_id)).sub_amount is None


async def test_the_database_refuses_a_free_invoice(make_lead, worker_id):
    lead, sale_id = await _sold(make_lead, worker_id)
    with pytest.raises(IntegrityError):
        async with Session() as s, s.begin():
            s.add(Invoice(lead_id=lead.id, sale_id=sale_id,
                          period_start=_now(), amount=Decimal("0")))


# --- выставление счёта (12.29) ------------------------------------------------

async def test_a_due_period_makes_one_invoice(make_lead, worker_id):
    lead, sale_id = await _sold(make_lead, worker_id, currency="EUR")
    await _cmd(f"on {lead.id} 40")
    bot = FakeBot()

    await billing.tick(bot)

    invoices = await _invoices(lead.id)
    assert len(invoices) == 1
    invoice = invoices[0]
    assert invoice.status == "issued" and invoice.amount == Decimal("40")
    # валюта счёта — валюта сделки, а не «доллары по умолчанию»
    assert invoice.currency == "EUR"
    assert invoice.due_at - invoice.period_start == timedelta(
        days=config.INVOICE_DUE_DAYS)
    sale = await _sale(sale_id)
    assert sale.sub_next_at > _now()
    assert "invoice_issued" in await _events(lead.id)
    assert any("Счёт" in text for text in _mine(bot, lead))


async def test_a_second_pass_does_not_double_the_invoice(make_lead, worker_id):
    lead, _ = await _sold(make_lead, worker_id)
    await _cmd(f"on {lead.id} 40")

    await billing.tick(FakeBot())
    await billing.tick(FakeBot())

    assert len(await _invoices(lead.id)) == 1


async def test_missed_months_catch_up(make_lead, worker_id):
    lead, sale_id = await _sold(make_lead, worker_id)
    await _cmd(f"on {lead.id} 40")
    # бот стоял два месяца: долг клиента от этого не исчез
    await _rewind(sale_id, sub_next_at=_now() - timedelta(days=62))

    await billing.tick(FakeBot())

    periods = [i.period_start for i in await _invoices(lead.id)]
    assert len(periods) == 3
    assert periods == sorted(periods)
    assert (await _sale(sale_id)).sub_next_at > _now()


async def test_a_cancelled_cycle_issues_nothing(make_lead, worker_id):
    lead, sale_id = await _sold(make_lead, worker_id)
    await _cmd(f"on {lead.id} 40")
    await billing.tick(FakeBot())

    msg = await _cmd(f"off {lead.id}")
    await _rewind(sale_id, sub_next_at=_now() - timedelta(days=1))
    await billing.tick(FakeBot())

    assert len(await _invoices(lead.id)) == 1
    # выставленный счёт — это долг: остановка цикла его не снимает
    assert "Открытых счетов: 1" in msg.sent[0]
    assert "sub_cycle_off" in await _events(lead.id)


async def test_off_without_a_cycle_says_so(make_lead, worker_id):
    lead, _ = await _sold(make_lead, worker_id)

    msg = await _cmd(f"off {lead.id}")

    assert "Подписки" in msg.sent[0] and "нет" in msg.sent[0]


def test_next_month_keeps_the_day_inside_the_month():
    january = datetime(2026, 1, 31, 12, 0, tzinfo=config.TZ)
    february = billing.next_month(january)
    assert (february.month, february.day) == (2, 28)
    assert billing.next_month(datetime(2026, 12, 15, tzinfo=config.TZ)).year == 2027


# --- отметки оплаты (12.29) ---------------------------------------------------

async def test_money_arrived_closes_the_invoice(make_lead, worker_id):
    lead, _ = await _sold(make_lead, worker_id)
    await _cmd(f"on {lead.id} 40")
    await billing.tick(FakeBot())
    invoice = (await _invoices(lead.id))[0]

    msg = await _cmd(f"paid {invoice.id}")

    async with Session() as s:
        after = await s.get(Invoice, invoice.id)
    assert after.status == "paid" and after.paid_at is not None
    assert "оплачен" in msg.sent[0]
    assert "invoice_paid" in await _events(lead.id)


async def test_the_paid_button_is_idempotent(make_lead, worker_id):
    lead, _ = await _sold(make_lead, worker_id)
    await _cmd(f"on {lead.id} 40")
    await billing.tick(FakeBot())
    invoice = (await _invoices(lead.id))[0]

    await invoice_paid(FakeCb(f"ipd:{invoice.id}"))
    async with Session() as s:
        first = (await s.get(Invoice, invoice.id)).paid_at
    again = FakeCb(f"ipd:{invoice.id}")
    await invoice_paid(again)

    async with Session() as s:
        after = await s.get(Invoice, invoice.id)
    assert after.paid_at == first
    assert "уже оплачен" in again.message.sent[0]


async def test_cancelling_an_invoice_takes_it_off_the_books(make_lead,
                                                            worker_id):
    lead, _ = await _sold(make_lead, worker_id)
    await _cmd(f"on {lead.id} 40")
    await billing.tick(FakeBot())
    invoice = (await _invoices(lead.id))[0]

    await _cmd(f"cancel {invoice.id}")

    async with Session() as s:
        after = await s.get(Invoice, invoice.id)
    assert after.status == "cancelled" and after.cancelled_at is not None


async def test_unknown_arguments_show_the_format():
    msg = await _cmd("сделай хорошо")
    assert msg.sent[0] == INVOICE_USAGE


# --- напоминания (12.16) ------------------------------------------------------

async def _overdue(make_lead, worker_id) -> tuple:
    lead, sale_id = await _sold(make_lead, worker_id)
    await _cmd(f"on {lead.id} 40")
    await billing.tick(FakeBot())
    invoice = (await _invoices(lead.id))[0]
    await _rewind_invoice(invoice.id, due_at=_now() - timedelta(days=1))
    return lead, invoice.id


async def test_unpaid_invoice_reminds_admins_and_the_worker(make_lead,
                                                            worker_id):
    lead, invoice_id = await _overdue(make_lead, worker_id)
    bot = FakeBot()

    await billing.remind_unpaid(bot)

    async with Session() as s:
        invoice = await s.get(Invoice, invoice_id)
        worker = await s.get(Worker, worker_id)
    assert invoice.status == "overdue" and invoice.reminders == 1
    assert invoice.reminded_at is not None
    told = {chat for chat, text in bot.sent if f"#{lead.id} " in text}
    assert told == {ADMIN, worker.tg_id}
    assert "invoice_reminded" in await _events(lead.id)


async def test_reminders_are_throttled(make_lead, worker_id):
    lead, invoice_id = await _overdue(make_lead, worker_id)
    await billing.remind_unpaid(FakeBot())

    silent = FakeBot()
    await billing.remind_unpaid(silent)

    assert _mine(silent, lead) == []
    # прошёл интервал троттлинга — напоминание повторяется
    await _rewind_invoice(invoice_id, reminded_at=_now() - timedelta(
        days=config.INVOICE_REMIND_EVERY_DAYS + 1))
    again = FakeBot()
    await billing.remind_unpaid(again)

    async with Session() as s:
        invoice = await s.get(Invoice, invoice_id)
    assert invoice.reminders == 2 and _mine(again, lead)


async def test_a_paid_invoice_is_never_reminded(make_lead, worker_id):
    lead, invoice_id = await _overdue(make_lead, worker_id)
    await _cmd(f"paid {invoice_id}")
    bot = FakeBot()

    await billing.remind_unpaid(bot)

    async with Session() as s:
        invoice = await s.get(Invoice, invoice_id)
    assert invoice.reminders == 0 and _mine(bot, lead) == []


# --- предупреждение о следующем счёте (12.30) ---------------------------------

async def test_notice_goes_out_before_the_next_invoice(make_lead, worker_id):
    lead, sale_id = await _sold(make_lead, worker_id)
    await _cmd(f"on {lead.id} 40")
    await billing.tick(FakeBot())
    await _rewind(sale_id, sub_next_at=_now() + timedelta(
        days=config.INVOICE_NOTICE_DAYS - 1))
    bot = FakeBot()

    await billing.notify_upcoming(bot)

    told = _mine(bot, lead)
    assert len(told) == 1
    # предупредили — значит дали и выйти: команда отмены названа в тексте
    assert f"/invoice off {lead.id}" in told[0]
    assert (await _sale(sale_id)).sub_notified_at is not None
    # адресат внутренний: клиенту бот не пишет
    assert {chat for chat, _ in bot.sent} == {ADMIN}


async def test_notice_is_sent_once_per_invoice(make_lead, worker_id):
    lead, sale_id = await _sold(make_lead, worker_id)
    await _cmd(f"on {lead.id} 40")
    await billing.tick(FakeBot())
    await _rewind(sale_id, sub_next_at=_now() + timedelta(days=1))
    await billing.notify_upcoming(FakeBot())

    bot = FakeBot()
    await billing.notify_upcoming(bot)

    assert _mine(bot, lead) == []


async def test_a_distant_invoice_is_not_announced(make_lead, worker_id):
    lead, sale_id = await _sold(make_lead, worker_id)
    await _cmd(f"on {lead.id} 40")
    await billing.tick(FakeBot())
    bot = FakeBot()

    await billing.notify_upcoming(bot)

    # следующий счёт через месяц: предупреждать о нём сегодня незачем
    assert _mine(bot, lead) == []


async def test_a_cancelled_cycle_is_not_announced(make_lead, worker_id):
    lead, sale_id = await _sold(make_lead, worker_id)
    await _cmd(f"on {lead.id} 40")
    await _cmd(f"off {lead.id}")
    await _rewind(sale_id, sub_next_at=_now() + timedelta(days=1))
    bot = FakeBot()

    await billing.notify_upcoming(bot)

    assert _mine(bot, lead) == []


# --- ручной заход -------------------------------------------------------------

async def test_run_reports_what_it_did(make_lead, worker_id):
    lead, _ = await _sold(make_lead, worker_id)
    await _cmd(f"on {lead.id} 40")

    msg = await _cmd("run")

    assert "Выставлено счетов" in msg.sent[0]
    assert len(await _invoices(lead.id)) == 1


async def _close_other_invoices(lead_id: int):
    """Список показывает первые PAGE_SIZE счетов, а соседние тесты оставляют
    свои открытыми. Закрываются только счета лидов pytest — ручные строки
    тестовой базы переживают прогон, как и договорено в conftest."""
    async with Session() as s, s.begin():
        pytest_leads = select(Lead.id).where(Lead.worker_id.in_(
            select(Worker.id).where(Worker.tg_id >= TEST_TG_BASE)
        ))
        await s.execute(
            update(Invoice)
            .where(Invoice.lead_id.in_(pytest_leads),
                   Invoice.lead_id != lead_id,
                   Invoice.status.in_(OPEN_INVOICE_STATUSES))
            .values(status="cancelled", cancelled_at=_now())
        )


async def test_list_shows_open_invoices_and_cycles(make_lead, worker_id):
    lead, _ = await _sold(make_lead, worker_id)
    await _cmd(f"on {lead.id} 40")
    await billing.tick(FakeBot())
    await _close_other_invoices(lead.id)

    msg = await _cmd("")

    invoice = (await _invoices(lead.id))[0]
    assert f"#{invoice.id}" in msg.sent[0] and "Подписок:" in msg.sent[0]
    assert msg.markups[0].inline_keyboard
