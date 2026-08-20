"""Оркестратор прогона скаута: источник → probe → скоринг → ingest → дайджест.

Запускается фоновой задачей из /scout и /scout_paste (15.22, 15.24).
Каждый прогон пишет свои вызовы в cost_ledger (15.20): Overpass и probe
бесплатны, но /costs должен видеть и число вызовов — иначе неоткуда узнать,
что скаут вообще работал.
"""
import asyncio
import logging
from datetime import datetime
from decimal import Decimal

from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select

import config
import costs
from dedup import normalize_domain
from models import CostLedger, Session, ensure_admin_worker
from scout import overpass, site_probe
from scout.niches import NICHE_TAGS
from scout.scoring import score
from scout.types import RawBiz
from scout.ingest import IngestStats, ingest

log = logging.getLogger(__name__)

# один прогон за раз: Overpass просит быть вежливыми, а дневной лимит импорта
# при параллельных прогонах превращается в гонку
_lock = asyncio.Lock()


def scout_busy() -> bool:
    return _lock.locked()


def _batch_id() -> str:
    return "scout-" + datetime.now(config.TZ).strftime("%Y%m%d-%H%M%S")


async def _spent(batch_id: str) -> Decimal:
    async with Session() as s:
        return Decimal(await s.scalar(
            select(func.coalesce(func.sum(CostLedger.cost_usd), 0))
            .where(CostLedger.batch_id == batch_id)
        ))


def _digest(header: str, found: int, rejected: int, stats: IngestStats,
            spent: Decimal) -> tuple[str, object]:
    lines = [
        f"<b>🔭 {header}</b>",
        f"Найдено карточек: {found}",
        f"Отсеяно скорингом: {rejected}",
        f"Дубликаты: домен {stats.dup_domain}, телефон {stats.dup_phone}, "
        f"гонка {stats.race_dup}",
        f"Импортировано: кандидатов {stats.imported_candidate}, "
        f"сырых {stats.imported_raw}",
    ]
    if stats.flagged_name_city:
        lines.append(f"⚠️ Помечено возможными дубликатами (имя+город): "
                     f"{stats.flagged_name_city}")
    if stats.limit_skipped:
        lines.append(f"Дневной лимит импорта ({config.SCOUT_DAILY_RAW_LIMIT}) "
                     f"исчерпан, пропущено: {stats.limit_skipped}")
    lines.append(f"Потрачено: ${spent:.2f}")
    markup = None
    if stats.imported:
        lines.append("\nТоп по скорингу — открыть карточку:")
        b = InlineKeyboardBuilder()
        for sc, lead_id, name in stats.imported[:10]:
            b.button(text=f"{sc} · {name[:28]}", callback_data=f"acd:{lead_id}")
        b.adjust(1)
        markup = b.as_markup()
    return "\n".join(lines), markup


async def _run_cards(bot, chat_id: int, *, header: str, cards: list[RawBiz],
                     country: str, niche: str, city: str, batch_id: str):
    urls = [c.website for c in cards if c.website]
    if urls:
        probes = await site_probe.probe_many(urls)
        for c in cards:
            if c.website:
                c.probe = probes.get(c.website)
        await costs.log_cost(
            op="scout", cost_usd=0, api_calls=len(set(urls)),
            note=f"site_probe {niche} {city}", batch_id=batch_id, bot=bot,
        )

    for c in cards:
        score(c, c.probe)
    keep = [c for c in cards if c.verdict != "reject"]
    rejected = len(cards) - len(keep)

    admin = await ensure_admin_worker()
    stats = await ingest(
        keep, country=country, niche=niche, default_city=city,
        worker_id=admin.id, actor_tg_id=config.ADMIN_TG_ID, batch_id=batch_id,
    )
    text, markup = _digest(header, len(cards), rejected, stats,
                           await _spent(batch_id))
    await bot.send_message(chat_id, text, reply_markup=markup)
    await _psi_followup(bot, chat_id, keep, niche=niche, city=city,
                        batch_id=batch_id)


async def _psi_followup(bot, chat_id: int, cards: list[RawBiz], *, niche: str,
                        city: str, batch_id: str):
    """PageSpeed лучших кандидатов (15.12): «ваш сайт 23/100 по Google» в письме
    бьёт сильнее любых слов. Медленно (15–40 сек/URL) — поэтому только топ,
    после дайджеста и отдельным сообщением. Ошибки глотаем: дайджест уже ушёл,
    и «❌ Скаут упал» после успешного импорта только запутал бы."""
    if config.PSI_MAX_PER_RUN <= 0:
        return
    top = sorted(
        (c for c in cards if c.verdict == "candidate" and c.website),
        key=lambda c: c.score, reverse=True,
    )[:config.PSI_MAX_PER_RUN]
    if not top:
        return
    try:
        scores = await site_probe.psi_many(
            [c.website for c in top], api_key=config.PAGESPEED_API_KEY,
        )
        await costs.log_cost(
            op="scout", cost_usd=0, api_calls=len(scores),
            note=f"pagespeed {niche} {city}", batch_id=batch_id, bot=bot,
        )
        lines = ["<b>📊 PageSpeed (мобильный, performance)</b>"]
        for c in top:
            got = scores.get(c.website)
            lines.append(f"{c.name[:32]} — "
                         + ("без оценки" if got is None else f"{got}/100"))
        await bot.send_message(chat_id, "\n".join(lines))
    except Exception:
        log.exception("psi followup failed")


async def run_scout(bot, chat_id: int, country: str, niche: str, city: str):
    """Прогон Overpass-источника. Ошибки уходят сообщением, не в тишину."""
    async with _lock:
        batch_id = _batch_id()
        try:
            cards = await overpass.fetch(NICHE_TAGS[niche], city)
            await costs.log_cost(
                op="scout", cost_usd=0, api_calls=1,
                note=f"overpass {niche} {city}", batch_id=batch_id, bot=bot,
            )
            await _run_cards(
                bot, chat_id,
                header=f"Скаут: {niche}, {city} ({country})",
                cards=cards, country=country, niche=niche, city=city,
                batch_id=batch_id,
            )
        except Exception as e:
            log.exception("scout run failed")
            await bot.send_message(chat_id, f"❌ Скаут упал: {e}")


async def run_scout_paste(bot, chat_id: int, country: str, niche: str,
                          domains: list[str]):
    """Домены из Ads Transparency (вставлены руками — 15.7): has_ads=true."""
    async with _lock:
        batch_id = _batch_id()
        try:
            cards = [
                RawBiz(
                    name=_domain_name(d), website=d, city="не указан",
                    source="ads", has_ads=True,
                    source_url="https://adstransparency.google.com/?domain="
                               + _domain_name(d),
                )
                for d in domains
            ]
            await _run_cards(
                bot, chat_id,
                header=f"Скаут (Ads Transparency): {niche}",
                cards=cards, country=country, niche=niche, city="не указан",
                batch_id=batch_id,
            )
        except Exception as e:
            log.exception("scout paste failed")
            await bot.send_message(chat_id, f"❌ Скаут упал: {e}")


def _domain_name(domain: str) -> str:
    """Имя карточки из домена: реальное название узнает админ при верификации."""
    return normalize_domain(domain) or domain
