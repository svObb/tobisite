"""Пересчёт value_norm у телефонов после смены правил нормализации.

Раньше проверка номера шла без региона, а в базу он ложился с регионом, плюс
работал фолбэк «плюс и одни цифры». Из-за этого в contacts осталась смесь:
один и тот же номер записан то как +380501234567, то как +0501234567, и
уникальный индекс uq_contacts_phone_norm_active такие пары дубликатами
не считает. Скрипт приводит value_norm к текущим правилам.

Запускать из корня проекта:

    .venv/bin/python tools/renorm_phones.py           # только показать, что будет
    .venv/bin/python tools/renorm_phones.py --apply   # записать
    .venv/bin/python tools/renorm_phones.py --test    # то же на тестовой базе

Без --apply не меняется ничего.
"""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import select, update  # noqa: E402

import config  # noqa: E402
from dedup import normalize_phone  # noqa: E402
from models import Contact, Lead, Session  # noqa: E402


async def collect():
    """Все телефоны с их новой нормой: (contact, country, active, new_norm)."""
    async with Session() as s:
        rows = (await s.execute(
            select(Contact, Lead.country)
            .join(Lead, Lead.id == Contact.lead_id)
            .where(Contact.ctype == "phone")
            .order_by(Contact.id)
        )).all()
    out = []
    for contact, country in rows:
        # индекс покрывает только живые контакты живых записей — конфликтовать
        # между собой могут только они
        active = contact.deleted_at is None and contact.lead_cancelled_at is None
        new = normalize_phone(contact.value, config.COUNTRY_ISO.get(country))
        out.append((contact, country, active, new))
    return out


def resolve(items):
    """Разложить по корзинам и снять коллизии внутри активных контактов."""
    plan = {}          # contact_id -> новое значение value_norm
    unchanged, cleared, conflicts = [], [], []
    seen = {}          # норма -> id первого активного контакта с ней

    for contact, country, active, new in items:
        if new is None:
            # номер не разбирается по текущим правилам: мусорную норму лучше
            # убрать, чем держать в уникальном индексе
            if contact.value_norm is not None:
                plan[contact.id] = None
                cleared.append((contact, country))
            else:
                unchanged.append(contact)
            continue
        if active:
            first = seen.get(new)
            if first is not None:
                # два живых контакта сходятся в один номер — второй оставляем
                # без нормы, иначе уникальный индекс не даст записать
                plan[contact.id] = None
                conflicts.append((contact, new, first))
                continue
            seen[new] = contact.id
        if contact.value_norm == new:
            unchanged.append(contact)
        else:
            plan[contact.id] = new

    updated = [(c, c.value_norm, plan[c.id]) for c, _, _, _ in items
               if c.id in plan and plan[c.id] is not None]
    return plan, unchanged, updated, cleared, conflicts


async def write(plan, conflict_lead_ids):
    """Записать план в один заход.

    Сначала все затронутые строки обнуляются, и только потом получают новые
    значения. Иначе на середине прохода два контакта могут одновременно
    претендовать на одну норму и уронить уникальный индекс.
    """
    async with Session() as s, s.begin():
        ids = list(plan)
        for chunk in (ids[i:i + 500] for i in range(0, len(ids), 500)):
            await s.execute(
                update(Contact).where(Contact.id.in_(chunk)).values(value_norm=None)
            )
        await s.flush()
        for cid, norm in plan.items():
            if norm is not None:
                await s.execute(
                    update(Contact).where(Contact.id == cid).values(value_norm=norm)
                )
        if conflict_lead_ids:
            await s.execute(
                update(Lead).where(Lead.id.in_(list(conflict_lead_ids)))
                .values(possible_duplicate=True)
            )


async def main(apply: bool):
    items = await collect()
    plan, unchanged, updated, cleared, conflicts = resolve(items)

    print(f"Телефонов в базе: {len(items)}")
    print(f"  уже правильных: {len(unchanged)}")
    print(f"  пересчитать:    {len(updated)}")
    print(f"  обнулить:       {len(cleared)}")
    print(f"  дубликаты:      {len(conflicts)}")

    if updated:
        print("\n--- пересчёт ---")
        for contact, old, new in updated:
            print(f"  #{contact.id} лид {contact.lead_id}: {old} -> {new}")
    if cleared:
        print("\n--- номер не разбирается, норма снимается ---")
        print("    (значение в карточке остаётся, дедуп по нему работать не будет)")
        for contact, country in cleared:
            print(f"  #{contact.id} лид {contact.lead_id}: {contact.value!r} [{country}]")
    if conflicts:
        print("\n--- один номер у двух живых записей ---")
        print("    (норму получает первый, лиды помечаются как возможный дубликат)")
        for contact, norm, first in conflicts:
            print(f"  #{contact.id} лид {contact.lead_id}: {norm} — уже у контакта #{first}")

    if not plan:
        print("\nМенять нечего.")
        return
    if not apply:
        print(f"\nЭто сухой прогон. Чтобы записать: {sys.argv[0]} --apply")
        return

    conflict_lead_ids = {c.lead_id for c, _, _ in conflicts}
    await write(plan, conflict_lead_ids)
    print(f"\nГотово: изменено строк {len(plan)}, "
          f"помечено лидов как дубликаты {len(conflict_lead_ids)}.")


if __name__ == "__main__":
    asyncio.run(main("--apply" in sys.argv))
