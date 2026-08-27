"""Разбор сайта лида: чистые функции на фикстурах и вежливость обхода.

Сети здесь нет ни одного байта. Страницы лежат в tests/fixtures/scrape и
написаны нами: чужие сайты в репозиторий не копируются. Обход проверяется
фальшивой сессией, которая считает запросы и отдаёт заготовленные ответы.
"""
import pathlib

import pytest

import site_scrape as ss

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "scrape"
BASE = "https://lihtaryk.example/"


def page(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture(scope="module")
def shop():
    return ss.soup_of(page("woocommerce.html"))


@pytest.fixture(scope="module")
def builder():
    return ss.soup_of(page("builder.html"))


@pytest.fixture(scope="module")
def handmade():
    return ss.soup_of(page("handmade.html"))


@pytest.fixture(scope="module")
def bakery():
    return ss.soup_of(page("svg_logo.html"))


@pytest.fixture(scope="module")
def prom():
    return ss.soup_of(page("prom_shop.html"))


# --- кодировки ----------------------------------------------------------------

def test_windows_1251_is_decoded_by_its_meta():
    """Кодировка объявлена только в <meta>, и байты за ней настоящие."""
    raw = page("handmade.html")
    assert b"\xd1\xf2\xf3\xe4\xe8\xff" in raw          # «Студия» в cp1251

    text = ss.decode(raw)

    assert "Красная нитка" in text
    assert "Мастерская ручной вышивки" in text


def test_windows_1251_page_gives_readable_fields(handmade):
    assert ss.site_name(handmade).startswith("Студия")
    assert "Пошив скатертей" in ss.services(handmade)
    assert ss.address(handmade)["parts"]["locality"] == "Выгаданск"


# --- имя, ссылки, объём -------------------------------------------------------

def test_site_name_prefers_og_over_title(shop):
    assert ss.site_name(shop) == "Ліхтарик"


def test_site_name_falls_back_to_the_head_of_the_title(builder):
    # «Autoservis Kolesko | Opravy a diagnostika» — название слева от палки
    assert ss.site_name(builder) == "Autoservis Kolesko"


def test_internal_links_are_scored_and_capped(shop):
    links = ss.pick_internal_links(shop, BASE)

    assert links == [f"{BASE}contacts/", f"{BASE}catalog/", f"{BASE}about/"]
    assert len(links) <= ss.MAX_LINKS
    # чужой хост внутренней страницей не бывает
    assert not any("facebook" in url for url in links)


def test_text_volume_is_a_ladder():
    assert ss.text_volume("") == "none"
    assert ss.text_volume("x" * 500) == "short"
    assert ss.text_volume("x" * 2000) == "medium"
    assert ss.text_volume("x" * 9000) == "long"


def test_old_site_state_names_the_worst_thing_first():
    assert ss.old_site_state({}, reachable=False) == "broken"
    assert ss.old_site_state({"viewport": False}) == "not_mobile"
    assert ss.old_site_state({"viewport": True, "copyright_year": 2009}) == "outdated"
    assert ss.old_site_state({"viewport": True, "builders": ["wix"]}) == "outdated"
    assert ss.old_site_state({"viewport": True, "copyright_year": None}) == "ok"


# --- контакты -----------------------------------------------------------------

def test_only_valid_numbers_survive():
    text = "Телефон +380 44 000 00 11, факс 12-34, ще 000000"

    assert ss.phones(text, "UA") == ["+380440000011"]


def test_a_number_without_a_country_code_needs_a_region():
    assert ss.phones("044 000 00 11") == []
    assert ss.phones("044 000 00 11", "UA") == ["+380440000011"]


def test_emails_come_from_mailto_first(shop):
    assert ss.emails(shop)[0] == "shop@lihtaryk.example"


# --- адрес и часы -------------------------------------------------------------

def test_address_parts_come_only_from_json_ld(shop):
    found = ss.address(shop)

    assert found["parts"] == {"street": "вулиця Вигадана, 4",
                              "locality": "Вигаданськ",
                              "postal_code": "01001", "country": "UA"}
    assert found["display"] == "вулиця Вигадана, 4, Вигаданськ, 01001"


def test_address_without_structure_has_no_parts(builder):
    found = ss.address(builder)

    assert found["display"] == "Hlavná 12, Košice"
    assert found["parts"] == {}


def test_hours_prefer_json_ld_over_tables(shop):
    assert ss.hours(shop) == ["Monday, Friday: 09:00–19:00",
                              "Saturday: 10:00–16:00"]


def test_hours_fall_back_to_a_day_and_time_table(builder):
    lines = ss.hours(builder)

    assert lines[0] == "Pondelok – piatok: 08:00 – 17:00"
    assert "Sobota: 09:00 – 13:00" in lines
    # строка без времени часами не считается
    assert not any("zatvorené" in line for line in lines)


def test_hours_are_capped(shop):
    assert len(ss.hours(shop)) <= ss.MAX_HOURS


# --- услуги -------------------------------------------------------------------

def test_services_are_read_from_a_named_block(shop):
    found = ss.services(shop)

    assert "Продаж ноутбуків" in found and "Заміна екрана" in found
    assert len(found) <= ss.MAX_SERVICES
    assert all(len(line) <= ss.SERVICE_MAX_CHARS for line in found)


def test_services_are_read_from_card_headings(builder):
    found = ss.services(builder)

    assert "Diagnostika motora" in found
    assert "Prezúvanie pneumatík" in found


# --- товары -------------------------------------------------------------------

def test_products_prefer_json_ld(shop):
    found = ss.products(shop, BASE)
    names = [item["name"] for item in found]

    assert names[:2] == ["Ноутбук Промінь 14", "Ноутбук Промінь 16"]
    assert found[0]["price"] == "24990 UAH"
    assert found[0]["image"] == f"{BASE}media/promin-14-1200.jpg"


def test_products_from_cards_carry_the_price_string_as_written(shop):
    found = ss.products(shop, BASE)
    dock = next(item for item in found if "Докстанція" in item["name"])

    # цену не пересчитываем и не переформатируем: она факт с чужого сайта
    assert dock["price"] == "2 190 грн"
    assert dock["image"] == f"{BASE}media/dokstantsiia-800.jpg"


def test_products_are_capped(shop):
    assert len(ss.products(shop, BASE)) <= ss.MAX_PRODUCTS


# --- картинки и цвета ---------------------------------------------------------

def test_og_image_leads_the_photo_queue(shop):
    urls = [item["url"] for item in ss.image_candidates(shop, BASE)]

    assert urls[0] == f"{BASE}media/vitryna-2400.jpg"
    assert ss.image_candidates(shop, BASE)[0]["og"] is True


def test_sprites_icons_and_badges_are_not_photos(shop):
    urls = [item["url"] for item in ss.image_candidates(shop, BASE)]

    assert not any("sprite" in url or "badge" in url for url in urls)
    assert not any("logo" in url for url in urls)


def test_srcset_gives_the_widest_variant(builder):
    urls = [item["url"] for item in ss.image_candidates(builder, BASE)]

    assert f"{BASE}uploads/dielna-1600.jpg" in urls
    assert not any("icon-phone" in url for url in urls)


def test_logo_from_the_header_beats_the_touch_icon(shop):
    logos = ss.logo_candidates(shop, BASE)

    assert logos[0]["url"] == f"{BASE}media/logo-lihtaryk.png"
    assert logos[0]["kind"] == "img"
    assert any(item["kind"] == "icon" for item in logos)


def test_inline_svg_logo_is_its_own_kind(bakery):
    logos = ss.logo_candidates(bakery, BASE)

    assert logos[0]["kind"] == "svg"
    assert "<script" in logos[0]["markup"]     # чистит его site_images


def test_logo_is_found_by_the_wrapper_class_in_the_header(prom):
    """У конструктора «logo» стоит на ссылке-обёртке, а у самой картинки нет."""
    logos = ss.logo_candidates(prom, BASE)
    urls = [item["url"] for item in logos]

    assert logos[0]["url"] == f"{BASE}images/6773692817_w200_h100_vysokyi-servis.jpg"
    assert logos[0]["kind"] == "img"
    # остальные картинки шапки логотипом от этого не стали
    assert not any("cart" in url for url in urls)


def test_a_logo_class_outside_the_header_is_not_a_logo(prom):
    urls = [item["url"] for item in ss.logo_candidates(prom, BASE)]

    # div.logo-partners в подвале — это чужие лого, а не наше
    assert not any("partners" in url for url in urls)


def test_theme_color_wins_over_css_variables(shop):
    assert ss.theme_colors(shop) == {"primary": "#1f6f4a", "source": "meta"}


def test_grey_theme_color_is_not_a_brand_colour(builder):
    colors = ss.theme_colors(builder)

    # #f5f5f5 из meta — это фон, а не бренд; берутся переменные
    assert colors == {"primary": "#b3341f", "accent": "#2b4a8c", "source": "css"}


# --- антибот ------------------------------------------------------------------

def test_challenge_page_is_recognised_without_being_solved():
    assert ss.is_blocked(503, page("cf_challenge.html")) is True
    assert ss.is_blocked(200, page("cf_challenge.html")) is True
    assert ss.is_blocked(403, b"<html>nothing</html>") is True
    assert ss.is_blocked(200, page("woocommerce.html")) is False


# --- обход: вежливость и деградация -------------------------------------------

class FakeResponse:
    def __init__(self, url, status, body, headers=None):
        self.url, self.status, self._body = url, status, body
        self.headers = headers or {}
        self.content = self

    async def read(self, limit):
        return self._body[:limit]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Сессия без сокетов: считает запросы и отдаёт заготовленные ответы.

    Значение страницы — байты или ("302", адрес): редиректы бот проходит сам,
    и каждый их адрес обязан пройти проверку.
    """

    def __init__(self, pages, fail=None):
        self.pages, self.fail = pages, fail
        self.asked = []

    def get(self, url, **kw):
        self.asked.append(url)
        if self.fail is not None:
            raise self.fail
        body = self.pages.get(url)
        if body is None:
            return FakeResponse(url, 404, b"")
        if isinstance(body, tuple):
            return FakeResponse(url, 302, b"", {"Location": body[1]})
        return FakeResponse(url, 200, body)


@pytest.fixture(autouse=True)
def _no_pause(monkeypatch):
    """Пауза между страницами реальная, но ждать её в тестах незачем."""
    monkeypatch.setattr(ss, "PAUSE_SEC", 0)


# Хост фикстур в DNS не существует, а гард обязан его резолвить — отсюда
# подменённый резолвер. Адрес взят настоящий публичный: диапазоны TEST-NET
# ipaddress считает приватными, и гард отверг бы их вместе с 10.0.0.0/8.
PUBLIC_IP = "93.184.216.34"
HOSTS = {"lihtaryk.example": PUBLIC_IP, "shop.lihtaryk.example": PUBLIC_IP,
         "metadata.lihtaryk.example": "169.254.169.254",
         "inside.lihtaryk.example": "10.1.2.3", "localhost": "127.0.0.1"}


@pytest.fixture(autouse=True)
def _resolver(monkeypatch):
    """DNS вместо сети: имя вне таблицы не резолвится вовсе."""
    async def fake(host):
        if host not in HOSTS:
            raise OSError(f"{host} не найден")
        return [HOSTS[host]]

    monkeypatch.setattr(ss, "resolve_host", fake)


async def test_walk_stops_at_the_page_limit():
    pages = {BASE: page("woocommerce.html")}
    pages |= {f"{BASE}{tail}/": page("builder.html")
              for tail in ("contacts", "catalog", "about")}
    session = FakeSession(pages)

    result = await ss.scrape_site(BASE, region="UA", session=session)

    assert result.ok and len(result.pages) == ss.MAX_PAGES
    assert len(session.asked) == ss.MAX_PAGES


async def test_inner_pages_add_but_do_not_overwrite():
    pages = {BASE: page("woocommerce.html"),
             f"{BASE}contacts/": page("builder.html")}
    session = FakeSession(pages)

    result = await ss.scrape_site(BASE, region="UA", session=session)

    # адрес и часы с главной остаются главными
    assert result.address["parts"]["locality"] == "Вигаданськ"
    assert result.hours[0].startswith("Monday")
    # а телефон со страницы контактов добавляется к найденному
    assert "+380440000011" in result.phones
    assert "+421550001122" in result.phones
    assert result.name == "Ліхтарик"


async def test_a_dead_site_is_data_not_an_exception():
    import aiohttp

    session = FakeSession({}, fail=aiohttp.ClientConnectionError("нет хоста"))

    result = await ss.scrape_site(BASE, session=session)

    assert not result.ok and "ClientConnection" in result.reason
    assert result.old_site_state == "broken"
    assert result.products == [] and result.images == []


async def test_a_blocked_site_is_reported_not_bypassed():
    session = FakeSession({BASE: page("cf_challenge.html")})

    result = await ss.scrape_site(BASE, session=session)

    assert not result.ok and "защитой от ботов" in result.reason
    # ни второй попытки, ни другой страницы: обходов у нас нет
    assert len(session.asked) == 1
    assert result.old_site_state == ""


async def test_download_respects_the_count_limit():
    urls = [f"{BASE}img/{n}.jpg" for n in range(20)]
    session = FakeSession({url: b"x" * 100 for url in urls})

    got = await ss.download_images(session, urls)

    assert len(got) == ss.MAX_IMAGES
    assert len(session.asked) == ss.MAX_IMAGES


async def test_download_respects_the_total_weight():
    urls = [f"{BASE}img/{n}.jpg" for n in range(6)]
    heavy = b"x" * (ss.MAX_TOTAL_BYTES // 4)
    session = FakeSession({url: heavy for url in urls})

    got = await ss.download_images(session, urls)

    assert sum(len(data) for _, data in got) <= ss.MAX_TOTAL_BYTES


async def test_an_oversized_file_is_skipped_whole():
    small, big = f"{BASE}a.jpg", f"{BASE}b.jpg"
    session = FakeSession({small: b"x" * 10,
                           big: b"x" * (ss.MAX_IMAGE_BYTES + 10)})

    got = await ss.download_images(session, [small, big])

    assert [url for url, _ in got] == [small]


async def test_excerpts_stay_within_the_token_budget():
    session = FakeSession({BASE: page("woocommerce.html")})

    result = await ss.scrape_site(BASE, session=session)

    assert result.excerpts and "<" not in " ".join(result.excerpts)
    assert sum(len(line) for line in result.excerpts) <= ss.MAX_EXCERPT_CHARS


# --- куда ходить нельзя -------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://169.254.169.254/latest/meta-data/",
    "http://10.1.2.3/", "http://[::1]/", "https://localhost/",
    "http://lihtaryk.example:6379/", "http://lihtaryk.example:8080/",
    "file:///etc/passwd", "gopher://lihtaryk.example/",
    "http://inside.lihtaryk.example/", "http://metadata.lihtaryk.example/",
])
async def test_internal_addresses_are_not_read(url):
    assert await ss.target_allowed(url) is False


@pytest.mark.parametrize("url", ["https://lihtaryk.example/",
                                 "https://lihtaryk.example:443/",
                                 f"http://{PUBLIC_IP}/"])
async def test_the_public_web_is_read_as_before(url):
    assert await ss.target_allowed(url) is True


async def test_a_page_at_an_internal_address_is_data_not_a_download():
    session = FakeSession({"http://169.254.169.254/": b"<html>secrets</html>"})

    result = await ss.scrape_site("http://169.254.169.254/", session=session)

    # ни одного запроса: гард стоит перед сетью, а не разбирает ответ
    assert not result.ok and result.reason == ss.BLOCKED_TARGET
    assert session.asked == []


async def test_a_redirect_into_the_private_network_is_stopped():
    session = FakeSession({BASE: ("302", "http://169.254.169.254/latest/"),
                           "http://169.254.169.254/latest/": b"<html>token</html>"})

    result = await ss.scrape_site(BASE, session=session)

    # первый адрес публичный, второй нет — проверяется каждый хоп цепочки
    assert not result.ok and result.reason == ss.BLOCKED_TARGET
    assert session.asked == [BASE]


async def test_an_ordinary_redirect_still_leads_to_the_page():
    moved = "https://shop.lihtaryk.example/"
    session = FakeSession({BASE: ("302", moved),
                           moved: page("woocommerce.html")})

    result = await ss.scrape_site(BASE, session=session)

    assert result.ok and result.url == moved
    assert result.name == "Ліхтарик"


async def test_a_redirect_ring_ends_by_itself():
    session = FakeSession({BASE: ("302", BASE)})

    result = await ss.scrape_site(BASE, session=session)

    assert not result.ok and "перенаправлений" in result.reason
    assert len(session.asked) == ss.MAX_REDIRECTS + 1


async def test_a_picture_from_an_internal_address_is_skipped():
    inside = "http://inside.lihtaryk.example/logo.png"
    good = f"{BASE}logo.png"
    session = FakeSession({inside: b"x" * 10, good: b"x" * 10})

    got = await ss.download_images(session, [inside, good])

    assert [url for url, _ in got] == [good]
    assert session.asked == [good]


async def test_a_picture_redirected_inside_is_skipped():
    url = f"{BASE}logo.png"
    session = FakeSession({url: ("302", "http://127.0.0.1:9200/logo.png"),
                           "http://127.0.0.1:9200/logo.png": b"x" * 10})

    assert await ss.download_images(session, [url]) == []
    assert session.asked == [url]
