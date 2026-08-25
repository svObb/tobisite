"""Загрузка карточек скаута в базу бота (15.16–15.17, 15.21, 15.23).

Дедуп ДО INSERT теми же нормами, что у ручного ввода: домен через
normalize_domain, телефон через normalize_phone + предикат уникального
индекса (phone_dup_exists). Совпадение имя+город не блокирует, а помечает
possible_duplicate — решает человек на модерации.
"""
import logging
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

import config
from dedup import normalize_domain, normalize_phone
from handlers_worker import phone_dup_exists
from models import Contact, Lead, Session, day_start, log_event
from scout.types import RawBiz

log = logging.getLogger(__name__)


@dataclass
class IngestStats:
    imported_candidate: int = 0
    imported_raw: int = 0
    dup_domain: int = 0
    dup_phone: int = 0
    flagged_name_city: int = 0
    limit_skipped: int = 0
    race_dup: int = 0
    # (score, lead_id, name) — для топ-10 в дайджесте
    imported: list = field(default_factory=list)


async def scout_imported_today(session) -> int:
    return await session.scalar(
        select(func.count()).select_from(Lead).where(
            Lead.found_via.like("scout%"),
            Lead.deleted_at.is_(None),
            Lead.created_at >= day_start(),
        )
    )


async def ingest(cards: list[RawBiz], *, country: str, niche: str,
                 default_city: str, worker_id: int, actor_tg_id: int,
                 batch_id: str) -> IngestStats:
    """Пишет карточки candidate/review; reject и отсеянные гейтом не доходят.

    Транзакция на карточку: один дубль не откатывает весь прогон.
    """
    stats = IngestStats()
    region = config.COUNTRY_ISO.get(country)

    for card in cards:
        async with Session() as s:
            # дневной потолок сырых карточек — не топить модерацию мусором
            if await scout_imported_today(s) >= config.SCOUT_DAILY_RAW_LIMIT:
                stats.limit_skipped += 1
                continue

            dom = normalize_domain(card.website)
            if dom:
                exists = await s.scalar(
                    select(Lead.id).where(
                        Lead.domain_norm == dom,
                        Lead.cancelled_at.is_(None),
                        Lead.deleted_at.is_(None),
                    ).limit(1)
                )
                if exists:
                    stats.dup_domain += 1
                    continue

            norm = normalize_phone(card.phone, region) if card.phone else None
            if norm and await phone_dup_exists(s, norm):
                stats.dup_phone += 1
                continue

            city = (card.city or default_city).strip() or default_city
            similar = await s.scalar(
                select(Lead.id).where(
                    func.lower(func.btrim(Lead.name)) == card.name.strip().lower(),
                    func.lower(func.btrim(Lead.city)) == city.lower(),
                    Lead.cancelled_at.is_(None),
                    Lead.deleted_at.is_(None),
                ).limit(1)
            )

            status = "candidate" if card.verdict == "candidate" else "raw"
            note = (f"скаут {batch_id}: {card.score}/100 — "
                    + "; ".join(card.reasons))
            if card.address:
                note += f". Адрес: {card.address}"
            if card.gate_hook:
                # зацепку писала модель, и никто её не проверял: в письмо она
                # попадёт только руками модератора (15.18)
                note += f". Зацепка гейта (не проверено): {card.gate_hook}"
            # SELECT-ы дедупа выше уже открыли транзакцию (autobegin),
            # поэтому s.begin() здесь бросил бы InvalidRequestError. INSERT
            # едет в той же транзакции — дедуп и вставка видят один снимок.
            try:
                lead = Lead(
                    worker_id=worker_id,
                    name=card.name,
                    website_url=card.website,
                    domain_norm=dom,
                    source_url=card.source_url or "https://www.openstreetmap.org/",
                    country=country,
                    city=city,
                    language="не определён",
                    niche=niche,
                    note=note,
                    found_via=f"scout:{card.source}",
                    status=status,
                    possible_duplicate=similar is not None,
                    has_ads=card.has_ads,
                )
                s.add(lead)
                await s.flush()
                if card.phone:
                    s.add(Contact(
                        lead_id=lead.id, ctype="phone",
                        value=card.phone, value_norm=norm,
                    ))
                log_event(s, lead.id, "scout_import", actor_tg_id,
                          field="score", new=str(card.score))
                lead_id = lead.id
                await s.commit()
            except IntegrityError as e:
                # параллельная вставка успела первой — это тот же дубль
                await s.rollback()
                log.warning("scout race dup %r: %s", card.name, e.orig)
                stats.race_dup += 1
                continue

        if similar is not None:
            stats.flagged_name_city += 1
        if status == "candidate":
            stats.imported_candidate += 1
        else:
            stats.imported_raw += 1
        stats.imported.append((card.score, lead_id, card.name))

    stats.imported.sort(reverse=True)
    return stats
