"""Слот-генерация черновика (Д13 §2): лимиты, перегенерация, язык, факты.

Клиента модели подменяют фикстуры slot_model/slot_answer из conftest: сети в
тестах нет, платных вызовов тоже.
"""
import json

from sqlalchemy import select

import config
import costs
import draft_service
import slot_gen
from models import CostLedger, Session
from site_factory.engine import render
from site_factory.engine import slots as sf_slots


async def test_too_long_slot_is_asked_again_alone(slot_plan, slot_answer,
                                                  draft_lead):
    lead = await draft_lead()
    victim = _spec(await slot_plan(lead), "headline")
    state = await slot_answer(lead, {victim["slot"]: "я" * (victim["max_chars"] + 1)},
                              {})

    result = await slot_gen.fill_slots(state.profile, state.sections, "uk",
                                       lead_id=lead.id)

    assert result.ok and result.empty == []
    assert 0 < len(result.texts[victim["slot"]]) <= victim["max_chars"]
    assert len(state.fake.messages.calls) == 2
    # первый заход — вся страница, второй — ровно нарушивший слот
    assert _asked(state.fake, 0) == [spec["slot"] for spec in state.specs]
    assert _asked(state.fake, 1) == [victim["slot"]]


async def test_second_violation_leaves_the_slot_empty(slot_plan, slot_answer,
                                                      draft_lead):
    lead = await draft_lead()
    victim = _spec(await slot_plan(lead), "privacy_note")
    # фальшивка повторяет последний ответ: модель нарушает лимит и во второй раз
    state = await slot_answer(lead, {victim["slot"]: "я" * (victim["max_chars"] + 1)})

    result = await slot_gen.fill_slots(state.profile, state.sections, "uk",
                                       lead_id=lead.id)

    assert result.ok and result.empty == [victim["slot"]]
    assert result.texts[victim["slot"]] == ""
    # ровно одна перегенерация: дальше судьбу секции решает лестница деградации
    assert len(state.fake.messages.calls) == 2


async def test_ukrainian_lead_gets_ukrainian_prompt(slot_answer, draft_lead):
    lead = await draft_lead()
    assert draft_service.lang_of(lead) == "uk"
    state = await slot_answer(lead)

    await slot_gen.fill_slots(state.profile, state.sections, "uk", lead_id=lead.id)

    call = state.fake.messages.calls[0]
    assert call["model"] == slot_gen.MODEL
    assert call["thinking"] == {"type": "disabled"}
    assert "temperature" not in call and "top_p" not in call
    system = call["system"][0]
    assert system["cache_control"] == {"type": "ephemeral"}
    assert "Стоматологія «Лінія»" in system["text"]
    assert "Corner Bakery" not in system["text"]
    assert "<output_language>uk</output_language>" in call["messages"][0]["content"]


async def test_foreign_country_gets_english_prompt(slot_answer, draft_lead):
    lead = await draft_lead(country="Словакия", language="Словацкий")
    assert draft_service.lang_of(lead) == "en"
    state = await slot_answer(lead)

    await slot_gen.fill_slots(state.profile, state.sections, "en", lead_id=lead.id)

    call = state.fake.messages.calls[0]
    assert "Em-dash count must be exactly 0." in call["system"][0]["text"]
    assert "Corner Bakery" in call["system"][0]["text"]
    assert "Стоматологія «Лінія»" not in call["system"][0]["text"]
    assert "<output_language>en</output_language>" in call["messages"][0]["content"]


async def test_facts_never_reach_the_model(slot_answer, draft_lead):
    lead = await draft_lead(phone="+380 00 000 00 07")
    state = await slot_answer(lead)

    await slot_gen.fill_slots(state.profile, state.sections, "uk", lead_id=lead.id)

    prompt = state.fake.messages.calls[0]["messages"][0]["content"]
    for secret in ("+380 00 000 00 07", "office@example.com", "вул. Тестова",
                   "Лікування карієсу", "4.7"):
        assert secret not in prompt, secret
    # fact-слоты не показываются даже именем: их закрывает белый список профиля
    for name in ("phone_href", "business_name", "service_name", "stat_value"):
        assert name not in prompt, name
    assert lead.name in prompt and "Тест-город" in prompt


async def test_cost_lands_in_the_ledger(slot_answer, draft_lead):
    lead = await draft_lead()
    state = await slot_answer(lead)

    await slot_gen.fill_slots(state.profile, state.sections, "uk", lead_id=lead.id)

    async with Session() as s:
        row = (await s.scalars(
            select(CostLedger).where(CostLedger.lead_id == lead.id)
        )).one()
    assert row.op == "draft" and row.model == slot_gen.MODEL
    assert row.input_tokens == 120 and row.output_tokens == 60
    assert row.cache_read_tokens == 1800 and row.cost_usd > 0


async def test_no_key_refuses_without_a_call(slot_answer, draft_lead, monkeypatch):
    lead = await draft_lead()
    state = await slot_answer(lead)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")

    result = await slot_gen.fill_slots(state.profile, state.sections, "uk",
                                       lead_id=lead.id)

    assert not result.ok and "ANTHROPIC_API_KEY" in result.reason
    assert state.fake.messages.calls == []


async def test_cap_stops_before_the_call(slot_answer, draft_lead, monkeypatch):
    lead = await draft_lead()
    state = await slot_answer(lead)
    monkeypatch.setattr(costs, "cap_reached", _always(True))

    result = await slot_gen.fill_slots(state.profile, state.sections, "uk",
                                       lead_id=lead.id)

    assert not result.ok and "кэп" in result.reason
    assert state.fake.messages.calls == []


async def test_unknown_language_refuses(slot_answer, draft_lead):
    lead = await draft_lead()
    state = await slot_answer(lead)

    result = await slot_gen.fill_slots(state.profile, state.sections, "sk",
                                       lead_id=lead.id)

    assert not result.ok and "sk" in result.reason
    assert state.fake.messages.calls == []


async def test_regeneration_offers_a_tighter_budget(slot_plan, slot_answer,
                                                    draft_lead):
    lead = await draft_lead()
    victim = _spec(await slot_plan(lead), "headline")
    state = await slot_answer(lead, {victim["slot"]: "я" * (victim["max_chars"] + 1)},
                              {})

    await slot_gen.fill_slots(state.profile, state.sections, "uk",
                              lead_id=lead.id)

    # модель промахивается в счёте символов на два-три знака, поэтому на
    # повторе ей показывают меньший предел; настоящий лимит проверяет код
    sent = _sent_specs(state.fake, 1)
    limit = victim["max_chars"]
    assert [spec["slot"] for spec in sent] == [victim["slot"]]
    assert sent[0]["max_chars"] == limit - max(2, limit // 5)


def test_the_dictionary_names_every_free_slot_of_the_library():
    """Слот без строки в словаре — слот, о смысле которого модель гадает."""
    kinds = {spec["name"]
             for contract in render.load_library().values()
             for spec in sf_slots.free_specs(contract)}
    listed = set()
    for line in slot_gen.SYSTEM_PROMPT.split("СЛОВАРЬ СЛОТОВ")[1].splitlines():
        head, dash, _ = line.partition(" — ")
        if dash and not line.startswith(" "):
            listed |= {name.strip() for name in head.split(",")}

    grouped = {spec["name"]
               for contract in render.load_library().values()
               for spec in sf_slots.group_free_specs(contract)
               if spec["name"] not in slot_gen.FACT_BOUND_GROUP_SLOTS}
    assert kinds and (kinds | grouped) <= listed, sorted((kinds | grouped) - listed)
    assert slot_gen.PROMPT_VERSION == "s6"


async def test_the_label_of_a_google_figure_is_never_asked_of_the_model(
        slot_plan, draft_lead):
    """Какая цифра стоит рядом, знает профиль: подпись к ней модель не пишет."""
    lead = await draft_lead()
    plan = await slot_plan(lead)
    proof = next(part for part in plan.sections if part["role"] == "proof")

    assert proof["slots"]["stats"]
    assert not any(spec["kind"] == "stat_label" for spec in plan.specs)
    # руками подпись написать можно: цифру рядом человек видит, модель — нет
    dev = slot_gen.slot_specs(plan.sections, include_fact_bound=True)
    assert [spec["slot"] for spec in dev
            if spec["kind"] == "stat_label"] == \
        [f"{proof['variant']}.stat_label[{i}]"
         for i in range(len(proof["slots"]["stats"]))]


async def test_an_honestly_empty_blurb_is_not_asked_again(slot_plan,
                                                          slot_answer,
                                                          draft_lead):
    """Правило 6 промпта: нечего сказать — верни "". Для блёрба это рутина.

    Названий услуг модель не видит вовсе, и второй вызов лечил бы длину,
    которой нет: платный заход ради того же пустого места.
    """
    lead = await draft_lead()
    blurb = _spec(await slot_plan(lead), "service_blurb")
    state = await slot_answer(lead, {blurb["slot"]: ""})

    result = await slot_gen.fill_slots(state.profile, state.sections, "uk",
                                       lead_id=lead.id)

    assert result.ok and len(state.fake.messages.calls) == 1
    # ключ есть, значение пустое: услуга останется без строки, а не с
    # заготовкой рецепта, и секцию это не валит
    assert result.texts[blurb["slot"]] == ""
    assert blurb["slot"] not in result.empty


async def test_an_empty_scalar_slot_is_asked_again(slot_plan, slot_answer,
                                                   draft_lead):
    """Пустой заголовок секции уводит секцию со страницы — тут повтор уместен."""
    lead = await draft_lead()
    victim = _spec(await slot_plan(lead), "section_title")
    state = await slot_answer(lead, {victim["slot"]: ""}, {})

    result = await slot_gen.fill_slots(state.profile, state.sections, "uk",
                                       lead_id=lead.id)

    assert result.ok and result.empty == []
    assert len(state.fake.messages.calls) == 2
    assert _asked(state.fake, 1) == [victim["slot"]]
    assert result.texts[victim["slot"]]


async def test_every_service_gets_its_own_blurb_slot(slot_plan, draft_lead):
    """Блёрб пишется на каждую услугу отдельно: одной строкой их не закрыть."""
    lead = await draft_lead()
    plan = await slot_plan(lead)
    section = next(part for part in plan.sections if part["role"] == "services")
    count = len(section["slots"]["services"])

    blurbs = [spec for spec in plan.specs if spec["kind"] == "service_blurb"]

    assert count >= 2
    assert [spec["slot"] for spec in blurbs] == \
        [f"{section['variant']}.service_blurb[{i}]" for i in range(count)]
    limit = next(spec["max_chars"]
                 for spec in section["contract"]["slots"]
                 if spec["name"] == "service_blurb")
    assert all(spec["grouped"] and spec["max_chars"] == limit for spec in blurbs)
    # у скалярных слотов признака группы нет: их ключ без индекса
    assert not any(spec.get("grouped") for spec in plan.specs
                   if spec["slot"].endswith(".section_title"))


def test_model_json_survives_markdown_fence():
    fenced = '```json\n{"hero.headline": "Запчастини та ремонт"}\n```'
    assert slot_gen.parse_model_json(fenced) == {
        "hero.headline": "Запчастини та ремонт"}
    # фенс без языка — модели пишут и так
    assert slot_gen.parse_model_json('```\n{"a": "б"}\n```') == {"a": "б"}
    assert slot_gen.parse_model_json('{"a": "б"}') == {"a": "б"}


def test_model_json_rejects_junk():
    assert slot_gen.parse_model_json("почти json") is None
    assert slot_gen.parse_model_json('```json\nне json\n```') is None
    assert slot_gen.parse_model_json('["список"]') is None
    assert slot_gen.parse_model_json("") is None
    # фенс не закрыт — содержимое не восстанавливаем, честный отказ
    assert slot_gen.parse_model_json('```json\n{"a": "б"}') is None


def _spec(plan, kind: str) -> dict:
    return next(spec for spec in plan.specs if spec["kind"] == kind)


def _asked(fake, index: int) -> list[str]:
    """Какие слоты ушли в модель на этом вызове."""
    return [spec["slot"] for spec in _sent_specs(fake, index)]


def _sent_specs(fake, index: int) -> list[dict]:
    """Спеки слотов, как их увидела модель на этом вызове."""
    content = fake.messages.calls[index]["messages"][0]["content"]
    payload = content.split("<slots>")[1].split("</slots>")[0]
    return json.loads(payload)


def _always(value):
    async def _call(*a, **kw):
        return value
    return _call
