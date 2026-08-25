"""Сборка черновика лида (Д13 §3): строка drafts, обогащение, описание.

Модель подменяет фикстура slot_answer из conftest. Ключей R2 в тестовом
окружении нет, поэтому здесь конвейер заканчивается строкой в базе; публикация
превью проверяется в test_preview_publish.py.
"""
from sqlalchemy import select

import config
import draft_service
import queue_service as qs
from conftest import TEST_TG_BASE
from models import Draft, Lead, Session
from site_factory.engine import render
from test_email_gen import UK_JSON


async def test_build_writes_the_row(slot_answer, draft_lead):
    lead = await draft_lead()
    state = await slot_answer(lead)

    result = await draft_service.build_draft(lead.id)

    assert result.ok and result.status == "generated", result.reason
    assert result.checks == {} and not result.missing
    row = await _row(lead.id)
    assert row.status == "generated" and row.checks_json == {}
    assert row.seed == render.seed_for(lead.domain_norm)
    assert row.recipe_id == "generic_light" and row.token_preset
    assert row.library_version and row.expires_at
    assert row.section_variants == row.recipe_json["sections"]
    assert "hero_type_only" in row.section_variants
    assert "footer_nap" in row.section_variants
    assert row.image_ids == []
    # рендер собрал ту же композицию, что видела слот-генерация: seed один
    assert row.recipe_json["sections"] == [s["variant"] for s in state.sections]
    assert row.recipe_json["dropped_sections"] == []
    assert row.recipe_json["empty_slots"] == []
    async with Session() as s:
        fresh = await s.get(Lead, lead.id)
    assert fresh.needs_enrichment is False and fresh.enrichment_request is None


async def test_rebuild_repeats_the_same_composition(slot_answer, draft_lead):
    lead = await draft_lead()
    await slot_answer(lead)

    first = await draft_service.build_draft(lead.id)
    before = await _row(lead.id)
    second = await draft_service.build_draft(lead.id)
    after = await _row(lead.id)

    assert first.ok and second.ok
    assert second.draft_id == first.draft_id      # черновик у лида один
    assert after.seed == before.seed
    assert after.token_preset == before.token_preset
    assert after.section_variants == before.section_variants


async def test_failed_rebuild_keeps_the_good_draft(slot_answer, draft_lead,
                                                   monkeypatch):
    lead = await draft_lead()
    await slot_answer(lead)
    assert (await draft_service.build_draft(lead.id)).ok
    before = await _row(lead.id)

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    result = await draft_service.build_draft(lead.id)

    assert not result.ok and result.status == "failed"
    assert "ANTHROPIC_API_KEY" in result.reason
    after = await _row(lead.id)
    assert after.id == before.id and after.status == "generated"
    assert after.generated_at == before.generated_at


async def test_thin_lead_asks_the_finder_for_enrichment(slot_answer, draft_lead):
    lead = await draft_lead(enrichment={})
    state = await slot_answer(lead)

    result = await draft_service.build_draft(lead.id)

    assert not result.ok and result.needs_enrichment
    assert result.draft_id and result.status == "failed"
    assert any("адрес" in hint.lower() for hint in result.missing)
    assert any("услуг" in hint.lower() for hint in result.missing)
    # просьба уходит тому, кто лид нашёл
    assert result.notify_tg_id >= TEST_TG_BASE
    async with Session() as s:
        fresh = await s.get(Lead, lead.id)
    assert fresh.needs_enrichment is True
    assert fresh.enrichment_request.startswith("• ")
    assert fresh.enrichment_request.count("•") == len(result.missing)
    # у модели просить нечего: страницы всё равно не будет
    assert state.fake.messages.calls == []


async def test_summary_names_only_what_is_on_the_page(slot_answer, draft_lead):
    lead = await draft_lead()
    state = await slot_answer(lead)

    result = await draft_service.build_draft(lead.id)
    row = await _row(lead.id)

    uk = draft_service.draft_summary(row, "uk")
    assert uk == result.summary == draft_service.draft_summary(row)
    assert 5 <= len(uk.split()) <= 12
    assert uk == draft_service.draft_summary(row, "uk")
    named = [variant for variant, parts in draft_service.SUMMARY_PARTS.items()
             if parts["uk"] in uk]
    assert named and set(named) <= set(row.section_variants)
    en = draft_service.draft_summary(row, "en")
    assert 5 <= len(en.split()) <= 12 and en != uk
    # описание собирается из состава композиции, а не моделью
    assert len(state.fake.messages.calls) == 1


async def test_queue_takes_the_summary_from_the_draft(slot_answer, model,
                                                      draft_lead):
    lead = await draft_lead(status="verified")
    await slot_answer(lead)
    built = await draft_service.build_draft(lead.id)
    assert built.ok, built.reason
    fake = model(UK_JSON)

    queued = await qs.enqueue(lead.id, actor_tg_id=config.ADMIN_TG_ID)

    assert queued.ok, queued.reason
    assert f"<draft>{built.summary}</draft>" in _prompt(fake)


async def test_lead_without_a_draft_waits_for_hands(model, gap_lead):
    fake = model(UK_JSON)
    lead = await gap_lead(status="verified")

    queued = await qs.enqueue(lead.id, actor_tg_id=config.ADMIN_TG_ID)

    assert not queued.ok and "черновик" in queued.reason
    assert fake.messages.calls == []


async def _row(lead_id: int) -> Draft:
    async with Session() as s:
        return (await s.scalars(
            select(Draft).where(Draft.lead_id == lead_id)
        )).one()


def _prompt(fake) -> str:
    return fake.messages.calls[0]["messages"][0]["content"]
