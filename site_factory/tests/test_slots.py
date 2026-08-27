"""Слоты групп: товар «название + цена + фото» и часы «день + время».

Контракты здесь синтетические — секций этих ролей ещё нет, а правила сборки
группы уже есть, и проверять их надо до того, как на них ляжет вёрстка.
"""
import pytest

from site_factory.engine import slots
from site_factory.engine.gates import FACT_MISSING, TOO_FEW, TOO_LONG
from site_factory.engine.profile import Profile

from .conftest import LAWYER_RICH, PRODUCTS, PRODUCTS_LEAD

BLURB = "Заготовка рецепта"

RECIPE = {
    "id": "synthetic",
    "free_defaults": {
        "uk": {"_common": {"section_title": "Товари", "hours_title": "Графік",
                           "service_blurb": BLURB}},
    },
}

# Список товаров: картинка не нужна, цена бывает не у каждого.
PRODUCTS_LIST = {
    "id": "products_list",
    "role": "products",
    "slots": [
        {"name": "section_title", "type": "free", "max_chars": 42},
        {"name": "product_name", "type": "fact", "group": "products",
         "repeat": "3..6", "max_chars": 34},
        {"name": "product_price", "type": "fact", "source": "item",
         "group": "products", "repeat": "same_as_group", "max_chars": 20,
         "optional": True},
    ],
}

# Товарная сетка: без картинки товару в ней делать нечего.
PRODUCTS_GRID = {
    "id": "products_grid",
    "role": "products",
    "slots": [
        {"name": "section_title", "type": "free", "max_chars": 42},
        {"name": "product_name", "type": "fact", "group": "products",
         "repeat": "3..6", "max_chars": 34, "group_filter": "has_image"},
        {"name": "product_image", "type": "fact", "source": "item",
         "group": "products", "repeat": "same_as_group"},
        {"name": "product_price", "type": "fact", "source": "item",
         "group": "products", "repeat": "same_as_group", "max_chars": 20,
         "optional": True},
    ],
}

HOURS_CARD = {
    "id": "info_hours_card",
    "role": "info",
    "slots": [
        {"name": "hours_title", "type": "free", "max_chars": 20},
        {"name": "hour_day", "type": "fact", "group": "hours",
         "repeat": "1..7", "max_chars": 24},
        {"name": "hour_time", "type": "fact", "source": "item",
         "group": "hours", "repeat": "same_as_group", "max_chars": 24,
         "optional": True},
    ],
}


# Карточки услуг: у каждой позиции свой blurb, и пишет его модель.
SERVICE_CARDS = {
    "id": "svc_cards_3",
    "role": "services",
    "slots": [
        {"name": "section_title", "type": "free", "max_chars": 42},
        {"name": "service_name", "type": "fact", "group": "services",
         "repeat": "3..6", "max_chars": 60},
        {"name": "service_blurb", "type": "free", "group": "services",
         "repeat": "same_as_group", "max_chars": 110},
    ],
}


def build(contract, data):
    return slots.build(contract, Profile.from_dict(data), RECIPE)


def section(contract, data) -> dict:
    """Секция в той форме, в которой её отдаёт compose и ждёт apply_free_texts."""
    filled = build(contract, data)
    return {"id": contract["role"], "role": contract["role"],
            "variant": contract["id"], "contract": contract,
            "slots": dict(filled.slots), "images": filled.images}


def test_group_reads_price_from_the_driver_item(products_lead):
    filled = slots.build(PRODUCTS_LIST, products_lead, RECIPE)
    assert filled.ok
    rows = filled.slots["products"]
    assert [row["name"] for row in rows] == [item["name"] for item in PRODUCTS]
    assert rows[0]["price"] == "від 890 грн"
    assert rows[4]["price"] is None      # цены нет, слот необязателен


def test_missing_required_item_key_drops_the_variant():
    slots_ = [dict(spec) for spec in PRODUCTS_LIST["slots"]]
    slots_[-1].pop("optional")
    filled = build(dict(PRODUCTS_LIST, slots=slots_), PRODUCTS_LEAD)
    assert not filled.ok
    assert {reason.kind for reason in filled.reasons} == {FACT_MISSING}


def test_group_filter_keeps_only_items_with_images():
    filled = build(PRODUCTS_GRID, PRODUCTS_LEAD)
    assert filled.ok
    rows = filled.slots["products"]
    assert [row["name"] for row in rows] == [item["name"] for item in PRODUCTS[:3]]
    assert rows[0]["image"] == PRODUCTS[0]["image"]


def test_group_filter_can_starve_the_group():
    """Картинок меньше, чем требует repeat, — роль уходит на ступень ниже."""
    data = dict(PRODUCTS_LEAD, products=PRODUCTS[:1] + PRODUCTS[3:])
    filled = build(PRODUCTS_GRID, data)
    assert not filled.ok
    assert {reason.kind for reason in filled.reasons} == {TOO_FEW}


def test_group_filter_leaving_nothing_is_still_too_few():
    """Товары есть, картинок нет: это «мало», а не «данных не спрашивали»."""
    filled = build(PRODUCTS_GRID, dict(PRODUCTS_LEAD, products=PRODUCTS[3:]))
    assert not filled.ok
    assert {reason.kind for reason in filled.reasons} == {TOO_FEW}


def test_unknown_group_filter_is_a_contract_error():
    slots_ = [dict(spec, group_filter="has_unicorn")
              if spec["name"] == "product_name" else spec
              for spec in PRODUCTS_GRID["slots"]]
    with pytest.raises(ValueError, match="has_unicorn"):
        build(dict(PRODUCTS_GRID, slots=slots_), PRODUCTS_LEAD)


def test_image_dict_goes_past_max_chars():
    """{src, width, height} длиннее любого лимита — считать в нём нечего."""
    slots_ = [dict(spec, max_chars=8) if spec["name"] == "product_image" else spec
              for spec in PRODUCTS_GRID["slots"]]
    filled = build(dict(PRODUCTS_GRID, slots=slots_), PRODUCTS_LEAD)
    assert filled.ok
    assert filled.slots["products"][0]["image"] == PRODUCTS[0]["image"]


def test_long_product_name_still_drops_the_variant():
    long_name = "Комплект зчеплення для важкої комерційної техніки"
    data = dict(PRODUCTS_LEAD,
                products=[dict(PRODUCTS[0], name=long_name)] + PRODUCTS[1:])
    filled = build(PRODUCTS_LIST, data)
    assert {reason.kind for reason in filled.reasons} == {TOO_LONG}


def test_products_without_a_name_are_skipped():
    data = dict(PRODUCTS_LEAD, products=PRODUCTS + [{"name": "  "}, {}])
    filled = build(PRODUCTS_LIST, data)
    assert [row["name"] for row in filled.slots["products"]] == \
        [item["name"] for item in PRODUCTS]


def test_products_not_asked_is_not_products_absent():
    filled = build(PRODUCTS_LIST, LAWYER_RICH)
    assert not filled.ok
    assert {reason.field for reason in filled.reasons} == {"products"}
    assert {reason.kind for reason in filled.reasons} == {FACT_MISSING}


def test_hours_split_into_day_and_time(products_lead):
    filled = slots.build(HOURS_CARD, products_lead, RECIPE)
    assert filled.ok
    assert filled.slots["hours"] == [{"day": "Пн–Сб", "time": "08:00–19:00"}]


def test_hours_line_without_separator_stays_whole():
    data = dict(PRODUCTS_LEAD, hours=["Цілодобово", "Нд: вихідний"])
    filled = build(HOURS_CARD, data)
    assert filled.slots["hours"] == [{"day": "Цілодобово", "time": None},
                                     {"day": "Нд", "time": "вихідний"}]


def test_hours_split_takes_only_the_first_separator():
    data = dict(PRODUCTS_LEAD, hours=["Пн–Пт: 09:00–13:00, 14:00–18:00"])
    filled = build(HOURS_CARD, data)
    assert filled.slots["hours"] == [{"day": "Пн–Пт",
                                      "time": "09:00–13:00, 14:00–18:00"}]


def test_hours_stay_a_line_where_the_contract_asks_for_a_line(products_lead):
    """hour_day не отменяет старый слот hours: строкой в футере он тот же."""
    contract = {"id": "footer_nap", "role": "footer",
                "slots": [{"name": "hours", "type": "fact", "max_chars": 90}]}
    filled = slots.build(contract, products_lead, RECIPE)
    assert filled.slots["hours"] == "Пн–Сб: 08:00–19:00"


def test_hours_written_as_one_string_stay_one_row():
    """Ручная строка расписания — строка таблицы, а не таблица из букв."""
    filled = build(HOURS_CARD, dict(PRODUCTS_LEAD, hours="Пн-Пт 9:00-19:00"))
    assert filled.ok
    assert filled.slots["hours"] == [{"day": "Пн-Пт", "time": "9:00-19:00"}]


def test_hours_split_falls_back_to_the_space_before_the_time():
    """Двоеточия с пробелом нет — режем там, где начинается время."""
    data = dict(PRODUCTS_LEAD, hours=["Пн-Пт 9:00-19:00", "Сб 10.00-17.00",
                                      "Цілодобово"])
    filled = build(HOURS_CARD, data)
    assert filled.slots["hours"] == [{"day": "Пн-Пт", "time": "9:00-19:00"},
                                     {"day": "Сб", "time": "10.00-17.00"},
                                     {"day": "Цілодобово", "time": None}]


def test_hours_string_is_one_item_where_the_contract_repeats():
    """footer_nap просит список строк: строка приходит одним элементом."""
    contract = {"id": "footer_nap", "role": "footer",
                "slots": [{"name": "hours", "type": "fact", "repeat": "1..7",
                           "max_chars": 60}]}
    filled = build(contract, dict(PRODUCTS_LEAD, hours="Пн-Пт 9:00-19:00"))
    assert filled.slots["hours"] == ["Пн-Пт 9:00-19:00"]


def test_group_text_replaces_only_its_own_element():
    part = section(SERVICE_CARDS, LAWYER_RICH)
    written = "Ведемо справу в усіх інстанціях."

    ok = slots.apply_free_texts(part, {
        "svc_cards_3.section_title": "Напрями роботи",
        "svc_cards_3.service_blurb[1]": written,
    })

    assert ok
    rows = part["slots"]["services"]
    assert rows[1]["blurb"] == written
    # ключа нет — заготовка рецепта на месте: старые тексты страницу не валят
    assert rows[0]["blurb"] == BLURB


def test_empty_group_text_leaves_the_element_without_a_blurb():
    part = section(SERVICE_CARDS, LAWYER_RICH)

    ok = slots.apply_free_texts(part, {
        "svc_cards_3.section_title": "Напрями роботи",
        "svc_cards_3.service_blurb[0]": "   ",
    })

    # блёрба нет, а карточка есть: секцию пустой элемент группы не выводит
    assert ok
    assert part["slots"]["services"][0]["blurb"] is None


def test_group_text_over_the_limit_is_dropped_and_not_cut():
    part = section(SERVICE_CARDS, LAWYER_RICH)

    ok = slots.apply_free_texts(part, {
        "svc_cards_3.section_title": "Напрями роботи",
        "svc_cards_3.service_blurb[2]": "я" * 111,
    })

    assert ok
    assert part["slots"]["services"][2]["blurb"] is None


def test_group_free_specs_names_the_repeating_free_slots():
    assert [spec["name"] for spec in slots.group_free_specs(SERVICE_CARDS)] == \
        ["service_blurb"]
    assert slots.free_specs(SERVICE_CARDS) == [SERVICE_CARDS["slots"][0]]


def test_group_needs_exactly_one_driver():
    """Два источника длины списка — два разных числа элементов в одной группе."""
    slots_ = PRODUCTS_LIST["slots"] + [
        {"name": "product_extra", "type": "fact", "group": "products",
         "repeat": "3..6"}]
    with pytest.raises(ValueError, match="ровно один fact-слот"):
        build(dict(PRODUCTS_LIST, slots=slots_), PRODUCTS_LEAD)
