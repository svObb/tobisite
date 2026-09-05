"""Разметка секций: что именно попало на страницу и чего на ней быть не может.

Проверяется собранная страница фикстуры, а не шаблон в отрыве от данных:
контракт секции имеет смысл только вместе с профилем, который его закрыл.
"""
import re
from dataclasses import replace

from site_factory.engine import slots
from site_factory.engine.checks import run_all
from site_factory.engine.compose import CONTRAST_ROLES, compose, link_sections
from site_factory.engine.naming import split_product_name
from site_factory.engine.palette import contrast_tones
from site_factory.engine.profile import Profile, known
from site_factory.engine.render import (ROOT, environment, load_library,
                                        load_recipe, load_tokens, palette_for,
                                        recipe_id_for, render, resolve_preset,
                                        seed_for)

from . import smoke_render
from .conftest import (BRAND_SHOP, GALLERY, LAWYER_LIGHT, LAWYER_RICH, LOGO,
                       PRODUCTS, shop_with_ambient, shop_with_pool,
                       shop_without_hero, with_a_light_logo)

# Название из настоящего прайса запчастей: имя товара, артикулы в скобках и
# пометка состояния — три разных сообщения в одной строке.
SHELF_NAME = "Динаміки для ноутбука Dell Latitude E5470 (PK23000RB00 0CGDGM) бу"

IMG_SRC = re.compile(r'<img[^>]+\bsrc="([^"]+)"')

# Потолок медиа страницы: всё, что способен накопить стейджинг, — логотип и
# семь снимков скрейпа плюс фон первого экрана и три амбиент-кадра поверх
# (draft_service.MEDIA_BUDGET; вывод числа держит тест бота).
MEDIA_BUDGET = 12

# Название товара из настоящего прайса: в маркетплейсах они длиной со строку
# описания, и лимит слота обязан их пропускать (усечение данных запрещено).
LONG_PRODUCT = ("Комплект зчеплення в зборі для важкої комерційної техніки "
                "з підшипником вимикання зчеплення")

# Часы одной строкой: так их пишет и сам бизнес, и работник в карточке. Длина
# — под потолок источника (120 знаков режет и скрейп, и шлюз карточки).
LONG_HOURS = ("Пн–Чт: 09:00–13:00 та 14:00–19:00, Пт: 09:00–18:00, "
              "Сб: 10:00–17:00 без перерви, Нд і святкові дні: вихідний")

# Подпись кладки такая, какую пишет слот-генерация живому лиду: она утверждает
# нечто о том, что на кадрах, — и над дорисованным кадром это ложь.
CAPTION = "Наш сервіс у Харкові"


def images_of(html):
    return IMG_SRC.findall(html)


def test_header_stands_before_the_content(buildable_profile):
    """Шапка вне <main>: она не часть содержимого и h1 не несёт."""
    html, _ = render(buildable_profile)
    assert html.index("<header") < html.index('<main id="main">')
    assert html.count("<header") == 1
    assert html.count("<h1") == 1
    assert html.index('<main id="main">') < html.index("<h1")


def test_the_header_lies_on_the_photo_of_the_first_screen(brand_shop):
    """Первый экран — кадр во всю ширину: он начинается от кромки окна.

    Просит об этом контракт секции, а не шапка: у шапки одна разметка на оба
    случая, и меняется у неё класс, а не набор цветов в каждом теге.
    """
    html, trace = render(with_a_light_logo(brand_shop))
    header = section_html(html, "header")

    assert trace["sections"][0] == "header_logo"
    assert "header-overlay" in header
    assert "bg-paper" not in header
    assert "data-header-sentinel" in html


def test_a_dark_logo_takes_the_header_off_the_photo(brand_shop):
    """Тёмный логотип на тёмном скриме нечитаем — шапка встаёт своей полосой.

    Цвета логотипа у brand_shop не сняты, и судить приходится по фирменному
    цвету: он взят с самого логотипа (source: logo), и он тёмно-синий.
    """
    profile = replace(brand_shop, images=known(
        dict(brand_shop.images.value, logo=dict(LOGO, colors=["#005068"]))))

    for lead in (brand_shop, profile):
        html, trace = render(lead)
        header = section_html(html, "header")

        assert trace["sections"][1] == "hero_bg_photo"
        assert "header-overlay" not in header
        assert "border-b border-line bg-paper" in header
        assert "data-header-sentinel" not in html


def test_a_brand_colour_from_the_old_site_says_nothing_about_the_logo(brand_shop):
    """Цвет из CSS старого сайта о самой картинке не говорит: шапка как раньше."""
    profile = replace(brand_shop, brand_colors=known(
        dict(brand_shop.brand_colors.value, source="css")))

    assert not profile.feature("logo_is_dark").known
    assert "header-overlay" in section_html(render(profile)[0], "header")


def test_a_page_that_opens_with_text_keeps_the_header_over_the_content(
        lawyer_light):
    """Первый экран без кадра — шапка стоит своей полосой, и сентинела нет."""
    html, trace = render(lawyer_light)
    header = section_html(html, "header")

    assert trace["sections"][1] == "hero_type_only"
    assert "header-overlay" not in header
    assert "border-b border-line bg-paper" in header
    assert "data-header-sentinel" not in html


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


def test_the_gallery_gives_up_when_the_products_took_the_photos():
    """Три снимка, и все три заняты витриной: галерее не остаётся ничего."""
    profile = _shop_with_product_photos(("photo-2", "photo-3", "photo-4"), pool=3)
    html, trace = render(profile)
    assert "products_grid" in trace["sections"]
    assert not [variant for variant in trace["sections"]
                if variant.startswith("gallery_")]
    assert 'id="gallery"' not in html
    assert "about_note" in trace["sections"]


def test_the_gallery_shows_only_the_photos_no_product_took():
    """Три свободных снимка из шести — кладка живёт и повторов на странице нет."""
    profile = _shop_with_product_photos(("photo-5", "photo-6", "photo-7"), pool=6)
    html, trace = render(profile)
    assert "products_grid" in trace["sections"]
    assert "gallery_collage" in trace["sections"]
    assert images_of(section_html(html, "gallery")) == \
        ["/img/photo-2.webp", "/img/photo-3.webp", "/img/photo-4.webp"]
    used = images_of(html)
    assert len(used) == len(set(used))


def test_a_single_product_photo_stays_out_of_the_pool_anyway():
    """Товар с картинкой один: витрины на странице не будет, но кадр занят.

    Пул беспредметный: снимок товара, дошедший до gallery_statement или до
    первого экрана, уезжает предметной съёмкой во всю ширину. Порог товарной
    секции этого не отменяет — и раньше отменял.
    """
    products = [dict(PRODUCTS[0], image=GALLERY["photo-2"])]
    products += [{key: value for key, value in item.items() if key != "image"}
                 for item in PRODUCTS[1:3]]
    profile = Profile.from_dict(dict(BRAND_SHOP, products=products))
    html, trace = render(profile)

    assert profile.feature("product_image_names").value == {"photo-2"}
    assert profile.free_photos() == ["photo-3", "photo-4"]
    assert "products_list" in trace["sections"]
    assert "/img/photo-2.webp" not in images_of(html)


def test_a_product_photo_is_taken_even_when_the_shop_window_falls_apart():
    """Названий у товаров нет — витрина выбывает на слотах, кадры не свободны.

    Гейт по products_with_images витрина проходит, а slots.build отсеивает её
    за пустыми названиями: роли products на странице нет вовсе. Снимки товаров
    от этого не становятся атмосферными кадрами.
    """
    products = [{"name": "", "image": GALLERY[name]}
                for name in ("photo-2", "photo-3", "photo-4")]
    profile = Profile.from_dict(dict(BRAND_SHOP, products=products))
    _, trace = render(profile)

    assert profile.feature("products_with_images").value == 3
    assert not [variant for variant in trace["sections"]
                if variant.startswith("product")]
    assert profile.free_photos() == []


def test_a_lead_whose_products_were_never_asked_about_keeps_every_frame():
    """Товары не спрашивали — товарной съёмки нет, и весь пул свободен."""
    images = {name: image for name, image in BRAND_SHOP["images"].items()}
    profile = Profile.from_dict(
        {key: value for key, value in dict(BRAND_SHOP, images=images).items()
         if key != "products"})

    assert not profile.feature("product_image_names").known
    assert profile.free_photos() == ["photo-2", "photo-3", "photo-4"]


def test_two_frames_go_to_the_statement_and_the_about_block():
    """Курсор пула: каждая секция берёт из остатка, повторов на странице нет.

    Кадров на кладку не набралось — единственный широкий снимок работает фоном
    заявления, второй уходит блоку «о компании», и оба видны по одному разу.
    """
    profile = shop_with_pool((1200, 1600))
    html, trace = render(profile)

    assert trace["sections"].count("gallery_statement") == 1
    assert "about_photo_split" in trace["sections"]
    # widest вперёд: под фон уходит самый широкий кадр лида, а не первый по номеру
    assert images_of(section_html(html, "gallery")) == ["/img/photo-3.webp"]
    assert images_of(section_html(html, "about")) == ["/img/photo-2.webp"]
    used = images_of(html)
    assert len(used) == len(set(used))


def test_the_collage_leaves_a_frame_to_the_about_block():
    """Кладка тянется до пяти кадров, но кадр блоку «о компании» оставляет."""
    profile = shop_with_pool((1200,) * 5)
    html, trace = render(profile)

    assert "gallery_collage" in trace["sections"]
    assert "about_photo_split" in trace["sections"]
    assert len(images_of(section_html(html, "gallery"))) == 4
    assert images_of(section_html(html, "about")) == ["/img/photo-6.webp"]


def test_three_frames_stay_with_the_collage_whole():
    """Порог кладки сильнее вежливости: на трёх кадрах отдавать нечего."""
    profile = shop_with_pool((1200,) * 3)
    html, trace = render(profile)

    assert "gallery_collage" in trace["sections"]
    assert len(images_of(section_html(html, "gallery"))) == 3
    assert "about_note" in trace["sections"]


def test_a_collage_of_the_leads_own_photos_keeps_its_caption():
    """Снимков лида хватает на кладку — она подписана и ни одного чужого кадра."""
    profile = shop_with_ambient(3, 2)
    html, trace = render(profile, free_texts=free_texts_for(
        profile, {"gallery_collage.caption": CAPTION}))

    assert "gallery_collage" in trace["sections"]
    assert images_of(section_html(html, "gallery")) == \
        [f"/img/photo-{number}.webp" for number in (2, 3, 4)]
    assert CAPTION in section_html(html, "gallery")


def test_a_collage_that_borrowed_a_frame_says_nothing_about_it():
    """Дорисованный кадр закрывает дыру — подпись о компании над ним не идёт."""
    profile = shop_with_ambient(2, 2)
    html, trace = render(profile, free_texts=free_texts_for(
        profile, {"gallery_collage.caption": CAPTION}))

    assert "gallery_collage" in trace["sections"]
    assert images_of(section_html(html, "gallery")) == \
        ["/img/photo-2.webp", "/img/photo-3.webp", "/img/ambient-1.webp"]
    assert CAPTION not in html


def test_a_lead_without_photos_of_its_own_gets_a_collage_of_drawn_frames():
    """Своих снимков нет — кладка стоит на дорисованных и молчит о них."""
    profile = shop_with_ambient(0, 3)
    html, trace = render(profile, free_texts=free_texts_for(
        profile, {"gallery_collage.caption": CAPTION}))

    assert "gallery_collage" in trace["sections"]
    assert images_of(section_html(html, "gallery")) == \
        [f"/img/ambient-{number}.webp" for number in (1, 2, 3)]
    assert CAPTION not in html


def test_narrow_frames_never_become_a_full_width_background():
    """pool_min_width: снимок 800px под фон секции не годится вовсе.

    Кладке ширина безразлична — она режет кадр тайлом, а не растягивает его на
    экран, — поэтому узкие снимки уходят ей, а не заявлению.
    """
    wide, narrow = shop_with_pool((1200, 1200)), shop_with_pool((800, 800))

    assert "gallery_statement" in render(wide)[1]["sections"]
    assert not [variant for variant in render(narrow)[1]["sections"]
                if variant.startswith("gallery_")]
    assert "about_photo_split" in render(narrow)[1]["sections"]


def test_the_split_hero_takes_two_frames_and_the_page_gets_the_rest():
    """Именованного кадра под фон нет — первый экран собирается из пула.

    Hero стоит в roles_order первым и разбирает снимки раньше галереи: два
    полотна важнее полосы ниже. Остаток достаётся секциям под ними, и ни один
    кадр не выходит на страницу дважды.
    """
    profile = shop_without_hero((1600, 1400, 1200, 1000))
    html, trace = render(profile)

    assert "hero_split_2" in trace["sections"]
    assert images_of(section_html(html, "hero")) == \
        ["/img/photo-2.webp", "/img/photo-3.webp"]
    assert images_of(section_html(html, "gallery")) == ["/img/photo-4.webp"]
    assert images_of(section_html(html, "about")) == ["/img/photo-5.webp"]
    assert html.count("<h1") == 1
    used = images_of(html)
    assert len(used) == len(set(used))


def test_the_header_lies_on_the_two_frames_of_the_split_hero():
    """Первый экран из пула просит шапку тем же ключом, что и фон из белого списка."""
    profile = with_a_light_logo(shop_without_hero((1600, 1400, 1200, 1000)))
    html, trace = render(profile)
    header = section_html(html, "header")

    assert trace["sections"][1] == "hero_split_2"
    assert "header-overlay" in header
    assert "bg-paper" not in header
    assert "data-header-sentinel" in html


def test_the_split_hero_needs_two_frames_wide_enough():
    """Полотно идёт во всю ширину окна до lg: снимок 800px на нём — мыло.

    Кладке ширина безразлична — она режет кадр тайлом, — поэтому узкие снимки
    достаются ей, а первый экран честно спускается на ступень ниже.
    """
    profile = shop_without_hero((800,) * 4)
    _, trace = render(profile)

    assert "hero_split_2" not in trace["sections"]
    assert "hero_type_only" in trace["sections"]
    assert "gallery_collage" in trace["sections"]


def test_the_split_hero_holds_the_same_floor_as_the_full_width_statement():
    """Оба кадра идут от кромки до кромки — и порог ширины у них общий."""
    library = load_library()
    assert library["hero_split_2"]["pool_min_width"] == \
        library["gallery_statement"]["pool_min_width"]


def test_the_statement_says_one_thing_over_the_frame(brand_shop):
    """Заявление — одна фраза поверх кадра: ни подписи снимку, ни второй строки."""
    profile = shop_with_pool((1600,))
    html, trace = render(profile)
    gallery = section_html(html, "gallery")

    assert "gallery_statement" in trace["sections"]
    assert gallery.count("<h2") == 1
    assert 'class="scrim' in gallery
    assert "data-parallax" in gallery and "data-parallax-layer" in gallery
    assert re.search(r'<img[^>]+alt="" aria-hidden="true"', gallery)
    assert "/assets/parallax.js" in html


def test_the_facts_card_leans_over_the_photo_and_keeps_its_own_ground():
    """Оверлей даёт глубину, а не проблему с контрастом.

    Карточка фактов шире текстовой колонки на две доли и лежит ступенью выше
    кадра, но текст в ней стоит на непрозрачной подложке — той же, что у любой
    другой карточки страницы, — и все автопроверки черновика остаются пустыми.
    """
    profile = shop_with_pool((1200, 1600))
    html, _ = render(profile)
    card = re.search(r"<dl[^>]+>", section_html(html, "about")).group(0)

    assert "lg:col-span-9" in card and "lg:col-start-1" in card
    assert "card" in card and "panel" in card
    assert "lg:z-[var(--z-raise)]" in card
    assert run_all(html, profile, palette_for(profile)) == {}


def test_the_carousel_takes_the_shelf_the_grid_can_no_longer_hold():
    """Восемь товаров с фото: витрина ограничена шестью — роль берёт лента.

    Показать первые шесть и молча забыть остальные витрина права не имеет,
    поэтому её гейт ограничен сверху, а не только снизу.
    """
    photos = {f"photo-{number}": {"src": f"/img/photo-{number}.webp",
                                  "width": 1200, "height": 900}
              for number in range(2, 12)}
    profile = Profile.from_dict(dict(
        BRAND_SHOP,
        products=[{"name": f"Товар {number}", "image": photos[f"photo-{number}"]}
                  for number in range(2, 10)],
        images=dict(BRAND_SHOP["images"], **photos)))
    html, trace = render(profile)

    assert "product_carousel" in trace["sections"]
    assert "products_grid" not in trace["sections"]
    assert images_of(section_html(html, "products")) == \
        [f"/img/photo-{number}.webp" for number in range(2, 10)]
    # снимки товаров пулу не достаются, остальные достаются целиком
    assert profile.free_photos() == ["photo-10", "photo-11"]


def test_the_carousel_scrolls_without_a_line_of_script():
    """Лента листается прокруткой и доступна с клавиатуры: имя и фокус есть."""
    photos = {f"photo-{number}": {"src": f"/img/photo-{number}.webp",
                                  "width": 1200, "height": 900}
              for number in range(2, 12)}
    profile = Profile.from_dict(dict(
        BRAND_SHOP,
        products=[{"name": f"Товар {number}", "image": photos[f"photo-{number}"]}
                  for number in range(2, 10)],
        images=dict(BRAND_SHOP["images"], **photos)))
    products = section_html(render(profile)[0], "products")

    assert "snap-x" in products and "overflow-x-auto" in products
    assert 'tabindex="0"' in products
    assert 'aria-labelledby="products-title"' in products
    assert "onclick" not in products


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


def test_about_note_says_its_piece_once(brand_shop):
    """Название уже в шапке, адрес — в info: панель рядом с текстом их не повторяет."""
    html, trace = render(brand_shop)
    about = section_html(html, "about")

    assert "about_note" in trace["sections"]
    assert "info_hours_card" in trace["sections"]
    assert brand_shop.name.value not in about
    assert brand_shop.address.value not in about
    assert "<dl" not in about
    assert not any(char.isdigit() for char in _text_of(about, "text-lede"))


def test_without_the_info_section_the_about_note_keeps_the_address():
    """Секции info нет — адрес читать больше негде, и он остаётся при тексте."""
    profile = Profile.from_dict(dict(LAWYER_RICH, hours=[]))
    html, trace = render(profile)
    about = section_html(html, "about")

    assert "about_note" in trace["sections"]
    assert "info_hours_card" not in trace["sections"]
    assert profile.address.value in about
    assert about.count("<dl") == 1
    assert profile.name.value not in about
    assert html.count(profile.address.value) == 2   # блок о компании и подвал


def test_a_long_product_name_reaches_the_page_whole():
    """Лимит слота пропускает название прайса целиком, обрезает его вёрстка."""
    profile = Profile.from_dict(dict(
        BRAND_SHOP, products=[dict(PRODUCTS[0], name=LONG_PRODUCT)] + PRODUCTS[1:]))
    html, trace = render(profile)
    products = section_html(html, "products")
    assert "products_grid" in trace["sections"]
    assert LONG_PRODUCT in products
    assert "line-clamp-3" in products


def test_a_long_hours_line_keeps_the_footer_on_the_page():
    """Роль footer обязательна: длинная строка часов не имеет права её увести."""
    profile = Profile.from_dict(dict(BRAND_SHOP, hours=LONG_HOURS))
    html, trace = render(profile)
    assert html is not None, trace.get("failed") or trace["needs_enrichment"]
    assert "footer_nap" in trace["sections"]
    assert LONG_HOURS in section_html(html, "footer")


def test_a_service_without_a_blurb_prints_nothing_in_its_place(lawyer_rich):
    """Пустой блёрб — карточка без пояснения, а не литерал None в разметке."""
    variant = variant_of(lawyer_rich, "services")
    texts = free_texts_for(lawyer_rich, {f"{variant}.service_blurb[0]": ""})

    html, trace = render(lawyer_rich, free_texts=texts)

    assert variant in trace["sections"]
    assert ">None<" not in html
    assert section_html(html, "services").count("<li") >= 3


def test_the_header_menu_leads_to_the_sections_of_the_page(brand_shop):
    """Пункт меню — заголовок секции и её якорь, придуманных пунктов нет."""
    html, _ = render(brand_shop)
    header = html[:html.index("</header>")]
    assert 'href="#services"' in header and 'href="#cta"' in header
    for role in ("services", "cta"):
        title = _text_of(section_html(html, role), "text-h2")
        assert title and f">{title}</a>" in header
    assert 'href="#"' not in html


def test_a_lead_without_products_has_no_products_link(lawyer_rich):
    html, trace = render(lawyer_rich)
    assert not any(v.startswith("products_") for v in trace["sections"])
    assert 'href="#products"' not in html[:html.index("</header>")]


def test_no_more_than_one_section_of_the_page_takes_the_contrast_tone(
        buildable_profile):
    """Тональный ритм: одна контрастная секция, и не первый экран и не форма.

    Ни одной — тоже норма: страница, открытая кадром во всю ширину, тёмный
    акцент уже потратила (engine/compose.link_sections).
    """
    html, trace = render(buildable_profile)
    assert html.count('data-tone="contrast"') == (1 if trace["tone"] else 0)
    if trace["tone"] is None:
        return
    assert trace["tone"] in CONTRAST_ROLES
    assert f'id="{trace["tone"]}"' in html
    marked = section_html(html, trace["tone"])
    assert 'data-tone="contrast"' in marked[:marked.index(">")]


def test_a_page_without_about_proof_or_info_wears_no_contrast_tone():
    """Контрастной секции может не быть вовсе — тон это свойство состава.

    Часов и показателей профиль не дал, поэтому info и proof отсеяли гейты;
    about держится на адресе, без которого не собрать обязательный футер, и
    увести его со страницы может только пустой текст модели.
    """
    profile = Profile.from_dict(dict(LAWYER_LIGHT, hours=[], review_count=None,
                                     google_rating=None))
    variant = variant_of(profile, "about")
    texts = free_texts_for(profile, {f"{variant}.about_text": ""})

    html, trace = render(profile, free_texts=texts)

    library = load_library()
    roles = {library[chosen]["role"] for chosen in trace["sections"]}
    assert not roles & set(CONTRAST_ROLES)
    assert trace["tone"] is None
    assert 'data-tone="contrast"' not in html


def test_the_contrast_tones_come_from_the_palette_of_the_page(brand_shop):
    """Тона считаны с палитры лида, а не вшиты в разметку чёрным по белому."""
    html, _ = render(brand_shop)
    tones = contrast_tones(palette_for(brand_shop))
    for name, value in tones.items():
        assert f"--contrast-{name}:{value}" in html


def test_the_bands_of_the_page_come_from_position_not_from_the_markup(
        buildable_profile):
    """Полосу секции задаёт её место на странице: класса bg-* в разметке нет.

    Исключение одно и оно видно глазом: секция, у которой свой фон есть по
    смыслу, — кадр под первым экраном и полоса галереи.
    """
    html, trace = render(buildable_profile)
    own_ground = {"hero_bg_photo", "hero_split_2", "gallery_statement",
                  "gallery_strip"}
    for variant in trace["sections"]:
        role = load_library()[variant]["role"]
        if role in ("header", "footer") or variant in own_ground:
            continue
        opening = section_html(html, role)
        opening = opening[:opening.index(">")]
        assert "bg-" not in opening, f"{variant}: {opening}"


def test_the_footer_stops_repeating_what_the_info_section_already_says(brand_shop):
    """Адрес и телефон на странице по одному блоку, а не по четыре раза."""
    html, trace = render(brand_shop)
    assert "info_hours_card" in trace["sections"]
    footer = section_html(html, "footer")
    assert brand_shop.name.value in footer
    assert "Чернетка" in footer
    assert brand_shop.address.value not in footer
    assert brand_shop.phone.value not in footer
    assert html.count(brand_shop.address.value) == 1   # только секция info


def test_without_the_info_section_the_footer_keeps_the_contacts(lawyer_rich):
    """Секции info нет — подвал остаётся полным, иначе контакты пропали бы."""
    profile = Profile.from_dict(dict(LAWYER_RICH, hours=[]))
    html, trace = render(profile)
    assert "info_hours_card" not in trace["sections"]
    footer = section_html(html, "footer")
    assert profile.address.value in footer
    assert profile.phone.value in footer


def test_the_menu_of_a_narrow_screen_collapses_without_a_line_of_script(brand_shop):
    """CSP превью разрешает только свои файлы: раскрытие держит <details>."""
    html, _ = render(brand_shop)
    header = html[html.index("<header"):html.index("</header>")]
    assert '<details class="nav-collapse' in header
    assert "<summary" in header and 'aria-label="' in header
    assert "<svg" in header and "onclick" not in header
    # пункты одни и те же, просто нарисованы дважды
    anchors = re.findall(r'<a href="#([a-z]+)"', header)
    assert anchors[:len(anchors) // 2] == anchors[len(anchors) // 2:]


def test_the_second_button_of_the_hero_says_where_it_leads(generic_light):
    """Подпись кнопки — заголовок секции, к которой она ведёт, а не «Написати»."""
    html, _ = render(generic_light)
    hero = section_html(html, "hero")
    target, label = re.search(r'<a href="#([a-z]+)" class="btn btn-quiet">([^<]+)<',
                              hero).groups()
    assert target == "products"
    assert label == _text_of(section_html(html, target), "text-h2")


def test_the_second_button_follows_the_section_that_dropped_out(generic_light):
    """Секция выбыла на текстах модели — кнопка ведёт к следующей, не в никуда."""
    variant = variant_of(generic_light, "products")
    texts = free_texts_for(generic_light, {f"{variant}.section_title": ""})

    html, trace = render(generic_light, free_texts=texts)

    assert variant not in trace["sections"]
    assert 'href="#products"' not in html
    hero = section_html(html, "hero")
    target, label = re.search(r'<a href="#([a-z]+)" class="btn btn-quiet">([^<]+)<',
                              hero).groups()
    assert target == "about"
    assert label == _text_of(section_html(html, target), "text-h2")


def test_without_a_named_section_the_hero_keeps_no_second_button():
    """Называть кнопку нечем — её нет: подписи «Написати» в никуда не будет."""
    hero = bare_section("hero", "hero_type_only")
    page = [hero, bare_section("footer", "footer_nap")]

    link_sections(page)

    assert hero["slots"]["secondary_label"] is None
    assert hero["slots"]["secondary_target"] == "footer"


def test_the_second_button_reads_the_page_order_not_the_list_of_roles():
    """«Первый раздел» — первый сверху: about выше прайса, кнопка ведёт к нему."""
    hero = bare_section("hero", "hero_type_only")
    page = [hero,
            bare_section("about", "about_note", "Про майстерню"),
            bare_section("products", "products_list", "Прайс"),
            bare_section("footer", "footer_nap")]

    link_sections(page)

    assert hero["slots"]["secondary_target"] == "about"
    assert hero["slots"]["secondary_label"] == "Про майстерню"


def test_a_shelf_name_is_split_but_never_shortened():
    """Артикулы уезжают второй строкой, но остаются на странице целиком."""
    head, tail = split_product_name(SHELF_NAME)
    assert head == "Динаміки для ноутбука Dell Latitude E5470"
    assert tail == "(PK23000RB00 0CGDGM) бу"
    assert f"{head} {tail}" == SHELF_NAME


def test_a_plain_name_stays_on_one_line():
    for name in ("Гальмівні колодки", "Sourdough loaf", "Амортизатор передній"):
        assert split_product_name(name) == (name, "")


def test_a_used_marker_leaves_the_name_alone():
    assert split_product_name("Клавіатура Acer Aspire бу") == \
        ("Клавіатура Acer Aspire", "бу")
    # голова короче слова — резать нечего
    assert split_product_name("бу") == ("бу", "")


def test_a_short_head_hands_the_cut_over_to_the_marker():
    """Скобка с короткой головой уступает пометке, а не отменяет разрез.

    «TB (Samsung)» — одно название, головы из двух букв не бывает; но пометка
    состояния в конце строки режет ту же строку честно.
    """
    assert split_product_name("TB (Samsung) used") == ("TB (Samsung)", "used")
    assert split_product_name("Fan (120mm) бу") == ("Fan (120mm)", "бу")
    # пометки нет — резать нечем, название идёт одной строкой
    assert split_product_name("TB (Samsung)") == ("TB (Samsung)", "")


def test_a_line_that_opens_with_a_bracket_has_no_head_at_all():
    """«(2 шт) бу» — количество и состояние: товара в строке нет, головы тоже."""
    assert split_product_name("(2 шт) бу") == ("(2 шт) бу", "")


def test_both_halves_carry_the_name_even_without_a_space_to_rejoin_them():
    """Пробела на месте разреза не было — склейка идёт без него, но целиком."""
    head, tail = split_product_name("Dell(PK123)")
    assert (head, tail) == ("Dell", "(PK123)")
    assert head + tail == "Dell(PK123)"


def test_both_halves_of_the_shelf_name_reach_the_card():
    """Разрез — вёрстка: на странице обе части, в профиле название нетронуто."""
    profile = Profile.from_dict(dict(
        BRAND_SHOP, products=[dict(PRODUCTS[0], name=SHELF_NAME)] + PRODUCTS[1:]))
    html, trace = render(profile)
    products = section_html(html, "products")

    assert "products_grid" in trace["sections"]
    assert "Динаміки для ноутбука Dell Latitude E5470</h3>" in products
    assert "(PK23000RB00 0CGDGM) бу</p>" in products
    assert profile.products.value[0]["name"] == SHELF_NAME


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


def _shop_with_product_photos(names, pool=5):
    """brand_shop, у которого снимки товаров лежат в общем пуле photo-N.

    Так их и складывает стейджинг: у товарной картинки нет своего имени, она
    такой же photo-N, как снимок витрины, — и по имени видно, что она занята.
    Остальным товарам картинки не оставляем: пул профиля должен быть виден
    целиком, без ссылок на файлы вне белого списка.
    """
    photos = {f"photo-{number}": {"src": f"/img/photo-{number}.webp",
                                  "width": 1200, "height": 900}
              for number in range(2, 2 + pool)}
    products = [dict(item, image=photos[name])
                for item, name in zip(PRODUCTS, names)]
    products += [{key: value for key, value in item.items() if key != "image"}
                 for item in PRODUCTS[len(names):]]
    return Profile.from_dict(dict(BRAND_SHOP, products=products,
                                  images=dict(BRAND_SHOP["images"], **photos)))


def bare_section(role, variant, title=None):
    """Секция без данных: link_sections смотрит только на роль и заголовок."""
    return {"id": role, "role": role, "variant": variant,
            "slots": {"section_title": title} if title else {},
            "contract": load_library()[variant]}


def variant_of(profile, role):
    """Какой вариант выиграл роль у этого профиля."""
    _, trace = render(profile)
    library = load_library()
    return next(variant for variant in trace["sections"]
                if library[variant]["role"] == role)


def free_texts_for(profile, overrides):
    """Тексты «модели» на композицию профиля: заготовки рецепта плюс правка.

    Групповые ключи сюда не входят — недостающий ключ оставляет заготовку
    рецепта, и подменить надо ровно тот блёрб, о котором тест.
    """
    recipe = load_recipe(recipe_id_for(profile))
    composition = compose(profile, recipe, load_library(),
                          seed_for(profile.domain_norm))
    texts = {f"{part['variant']}.{spec['name']}": part["slots"][spec["name"]]
             for part in composition.sections
             for spec in slots.free_specs(part["contract"])}
    return texts | overrides


def section_html(html, role):
    """Кусок страницы одной роли: id секции — это её роль (engine/compose)."""
    start = html.index(f'id="{role}"')
    tail = html[start:]
    end = tail.find("</section>")
    return tail[:end if end > 0 else len(tail)]


def _text_of(html, marker):
    match = re.search(rf'class="[^"]*{marker}[^"]*"[^>]*>([^<]+)<', html)
    return match.group(1) if match else ""
