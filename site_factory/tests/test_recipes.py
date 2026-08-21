"""Рецепты и контракты обязаны сходиться друг с другом — до всякого рендера."""
import pathlib
import re

import pytest

from site_factory.engine.render import ROOT, load_library, load_recipe

RECIPES = sorted(path.stem for path in (ROOT / "recipes").glob("*.yaml"))
LANGS = ("uk", "en")
COMMENT = re.compile(r"\{#.*?#\}", re.S)
CYRILLIC = re.compile(r"[Ѐ-ӿ]")


@pytest.fixture(params=RECIPES)
def recipe(request):
    return load_recipe(request.param)


def test_all_four_recipes_exist():
    assert RECIPES == ["generic_light", "generic_rich", "lawyer_light", "lawyer_rich"]


def test_ladder_variants_exist_and_match_role(recipe):
    library = load_library()
    for role, ladder in recipe["downgrade_ladder"].items():
        assert ladder, f"{recipe['id']}: пустая лестница роли {role}"
        for variant in ladder:
            assert library[variant]["role"] == role


def test_affinity_falls_along_the_ladder(recipe):
    """Порядок лестницы и веса — одно решение, записанное дважды (compose.py)."""
    affinity = recipe["niche_affinity"]
    for role, ladder in recipe["downgrade_ladder"].items():
        weights = [affinity[variant] for variant in ladder]
        assert weights == sorted(weights, reverse=True), f"{recipe['id']}: {role}"


def test_roles_and_thresholds_agree(recipe):
    order = recipe["roles_order"]
    assert set(recipe["downgrade_ladder"]) == set(order)
    assert set(recipe.get("optional_roles") or []) <= set(order)
    assert 0 < recipe["min_sections"] <= len(order)
    for role, substitutes in (recipe.get("role_substitutes") or {}).items():
        assert role in order
        assert set(substitutes) <= set(order)


def test_free_defaults_cover_both_languages(recipe):
    for lang in LANGS:
        page = recipe["free_defaults"][lang]["_page"]
        assert page["skip_to_content"] and page["description"]
        assert recipe["free_defaults"][lang]["_common"]


def test_free_defaults_have_no_numbers(recipe):
    """Цифра в заготовке — это выдуманный показатель: они приходят из профиля."""
    for text in _texts(recipe["free_defaults"]):
        assert not any(char.isdigit() for char in text), text


def test_templates_carry_no_cyrillic():
    """Текст живёт в слотах: в разметке .j2 кириллицы нет ни буквы.

    Комментарии {# ... #} не в счёт — они пишутся для нас и в выдачу не идут.
    """
    for path in sorted((ROOT / "sections").glob("*/*.j2")) + \
            sorted((ROOT / "base").glob("*.j2")):
        markup = COMMENT.sub("", pathlib.Path(path).read_text(encoding="utf-8"))
        assert not CYRILLIC.search(markup), f"{path.name}: кириллица в разметке"


def _texts(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _texts(value)
    elif isinstance(node, list):
        for value in node:
            yield from _texts(value)
