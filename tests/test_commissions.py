"""Комиссии, продажи и начисления (раздел 7: 7.9–7.17, 7.19).

Хендлеры зовутся напрямую — декоратор aiogram возвращает функцию как есть.
Telegram имитируют FakeMsg/FakeCb: сети нет, ни одно сообщение наружу не идёт.
База нужна настоящая — здесь проверяются в том числе констрейнты.
"""
import asyncio
import csv
import pathlib
import re
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

import config
import queue_service as qs
from handlers_admin import (
    Adm, export_payouts_csv, parse_deal, sale_paid, sale_received, sale_save,
    stats, status_set, worker_commission_save,
)
from handlers_worker import my_stats
from models import (
    CommissionChange, Contact, Lead, LeadEvent, MessageDraft, Sale, Session,
    Worker, commission_due,
)
from test_email_gen import UK_DRAFT, UK_JSON

ROOT = pathlib.Path(__file__).resolve().parent.parent
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
    def __init__(self, text="", tg_id=ADMIN):
        self.text = text
        self.from_user = SimpleNamespace(id=tg_id)
        self.sent = []
        self.docs = []
        self.notified = []
        self.bot = SimpleNamespace(send_message=self._send)

    async def answer(self, text, **kw):
        self.sent.append(text)

    async def answer_document(self, document, caption=None):
        # файл читается сразу: хендлер удаляет его, как только отдал
        self.docs.append((pathlib.Path(document.path).read_text("utf-8-sig"),
                          caption))

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


async def _sell(lead_id, amount="120", state=None):
    """Перевод лида в sold и ввод суммы — ровно как это делает админ."""
    state = state or FakeState()
    cb = FakeCb(f"stv:{lead_id}:sold")
    await status_set(cb, state)
    msg = FakeMsg(amount)
    await sale_save(msg, state)
    return cb, msg


async def _sale_of(lead_id) -> Sale | None:
    async with Session() as s:
        return await s.scalar(select(Sale).where(Sale.lead_id == lead_id))


# --- статус replied_interested и констрейнты ----------------------------------

def test_new_status_sits_between_replied_and_sold():
    keys = [k for k, _ in config.STATUSES]
    assert keys.index("replied") + 1 == keys.index("replied_interested")
    assert keys.index("replied_interested") + 1 == keys.index("sold")
    assert "replied_interested" in config.ACCEPTED_STATUSES


async def test_check_constraint_lets_new_status_in(make_lead):
    async with Session() as s, s.begin():
        lead = await make_lead(s, status="replied_interested")
        lid = lead.id
    async with Session() as s:
        assert (await s.get(Lead, lid)).status == "replied_interested"


async def test_check_constraint_still_refuses_unknown_status(make_lead):
    with pytest.raises(IntegrityError):
        async with Session() as s, s.begin():
            await make_lead(s, status="почти_продал")


async def test_commission_bounds_are_enforced_by_the_base(worker_id):
    async with Session() as s:
        assert (await s.get(Worker, worker_id)).commission_pct == 20
    for bad in (14, 31):
        with pytest.raises(IntegrityError):
            async with Session() as s, s.begin():
                (await s.get(Worker, worker_id)).commission_pct = bad


def test_downgrade_refuses_while_new_status_lives(worker_id, make_lead):
    """Откат 0009 сузил бы список статусов — с живыми лидами он обязан отказать.

    Успешный откат и накат обратно проверяются вне pytest (alembic downgrade
    0008 → upgrade head): здесь схема общая для всех тестов, ронять её нельзя.
    """
    from alembic import command
    from alembic.config import Config as AlembicConfig

    async def _make():
        async with Session() as s, s.begin():
            await make_lead(s, status="replied_interested")

    asyncio.run(_make())
    cfg = AlembicConfig(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    with pytest.raises(RuntimeError, match="replied_interested"):
        command.downgrade(cfg, "0008")
    # отказ случился до единой команды DDL — схема осталась на 0009
    asyncio.run(_still_head())


async def _still_head():
    async with Session() as s:
        await s.scalar(select(func.count()).select_from(Sale))


async def test_autostop_fires_on_replied_interested(model, gap_lead):
    model(UK_JSON)
    lead = await gap_lead(status="verified")
    result = await qs.enqueue(lead.id, actor_tg_id=ADMIN,
                              draft_summary=UK_DRAFT)
    assert result.ok, result.reason

    await status_set(FakeCb(f"stv:{lead.id}:replied_interested"), FakeState())
    async with Session() as s:
        assert (await s.get(MessageDraft, result.draft_id)).status == "cancelled"


# --- продажа ------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("120", (Decimal("120"), "USD")),
    ("120 EUR", (Decimal("120"), "EUR")),
    ("120,50 eur", (Decimal("120.50"), "EUR")),
    ("0", None),
    ("-5", None),
    ("120.555", None),
    ("NaN", None),
    ("Infinity", None),
    ("сто", None),
    ("120 EUR USD", None),
    ("120 €", None),
])
def test_parse_deal(raw, expected):
    assert parse_deal(raw) == expected


async def test_sold_opens_the_amount_step(make_lead):
    async with Session() as s, s.begin():
        lid = (await make_lead(s)).id

    state = FakeState()
    cb = FakeCb(f"stv:{lid}:sold")
    await status_set(cb, state)
    assert state.state == Adm.sale
    assert state.data["lead_id"] == lid
    assert any("Сумма сделки" in t for t in cb.message.sent)


async def test_sold_writes_sale_and_notifies_worker(make_lead, worker_id):
    async with Session() as s, s.begin():
        lead = await make_lead(s)
        lid, name = lead.id, lead.name
    _, msg = await _sell(lid)

    sale = await _sale_of(lid)
    assert (sale.deal_amount, sale.currency) == (Decimal("120.00"), "USD")
    assert (sale.rate_pct, sale.amount_due) == (20, Decimal("24.00"))
    assert sale.received_at is None and sale.paid_at is None

    async with Session() as s:
        tg_id = (await s.get(Worker, worker_id)).tg_id
    (chat_id, text), = msg.notified
    assert chat_id == tg_id
    assert name in text and "20%" in text and "24.00" in text
    # честная оговорка 7.15: до оплаты клиентом это ещё не деньги работника
    assert "оплатит" in text


async def test_second_sold_does_not_create_second_sale(make_lead):
    async with Session() as s, s.begin():
        lid = (await make_lead(s)).id
    await _sell(lid)

    state = FakeState()
    cb = FakeCb(f"stv:{lid}:sold")
    await status_set(cb, state)
    # форма даже не открывается: продажа уже записана
    assert state.state is None
    assert any("уже записана" in t for t in cb.message.sent)

    async with Session() as s:
        assert await s.scalar(
            select(func.count()).select_from(Sale).where(Sale.lead_id == lid)
        ) == 1


async def test_race_on_the_same_lead_hits_unique(make_lead):
    async with Session() as s, s.begin():
        lid = (await make_lead(s)).id
    await _sell(lid)
    # состояние формы, пережившее первую запись: вторая обязана упереться
    # в uq_sales_lead, а не начислить работнику ещё раз
    msg = FakeMsg("300")
    await sale_save(msg, FakeState(lead_id=lid))
    assert any("уже записана" in t for t in msg.sent)
    assert (await _sale_of(lid)).deal_amount == Decimal("120.00")


async def test_rate_is_frozen_in_the_sale(make_lead, worker_id):
    async with Session() as s, s.begin():
        first = (await make_lead(s)).id
        second = (await make_lead(s)).id
    await _sell(first)

    msg = FakeMsg("30")
    await worker_commission_save(msg, FakeState(worker_id=worker_id))
    assert any("20% → 30%" in t for t in msg.sent)

    # 7.13: смена процента не переписывает уже записанное начисление
    assert (await _sale_of(first)).rate_pct == 20
    assert (await _sale_of(first)).amount_due == Decimal("24.00")

    await _sell(second, "200")
    assert (await _sale_of(second)).rate_pct == 30
    assert (await _sale_of(second)).amount_due == Decimal("60.00")

    async with Session() as s:
        changes = list(await s.scalars(
            select(CommissionChange).where(CommissionChange.worker_id == worker_id)
        ))
    assert [(c.old_pct, c.new_pct, c.changed_by) for c in changes] == [(20, 30, ADMIN)]


@pytest.mark.parametrize("raw", ["14", "31", "сколько", ""])
async def test_commission_outside_bounds_is_refused(worker_id, raw):
    msg = FakeMsg(raw)
    await worker_commission_save(msg, FakeState(worker_id=worker_id))
    assert any("от 15 до 30" in t for t in msg.sent)
    async with Session() as s:
        assert (await s.get(Worker, worker_id)).commission_pct == 20
        assert await s.scalar(
            select(func.count()).select_from(CommissionChange)
            .where(CommissionChange.worker_id == worker_id)
        ) == 0


async def test_same_commission_writes_no_history(worker_id):
    msg = FakeMsg("20")
    await worker_commission_save(msg, FakeState(worker_id=worker_id))
    assert any("и так 20%" in t for t in msg.sent)
    async with Session() as s:
        assert await s.scalar(
            select(func.count()).select_from(CommissionChange)
            .where(CommissionChange.worker_id == worker_id)
        ) == 0


def test_commission_due_rounds_to_a_cent():
    assert commission_due(Decimal("99.99"), 15) == Decimal("15.00")
    assert commission_due(Decimal("33.33"), 20) == Decimal("6.67")


# --- отметки «деньги пришли» и «выплачено» ------------------------------------

async def _events(lead_id, event) -> int:
    async with Session() as s:
        return await s.scalar(
            select(func.count()).select_from(LeadEvent)
            .where(LeadEvent.lead_id == lead_id, LeadEvent.event == event)
        )


async def test_paid_needs_received_first(make_lead):
    async with Session() as s, s.begin():
        lid = (await make_lead(s)).id
    await _sell(lid)
    sale_id = (await _sale_of(lid)).id

    cb = FakeCb(f"ppd:{sale_id}")
    await sale_paid(cb)
    assert cb.alerts == ["Сначала отметьте, что деньги пришли"]
    assert (await _sale_of(lid)).paid_at is None
    assert await _events(lid, "sale_paid") == 0


async def test_marks_are_idempotent(make_lead):
    async with Session() as s, s.begin():
        lid = (await make_lead(s)).id
    await _sell(lid)
    sale_id = (await _sale_of(lid)).id

    await sale_received(FakeCb(f"prc:{sale_id}"))
    received = (await _sale_of(lid)).received_at
    assert received is not None

    cb = FakeCb(f"prc:{sale_id}")
    await sale_received(cb)
    assert cb.alerts == ["Уже отмечено"]
    assert (await _sale_of(lid)).received_at == received
    assert await _events(lid, "sale_received") == 1

    await sale_paid(FakeCb(f"ppd:{sale_id}"))
    paid = (await _sale_of(lid)).paid_at
    assert paid is not None

    cb = FakeCb(f"ppd:{sale_id}")
    await sale_paid(cb)
    assert cb.alerts == ["Уже отмечено"]
    assert (await _sale_of(lid)).paid_at == paid
    assert await _events(lid, "sale_paid") == 1


# --- экраны и выгрузка --------------------------------------------------------

async def test_my_stats_shows_commission_and_payouts(make_lead, worker_id):
    async with Session() as s, s.begin():
        lid = (await make_lead(s)).id
        worker = await s.get(Worker, worker_id)
    await _sell(lid)

    msg = FakeMsg(tg_id=worker.tg_id)
    await my_stats(msg, FakeState(), worker)
    text, = msg.sent
    assert "Комиссия: 20%" in text
    # деньги от клиента не пришли — начислений в статистике ещё нет
    assert "Начислено" not in text

    await sale_received(FakeCb(f"prc:{(await _sale_of(lid)).id}"))
    msg = FakeMsg(tg_id=worker.tg_id)
    await my_stats(msg, FakeState(), worker)
    assert "Начислено USD: к выплате 24.00, выплачено 0.00" in msg.sent[0]


def _counters(text) -> tuple[int, int, int, int]:
    verified, with_email = re.search(
        r"Проверенных: (\d+), с email-контактом: (\d+)", text).groups()
    replied, interested = re.search(
        r"Ответили: (\d+) · заинтересованы: (\d+)", text).groups()
    return int(verified), int(with_email), int(replied), int(interested)


async def test_stats_counts_email_share_and_two_kinds_of_replies(make_lead):
    msg = FakeMsg()
    await stats(msg, FakeState())
    before = _counters(msg.sent[0])

    async with Session() as s, s.begin():
        with_email = await make_lead(s, status="verified", gap_type="slow")
        s.add(Contact(lead_id=with_email.id, ctype="email",
                      value="pytest@example.com"))
        await make_lead(s, status="verified", gap_type="slow")
        await make_lead(s, status="replied")
        await make_lead(s, status="replied_interested")

    msg = FakeMsg()
    await stats(msg, FakeState())
    after = _counters(msg.sent[0])
    assert [a - b for a, b in zip(after, before)] == [2, 1, 1, 1]


async def test_payouts_csv_collects_every_column(make_lead):
    async with Session() as s, s.begin():
        lead = await make_lead(s)
        lid, name = lead.id, lead.name
    await _sell(lid, "120 EUR")
    await sale_received(FakeCb(f"prc:{(await _sale_of(lid)).id}"))

    cb = FakeCb("pcs:0")
    await export_payouts_csv(cb)
    (body, caption), = cb.message.docs
    rows = list(csv.reader(body.splitlines(), delimiter=";"))
    header, *data = rows
    row = dict(zip(header, next(r for r in data if r[5] == name)))
    assert row["сумма_сделки"] == "120.00" and row["валюта"] == "EUR"
    assert row["процент"] == "20" and row["начисление"] == "24.00"
    assert row["деньги_пришли"] and not row["выплачено"]
    assert re.fullmatch(r"\d{4}-\d{2}", row["период"])
    assert caption.startswith("Начислений: ")
