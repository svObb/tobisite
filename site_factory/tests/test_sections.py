"""Разметка секций: что именно попало на страницу и чего на ней быть не может.

Проверяется собранная страница фикстуры, а не шаблон в отрыве от данных:
контракт секции имеет смысл только вместе с профилем, который его закрыл.
"""
import re

from site_factory.engine.profile import Profile
from site_factory.engine.render import (ROOT, environment, load_tokens, render,
                                        resolve_preset)

from . import smoke_render
from .conftest import BRAND_SHOP

IMG_SRC = re.compile(r'<img[^>]+\bsrc="([^"]+)"')

# Потолок медиа страницы: логотип и семь картинок скрейпа (site_scrape
# складывает в стейджинг не больше восьми файлов).
MEDIA_BUDGET = 8


def images_of(html):
    return IMG_SRC.findall(html)


def test_header_stands_before_the_content(buildable_profile):
    """Шапка вне <main>: она не часть содержимого и h1 не несёт."""
    html, _ = render(buildable_profile)
    assert html.index("<header") < html.index('<main id="main">')
    assert html.count("<header") == 1
    assert html.count("<h1") == 1
    assert html.index('<main id="main">') < html.index("<h1")


def test_sections_have_no_side_container(buildable_profile):
    """Железное правило: узкой колонки посреди пустой страницы не бывает.

    Ширину держит только боковой отступ px-gutter, поэтому центрирующей
    обёртки в разметке быть не может ни одной.
    """
    html, _ = render(buildable_profile)
    assert "mx-auto" not in html
    assert not re.search(r'max-w-(screen-|\d)', html)


def test_nothing_wears_a_frame_and_a_shadow_at_once(buildable_profile):
    """Границу коробки рисует что-то одно: рамка или тень, но не обе сразу.

    Тень на странице бывает только у .card, и рамку ей на raised гасит
    правило [data-elev] в бандле, — значит в самой разметке пары быть не может.
    """
    html, _ = render(buildable_profile)
    for classes in re.findall(r'class="([^"]*)"', html):
        names = classes.split()
        assert not (any(n.startswith("shadow-") for n in names)
                    and any(n.startswith("border") for n in names)), classes


def test_page_stays_within_the_media_budget(buildable_profile):
    html, _ = render(buildable_profile)
    assert len(set(images_of(html))) <= MEDIA_BUDGET


def test_full_page_uses_every_picture_once(brand_shop):
    """Лид со всем набором картинок: каждая на своём месте и ровно один раз."""
    html, trace = render(brand_shop)
    assert trace["sections"][:3] == ["header_logo", "hero_bg_photo",
                                     "products_grid"]
    used = images_of(html)
    assert len(used) == len(set(used))
    assert set(used) == {"/img/logo.webp", "/img/hero_bg.webp",
                         "/img/photo-2.webp", "/img/photo-3.webp",
                         "/img/photo-4.webp", "/img/product-1.webp",
                         "/img/product-2.webp", "/img/product-3.webp"}


def test_background_photo_is_a_background(brand_shop):
    """КРАСНОЕ правило: большая картинка — фон секции с параллаксом, не полоса."""
    html, _ = render(brand_shop)
    hero = section_html(html, "hero")
    assert "data-parallax" in hero
    assert "data-parallax-layer" in hero
    assert 'class="scrim' in hero
    assert re.search(r'<img[^>]+alt="" aria-hidden="true"', hero)


def test_products_grid_drops_the_items_without_a_picture(brand_shop):
    """group_filter: пустых рамок в сетке не бывает, товар без фото не берём."""
    html, _ = render(brand_shop)
    products = section_html(html, "products")
    assert products.count("<li") == 3
    assert "Гальмівні колодки" in products
    assert "Масляний фільтр" not in products
    assert "Акумулятор" not in products


def test_prices_are_printed_exactly_as_the_business_wrote_them(generic_light):
    html, _ = render(generic_light)
    products = section_html(html, "products")
    assert "2,40 €" in products
    assert "Sourdough loaf" in products
    # Цены у последней позиции нет — и придумать её нечем.
    assert "Cinnamon bun" in products
    assert products.count("text-accent-ink") == 3


def test_gallery_is_edge_to_edge_and_silent(brand_shop):
    html, _ = render(brand_shop)
    gallery = section_html(html, "gallery")
    assert "px-gutter" not in gallery
    assert images_of(gallery) == ["/img/photo-2.webp", "/img/photo-3.webp",
                                  "/img/photo-4.webp"]
    assert gallery.count('alt="" aria-hidden="true"') == 3


def test_hours_become_a_table(brand_shop):
    html, _ = render(brand_shop)
    info = section_html(html, "info")
    assert '<th scope="row"' in info
    assert "Пн–Пт" in info and "08:00–20:00" in info
    assert brand_shop.address.value in info


def test_hours_line_that_does_not_split_keeps_the_cell_empty():
    """Расписание пишет сам бизнес: не режется по «: » — время пустое."""
    profile = Profile.from_dict(dict(BRAND_SHOP, hours=["Цілодобово"]))
    html, _ = render(profile)
    info = section_html(html, "info")
    assert "Цілодобово</th>" in info
    assert re.search(r'<td[^>]*></td>', info)


def test_about_note_anchors_the_free_text_in_facts(brand_shop):
    html, _ = render(brand_shop)
    about = section_html(html, "about")
    assert brand_shop.name.value in about
    assert brand_shop.address.value in about
    assert not any(char.isdigit() for char in _text_of(about, "text-lede"))


def test_smoke_pages_render_on_every_preset():
    """Смоук держит те варианты, до которых фикстуры движка не доходят.

    Данные в нём написаны руками, поэтому расхождение с контрактом ловится
    только сборкой — пусть ловится здесь, а не глазами при просмотре вёрстки.
    """
    tokens = load_tokens()
    layout = environment(ROOT).get_template("base/layout.html.j2")
    for preset in tokens["presets"]:
        for suffix, sections in smoke_render.PAGES.items():
            html = layout.render(preset=resolve_preset(preset, tokens),
                                 site=smoke_render.site_for(sections),
                                 facts=smoke_render.FACTS, sections=sections)
            assert "{{" not in html and "{%" not in html, preset["id"] + suffix
            assert html.count("<h1") == 1, preset["id"] + suffix


def section_html(html, role):
    """Кусок страницы одной роли: id секции — это её роль (engine/compose)."""
    start = html.index(f'id="{role}"')
    tail = html[start:]
    end = tail.find("</section>")
    return tail[:end if end > 0 else len(tail)]


def _text_of(html, marker):
    match = re.search(rf'class="[^"]*{marker}[^"]*"[^>]*>([^<]+)<', html)
    return match.group(1) if match else ""
