"""Пересчёт domain_norm после смены правил нормализации домена.

Раньше normalize_domain снимала только схему, www. и хвостовой слэш, а путь
и query оставляла: в базу ложилось shop.example.com/ua. Из-за этого та же
компания под адресом shop.example.com/ проходила и проверку в форме, и
уникальный индекс uq_leads_domain_norm_active — дедупликация сайтов не
работала ни для одного адреса сложнее корня. Скрипт приводит domain_norm
к текущим правилам.

Запускать из корня проекта:

    .venv/bin/python tools/renorm_domains.py           # только показать, что будет
    .venv/bin/python tools/renorm_domains.py --apply   # записать
    .venv/bin/python tools/renorm_domains.py --test    # то же на тестовой базе

В контейнере:

    docker compose run --rm bot python tools/renorm_domains.py

Без --apply не меняется ничего.
"""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import select, update  # noqa: E402

from dedup import normalize_domain  # noqa: E402
from models import Lead, Session  # noqa: E402


async def collect():
    """Все записи с сайтом: (lead, active, new_norm)."""
    async with Session() as s:
        leads = list(await s.scalars(
            select(Lead)
            .where(Lead.website_url.is_not(None))
            .order_by(Lead.id)
        ))
    out = []
    for lead in leads:
        # индекс покрывает только живые записи — конфликтовать между собой
        # могут только они
        active = lead.cancelled_at is None and lead.deleted_at is None
        out.append((lead, active, normalize_domain(lead.website_url)))
    return out


def resolve(items):
    """Разложить по корзинам и снять коллизии внутри живых записей."""
    plan = {}          # lead_id -> новое значение domain_norm
    unchanged, cleared, conflicts = [], [], []
    seen = {}          # домен -> id первой живой записи с ним

    for lead, active, new in items:
        if new is None:
            # адрес не разбирается: мусорную норму лучше убрать, чем держать
            # её в уникальном индексе
            if lead.domain_norm is not None:
                plan[lead.id] = None
                cleared.append(lead)
            else:
                unchanged.append(lead)
            continue
        if active:
            first = seen.get(new)
            if first is not None:
                # две живые записи сходятся в один домен — второй остаётся
                # без нормы, иначе уникальный индекс не даст записать
                conflicts.append((lead, new, first))
                if lead.domain_norm is not None:
                    plan[lead.id] = None
                continue
            seen[new] = lead.id
        if lead.domain_norm == new:
            unchanged.append(lead)
        else:
            plan[lead.id] = new

    updated = [(l, l.domain_norm, plan[l.id]) for l, _, _ in items
               if l.id in plan and plan[l.id] is not None]
    return plan, unchanged, updated, cleared, conflicts


async def write(plan, conflict_ids):
    """Записать план в один заход.

    Сначала все затронутые строки обнуляются, и только потом получают новые
    значения. Иначе на середине прохода две записи могут одновременно
    претендовать на один домен и уронить уникальный индекс.
    """
    async with Session() as s, s.begin():
        ids = list(plan)
        for chunk in (ids[i:i + 500] for i in range(0, len(ids), 500)):
            await s.execute(
                update(Lead).where(Lead.id.in_(chunk)).values(domain_norm=None)
            )
        await s.flush()
        for lead_id, norm in plan.items():
            if norm is not None:
                await s.execute(
                    update(Lead).where(Lead.id == lead_id).values(domain_norm=norm)
                )
        if conflict_ids:
            await s.execute(
                update(Lead).where(Lead.id.in_(list(conflict_ids)))
                .values(possible_duplicate=True)
            )


async def main(apply: bool):
    items = await collect()
    plan, unchanged, updated, cleared, conflicts = resolve(items)

    print(f"Записей с сайтом: {len(items)}")
    print(f"  уже правильных: {len(unchanged)}")
    print(f"  пересчитать:    {len(updated)}")
    print(f"  обнулить:       {len(cleared)}")
    print(f"  дубликаты:      {len(conflicts)}")

    if updated:
        print("\n--- пересчёт ---")
        for lead, old, new in updated:
            print(f"  #{lead.id} {lead.name}: {old} -> {new}")
    if cleared:
        print("\n--- адрес не разбирается, норма снимается ---")
        print("    (ссылка в карточке остаётся, дедуп по ней работать не будет)")
        for lead in cleared:
            print(f"  #{lead.id} {lead.name}: {lead.website_url!r}")
    if conflicts:
        print("\n--- один домен у двух живых записей ---")
        print("    (норму получает первая, вторая помечается возможным дубликатом)")
        for lead, norm, first in conflicts:
            print(f"  #{lead.id} {lead.name}: {norm} — уже у записи #{first}")

    # пометку ставим только тем, у кого её ещё нет: иначе повторный прогон
    # каждый раз отчитывался бы о работе, которой не было
    conflict_ids = {l.id for l, _, _ in conflicts if not l.possible_duplicate}
    if not plan and not conflict_ids:
        print("\nМенять нечего.")
        return
    if not apply:
        print(f"\nЭто сухой прогон. Чтобы записать: {sys.argv[0]} --apply")
        return

    await write(plan, conflict_ids)
    print(f"\nГотово: изменено строк {len(plan)}, "
          f"помечено возможными дубликатами {len(conflict_ids)}.")


if __name__ == "__main__":
    asyncio.run(main("--apply" in sys.argv))
