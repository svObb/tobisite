"""Фикстуры движка: пять синтетических профилей, ни базы, ни сети.

Компании выдуманы целиком, телефоны — несуществующие, из нулей. Цифры в
профилях (рейтинг, отзывы) это входные данные теста, а не текст сайта: на
страницу они попадают только через белый список fact-слотов.

Пять профилей закрывают ветки лестницы деградации:
    lawyer_rich    — фото, услуги, адрес, рейтинг: верхняя ступень каждой роли
    lawyer_light   — без фото и без картинок: понижение внутри роли
    lawyer_poor    — почти всё unknown: needs_enrichment вместо черновика
    generic_rich   — ниша вне покрытия, полный набор данных
    generic_light  — услуг нет, зато есть показатели: замена роли, язык en
"""
import pathlib

import pytest

from site_factory.engine.profile import Profile

ROOT = pathlib.Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"

PORTRAIT = {"src": "/img/portrait.avif", "width": 1600, "height": 2000}
MAP = {"src": "/img/map.avif", "width": 1600, "height": 1200}

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
}

FIXTURES = {
    "lawyer_rich": LAWYER_RICH,
    "lawyer_light": LAWYER_LIGHT,
    "lawyer_poor": LAWYER_POOR,
    "generic_rich": GENERIC_RICH,
    "generic_light": GENERIC_LIGHT,
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
