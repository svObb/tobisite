"""Публикация превью: слаг, выкладка в R2, адрес у черновика и лида (10.11–10.13).

Сети нет: клиент R2 подменяет фикстура r2 из conftest, PUT'ы складываются в
список. Без ключей R2 фикстура не ставится, и тогда проверяется обратное —
сборка работает как раньше, а публикации просто нет.
"""
from datetime import datetime, timedelta

from sqlalchemy import select

import config
import draft_service
from models import PREVIEW_TTL_DAYS, Draft, Lead, LeadEvent, Session


async def _draft(lead_id: int) -> Draft:
    async with Session() as s:
        return (await s.scalars(
            select(Draft).where(Draft.lead_id == lead_id)
        )).one()


async def _events(lead_id: int, event: str) -> list[LeadEvent]:
    async with Session() as s:
        return list(await s.scalars(
            select(LeadEvent).where(LeadEvent.lead_id == lead_id,
                                    LeadEvent.event == event)
        ))


async def _lead(lead_id: int) -> Lead:
    async with Session() as s:
        return await s.get(Lead, lead_id)


async def test_built_draft_becomes_a_live_preview(slot_answer, draft_lead, r2):
    lead = await draft_lead(name="Право і Діло")
    await slot_answer(lead)

    result = await draft_service.build_draft(lead.id)

    assert result.ok and result.preview_url == "https://pravo-i-dilo" \
                                               ".tobisitepreview.com/"
    assert not result.publish_reason
    put, = r2.puts
    assert put["Key"] == "pravo-i-dilo/index.html"
    assert put["ContentType"] == "text/html; charset=utf-8"
    assert put["Bucket"] == draft_service.DEFAULT_BUCKET
    assert b"<html" in put["Body"].lower()

    row = await _draft(lead.id)
    assert row.status == "published" and row.published_at
    assert row.r2_prefix == "pravo-i-dilo"
    assert row.preview_host == "pravo-i-dilo.tobisitepreview.com"
    assert row.slots_json                      # страницу можно собрать заново
    # ссылка на лиде и адрес черновика — одно и то же, иначе письмо уйдёт не туда
    assert (await _lead(lead.id)).draft_url == row.preview_url
    assert len(await _events(lead.id, "preview_published")) == 1


async def test_second_company_with_the_same_name_gets_its_own_slug(
        slot_answer, draft_lead, r2):
    first = await draft_lead(name="Зубна Фея")
    await slot_answer(first)
    assert (await draft_service.build_draft(first.id)).ok
    second = await draft_lead(name="Зубна Фея")
    await slot_answer(second)

    result = await draft_service.build_draft(second.id)

    assert (await _draft(first.id)).r2_prefix == "zubna-feia"
    assert (await _draft(second.id)).r2_prefix == "zubna-feia-2"
    assert result.preview_url == "https://zubna-feia-2.tobisitepreview.com/"


async def test_dead_r2_does_not_cancel_the_draft(slot_answer, draft_lead, r2):
    lead = await draft_lead()
    await slot_answer(lead)
    r2.fail = RuntimeError("бакета нет")

    result = await draft_service.build_draft(lead.id)

    assert result.ok and result.status == "generated"
    assert not result.preview_url and "бакета нет" in result.publish_reason
    row = await _draft(lead.id)
    assert row.status == "generated" and row.r2_prefix is None
    assert (await _lead(lead.id)).draft_url is None


async def test_republish_keeps_the_slug_and_does_not_ask_the_model(
        slot_answer, draft_lead, r2):
    lead = await draft_lead()
    state = await slot_answer(lead)
    built = await draft_service.build_draft(lead.id)
    calls = len(state.fake.messages.calls)

    result = await draft_service.publish_preview(lead.id)

    # тот же адрес: ссылка из уже отправленного письма обязана открываться
    assert result.ok and result.url == built.preview_url
    assert len(r2.puts) == 2 and r2.puts[0]["Body"] == r2.puts[1]["Body"]
    assert len(state.fake.messages.calls) == calls


async def test_draft_without_saved_slots_is_not_published(slot_answer,
                                                          draft_lead, r2):
    lead = await draft_lead()
    await slot_answer(lead)
    assert (await draft_service.build_draft(lead.id)).ok
    async with Session() as s, s.begin():
        (await s.get(Draft, (await _draft(lead.id)).id)).slots_json = None

    result = await draft_service.publish_preview(lead.id)

    assert not result.ok and "без слотов" in result.reason
    assert len(r2.puts) == 1                   # второй выкладки не было


# --- уборка превью (10.14, 10.15) ---------------------------------------------

async def _published(slot_answer, draft_lead, **kw) -> Lead:
    lead = await draft_lead(**kw)
    await slot_answer(lead)
    built = await draft_service.build_draft(lead.id)
    assert built.ok and built.preview_url, built.publish_reason
    return lead


async def _age(lead_id: int, days: int):
    async with Session() as s, s.begin():
        draft = await s.scalar(select(Draft).where(Draft.lead_id == lead_id))
        draft.published_at = datetime.now(config.TZ) - timedelta(days=days)


async def _set_status(lead_id: int, status: str):
    async with Session() as s, s.begin():
        (await s.get(Lead, lead_id)).status = status


async def test_preview_past_its_term_is_taken_down(slot_answer, draft_lead, r2):
    lead = await _published(slot_answer, draft_lead)
    slug = (await _draft(lead.id)).r2_prefix
    await _age(lead.id, PREVIEW_TTL_DAYS + 1)

    result = await draft_service.expire_previews()

    assert (lead.id, slug, f"срок {PREVIEW_TTL_DAYS} дней") in result.deleted
    assert f"{slug}/index.html" not in r2.objects
    row = await _draft(lead.id)
    assert row.status == "expired" and row.r2_prefix == slug
    # ссылка вела бы в 404 — с лида она снята, а событие осталось
    assert (await _lead(lead.id)).draft_url is None
    assert len(await _events(lead.id, "preview_expired")) == 1


async def test_sold_lead_keeps_its_preview(slot_answer, draft_lead, r2):
    lead = await _published(slot_answer, draft_lead)
    slug = (await _draft(lead.id)).r2_prefix
    await _age(lead.id, PREVIEW_TTL_DAYS * 3)
    await _set_status(lead.id, "sold")

    result = await draft_service.expire_previews()

    assert result.kept_sold and lead.id not in [x[0] for x in result.deleted]
    assert f"{slug}/index.html" in r2.objects
    assert (await _draft(lead.id)).status == "published"


async def test_closed_lead_loses_its_preview_at_once(slot_answer, draft_lead,
                                                     r2):
    lead = await _published(slot_answer, draft_lead)
    slug = (await _draft(lead.id)).r2_prefix
    await _set_status(lead.id, "refused")

    result = await draft_service.expire_previews()

    assert (lead.id, slug, "лид закрыт") in result.deleted
    assert f"{slug}/index.html" not in r2.objects


async def test_live_preview_is_not_touched(slot_answer, draft_lead, r2):
    lead = await _published(slot_answer, draft_lead)
    slug = (await _draft(lead.id)).r2_prefix

    result = await draft_service.expire_previews()

    assert lead.id not in [x[0] for x in result.deleted]
    assert f"{slug}/index.html" in r2.objects
    assert (await _draft(lead.id)).status == "published"


async def test_failed_deletion_keeps_the_row_as_it_was(slot_answer, draft_lead,
                                                       r2):
    lead = await _published(slot_answer, draft_lead)
    url = (await _lead(lead.id)).draft_url
    await _age(lead.id, PREVIEW_TTL_DAYS + 1)
    r2.fail = RuntimeError("R2 не отвечает")

    result = await draft_service.expire_previews()

    assert any(f"#{lead.id}" in line for line in result.failed)
    assert (await _draft(lead.id)).status == "published"
    assert (await _lead(lead.id)).draft_url == url


async def test_slug_of_a_removed_preview_is_not_given_away(slot_answer,
                                                           draft_lead, r2):
    first = await _published(slot_answer, draft_lead, name="Мовна Школа")
    await _age(first.id, PREVIEW_TTL_DAYS + 1)
    assert (await draft_service.expire_previews()).deleted
    second = await _published(slot_answer, draft_lead, name="Мовна Школа")

    # сохранённая клиентом ссылка не должна однажды открыть чужой сайт
    assert (await _draft(second.id)).r2_prefix == "movna-shkola-2"


async def test_without_r2_keys_the_pipeline_works_as_before(slot_answer,
                                                            draft_lead,
                                                            monkeypatch):
    for name in draft_service.R2_ENV:
        monkeypatch.delenv(name, raising=False)
    lead = await draft_lead()
    await slot_answer(lead)

    result = await draft_service.build_draft(lead.id)

    assert result.ok and not result.preview_url and not result.publish_reason
    assert (await _draft(lead.id)).status == "generated"
    assert not (await draft_service.publish_preview(lead.id)).ok
