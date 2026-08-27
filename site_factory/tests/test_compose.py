"""Гейты, скоринг и лестница деградации (§3 шаги 3-4 и пять ступеней)."""
import random

import pytest

from site_factory.engine import gates
from site_factory.engine.profile import Profile
from site_factory.engine.render import load_library, render
from site_factory.engine.score import Score, choose

from .conftest import LAWYER_POOR, LAWYER_RICH


def role_record(trace, role):
    return next(record for record in trace["roles"] if record["role"] == role)


def rejected_reasons(record, variant):
    rejection = next(r for r in record["rejected"] if r["variant"] == variant)
    return rejection["reasons"]


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
    assert ladder_top == {"hero_split_map", "hero_bg_photo", "hero_photo_left"}
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
    assert too_long not in (html or "")


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
