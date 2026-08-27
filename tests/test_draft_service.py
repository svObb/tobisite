"""Сборка черновика лида (Д13 §3): строка drafts, обогащение, описание.

Модель подменяет фикстура slot_answer из conftest. Ключей R2 в тестовом
окружении нет, поэтому здесь конвейер заканчивается строкой в базе; публикация
превью проверяется в test_preview_publish.py.
"""
import json

from sqlalchemy import select

import config
import draft_service
import queue_service as qs
from conftest import DRAFT_ENRICHMENT, SLOT_LINES, TEST_TG_BASE
from models import CostLedger, Draft, Lead, Session
from site_factory.engine import render
from test_email_gen import UK_JSON

# Товары и картинки, снятые с сайта лида (дорожка III). Компания выдумана,
# цены — входные данные теста: их пишет бизнес, а не модель.
SHOP_IMAGES = {
    "portrait": {"src": "/img/portrait.webp", "width": 1200, "height": 900},
    "photo-2": {"src": "/img/photo-2.webp", "width": 800, "height": 800},
    "photo-3": {"src": "/img/photo-3.webp", "width": 800, "height": 800},
    "photo-4": {"src": "/img/photo-4.webp", "width": 800, "height": 800},
}
SHOP_PRODUCTS = [
    {"name": "Ноутбук Alpha 14", "price": "24 900 грн",
     "image": dict(SHOP_IMAGES["portrait"])},
    {"name": "Ноутбук Beta 15", "price": "31 400 грн",
     "image": dict(SHOP_IMAGES["photo-2"])},
    {"name": "Док-станція Gamma",
     "image": dict(SHOP_IMAGES["photo-3"])},
]


def shop_enrichment(**kw) -> dict:
    """Обогащение магазина: товары с фотографиями и трек rich."""
    return DRAFT_ENRICHMENT | {"photo_count": 4, "images": dict(SHOP_IMAGES),
                               "products": [dict(p) for p in SHOP_PRODUCTS]} | kw


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


async def test_photos_from_the_site_bring_the_photo_hero(slot_answer,
                                                         draft_lead, r2):
    """Картинки, снятые с сайта лида, доходят до страницы (дорожка III)."""
    lead = await draft_lead(enrichment=dict(
        DRAFT_ENRICHMENT, photo_count=3,
        images={"portrait": {"src": "/img/portrait.webp",
                             "width": 1000, "height": 1200}},
    ))
    await slot_answer(lead)

    result = await draft_service.build_draft(lead.id)

    assert result.ok, result.reason
    row = await _row(lead.id)
    assert "hero_photo_left" in row.section_variants
    assert row.image_ids == ["/img/portrait.webp"]
    html = r2.puts[-1]["Body"].decode()
    # размеры в разметке — иначе страница дёргается, пока грузится фото
    assert 'src="/img/portrait.webp"' in html
    assert 'width="1000"' in html and 'height="1200"' in html


# --- товары с сайта лида (сшивка дорожек II и III) ----------------------------

async def test_products_from_the_site_reach_the_page(slot_answer, draft_lead, r2):
    lead = await draft_lead(enrichment=shop_enrichment())
    await slot_answer(lead)

    result = await draft_service.build_draft(lead.id)

    assert result.ok, result.reason
    row = await _row(lead.id)
    assert "products_grid" in row.section_variants
    html = r2.puts[-1]["Body"].decode()
    for product in SHOP_PRODUCTS:
        assert product["name"] in html
    # цену на страницу ставит код строкой ровно из карточки, а не модель
    assert "24 900 грн" in html
    assert 'src="/img/photo-2.webp"' in html


async def test_a_product_without_a_picture_stays_out_of_the_grid(slot_answer,
                                                                 draft_lead, r2):
    products = [dict(p) for p in SHOP_PRODUCTS] + [{"name": "Кабель без фото"}]
    lead = await draft_lead(enrichment=shop_enrichment(products=products))
    await slot_answer(lead)

    assert (await draft_service.build_draft(lead.id)).ok
    html = r2.puts[-1]["Body"].decode()

    # пустая рамка в сетке хуже отсутствующего товара (group_filter has_image)
    assert "Кабель без фото" not in html
    assert "Ноутбук Alpha 14" in html


async def test_half_written_pictures_do_not_reach_the_page(slot_answer,
                                                            draft_lead, r2):
    """Запись без размеров — не картинка: разметка ставит width/height как есть."""
    lead = await draft_lead(enrichment=shop_enrichment(
        images=dict(SHOP_IMAGES, portrait={"src": "/img/portrait.webp"}),
        products=[{"name": "Товар без назви ціни", "price": "  "},
                  {"name": "", "price": "10 грн"},
                  *[dict(p) for p in SHOP_PRODUCTS]],
    ))
    async with Session() as s:
        profile = await draft_service.build_profile(s, lead)

    assert "portrait" not in profile.images.value
    names = [item["name"] for item in profile.products.value]
    assert names == ["Товар без назви ціни", "Ноутбук Alpha 14",
                     "Ноутбук Beta 15", "Док-станція Gamma"]
    assert "price" not in profile.products.value[0]

    await slot_answer(lead)
    assert (await draft_service.build_draft(lead.id)).ok
    row = await _row(lead.id)
    # картинки без размеров нет в белом списке — секция с ней и не выигрывает
    assert "hero_photo_left" not in row.section_variants
    assert "/img/portrait.webp" not in row.image_ids


# --- часы, вписанные в карточку одной строкой ---------------------------------

HOURS_LINE = "Пн-Пт 9:00-19:00, Сб 10:00-17:00"


async def test_hours_written_as_one_line_become_a_list(draft_lead):
    lead = await draft_lead(enrichment=dict(DRAFT_ENRICHMENT, hours=HOURS_LINE))
    async with Session() as s:
        profile = await draft_service.build_profile(s, lead)

    assert profile.hours.value == ["Пн-Пт 9:00-19:00", "Сб 10:00-17:00"]


async def test_hours_line_reaches_the_page_as_days_and_not_letters(slot_answer,
                                                                   draft_lead,
                                                                   r2):
    lead = await draft_lead(enrichment=dict(DRAFT_ENRICHMENT, hours=HOURS_LINE))
    await slot_answer(lead)

    assert (await draft_service.build_draft(lead.id)).ok
    html = r2.puts[-1]["Body"].decode()
    info = html[html.index('id="info"'):]
    info = info[:info.index("</section>")]

    assert ">Пн-Пт</th>" in info and ">9:00-19:00<" in info
    # строк в таблице столько, сколько дней в расписании, а не сколько букв
    assert info.count("<tr") == 2


def test_a_comma_between_two_days_without_time_keeps_the_line_whole():
    """«Сб, Нд: вихідний» — один выходной на два дня, а не день без времени."""
    assert draft_service._clean_hours("Сб, Нд: вихідний") == ["Сб, Нд: вихідний"]


def test_a_lunch_break_stays_inside_the_day_it_belongs_to():
    """Второй кусок обеда собственного дня не называет — резать его нечем."""
    assert draft_service._clean_hours("Пн-Пт: 09:00-13:00, 14:00-18:00") == [
        "Пн-Пт: 09:00-13:00, 14:00-18:00"]


def test_a_semicolon_splits_the_line_whatever_stands_around_it():
    """Точка с запятой в расписании только одно и значит — конец дня."""
    assert draft_service._clean_hours("Пн-Пт: 9:00-19:00; Сб: вихідний") == [
        "Пн-Пт: 9:00-19:00", "Сб: вихідний"]


# --- готовые тексты слотов вместо модели (дельта 27.08) -----------------------

async def _ready(lead, texts: dict):
    async with Session() as s, s.begin():
        fresh = await s.get(Lead, lead.id)
        fresh.enrichment = dict(fresh.enrichment or {},
                                **{draft_service.DEV_TEXTS_KEY: texts})


def _texts(specs, drop=()) -> dict:
    return {spec["slot"]: SLOT_LINES.get(spec["kind"], "Рядок")
            for spec in specs if spec["slot"] not in drop}


async def test_ready_texts_build_the_draft_without_the_model(slot_answer,
                                                             draft_lead, r2):
    lead = await draft_lead()
    state = await slot_answer(lead)
    await _ready(lead, _texts(state.specs))

    result = await draft_service.build_draft(lead.id)

    assert result.ok and result.preview_url, result.reason
    assert state.fake.messages.calls == []
    row = await _row(lead.id)
    assert row.recipe_json["empty_slots"] == []
    assert row.slots_json == _texts(state.specs)
    # служебный ключ обогащения в профиль не просачивается
    assert draft_service.DEV_TEXTS_KEY not in row.recipe_json["profile"]
    async with Session() as s:
        spent = list(await s.scalars(
            select(CostLedger).where(CostLedger.lead_id == lead.id)
        ))
    assert spent == []


async def test_a_missing_ready_text_drops_its_section(slot_answer, draft_lead,
                                                      r2):
    lead = await draft_lead()
    state = await slot_answer(lead)
    lost = next(spec["slot"] for spec in state.specs
                if spec["slot"].startswith("about_note."))
    await _ready(lead, _texts(state.specs, drop=(lost,)))

    result = await draft_service.build_draft(lead.id)

    # пропущенный ключ — то же, что пустой ответ модели: работает лестница
    assert result.ok, result.reason
    row = await _row(lead.id)
    assert row.recipe_json["empty_slots"] == [lost]
    assert "about" in row.recipe_json["dropped_sections"]
    assert "about_note" not in row.section_variants


async def test_a_ready_text_over_the_limit_is_treated_as_empty(slot_answer,
                                                               draft_lead, r2):
    lead = await draft_lead()
    state = await slot_answer(lead)
    victim = next(spec for spec in state.specs
                  if spec["slot"].startswith("about_note."))
    texts = _texts(state.specs)
    texts[victim["slot"]] = "я" * (victim["max_chars"] + 1)
    await _ready(lead, texts)

    assert (await draft_service.build_draft(lead.id)).ok
    row = await _row(lead.id)

    # молча резать текст нельзя — тот же запрет, что и для ответов модели
    assert row.recipe_json["empty_slots"] == [victim["slot"]]
    assert "about_note" not in row.section_variants


async def test_ready_blurbs_reach_every_service_of_the_page(slot_answer,
                                                            draft_lead, r2):
    lead = await draft_lead()
    state = await slot_answer(lead)
    blurbs = [spec for spec in state.specs if spec["kind"] == "service_blurb"]
    assert blurbs
    texts = _texts(state.specs)
    texts[blurbs[0]["slot"]] = "Розкажемо, з чого почати, ще до візиту."
    texts[blurbs[1]["slot"]] = ""
    await _ready(lead, texts)

    assert (await draft_service.build_draft(lead.id)).ok
    html = r2.puts[-1]["Body"].decode()

    assert "Розкажемо, з чого почати, ще до візиту." in html
    # пустой блёрб — услуга без пояснения, а не выбывшая секция и не «None»
    assert ">None<" not in html
    row = await _row(lead.id)
    assert blurbs[1]["slot"] not in row.recipe_json["empty_slots"]


async def test_a_ready_label_for_a_google_figure_reaches_the_page(slot_answer,
                                                                  draft_lead,
                                                                  r2):
    """Подпись под цифрой модель не пишет, а человек — пишет: он цифру видит."""
    lead = await draft_lead()
    state = await slot_answer(lead)
    proof = next(part for part in state.sections if part["role"] == "proof")
    label = f"{proof['variant']}.stat_label[0]"
    assert label not in {spec["slot"] for spec in state.specs}
    await _ready(lead, dict(_texts(state.specs), **{label: "Оцінка клієнтів"}))

    assert (await draft_service.build_draft(lead.id)).ok
    row = await _row(lead.id)

    assert proof["variant"] in row.section_variants
    assert row.slots_json[label] == "Оцінка клієнтів"
    assert "Оцінка клієнтів" in r2.puts[-1]["Body"].decode()


async def test_a_draft_without_blurbs_keeps_the_recipe_lines(slot_answer,
                                                             draft_lead, r2):
    """Тексты старого черновика блёрбов не знают — страница всё равно собирается."""
    lead = await draft_lead()
    state = await slot_answer(lead)
    plain = {spec["slot"]: SLOT_LINES.get(spec["kind"], "Рядок")
             for spec in state.specs if not spec.get("grouped")}
    await _ready(lead, plain)

    assert (await draft_service.build_draft(lead.id)).ok
    row = await _row(lead.id)
    html = r2.puts[-1]["Body"].decode()

    assert row.recipe_json["empty_slots"] == []
    assert row.slots_json == plain
    assert any(v.startswith("svc_") for v in row.section_variants)
    defaults = render.load_recipe(
        row.recipe_id)["free_defaults"]["uk"]["_common"]["service_blurb"]
    assert defaults[0] in html


async def test_a_ready_text_that_is_not_a_string_is_treated_as_empty(
        slot_answer, draft_lead, r2):
    lead = await draft_lead()
    state = await slot_answer(lead)
    victim = next(spec for spec in state.specs
                  if spec["slot"].startswith("about_note."))
    texts = _texts(state.specs)
    texts[victim["slot"]] = {"reason": ["агент положил сюда разбор, а не текст"]}
    await _ready(lead, texts)

    assert (await draft_service.build_draft(lead.id)).ok
    row = await _row(lead.id)

    # str() от словаря — это питоновский repr, и уехал бы он на страницу клиента
    assert row.recipe_json["empty_slots"] == [victim["slot"]]
    assert "about_note" not in row.section_variants
    assert "reason" not in json.dumps(row.slots_json, ensure_ascii=False)


def test_every_section_of_the_library_has_a_summary():
    """Вариант без формулы — секция, о которой письмо промолчит."""
    assert set(draft_service.SUMMARY_PARTS) == set(render.load_library())
    for variant, parts in draft_service.SUMMARY_PARTS.items():
        assert set(parts) == {"uk", "en"}, variant
        for lang, text in parts.items():
            assert text and not any(ch.isdigit() for ch in text), variant


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
