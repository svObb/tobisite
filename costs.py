"""Учёт расходов на ИИ и платные API + месячный кэп (раздел 20 плана).

Правила для любого будущего платного кода (скаут, черновики, письма, QA):

1. ПЕРЕД операцией — `if await cap_reached(): не делать и сказать почему`.
2. ПОСЛЕ операции — `await log_cost(op=..., cost_usd=..., ...)`, даже если
   стоимость нулевая: /costs должен видеть и число вызовов.

Кэп задаётся AI_MONTHLY_CAP_USD в .env (0 — выключен). Алерты админу шлются
при пересечении 80% и 100% кэпа — ровно в тот момент, когда очередная запись
переводит сумму месяца через порог, поэтому дублей алертов нет.
"""
import logging
from decimal import Decimal

from sqlalchemy import func, select

import config
from models import CostLedger, Session, month_start

log = logging.getLogger(__name__)

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
                try:
                    await bot.send_message(
                        config.ADMIN_TG_ID,
                        f"{alert}: ${after:.2f} из ${cap:.2f}. Подробности: /costs",
                    )
                except Exception as e:
                    log.warning("cost alert failed: %s", e)
    return after
