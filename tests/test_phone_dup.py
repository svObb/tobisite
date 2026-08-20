"""Дубль телефона: ранняя проверка (шаг ввода) и спасение формы на INSERT.

Дефект 6.1/6.2: раньше дубль вылезал только на финальном INSERT, и работник
терял всю 12-шаговую форму.
"""
import itertools
from datetime import datetime

from sqlalchemy import update

import config
from handlers_worker import _drop_dup_phones, phone_dup_exists
from models import Contact, Lead, Session

_seq = itertools.count(1)


def _phone() -> str:
    return f"+38050{2000000 + next(_seq)}"


async def _add_phone(s, lead_id: int, value: str) -> int:
    c = Contact(lead_id=lead_id, ctype="phone", value=value, value_norm=value)
    s.add(c)
    await s.flush()
    return c.id


async def test_dup_matches_unique_index_predicate(make_lead):
    phone = _phone()
    async with Session() as s, s.begin():
        lead = await make_lead(s)
        cid = await _add_phone(s, lead.id, phone)
        lid = lead.id

    async with Session() as s:
        assert await phone_dup_exists(s, phone)
        # свой собственный контакт — не дубль (режим редактирования)
        assert not await phone_dup_exists(s, phone, exclude_contact_id=cid)
        assert not await phone_dup_exists(s, None)

    # отмена лида освобождает номер — ровно как в предикате индекса
    now = datetime.now(config.TZ)
    async with Session() as s, s.begin():
        (await s.get(Lead, lid)).cancelled_at = now
        await s.execute(
            update(Contact).where(Contact.lead_id == lid)
            .values(lead_cancelled_at=now)
        )
    async with Session() as s:
        assert not await phone_dup_exists(s, phone)


async def test_drop_dup_phones_keeps_rest_of_form(make_lead):
    taken = _phone()
    free = _phone()
    async with Session() as s, s.begin():
        lead = await make_lead(s)
        await _add_phone(s, lead.id, taken)

    d = {
        "country": "Украина",
        "contacts": [
            {"ctype": "phone", "ctype_other": None, "value": taken},
            {"ctype": "phone", "ctype_other": None, "value": free},
            {"ctype": "email", "ctype_other": None, "value": "x@example.com"},
        ],
    }
    dropped = await _drop_dup_phones(d)
    assert dropped == [taken]
    assert [c["value"] for c in d["contacts"]] == [free, "x@example.com"]
