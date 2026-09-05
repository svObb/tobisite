"""Фикстуры движка: восемь синтетических профилей, ни базы, ни сети.

Компании выдуманы целиком, телефоны — несуществующие, из нулей. Цифры в
профилях (рейтинг, отзывы, цены) это входные данные теста, а не текст сайта:
на страницу они попадают только через белый список fact-слотов.

Профили закрывают ветки лестницы деградации и ветки палитры:
    lawyer_rich    — фото, услуги, адрес, рейтинг: верхняя ступень каждой роли
    lawyer_light   — без фото и без картинок: понижение внутри роли
    lawyer_poor    — почти всё unknown: needs_enrichment вместо черновика
    generic_rich   — ниша вне покрытия, полный набор данных
    generic_light  — услуг нет, зато есть показатели и товары без картинок:
                     замена роли, products_list, язык en
    brand_shop     — логотип, бренд-цвета, товары и весь набор картинок:
                     шапка с логотипом, фоновое фото, полоса галереи
    brand_ugly     — бренд-цвета из чёрно-белого логотипа: палитра пресетная
    products_lead  — товары, картинки не у всех: отбор group_filter
"""
import dataclasses
import pathlib

import pytest

from site_factory.engine.profile import Profile, known

ROOT = pathlib.Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"

# Имена картинок — контракт скрейпа сайта лида: logo, hero_bg (широкий кадр
# под фон первого экрана), portrait, photo-2..photo-N (контентные снимки).
PORTRAIT = {"src": "/img/portrait.avif", "width": 1600, "height": 2000}
MAP = {"src": "/img/map.avif", "width": 1600, "height": 1200}
LOGO = {"src": "/img/logo.webp", "width": 320, "height": 96}
# Цвета логотипа снимает стейджинг картинок (site_images.dominant_colors).
# Светлый читается на тёмном скриме первого экрана, тёмный на нём пропадает.
LIGHT_LOGO = dict(LOGO, colors=["#f8c050"])
HERO_BG = {"src": "/img/hero_bg.webp", "width": 2000, "height": 1125}
GALLERY = {
    "photo-2": {"src": "/img/photo-2.webp", "width": 1200, "height": 900},
    "photo-3": {"src": "/img/photo-3.webp", "width": 1200, "height": 900},
    "photo-4": {"src": "/img/photo-4.webp", "width": 1200, "height": 900},
}

# Товары: цена — строка ровно такая, как её пишет сам бизнес. Картинка есть
# не у каждого, и это разные ветки товарных секций.
PRODUCTS = [
    {"name": "Гальмівні колодки", "price": "від 890 грн",
     "image": {"src": "/img/product-1.webp", "width": 800, "height": 800}},
    {"name": "Амортизатор передній", "price": "1 450 грн",
     "image": {"src": "/img/product-2.webp", "width": 800, "height": 800}},
    {"name": "Комплект зчеплення", "price": "3 200 грн",
     "image": {"src": "/img/product-3.webp", "width": 800, "height": 800}},
    {"name": "Масляний фільтр", "price": "240 грн"},
    {"name": "Акумулятор 60 Ah"},
]

# Прайс без единой картинки: ступень products_list.
BAKERY_PRODUCTS = [
    {"name": "Sourdough loaf", "price": "2,40 €"},
    {"name": "Rye bread", "price": "2,10 €"},
    {"name": "Butter croissant", "price": "1,30 €"},
    {"name": "Cinnamon bun"},
]

LAWYER_RICH = {
    "domain_norm": "advokatske-buro-fikstura.example",
    "niche": "Юрист",
    "lang": "uk",
    "country": "UA",
    "city": "Київ",
    "name": "Адвокатське бюро «Фікстура»",
    "phone": "+380 00 000 00 01",
    "email": "office@example.com",
    "address": "вул. Тестова, 1, Київ",
    "address_parts": {"street": "вул. Тестова, 1", "locality": "Київ",
                      "country": "UA"},
    "photo_count": 6,
    "services": ["Договірне право", "Судові спори", "Перевірки та штрафи",
                 "Корпоративне право", "Трудові спори"],
    "has_prices": False,
    "has_booking_url": False,
    "hours": ["Пн–Пт: 09:00–18:00", "Сб–Нд: за домовленістю"],
    "review_count": 34,
    "google_rating": 4.8,
    "text_volume": "long",
    "old_site_state": "outdated",
    "images": {"portrait": PORTRAIT, "map": MAP},
}

LAWYER_LIGHT = {
    "domain_norm": "yurydychna-praktyka-svitlo.example",
    "niche": "адвокат",
    "lang": "uk",
    "country": "UA",
    "city": "Львів",
    "name": "Юридична практика «Світло»",
    "phone": "+380 00 000 00 02",
    "email": "mail@example.com",
    "address": "просп. Тестовий, 12, Львів",
    "address_parts": {"street": "просп. Тестовий, 12", "locality": "Львів",
                      "country": "UA"},
    "photo_count": 0,
    "services": ["Сімейні спори", "Спадщина", "Нерухомість", "Договори"],
    "has_prices": False,
    "hours": ["Пн–Пт: 10:00–18:00"],
    "text_volume": "medium",
    "old_site_state": "none",
    "images": {},
}

# Карточка, которую работник открыл и не дозаполнил: имя и ниша есть, всё
# остальное не спрошено. unknown != false, поэтому гейты обязаны отсеять всё.
LAWYER_POOR = {
    "domain_norm": "yurydychna-konsultatsiya-test.example",
    "niche": "Юрист",
    "lang": "uk",
    "country": "UA",
    "city": "Одеса",
    "name": "Юридична консультація «Тест»",
    "text_volume": "short",
}

GENERIC_RICH = {
    "domain_norm": "remontna-maisternya-forma.example",
    "niche": "Ремонт квартир",
    "lang": "uk",
    "country": "UA",
    "city": "Дніпро",
    "name": "Ремонтна майстерня «Форма»",
    "phone": "+380 00 000 00 03",
    "email": "info@example.com",
    "address": "вул. Прикладна, 7, Дніпро",
    "address_parts": {"street": "вул. Прикладна, 7", "locality": "Дніпро",
                      "country": "UA"},
    "photo_count": 4,
    "services": ["Оздоблення", "Електрика", "Сантехніка"],
    "has_prices": True,
    "hours": ["Пн–Сб: 08:00–19:00"],
    "review_count": 12,
    "google_rating": 4.6,
    "text_volume": "medium",
    "old_site_state": "not_mobile",
    "images": {"portrait": PORTRAIT, "map": MAP},
}

# Услуги спрошены, и их нет (known-пустой список): роль services выбывает,
# на её место встаёт proof — ступень 2 лестницы.
GENERIC_LIGHT = {
    "domain_norm": "corner-bakery-test.example",
    "niche": "Bakery",
    "lang": "en",
    "country": "SK",
    "city": "Košice",
    "name": "Corner Bakery",
    "phone": "+421 000 000 004",
    "email": "hello@example.com",
    "address": "Testovacia 4, Košice",
    "address_parts": {"street": "Testovacia 4", "locality": "Košice",
                      "country": "SK"},
    "photo_count": 1,
    "services": [],
    "hours": ["Mon–Fri: 08:00–17:00", "Sat: 09:00–14:00"],
    "review_count": 58,
    "google_rating": 4.7,
    "text_volume": "short",
    "old_site_state": "none",
    "images": {},
    "products": BAKERY_PRODUCTS,
}

# Логотип и фирменный красный: пул автосервиса открывается тёмным bold-trade,
# и логотип обязан выбить его оттуда (engine/render._legible).
BRAND_SHOP = {
    "domain_norm": "avtoservis-fikstura.example",
    "niche": "Автосервіс",
    "lang": "uk",
    "country": "UA",
    "city": "Харків",
    "name": "Автосервіс «Фікстура»",
    "phone": "+380 00 000 00 05",
    "email": "service@example.com",
    "address": "вул. Прикладна, 15, Харків",
    "address_parts": {"street": "вул. Прикладна, 15", "locality": "Харків",
                      "country": "UA"},
    "photo_count": 5,
    "services": ["Діагностика", "Ремонт підвіски", "Заміна масла", "Шиномонтаж"],
    "has_prices": True,
    "hours": ["Пн–Пт: 08:00–20:00", "Сб: 09:00–15:00"],
    "review_count": 41,
    "google_rating": 4.9,
    "text_volume": "medium",
    "old_site_state": "not_mobile",
    "brand_colors": {"primary": "#0f3d61", "accent": "#e2452b", "source": "logo"},
    "images": dict({"logo": LOGO, "hero_bg": HERO_BG, "portrait": PORTRAIT,
                    "map": MAP}, **GALLERY),
    "products": PRODUCTS,
}

# Логотип чёрно-белый: «фирменные цвета» из него — серые, и палитра остаётся
# пресетной (engine/palette, причина low_chroma).
BRAND_UGLY = dict(
    GENERIC_RICH,
    domain_norm="stolyarna-maisternya-siro.example",
    name="Столярна майстерня «Сіро»",
    brand_colors={"primary": "#3a3d40", "accent": "#8a8f94", "source": "css"},
)

PRODUCTS_LEAD = dict(
    GENERIC_RICH,
    domain_norm="magazyn-zapchastyn-test.example",
    name="Магазин запчастин «Тест»",
    products=PRODUCTS,
)

FIXTURES = {
    "lawyer_rich": LAWYER_RICH,
    "lawyer_light": LAWYER_LIGHT,
    "lawyer_poor": LAWYER_POOR,
    "generic_rich": GENERIC_RICH,
    "generic_light": GENERIC_LIGHT,
    "brand_shop": BRAND_SHOP,
    "brand_ugly": BRAND_UGLY,
    "products_lead": PRODUCTS_LEAD,
}
BUILDABLE = [name for name in FIXTURES if name != "lawyer_poor"]


@pytest.fixture(scope="session", autouse=True)
def bundle_built():
    """Без собранного бандла страница не страница — тесты честно пропускаются."""
    missing = [path.name for path in (BUILD / "bundle.css", BUILD / "fonts")
               if not path.exists()]
    if missing:
        pytest.skip(f"нет build/{', build/'.join(missing)}: соберите бандл — "
                    "python tools/fetch_fonts.py && python tools/build_css.py")


def shop_with_pool(widths):
    """brand_shop, у которого свободных снимков ровно столько и такой ширины.

    Именованные кадры остаются только logo и hero_bg: portrait и map в пул не
    входят, и держать их здесь значило бы проверять не то, что проверяется.
    """
    photos = {f"photo-{2 + index}": {"src": f"/img/photo-{2 + index}.webp",
                                     "width": width,
                                     "height": width * 3 // 4}
              for index, width in enumerate(widths)}
    named = {name: image for name, image in BRAND_SHOP["images"].items()
             if name in ("logo", "hero_bg")}
    return Profile.from_dict(dict(BRAND_SHOP, images=named | photos))


def shop_with_ambient(real: int, drawn: int):
    """brand_shop, у которого столько снимков сайта и столько дорисованных кадров.

    Дорисованные кладёт в стейджинг человек штатной командой (bot/ambient_stage)
    под именами ambient-N: в белом списке они лежат рядом с photo-N и отличаются
    от них только именем.
    """
    frames = [f"photo-{2 + index}" for index in range(real)]
    frames += [f"ambient-{1 + index}" for index in range(drawn)]
    pool = {name: {"src": f"/img/{name}.webp", "width": 1200, "height": 900}
            for name in frames}
    named = {name: image for name, image in BRAND_SHOP["images"].items()
             if name in ("logo", "hero_bg")}
    return Profile.from_dict(dict(BRAND_SHOP, images=named | pool))


def shop_without_hours(widths):
    """То же, но часы спрошены и их нет: страница собирается без секции info."""
    return dataclasses.replace(shop_with_pool(widths), hours=known([]))


def shop_without_hero(widths):
    """То же, но без hero_bg: кадр под первый экран приходится брать из пула."""
    profile = shop_with_pool(widths)
    images = {name: image for name, image in profile.images.value.items()
              if name != "hero_bg"}
    return Profile.from_dict(dict(BRAND_SHOP, images=images))


def with_a_light_logo(profile: Profile) -> Profile:
    """Тот же лид, но логотип у него светлый — шапка ложится на кадр.

    Фирменный цвет у brand_shop снят с логотипа, поэтому меняются они вместе:
    стейджинг берёт primary из тех же цветов (enrich_service._brand_colors).
    """
    brand = dict(profile.brand_colors.value, primary=LIGHT_LOGO["colors"][0])
    images = dict(profile.images.value, logo=LIGHT_LOGO)
    return dataclasses.replace(profile, images=known(images),
                               brand_colors=known(brand))


@pytest.fixture
def shop_without_a_named_hero():
    """Лид под hero_split_2: четыре широких кадра и ни одного именованного фона."""
    return shop_without_hero((1600, 1400, 1200, 1000))


@pytest.fixture(params=sorted(FIXTURES))
def any_profile(request):
    return Profile.from_dict(FIXTURES[request.param])


@pytest.fixture(params=sorted(BUILDABLE))
def buildable_profile(request):
    return Profile.from_dict(FIXTURES[request.param])


@pytest.fixture
def lawyer_rich():
    return Profile.from_dict(LAWYER_RICH)


@pytest.fixture
def lawyer_light():
    return Profile.from_dict(LAWYER_LIGHT)


@pytest.fixture
def lawyer_poor():
    return Profile.from_dict(LAWYER_POOR)


@pytest.fixture
def generic_rich():
    return Profile.from_dict(GENERIC_RICH)


@pytest.fixture
def generic_light():
    return Profile.from_dict(GENERIC_LIGHT)


@pytest.fixture
def brand_shop():
    return Profile.from_dict(BRAND_SHOP)


@pytest.fixture
def brand_ugly():
    return Profile.from_dict(BRAND_UGLY)


@pytest.fixture
def products_lead():
    return Profile.from_dict(PRODUCTS_LEAD)
