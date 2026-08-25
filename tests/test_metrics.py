"""Метрики недели (13.1) и их выгрузка (13.2).

База общая, и соседние тесты пишут в те же таблицы, поэтому счётчики
проверяются разницей «до и после», а не абсолютными числами. Пустая доставка,
границы недели и арифметика $/лид — на чистых данных.
"""
import os
import pathlib
from datetime import date, timedelta
from decimal import Decimal

import config
import metrics
from handlers_admin import METRICS_USAGE, metrics_report
from models import (
    CostLedger, LeadEvent, Sale, Session, log_event, week_start,
)
from test_reject_reason import FakeMsg, FakeState
from test_suppression_log import cmd


class Msg(FakeMsg):
    """FakeMsg, умеющий принять документ: файл читается до его удаления."""

    def __init__(self):
        super().__init__()
        self.docs = []

    async def answer_document(self, document, caption=None, **kw):
        self.docs.append((document.filename,
                          pathlib.Path(document.path).read_text("utf-8-sig"),
                          caption))


async def _this_week() -> metrics.Week:
    return (await metrics.weekly(1))[0]


# --- границы недели -----------------------------------------------------------

def test_week_starts_on_monday():
    start = week_start()
    assert start.weekday() == 0 and start.hour == 0
    assert week_start(1) == start - timedelta(days=7)


def test_row_label_covers_seven_days():
    week = metrics.Week(start=date(2026, 8, 24))
    assert week.label == "24.08–30.08.2026"


# --- счётчики -----------------------------------------------------------------

async def test_week_counts_the_whole_funnel(make_lead, worker_id):
    before = await _this_week()
    async with Session() as s, s.begin():
        lead = await make_lead(s)
        log_event(s, lead.id, "letter_approved", 1)
        log_event(s, lead.id, "draft_generated", 1)
        log_event(s, lead.id, "preview_published", 1)
        log_event(s, lead.id, "status_change", 1, "status", "sent",
                  "replied_interested")
        s.add(Sale(lead_id=lead.id, worker_id=worker_id,
                   deal_amount=Decimal("300.00"), rate_pct=20,
                   amount_due=Decimal("60.00")))
        s.add(CostLedger(op="draft", model="pytest-metrics",
                         cost_usd=Decimal("0.0300"), batch_id="pytest-metrics"))

    week = await _this_week()

    assert week.leads == before.leads + 1
    assert week.letters == before.letters + 1
    assert week.drafts == before.drafts + 1
    assert week.previews == before.previews + 1
    # ответ и интерес — одна и та же смена статуса, она считается в обе колонки
    assert week.replies == before.replies + 1
    assert week.interested == before.interested + 1
    assert week.sales == before.sales + 1
    assert week.revenue == before.revenue + Decimal("300.00")
    assert week.spent == before.spent + Decimal("0.0300")
    assert week.draft_spent == before.draft_spent + Decimal("0.0300")


async def test_plain_reply_is_not_interest(make_lead):
    before = await _this_week()
    async with Session() as s, s.begin():
        lead = await make_lead(s)
        log_event(s, lead.id, "status_change", 1, "status", "sent", "replied")

    week = await _this_week()

    assert week.replies == before.replies + 1
    assert week.interested == before.interested


async def test_first_preview_open_lands_in_its_week(make_lead):
    before = await _this_week()
    async with Session() as s, s.begin():
        lead = await make_lead(s)
        lead.preview_opened_at = week_start()

    assert (await _this_week()).opens == before.opens + 1


async def test_event_of_a_past_week_stays_there(make_lead):
    async with Session() as s, s.begin():
        lead = await make_lead(s)
        s.add(LeadEvent(lead_id=lead.id, event="letter_approved", actor_tg_id=1,
                        created_at=week_start(2) + timedelta(days=1)))
    now, older = (await metrics.weekly(1))[0], await metrics.weekly(3)

    assert now.letters == older[0].letters
    assert older[2].start == week_start(2).date()
    assert older[2].letters >= 1


async def test_delivery_is_empty_because_nobody_measures_it():
    week = await _this_week()
    assert week.delivered is None
    assert "доставка —" in metrics.report([week])


# --- юнит-экономика (20.10) ---------------------------------------------------

def test_dollars_per_lead_and_per_draft():
    week = metrics.Week(start=date(2026, 8, 24), leads=4, drafts=2,
                        spent=Decimal("1.00"), draft_spent=Decimal("0.50"))
    assert week.per_lead == Decimal("0.2500")
    assert week.per_draft == Decimal("0.2500")


def test_empty_week_shows_a_dash_instead_of_a_number():
    week = metrics.Week(start=date(2026, 8, 24), spent=Decimal("1.00"))
    assert week.per_lead is None and week.per_draft is None
    assert "$/лид —" in metrics.report([week])


# --- выгрузка -----------------------------------------------------------------

def test_csv_has_a_header_and_a_row_per_week():
    rows = [metrics.Week(start=date(2026, 8, 24), leads=2,
                         spent=Decimal("0.5")),
            metrics.Week(start=date(2026, 8, 17))]
    path = metrics.export_csv(rows)
    try:
        lines = pathlib.Path(path).read_text("utf-8-sig").splitlines()
    finally:
        os.remove(path)

    assert lines[0] == ";".join(metrics.CSV_HEADER)
    assert len(lines) == 3
    # доставка прочерком, а не нулём: цифры этой колонки не существует
    assert lines[1].split(";")[3] == "—"
    assert lines[1].split(";")[11] == "0.2500"


# --- команда ------------------------------------------------------------------

async def test_metrics_command_shows_the_table():
    msg = Msg()
    await metrics_report(msg, FakeState(), cmd(""))

    assert "Метрики недели" in msg.sent[0]
    assert week_start().strftime("%d.%m") in msg.sent[0]


async def test_metrics_csv_command_sends_the_file():
    msg = Msg()
    await metrics_report(msg, FakeState(), cmd("csv"))

    filename, body, caption = msg.docs[0]
    assert filename == "metrics.csv"
    assert body.splitlines()[0] == ";".join(metrics.CSV_HEADER)
    assert len(body.splitlines()) == metrics.CSV_WEEKS + 1
    assert f"Недель: {metrics.CSV_WEEKS}" in caption


async def test_unknown_argument_shows_the_format():
    msg = Msg()
    await metrics_report(msg, FakeState(), cmd("за год"))

    assert msg.sent == [METRICS_USAGE]
    assert not msg.docs


def test_local_zone_is_the_bot_zone():
    assert metrics.LOCAL == config.TZ.key
