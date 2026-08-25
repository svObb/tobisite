"""Открытия превью: события из R2 → строки, уведомление, отметка на лиде.

Наружных портов у бота нет, и позвать его воркер не может. Поэтому каждое
событие с превью воркер кладёт в тот же бакет пустым объектом

    _hits/<слаг>/<событие>/<миллисекунды>-<нонс>

а бот раз в config.PREVIEW_HITS_POLL_SEC секунд забирает список и удаляет
разобранное. Тела у объектов нет: всё нужное лежит в имени ключа, поэтому
опрос — это один list, и ни куки, ни адрес посетителя не появляются нигде.

Ради чего всё: первое открытие превью — сигнал для 4-го касания (10.22).
Админам и работнику уходит сообщение, на лиде появляется preview_opened_at.
Повторные события копятся в preview_hits молча — сообщение шлётся один раз,
а нажатие кнопки на превью и без того приходит заявкой с формы.
"""
import asyncio
import logging
import re
from collections import defaultdict
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

import config
import draft_service
import keyboards as kb
import notify
from models import (
    PREVIEW_EVENTS, Draft, Lead, PreviewHit, Session, Worker, log_event,
)
from notify import esc

log = logging.getLogger(__name__)

PREFIX = "_hits/"
SLUG_RE = re.compile(r"[a-z0-9-]{1,63}")
# Потолок на один заход: пачка больше означает, что поллер стоял, и разбирать
# её всю разом незачем — остаток заберёт следующий заход через две минуты.
BATCH = 1000


def parse_key(key: str) -> tuple[str, str, datetime] | None:
    """`_hits/<слаг>/<событие>/<мс>-<нонс>` → слаг, событие, момент.

    None — ключ разобрать нечем. Такой объект удаляется: чужого мусора под
    служебным префиксом быть не должно, а копить его тем более незачем.
    """
    parts = key.split("/")
    if len(parts) != 4 or f"{parts[0]}/" != PREFIX:
        return None
    _, slug, event, tail = parts
    stamp = tail.split("-")[0]
    if not (SLUG_RE.fullmatch(slug) and event in PREVIEW_EVENTS
            and stamp.isdigit()):
        return None
    try:
        return slug, event, datetime.fromtimestamp(int(stamp) / 1000, config.TZ)
    except (ValueError, OSError, OverflowError):
        return None


async def poll_once(bot) -> int:
    """Один заход опроса. Сколько событий разобрано."""
    if not draft_service.r2_ready():
        return 0
    keys = await draft_service.list_keys(PREFIX, limit=BATCH)
    if not keys:
        return 0

    hits, done = {}, []
    for key in keys:
        parsed = parse_key(key)
        if parsed is None:
            done.append(key)
        else:
            hits[key] = parsed
    if done:
        log.warning("под %s мусорных ключей: %s (пример %s)", PREFIX,
                    len(done), done[0])

    leads = await _leads_by_slug({slug for slug, _, _ in hits.values()})
    by_lead = defaultdict(list)
    for key, (slug, event, happened) in hits.items():
        lead_id = leads.get(slug)
        if lead_id is None:
            # превью снято или слаг чужой: строку писать не к чему
            done.append(key)
            continue
        by_lead[lead_id].append((key, slug, event, happened))

    saved = 0
    for lead_id, group in by_lead.items():
        try:
            opened = await _save(lead_id, group)
        except Exception:
            # база подождёт до следующего захода, объекты остаются в бакете
            log.exception("лид %s: открытия превью не записаны", lead_id)
            continue
        saved += len(group)
        done += [key for key, *_ in group]
        if opened:
            await _notify(bot, lead_id, opened)
    if done:
        await draft_service.delete_keys(done)
    return saved


async def poll_forever(bot):
    """Фоновая задача бота. Без ключей R2 просто спит — это не поломка."""
    while True:
        await asyncio.sleep(config.PREVIEW_HITS_POLL_SEC)
        try:
            await poll_once(bot)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("опрос открытий превью не прошёл: %s", e)


# --- внутреннее ---------------------------------------------------------------

async def _leads_by_slug(slugs: set[str]) -> dict[str, int]:
    if not slugs:
        return {}
    async with Session() as s:
        rows = await s.execute(
            select(Draft.r2_prefix, Draft.lead_id)
            .where(Draft.r2_prefix.in_(slugs), Draft.deleted_at.is_(None))
        )
    return {slug: lead_id for slug, lead_id in rows}


async def _save(lead_id: int, group) -> list[str] | None:
    """Строки хитов и отметка первого открытия — одной транзакцией.

    Возвращает события первого открытия либо None: превью уже открывали, и
    второе сообщение об этом никому не нужно.
    """
    rows = [{"lead_id": lead_id, "slug": slug, "event": event,
             "happened_at": happened, "object_key": key}
            for key, slug, event, happened in group]
    events = sorted({event for _, _, event, _ in group})
    async with Session() as s, s.begin():
        await s.execute(
            pg_insert(PreviewHit).values(rows)
            .on_conflict_do_nothing(index_elements=["object_key"])
        )
        lead = await s.get(Lead, lead_id, with_for_update=True)
        if lead is None or lead.preview_opened_at is not None:
            return None
        lead.preview_opened_at = min(happened for *_, happened in group)
        log_event(s, lead_id, "preview_opened", config.ADMIN_TG_ID,
                  new=", ".join(events))
    return events


async def _notify(bot, lead_id: int, events: list[str]):
    """Первое открытие превью — админам и работнику (10.21)."""
    async with Session() as s:
        lead = await s.get(Lead, lead_id)
        worker = await s.get(Worker, lead.worker_id) if lead else None
    if lead is None or lead.deleted_at:
        return
    text = (f"👀 <b>#{lead.id} {esc(lead.name)}</b> открыл превью.\n"
            f"События: {esc(', '.join(events))}")
    await notify.to_admins(bot, text, reply_markup=kb.open_card_kb(lead.id))
    # is_active и deleted_at — независимые флаги: отстранённому работнику бот
    # не пишет, а строку его лида это не отменяет
    if worker is not None and not worker.deleted_at and worker.is_active:
        await notify.send(bot, worker.tg_id, text)
