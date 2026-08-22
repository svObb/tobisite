"""Сборка письма 1 (Д12 §1, §4): слои, кэшируемый префикс, ручная ветка.

Клиент модели подменяет фикстура model из conftest: сети в тестах нет.
"""
from datetime import datetime, timedelta

from sqlalchemy import select

import config
import costs
import email_gen
from models import GAP_TTL_DAYS, CostLedger, Session, Suppression, company_key

# Ответы модели: валидный JSON, честный отказ и мусор вместо JSON.
UK_JSON = (
    '{"bridge": "На такому екрані людині простіше повернутись у пошук, '
    'ніж дочекатись.", "offer": "Я зібрав чернетку вашої головної на ваших '
    'реальних даних, вона відкривається за секунду."}'
)
EN_JSON = (
    '{"bridge": "Most people go back to the search results before a page '
    'like that loads.", "offer": "I\'ve built a draft of your homepage on '
    'your real data, and it loads in under a second."}'
)
NULL_JSON = '{"bridge": null, "offer": null, "reason": "немає чим підкріпити"}'
GARBAGE = "Конечно! Вот ваше письмо:\n\nДоброго дня..."

UK_DRAFT = "одна сторінка, кнопка запису вгорі, вантажиться за секунду"
EN_DRAFT = "single page, booking button on top, loads in under a second"


async def test_valid_answer_builds_the_letter(model, gap_lead):
    fake = model(UK_JSON)
    lead = await gap_lead()

    result = await email_gen.build_email(lead, UK_DRAFT)

    assert result.ok and not result.needs_manual
    assert result.lang == "uk" and result.model == email_gen.MODEL
    assert result.lint.fails == []
    # все четыре слоя на месте и ровно по одному разу
    assert result.body.startswith("Доброго дня!")
    # какой из трёх вариантов первой строки выпал — дело hash(lead_id),
    # но факт из наблюдения в письме обязан быть
    assert "8 секунд" in result.body
    assert result.slots["bridge"] in result.body
    assert result.slots["offer"] in result.body
    assert result.slots["cta"] in email_gen.CTA["uk"]
    assert "Микола Тобі, tobisite" in result.body
    assert email_gen.OPT_OUT["uk"] in result.body
    assert result.subject
    assert len(fake.messages.calls) == 1


async def test_letter_1_has_no_links(model, gap_lead):
    model(UK_JSON)
    lead = await gap_lead()
    result = await email_gen.build_email(lead, UK_DRAFT)
    assert "http" not in result.body and "www." not in result.body


async def test_prompt_carries_cacheable_prefix_and_card(model, gap_lead):
    fake = model(EN_JSON)
    lead = await gap_lead(language="Английский", gap_note="хвіст")

    await email_gen.build_email(lead, EN_DRAFT)

    call = fake.messages.calls[0]
    assert call["model"] == "claude-sonnet-5"
    system = call["system"][0]
    assert system["cache_control"] == {"type": "ephemeral"}
    # префикс: системный промпт + девять примеров языка вывода, и только его
    assert system["text"].startswith("Ты — редактор коротких деловых писем.")
    assert system["text"].count("Вход: gap=") == 9
    assert "Em-dash count must be exactly 0." in system["text"]
    assert "тільки телефон" not in system["text"]

    user = call["messages"][0]["content"]
    assert "<output_language>en</output_language>" in user
    first_line = user.split("<first_line>")[1].split("</first_line>")[0]
    assert first_line and first_line[0].isupper() and first_line.endswith(".")
    assert f"<draft>{EN_DRAFT}</draft>" in user
    assert "niche:" in user and "contact: Олена" in user
    assert "type: slow" in user and "value: 8" in user
    # деталь наблюдения не переводится и в EN-письмо не идёт (Д12 §3)
    assert "хвіст" not in user


async def test_uk_prompt_keeps_the_note(model, gap_lead):
    fake = model(UK_JSON)
    lead = await gap_lead(gap_note="кнопка запису не працює")
    await email_gen.build_email(lead, UK_DRAFT)
    assert "кнопка запису не працює" in fake.messages.calls[0]["messages"][0]["content"]


async def test_assembly_is_deterministic(model, gap_lead):
    model(UK_JSON, UK_JSON)
    lead = await gap_lead()
    first = await email_gen.build_email(lead, UK_DRAFT)
    second = await email_gen.build_email(lead, UK_DRAFT)
    assert first.body == second.body and first.subject == second.subject


async def test_ukrainian_greeting_never_carries_the_name(model, gap_lead):
    model(UK_JSON)
    lead = await gap_lead()
    result = await email_gen.build_email(lead, UK_DRAFT)
    # звательный падеж («Олено») мы не генерируем — имени в письме нет вообще
    assert "Олена" not in result.body


def test_english_greeting_falls_back_without_a_name():
    assert email_gen.greeting("en", "Anna") == "Hi Anna,"
    assert email_gen.greeting("en", "") == "Hi,"
    assert email_gen.greeting("uk", "Олена") == "Доброго дня!"


async def test_cta_has_four_variants_per_language():
    for lang, options in email_gen.CTA.items():
        assert len(options) == 4, lang
        picked = {email_gen.phrases.variant(i, options) for i in range(4)}
        assert len(picked) == 4, lang
        for text in options:
            assert text.count("?") == 1, text
            assert "http" not in text, text


# --- ручная ветка -------------------------------------------------------------

async def test_null_answer_goes_manual(model, gap_lead):
    fake = model(NULL_JSON)
    lead = await gap_lead()
    result = await email_gen.build_email(lead, UK_DRAFT)
    assert result.needs_manual and "не взялась" in result.reason
    assert "немає чим підкріпити" in result.reason
    # ослаблять правила и просить ещё раз — запрещено
    assert len(fake.messages.calls) == 1


async def test_garbage_instead_of_json_goes_manual(model, gap_lead):
    fake = model(GARBAGE)
    lead = await gap_lead()
    result = await email_gen.build_email(lead, UK_DRAFT)
    assert result.needs_manual and "не JSON" in result.reason
    assert len(fake.messages.calls) == 1


async def test_lint_failure_retries_once_then_manual(model, gap_lead):
    long_offer = " ".join(["слово"] * 30)
    bad = '{"bridge": "Коротко.", "offer": "%s."}' % long_offer
    fake = model(bad, bad)
    lead = await gap_lead()

    result = await email_gen.build_email(lead, UK_DRAFT)

    assert result.needs_manual and result.reason.startswith("линтер:")
    assert len(fake.messages.calls) == 2


async def test_no_api_key_goes_manual(model, gap_lead, monkeypatch):
    fake = model(UK_JSON)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    lead = await gap_lead()
    result = await email_gen.build_email(lead, UK_DRAFT)
    assert result.needs_manual and "ANTHROPIC_API_KEY" in result.reason
    assert fake.messages.calls == []


async def test_cap_stops_before_the_call(model, gap_lead, monkeypatch):
    fake = model(UK_JSON)
    monkeypatch.setattr(costs, "cap_reached", _always(True))
    lead = await gap_lead()
    result = await email_gen.build_email(lead, UK_DRAFT)
    assert result.needs_manual and "кэп" in result.reason
    assert fake.messages.calls == []


async def test_foreign_language_goes_manual(model, gap_lead):
    fake = model(UK_JSON)
    lead = await gap_lead(language="Словацкий")
    result = await email_gen.build_email(lead, UK_DRAFT)
    assert result.needs_manual and "нет фраз для языка" in result.reason
    assert fake.messages.calls == []


async def test_stale_observation_goes_manual(model, gap_lead):
    fake = model(UK_JSON)
    lead = await gap_lead(
        gap_captured_at=datetime.now(config.TZ) - timedelta(days=GAP_TTL_DAYS + 1)
    )
    result = await email_gen.build_email(lead, UK_DRAFT)
    assert result.needs_manual and "устарело" in result.reason
    assert fake.messages.calls == []


async def test_suppressed_lead_goes_manual(model, gap_lead):
    fake = model(UK_JSON)
    lead = await gap_lead(name="Стоп Клініка", city="Львів")
    async with Session() as s, s.begin():
        s.add(Suppression(kind="company",
                          value_norm=company_key("Стоп Клініка", "Львів"),
                          reason="pytest", source="pytest"))
    result = await email_gen.build_email(lead, UK_DRAFT)
    assert result.needs_manual and "стоп-лист" in result.reason
    assert fake.messages.calls == []


# --- учёт расходов ------------------------------------------------------------

async def test_cost_is_written_to_ledger(model, gap_lead):
    model(UK_JSON)
    lead = await gap_lead()

    await email_gen.build_email(lead, UK_DRAFT)

    async with Session() as s:
        row = (await s.scalars(
            select(CostLedger).where(CostLedger.lead_id == lead.id)
        )).one()
    assert row.op == "letter" and row.model == "claude-sonnet-5"
    assert row.input_tokens == 120 and row.output_tokens == 60
    assert row.cache_read_tokens == 1800
    assert row.cost_usd > 0


# --- письма 2 и 3 -------------------------------------------------------------

async def test_letters_2_and_3_are_constants(model, gap_lead):
    fake = model(UK_JSON)
    lead = await gap_lead()

    second = email_gen.build_email_2(lead, "klinika.tobisitepreview.com")
    third = email_gen.build_email_3(lead)

    assert second.ok and "klinika.tobisitepreview.com" in second.body
    assert third.ok and str(email_gen.DRAFT_HOLD_DAYS) in third.body
    assert "«ні»" in third.body
    assert second.body.startswith("Доброго дня!")
    assert email_gen.OPT_OUT["uk"] in second.body
    # модель для касаний 2 и 3 не зовётся вообще
    assert fake.messages.calls == []


async def test_letters_2_and_3_in_english(model, gap_lead):
    lead = await gap_lead(language="Английский")
    second = email_gen.build_email_2(lead, "clinic.tobisitepreview.com")
    third = email_gen.build_email_3(lead)
    assert second.lang == "en" and "Here is the draft" in second.body
    assert third.lang == "en" and "30 days" in third.body


def _always(value):
    async def _call(*a, **kw):
        return value
    return _call
