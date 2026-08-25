"""Метрики недели (13.1) и их выгрузка (13.2).

Неделя — от понедельника 00:00 по нашему поясу: так же режет её Postgres
(`date_trunc('week')`), поэтому цифры бота, панели и CSV совпадают буквально.

Откуда берётся каждая колонка:

| колонка   | источник                                                        |
|-----------|-----------------------------------------------------------------|
| лиды      | leads.created_at, без отменённых и удалённых (как /stats)        |
| письма    | события letter_approved — письма, одобренные к отправке          |
| доставка  | НЕТ ДАННЫХ: отправки нет до Instantly, в таблице стоит «—»       |
| превью    | события preview_published, по лидам                              |
| открытия  | leads.preview_opened_at — первое открытие превью (10.22)         |
| ответы    | смены статуса на replied и replied_interested, по лидам          |
| интерес   | смены статуса на replied_interested, по лидам                    |
| продажи   | строки sales и сумма их deal_amount                              |
| расходы   | cost_ledger целиком; отдельно операция draft — для $/черновик    |

Открытий писем в списке нет намеренно (13.3): пиксель мы не ставим, а
вовлечённость видно по превью-хитам и ответам.

Пустая колонка «доставка» — это None, а не ноль: цифры, которую никто не
измерял, у нас не будет.
"""
import csv
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select

import config
from models import (
    CostLedger, Lead, LeadEvent, Sale, Session, week_start,
)

# Сколько недель показывает /metrics и сколько уезжает в CSV: в чат — обозримое
# окно, в таблицу — вся история, ради которой выгрузка и делается.
WEEKS = 8
CSV_WEEKS = 26
# Пояс, в котором Postgres режет неделю. Тот же, что у day_start в боте.
LOCAL = config.TZ.key
ACTIVE = (Lead.cancelled_at.is_(None), Lead.deleted_at.is_(None))
REPLY_STATUSES = ("replied", "replied_interested")
INTERESTED = "replied_interested"
CENT = Decimal("0.0001")

CSV_HEADER = [
    "неделя", "лиды", "письма", "доставка", "превью", "открытия", "ответы",
    "интерес", "продажи", "выручка,$", "расходы,$", "$/лид", "$/черновик",
]


@dataclass(frozen=True)
class Week:
    """Строка таблицы. delivered — None: доставку до Instantly никто не считает."""
    start: date
    leads: int = 0
    letters: int = 0
    delivered: None = None
    previews: int = 0
    opens: int = 0
    replies: int = 0
    interested: int = 0
    sales: int = 0
    revenue: Decimal = field(default_factory=lambda: Decimal(0))
    spent: Decimal = field(default_factory=lambda: Decimal(0))
    drafts: int = 0
    draft_spent: Decimal = field(default_factory=lambda: Decimal(0))

    @property
    def per_lead(self) -> Decimal | None:
        """$/лид недели (20.10). None — лидов не было, делить не на что."""
        return (self.spent / self.leads).quantize(CENT) if self.leads else None

    @property
    def per_draft(self) -> Decimal | None:
        return ((self.draft_spent / self.drafts).quantize(CENT)
                if self.drafts else None)

    @property
    def label(self) -> str:
        end = self.start + timedelta(days=6)
        return f"{self.start:%d.%m}–{end:%d.%m.%Y}"


async def weekly(weeks: int = WEEKS, session=None) -> list[Week]:
    """Метрики последних недель, свежая — первой."""
    if session is not None:
        return await _weekly(session, weeks)
    async with Session() as s:
        return await _weekly(s, weeks)


def export_csv(rows: list[Week]) -> str:
    """Таблица в файл (13.2). Возвращает путь; удалять его — вызывающему.

    Разделитель и BOM те же, что у выгрузки компаний: этот файл открывают в
    той же таблице, и разнобой стоил бы получасовой возни с импортом.
    """
    fd, path = tempfile.mkstemp(suffix=".csv", prefix="tobisite_metrics_")
    os.close(fd)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(CSV_HEADER)
        for week in rows:
            writer.writerow([
                week.label, week.leads, week.letters, _cell(week.delivered),
                week.previews, week.opens, week.replies, week.interested,
                week.sales, f"{week.revenue:.2f}", f"{week.spent:.4f}",
                _cell(week.per_lead), _cell(week.per_draft),
            ])
    return path


def report(rows: list[Week]) -> str:
    """Таблица недель для чата: по блоку на неделю, свежая сверху."""
    lines = [f"<b>📈 Метрики недели</b> (последние {len(rows)})"]
    for week in rows:
        lines += [
            f"\n<b>{week.label}</b>",
            f"  лиды {week.leads} · письма {week.letters} · "
            f"доставка {_cell(week.delivered)}",
            f"  превью {week.previews} · открытия {week.opens} · "
            f"ответы {week.replies} · интерес {week.interested}",
            f"  продажи {week.sales} на ${week.revenue:.2f}",
            f"  расходы ${week.spent:.4f} · $/лид {_money(week.per_lead)} · "
            f"$/черновик {_money(week.per_draft)}",
        ]
    lines.append("\nДоставка писем не считается: отправки нет до Instantly, "
                 "открытия писем не трекаем совсем (13.3).")
    return "\n".join(lines)


# --- внутреннее ---------------------------------------------------------------

def _cell(value) -> str:
    """«—» вместо пустоты: пустая клетка читается как ноль, а это не ноль."""
    return "—" if value is None else str(value)


def _money(value) -> str:
    return "—" if value is None else f"${value:.4f}"


def _bucket(col):
    return func.date_trunc("week", func.timezone(LOCAL, col))


async def _by_week(session, col, since, conds=(), value=None) -> dict:
    """{понедельник: число} по одной колонке времени."""
    bucket = _bucket(col)
    rows = await session.execute(
        select(bucket, func.count() if value is None else value)
        .where(col >= since, *conds).group_by(bucket)
    )
    return {start.date(): count for start, count in rows}


async def _weekly(session, weeks: int) -> list[Week]:
    since = week_start(weeks - 1)
    leads = await _by_week(session, Lead.created_at, since, ACTIVE)
    opens = await _by_week(session, Lead.preview_opened_at, since, ACTIVE)
    letters = await _by_week(session, LeadEvent.created_at, since,
                             (LeadEvent.event == "letter_approved",))
    drafts = await _by_week(session, LeadEvent.created_at, since,
                            (LeadEvent.event == "draft_generated",))
    by_lead = func.count(func.distinct(LeadEvent.lead_id))
    previews = await _by_week(session, LeadEvent.created_at, since,
                              (LeadEvent.event == "preview_published",),
                              value=by_lead)
    status_change = (LeadEvent.event == "status_change",)
    replies = await _by_week(session, LeadEvent.created_at, since,
                             status_change
                             + (LeadEvent.new_value.in_(REPLY_STATUSES),),
                             value=by_lead)
    interested = await _by_week(session, LeadEvent.created_at, since,
                                status_change
                                + (LeadEvent.new_value == INTERESTED,),
                                value=by_lead)
    sales = await _by_week(session, Sale.created_at, since)
    revenue = await _by_week(session, Sale.created_at, since,
                             value=func.sum(Sale.deal_amount))
    spent = await _by_week(session, CostLedger.created_at, since,
                           value=func.sum(CostLedger.cost_usd))
    draft_spent = await _by_week(session, CostLedger.created_at, since,
                                 (CostLedger.op == "draft",),
                                 value=func.sum(CostLedger.cost_usd))

    out = []
    for back in range(weeks):
        start = week_start(back).date()
        out.append(Week(
            start=start,
            leads=leads.get(start, 0), letters=letters.get(start, 0),
            previews=previews.get(start, 0), opens=opens.get(start, 0),
            replies=replies.get(start, 0),
            interested=interested.get(start, 0),
            sales=sales.get(start, 0),
            revenue=Decimal(revenue.get(start) or 0),
            spent=Decimal(spent.get(start) or 0),
            drafts=drafts.get(start, 0),
            draft_spent=Decimal(draft_spent.get(start) or 0),
        ))
    return out
