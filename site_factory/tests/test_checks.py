"""Автопроверки: зелено на собранных страницах, красно на подсунутом браке."""
import re

import pytest

from site_factory.engine.checks import a11y, form_e2e, nap, placeholders, run_all, scroll
from site_factory.engine.render import load_tokens, palette_for, render


@pytest.fixture
def page(lawyer_rich):
    html, _ = render(lawyer_rich)
    return html


def test_every_draft_passes_every_check(buildable_profile):
    """Контраст считается по палитре страницы, а не по палитре пресета.

    Они расходятся у лида с бренд-цветами — и проверять надо ту, что в HTML.
    """
    html, _ = render(buildable_profile)
    assert html is not None
    assert run_all(html, buildable_profile, palette_for(buildable_profile)) == {}


@pytest.mark.parametrize("preset_id", [preset["id"]
                                       for preset in load_tokens()["presets"]])
def test_preset_passes_contrast(preset_id):
    """Каждый пресет библиотеки поимённо: брак одного не прячется за остальными."""
    preset = next(preset for preset in load_tokens()["presets"]
                  if preset["id"] == preset_id)
    assert a11y.contrast_problems(preset["palette"]) == []


def test_contrast_catches_a_bad_pair():
    bad = {"paper": "#ffffff", "ink": "#111111", "accent": "#cccccc",
           "accent_ink": "#cccccc", "accent_on": "#ffffff"}
    problems = a11y.contrast_problems(bad)
    assert any("accent_ink/paper" in problem for problem in problems)


def test_known_contrast_values():
    assert round(a11y.ratio((0.0, 0.0, 0.0), (1.0, 1.0, 1.0)), 2) == 21.0
    assert round(a11y.ratio((0.0, 0.0, 0.0), (0.0, 0.0, 0.0)), 2) == 1.0


def test_lorem_is_red(page):
    assert placeholders.check(page.replace("Контакти", "Lorem ipsum dolor", 1))


def test_editor_placeholder_is_red(page):
    assert placeholders.check(page.replace("Контакти", "[НАЗВАНИЕ]", 1))


def test_dead_link_is_red(page):
    assert placeholders.check(page.replace('href="#main"', 'href="#"', 1))


def test_developer_note_is_red(page):
    assert placeholders.check(page.replace("Контакти", "TODO", 1))


def test_unrendered_jinja_is_red(page):
    assert placeholders.check(page.replace("Контакти", "{{ contacts_title }}", 1))


def test_foreign_phone_is_red(page, lawyer_rich):
    swapped = page.replace(lawyer_rich.phone.value, "+380 00 000 00 99")
    problems = nap.check(swapped, lawyer_rich)
    assert any("телефон" in problem for problem in problems)


def test_foreign_tel_href_is_red(page, lawyer_rich):
    swapped = page.replace('href="tel:+380000000001"',
                           'href="tel:+380000000099"', 1)
    assert any("чужой tel:" in problem for problem in nap.check(swapped, lawyer_rich))


def test_foreign_address_is_red(page, lawyer_rich):
    swapped = page.replace(lawyer_rich.address.value, "вул. Чужа, 99, Київ")
    assert any("адрес" in problem for problem in nap.check(swapped, lawyer_rich))


def test_second_h1_is_red(page):
    assert any("h1" in problem for problem in a11y.check(page.replace("<h2", "<h1", 1)))


def test_image_without_alt_is_red(page):
    stripped = re.sub(r'\balt="[^"]*"', "", page, count=1)
    assert any("alt" in problem for problem in a11y.check(stripped))


def test_missing_form_is_red(page):
    assert form_e2e.check(re.sub(r"<form.*?</form>", "", page, flags=re.S))


def test_form_without_honeypot_is_red(page):
    assert form_e2e.check(page.replace('name="company_website"', 'name="website"', 1))


def test_fixed_width_is_red(page):
    wide = page.replace("<main id=\"main\">",
                        "<main id=\"main\"><div style=\"width:900px\"></div>", 1)
    assert any("900" in problem for problem in scroll.check(wide))


def test_image_without_fluid_class_is_red(page):
    loose = page.replace("<main id=\"main\">",
                         "<main id=\"main\"><img src=\"/x.avif\" alt=\"x\">", 1)
    assert any("ограничивающего ширину" in problem for problem in scroll.check(loose))


def test_unclosed_container_is_red(page):
    assert any("<div>" in problem for problem in scroll.check(page + "<div>"))
