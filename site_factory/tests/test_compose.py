"""Гейты, скоринг и лестница деградации (§3 шаги 3-4 и пять ступеней)."""
import random

import pytest

from site_factory.engine import gates, slots
from site_factory.engine.profile import Profile
from site_factory.engine.render import load_library, render
from site_factory.engine.score import Score, choose

from .conftest import GENERIC_RICH, LAWYER_POOR, LAWYER_RICH

# Рейтинг со страницы лида: так его отдаёт скрейп разметки (site_scrape.rating).
SCRAPED_RATING = {"value": 4.8, "count": 120, "source": "jsonld"}


def role_record(trace, role):
    return next(record for record in trace["roles"] if record["role"] == role)


def rejected_reasons(record, variant):
    rejection = next(r for r in record["rejected"] if r["variant"] == variant)
    return rejection["reasons"]


def section_of(html, role):
    """Кусок страницы одной роли: id секции — это её роль (engine/compose)."""
    tail = html[html.index(f'id="{role}"'):]
    end = tail.find("</section>")
    return tail[:end if end > 0 else len(tail)]


def _without_card_rating(**extra):
    """Профиль, у которого оценки в карточке лида нет вовсе."""
    data = {key: value for key, value in GENERIC_RICH.items()
            if key not in ("google_rating", "review_count")}
    return Profile.from_dict(dict(data, **extra))


def test_gate_rejects_unknown_field():
    """unknown != false: признак, которого не спрашивали, requires не выполняет."""
    profile = Profile.from_dict(LAWYER_POOR)
    verdict = gates.check(load_library()["hero_photo_left"], profile)
    assert not verdict.ok
    assert {reason.field for reason in verdict.reasons} >= {"photo_count"}
    assert all(reason.kind == gates.UNKNOWN_FIELD for reason in verdict.reasons
               if reason.field == "photo_count")


def test_gate_rejects_known_mismatch(generic_light):
    verdict = gates.check(load_library()["svc_list_icons"], generic_light)
    assert not verdict.ok
    assert verdict.reasons[0].kind == gates.MISMATCH


def test_profile_without_services_gets_no_services_section(generic_light):
    _, trace = render(generic_light)
    assert not [variant for variant in trace["sections"]
                if variant.startswith("svc_")]
    services = role_record(trace, "services")
    assert {r["field"] for r in rejected_reasons(services, "svc_list_icons")} == \
        {"service_count"}


def test_downgrade_inside_role(lawyer_light):
    """Верхние ступени лестницы отсеяны гейтом — роль берёт нижнюю."""
    _, trace = render(lawyer_light)
    hero = role_record(trace, "hero")
    ladder_top = {r["variant"] for r in hero["rejected"]}
    assert ladder_top == {"hero_split_map", "hero_bg_photo", "hero_split_2",
                          "hero_photo_left"}
    assert hero["chosen"] == "hero_type_only"
    assert {r["kind"] for r in rejected_reasons(hero, "hero_split_map")} == \
        {gates.MISSING_IMAGE}


def test_header_falls_back_to_the_wordmark(lawyer_rich, brand_shop):
    """Логотипа нет — шапка остаётся: название набирается display-шрифтом."""
    _, plain = render(lawyer_rich)
    assert role_record(plain, "header")["chosen"] == "header_wordmark"
    _, branded = render(brand_shop)
    assert role_record(branded, "header")["chosen"] == "header_logo"
    assert {(r["field"], r["kind"]) for r in
            rejected_reasons(role_record(plain, "header"), "header_logo")} == \
        {("has_logo", gates.MISMATCH), ("images", gates.MISSING_IMAGE)}


def test_products_grid_beats_the_list_when_there_are_pictures(products_lead):
    """Картинка стоит perf_cost — вес товарной сетки обязан его перекрывать."""
    _, trace = render(products_lead)
    products = role_record(trace, "products")
    assert products["chosen"] == "products_grid"
    totals = {c["variant"]: c["total"] for c in products["candidates"]}
    assert totals["products_grid"] > totals["products_list"]


def test_products_without_pictures_fall_to_the_list(generic_light):
    _, trace = render(generic_light)
    products = role_record(trace, "products")
    assert products["chosen"] == "products_list"
    assert {r["kind"] for r in rejected_reasons(products, "products_grid")} == \
        {gates.MISMATCH}


def test_optional_role_is_dropped(lawyer_light):
    _, trace = render(lawyer_light)
    assert role_record(trace, "proof")["status"] == "dropped"
    assert "proof_stats_bar" not in trace["sections"]


def test_role_substitution(generic_light):
    _, trace = render(generic_light)
    services = role_record(trace, "services")
    assert services["status"] == "substituted"
    assert services["substituted_by"] == "proof"
    assert role_record(trace, "proof")["status"] == "used_earlier"
    assert trace["sections"].count("proof_stats_bar") == 1


def test_a_page_that_opens_with_a_frame_spends_its_dark_accent_there(brand_shop):
    """Один тёмный акцент на страницу: либо кадр первого экрана, либо секция."""
    html, trace = render(brand_shop)

    assert trace["sections"][1] == "hero_bg_photo"
    assert trace["tone"] is None
    assert 'data-tone="contrast"' not in html


def test_a_page_that_opens_with_text_keeps_its_contrast_section(lawyer_light):
    """Первый экран без кадра — тёмной остаётся первая роль из CONTRAST_ROLES."""
    html, trace = render(lawyer_light)

    assert trace["sections"][1] == "hero_type_only"
    assert trace["tone"] == "about"
    assert html.count('data-tone="contrast"') == 1


def test_the_frames_of_the_split_hero_spend_the_accent_too(
        shop_without_a_named_hero):
    """Первый экран из двух полотен — тот же кадр во всю ширину."""
    html, trace = render(shop_without_a_named_hero)

    assert trace["sections"][1] == "hero_split_2"
    assert trace["tone"] is None
    assert 'data-tone="contrast"' not in html


def test_a_scraped_rating_keeps_the_proof_section_alive():
    """Оценки в карточке нет вовсе — полоса показателей живёт на рейтинге сайта."""
    profile = _without_card_rating(rating=SCRAPED_RATING)
    html, trace = render(profile)

    assert profile.feature("has_rating").value
    assert "proof_stats_bar" in trace["sections"]
    assert slots.FACT_SOURCES["rating_value"].build(profile, "uk") == "4,8"
    assert slots.FACT_SOURCES["rating_value"].build(profile, "en") == "4.8"
    assert slots.FACT_SOURCES["rating_count"].build(profile, "uk") == "120"
    assert "4,8" in section_of(html, "proof")


def test_without_any_rating_the_proof_section_leaves():
    profile = _without_card_rating()
    _, trace = render(profile)
    assert "proof_stats_bar" not in trace["sections"]
    assert role_record(trace, "proof")["status"] == "dropped"


def test_a_broken_rating_never_reaches_the_profile():
    """Оценка вне шкалы и ноль отзывов — сломанный разбор, а не «нет рейтинга»."""
    for broken in ({"value": 7, "count": 30}, {"value": 4.8, "count": 0},
                   {"count": 12}, {"value": 4.8}, "4.8", None,
                   # bool числом не считается: float(True) дал бы «оценку 1,0».
                   {"value": True, "count": 5}, {"value": 4.8, "count": True}):
        profile = _without_card_rating(rating=broken)
        assert not profile.rating.known, broken
        assert not profile.feature("has_rating").known, broken


def test_the_scraped_rating_wins_over_the_card():
    """Одна страница — одна оценка: и в полосе, и в JSON-LD она с сайта лида."""
    profile = Profile.from_dict(dict(GENERIC_RICH, rating=SCRAPED_RATING))
    html, _ = render(profile)
    assert profile.proof_stats() == [{"key": "rating", "value": 4.8},
                                     {"key": "reviews", "value": 120}]
    assert '"ratingValue": 4.8' in html and '"reviewCount": 120' in html
    assert str(GENERIC_RICH["google_rating"]) not in html


def test_the_note_names_the_site_when_the_rating_came_from_it():
    """Оценку снял скрейп — подпись про профиль Google была бы враньём."""
    profile = _without_card_rating(rating=SCRAPED_RATING)
    proof = section_of(render(profile)[0], "proof")
    assert "Дані з сайту компанії." in proof
    assert "Google" not in proof


def test_the_note_names_google_when_the_figures_came_from_the_card():
    proof = section_of(render(Profile.from_dict(GENERIC_RICH))[0], "proof")
    assert "Дані з профілю Google Business." in proof


def test_an_unknown_source_leaves_the_figures_without_a_note():
    """Источник не из таблицы — цифры остаются, подписи нет: врать нечем."""
    profile = _without_card_rating(
        rating=dict(SCRAPED_RATING, source="tripadvisor"))
    html, trace = render(profile)
    proof = section_of(html, "proof")
    assert "proof_stats_bar" in trace["sections"]
    assert "4,8" in proof and "Дані" not in proof


def test_needs_enrichment_lists_what_to_ask(lawyer_poor):
    html, trace = render(lawyer_poor)
    needed = trace["needs_enrichment"]
    assert html is None
    assert any("перечень услуг" in hint for hint in needed)
    assert any("телефон" in hint for hint in needed)
    assert any("фото" in hint for hint in needed)
    assert all(hint.strip() for hint in needed)


def test_long_fact_is_not_truncated():
    """Слот не влезает в max_chars — вариант выбывает, а не режет текст."""
    too_long = ("Супровід перевірок контролюючих органів та оскарження їхніх "
                "рішень в судах усіх інстанцій")
    profile = Profile.from_dict(dict(
        LAWYER_RICH, services=[too_long] + LAWYER_RICH["services"][:2]))
    html, trace = render(profile)
    services = role_record(trace, "services")
    kinds = {r["kind"] for r in rejected_reasons(services, "svc_cards_3")}
    assert kinds == {gates.TOO_LONG}
    assert too_long not in html[html.index("<body"):]
    # в описании страницы услуга стоит целиком: там свой потолок, и он режет
    # список услуг, а не саму строку (render.page_description)
    assert html.count(too_long) == 2      # description и og:description


def test_tie_is_broken_by_seed():
    tied = [Score("a", 5.0, 1.0, 1.0, 1.0, 1.0), Score("b", 5.0, 1.0, 1.0, 1.0, 1.0)]
    winners = {choose(tied, random.Random(seed)).variant for seed in range(20)}
    assert winners == {"a", "b"}


def test_no_tie_needs_no_dice():
    scores = [Score("a", 5.0, 1.0, 1.0, 1.0, 1.0), Score("b", 4.0, 1.0, 1.0, 1.0, 1.0)]
    assert choose(scores, random.Random(1)).variant == "a"
    assert choose(scores, random.Random(2)).variant == "a"


@pytest.mark.parametrize("condition, value, expected", [
    (">=3", 6, True), (">=3", 2, False),
    ("3..6", 4, True), ("3..6", 7, False),
    ("0..2", 0, True), ("3..", 9, True), ("..2", 3, False),
    (True, 1, True), (False, 0, True), (True, 0, False),
    (">=medium", "long", True), (">=medium", "short", False),
    (["lawyer", "dental"], "lawyer", True), (["lawyer"], "generic", False),
])
def test_condition_language(condition, value, expected):
    from site_factory.engine.profile import known
    assert gates.satisfies(condition, known(value)) is expected
