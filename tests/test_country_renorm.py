"""Смена страны лида пересчитывает value_norm телефонов (дефект 6.4).

Регион разбора местных номеров идёт от страны лида: «050 123 45 67» — это
+380… только пока лид украинский. Раньше норма оставалась от старой страны,
и дедуп телефонов такого лида ломался.
"""
import itertools

from sqlalchemy import select

from dedup import normalize_phone
from handlers_worker import apply_field
from models import Contact, Session

_seq = itertools.count(1)


async def test_country_change_recomputes_norms(make_lead):
    suffix = 2100000 + next(_seq)
    local_value = f"050{suffix}"            # локальный формат, зависит от страны
    intl_value = f"+38050{2200000 + next(_seq)}"  # E.164, от страны не зависит
    ua_norm = normalize_phone(local_value, "UA")
    assert ua_norm == f"+38050{suffix}"

    async with Session() as s, s.begin():
        lead = await make_lead(s, country="Украина")
        s.add(Contact(lead_id=lead.id, ctype="phone",
                      value=local_value, value_norm=ua_norm))
        s.add(Contact(lead_id=lead.id, ctype="phone",
                      value=intl_value, value_norm=intl_value))
        lid = lead.id

    err = await apply_field(lid, "country", "Словакия", 1, None, True)
    assert err is None

    async with Session() as s:
        by_value = {
            c.value: c.value_norm
            for c in await s.scalars(select(Contact).where(Contact.lead_id == lid))
        }
    # местный номер больше не украинский: норма пересчитана (не +380…)
    assert by_value[local_value] != ua_norm
    # международный номер страны не боится
    assert by_value[intl_value] == intl_value

    # обратная смена возвращает норму
    err = await apply_field(lid, "country", "Украина", 1, None, True)
    assert err is None
    async with Session() as s:
        norm = await s.scalar(
            select(Contact.value_norm).where(
                Contact.lead_id == lid, Contact.value == local_value
            )
        )
    assert norm == ua_norm
