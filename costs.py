"""Учёт расходов на ИИ и платные API + месячный кэп (раздел 20 плана).

Правила для любого будущего платного кода (скаут, черновики, письма, QA):

1. ПЕРЕД операцией — `if await cap_reached(): не делать и сказать почему`.
2. ПОСЛЕ операции — `await log_cost(op=..., cost_usd=..., ...)`, даже если
   стоимость нулевая: /costs должен видеть и число вызовов.
3. Не-ИИ API (Places, Twilio) — тем же журналом, но через log_api (20.3):
   стоят они вызовами, а не токенами, и цена вызова берётся из .env.

Кэп задаётся AI_MONTHLY_CAP_USD в .env (0 — выключен). Алерты админу шлются
при пересечении 80% и 100% кэпа — ровно в тот момент, когда очередная запись
переводит сумму месяца через порог, поэтому дублей алертов нет.

Факт-стоимости (13.5) считаются отсюда же: письмо, черновик сайта и лид —
три единицы работы, у каждой свой делитель.
"""
import logging
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select

import config
import notify
from models import CostLedger, Lead, Session, month_start

log = logging.getLogger(__name__)

# Факт-стоимость письма (9.17): цель ≤$0.01. Дороже — значит промпт разросся
# или кэш перестал попадать, и это видно в /costs раньше, чем в счёте.
LETTER_OP = "letter"
LETTER_TARGET_USD = Decimal("0.01")
# Черновик сайта: платит за него слот-генерация (op='draft'). Цели по нему нет
# — она появится, когда наберётся факт, а не наоборот.
DRAFT_OP = "draft"
# Единицы работы факт-стоимостей и их подписи для отчётов.
UNITS = (("letter", "Письмо"), ("draft", "Черновик"), ("lead", "Лид"))
CENT = Decimal("0.0001")

# порог → текст алерта; 1.0 идёт последним, чтобы при перепрыгивании обоих
# порогов одной записью админ получил сообщения в логичном порядке
_THRESHOLDS = (
    (Decimal("0.8"), "⚠️ Расходы на ИИ достигли 80% месячного кэпа"),
    (Decimal("1"), "⛔ Месячный кэп расходов на ИИ исчерпан — платные операции остановлены"),
)


def _cap() -> Decimal:
    return Decimal(str(config.AI_MONTHLY_CAP_USD))


async def month_spent(session=None) -> Decimal:
    """Сумма расходов с начала текущего месяца, $."""
    stmt = select(func.coalesce(func.sum(CostLedger.cost_usd), 0)).where(
        CostLedger.created_at >= month_start()
    )
    if session is not None:
        return Decimal(await session.scalar(stmt))
    async with Session() as s:
        return Decimal(await s.scalar(stmt))


@dataclass(frozen=True)
class UnitCost:
    """Во что обошлась единица работы. units == 0 — их в окне не было.

    target — ориентир, если он у единицы есть; None значит «цели нет», и
    within_target тогда ничего не обещает.
    """
    unit: str = ""
    label: str = ""
    units: int = 0
    calls: int = 0
    total: Decimal = Decimal(0)
    per_unit: Decimal = Decimal(0)
    target: Decimal | None = None

    @property
    def within_target(self) -> bool:
        return self.target is None or self.per_unit <= self.target


async def unit_cost(unit: str, since=None, session=None) -> UnitCost:
    """Факт-стоимость единицы работы за окно (13.5).

    letter и draft делят расходы своей операции на число лидов, для которых
    она делалась: перегенерация после линтера — второй вызов внутри того же
    письма, и делить стоимость на неё значит показывать письмо вдвое дешевле,
    чем оно вышло.

    lead — другая дробь: ВСЕ расходы окна на число лидов, заведённых в окне.
    Множества тут разные (деньги тратятся не на тех же лидов, что заведены), и
    это ровно та цифра юнит-экономики, которую спрашивает 20.10.
    """
    if unit == "lead":
        return await _lead_cost(since, session)
    stmt = select(
        func.coalesce(func.sum(CostLedger.cost_usd), 0),
        func.coalesce(func.sum(CostLedger.api_calls), 0),
        func.count(func.distinct(CostLedger.lead_id)),
    ).where(CostLedger.op == unit)
    if since is not None:
        stmt = stmt.where(CostLedger.created_at >= since)
    total, calls, units = await _one(stmt, session)
    return _unit(unit, units, calls, total)


async def letter_cost(since=None, session=None) -> UnitCost:
    return await unit_cost(LETTER_OP, since, session)


async def draft_cost(since=None, session=None) -> UnitCost:
    return await unit_cost(DRAFT_OP, since, session)


async def lead_cost(since=None, session=None) -> UnitCost:
    return await unit_cost("lead", since, session)


async def unit_costs(since=None, session=None) -> list[UnitCost]:
    """Три факт-стоимости окна подряд: письмо, черновик, лид."""
    return [await unit_cost(unit, since, session) for unit, _ in UNITS]


async def _lead_cost(since, session) -> UnitCost:
    spend = select(
        func.coalesce(func.sum(CostLedger.cost_usd), 0),
        func.coalesce(func.sum(CostLedger.api_calls), 0),
    )
    # лиды считаются те же, что в /stats: отменённые и удалённые не в счёт
    leads = select(func.count()).select_from(Lead).where(
        Lead.cancelled_at.is_(None), Lead.deleted_at.is_(None)
    )
    if since is not None:
        spend = spend.where(CostLedger.created_at >= since)
        leads = leads.where(Lead.created_at >= since)
    total, calls = await _one(spend, session)
    count = await _scalar(leads, session)
    return _unit("lead", count, calls, total)


def _unit(unit: str, units: int, calls: int, total) -> UnitCost:
    total = Decimal(total)
    per = (total / units).quantize(CENT) if units else Decimal(0)
    return UnitCost(
        unit=unit, label=dict(UNITS)[unit], units=units, calls=calls,
        total=total, per_unit=per,
        target=LETTER_TARGET_USD if unit == LETTER_OP else None,
    )


async def _one(stmt, session):
    if session is not None:
        return (await session.execute(stmt)).one()
    async with Session() as s:
        return (await s.execute(stmt)).one()


async def _scalar(stmt, session):
    if session is not None:
        return await session.scalar(stmt)
    async with Session() as s:
        return await s.scalar(stmt)


async def cap_reached() -> bool:
    """True — тратить больше нельзя; вызывающий код обязан не делать операцию."""
    cap = _cap()
    if cap <= 0:
        return False
    return await month_spent() >= cap


async def log_cost(*, op: str, cost_usd, model: str | None = None,
                   input_tokens: int = 0, output_tokens: int = 0,
                   cache_read_tokens: int = 0, api_calls: int = 1,
                   lead_id: int | None = None, batch_id: str | None = None,
                   note: str | None = None, bot=None) -> Decimal:
    """Записывает операцию в cost_ledger, возвращает сумму месяца после записи.

    bot — необязательный aiogram Bot: с ним при пересечении порога кэпа админ
    получает алерт (тем же паттерном, что уведомление о новом работнике).
    """
    cost = Decimal(str(cost_usd))
    async with Session() as s, s.begin():
        before = await month_spent(s)
        s.add(CostLedger(
            op=op, model=model, cost_usd=cost,
            input_tokens=input_tokens, output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens, api_calls=api_calls,
            lead_id=lead_id, batch_id=batch_id, note=note,
        ))
    after = before + cost
    cap = _cap()
    if bot is not None and cap > 0:
        for share, alert in _THRESHOLDS:
            edge = cap * share
            if before < edge <= after:
                await notify.to_admins(
                    bot,
                    f"{alert}: ${after:.2f} из ${cap:.2f}. Подробности: /costs",
                )
    return after


def api_price(op: str) -> Decimal:
    """Цена одного вызова платного не-ИИ API, $. Не задана — ноль (20.3)."""
    return Decimal(str(config.API_PRICES.get(op, 0)))


async def log_api(*, op: str, calls: int = 1, lead_id: int | None = None,
                  batch_id: str | None = None, note: str | None = None,
                  bot=None) -> Decimal:
    """Вызовы не-ИИ API в тот же журнал: стоимость = вызовы × цена SKU (20.3).

    Бесплатные источники скаута (Overpass, обход сайтов, PageSpeed) идут этим
    же путём с нулевой ценой: без их вызовов в /costs неоткуда узнать, что
    скаут вообще работал, а платными они могут стать в любой день.
    """
    return await log_cost(op=op, cost_usd=api_price(op) * calls,
                          api_calls=calls, lead_id=lead_id, batch_id=batch_id,
                          note=note, bot=bot)
