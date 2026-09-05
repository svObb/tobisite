"""Обогащение карточки с сайта лида: слияние, стейджинг, ИИ-ветка.

Сети нет ни одной: обход сайта подменяет фикстура `scraped`, байты картинок
рисует Pillow, бакет — FakeR2 из conftest. Проверяется то, ради чего модуль и
написан: перескрейп не затирает работу человека, а телефон из подвала чужого
шаблона не подменяет телефон карточки.
"""
import asyncio
import io
import itertools
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy import select

import config
import costs
import draft_service
import enrich_service as es
import preview_hits
import site_images
import site_scrape
from email_gen import LOSS_KEY
from models import Contact, CostLedger, Lead, LeadEvent, Session
from site_factory.engine import color
from site_factory.engine.profile import Profile

SITE = "https://lihtaryk.example/"
LOGO_URL = f"{SITE}logo.png"
WIDE_URL = f"{SITE}vitryna.jpg"
TEAM_URL = f"{SITE}team.jpg"
BOX_URL = f"{SITE}box.jpg"
_domains = itertools.count(1)


def png(width, height, color=(194, 98, 26)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, "PNG")
    return buf.getvalue()


BLOBS = {LOGO_URL: png(400, 120), WIDE_URL: png(2400, 1200),
         TEAM_URL: png(1000, 1200), BOX_URL: png(900, 900)}

# Логотип из двух цветов: приглушённый занимает больше места, яркий — меньше.
# Порядок по частоте тут обратен порядку по насыщенности, и на этом видно,
# какой из них уходит в accent: на самом частом цвете тест бы прошёл и с
# ошибкой «accent — просто первый в палитре».
VIVID, MUTED = (220, 20, 60), (130, 120, 90)
# Логотип одного тёмного серого: цвета его не видят (нейтральное в палитру не
# идёт), а светлота видит.
GREY = (60, 60, 60)


def two_tone_logo() -> bytes:
    img = Image.new("RGB", (400, 120), MUTED)
    img.paste(Image.new("RGB", (100, 120), VIVID), (300, 0))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _hex(rgb) -> str:
    """Цвет так, как его вернёт палитра: по сетке в 32 уровня на канал."""
    return "#%02x%02x%02x" % tuple(channel // 8 * 8 for channel in rgb)


def result(**kw) -> site_scrape.ScrapeResult:
    """Полный улов с выдуманного сайта: всё, что скрейп умеет находить."""
    data = {
        "ok": True, "url": SITE, "pages": [SITE, f"{SITE}contacts/"],
        "name": "Ліхтарик",
        "phones": ["+380440000011"], "emails": ["shop@lihtaryk.example"],
        "address": {"display": "вулиця Вигадана, 4, Вигаданськ",
                    "parts": {"street": "вулиця Вигадана, 4",
                              "locality": "Вигаданськ"}},
        "hours": ["Пн–Пт: 09:00–19:00"],
        "rating": {"value": 4.8, "count": 127, "source": "jsonld"},
        "services": ["Продаж ноутбуків", "Заміна екрана"],
        "products": [{"name": "Промінь 14", "price": "24 990 грн",
                      "image": BOX_URL}],
        "logos": [{"url": LOGO_URL, "kind": "img", "weight": 40}],
        "images": [{"url": WIDE_URL, "weight": 60, "og": True,
                    "width": 2400, "height": 1200},
                   {"url": TEAM_URL, "weight": 20, "og": False,
                    "width": 1000, "height": 1200}],
        "brand_colors": {"primary": "#1f6f4a", "source": "meta"},
        "text_volume": "medium", "old_site_state": "outdated",
        "excerpts": ["Ноутбуки з гарантією", "Ремонт материнських плат"],
    }
    return site_scrape.ScrapeResult(**(data | kw))


@pytest.fixture(autouse=True)
def _no_ai(monkeypatch):
    """Как на бою: ветка выключена флагом. Кому она нужна — берёт enrich_model."""
    monkeypatch.setattr(config, "ENRICH_AI", False)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")


@pytest.fixture
def scraped(monkeypatch):
    """scraped(улов) — обход сайта и скачивание картинок без единого сокета."""
    def _install(found, blobs=None):
        store = BLOBS if blobs is None else blobs

        async def fake_scrape(url, *, region=None, session=None):
            return found

        async def fake_download(session, urls):
            wanted = list(dict.fromkeys(urls))[:site_scrape.MAX_IMAGES]
            return [(url, store[url]) for url in wanted if url in store]

        monkeypatch.setattr(site_scrape, "scrape_site", fake_scrape)
        monkeypatch.setattr(site_scrape, "download_images", fake_download)
        return found

    return _install


@pytest.fixture
def site_lead(make_lead):
    """Лид с сайтом; контакты и уже написанное обогащение задаются вызовом."""
    async def _make(*, phone=None, email=None, enrichment=None, **kw):
        async with Session() as s, s.begin():
            # домен уникален среди живых лидов — отсюда счётчик
            lead = await make_lead(
                s, website_url=SITE, enrichment=enrichment or {},
                domain_norm=f"lihtaryk-{next(_domains)}.example", **kw,
            )
            for ctype, value in (("phone", phone), ("email", email)):
                if value:
                    s.add(Contact(lead_id=lead.id, ctype=ctype, value=value))
        return lead

    return _make


async def enrichment_of(lead_id: int) -> dict:
    async with Session() as s:
        return dict((await s.get(Lead, lead_id)).enrichment or {})


async def write_by_hand(lead_id: int, **values):
    """Правка карточки человеком: _scrape.written при этом не трогается."""
    async with Session() as s, s.begin():
        lead = await s.get(Lead, lead_id)
        lead.enrichment = dict(lead.enrichment or {}) | values


async def events_of(lead_id: int, name: str) -> list:
    async with Session() as s:
        return list(await s.scalars(
            select(LeadEvent).where(LeadEvent.lead_id == lead_id,
                                    LeadEvent.event == name)
        ))


# --- слияние: чистая функция --------------------------------------------------

def test_scraper_owned_keys_are_overwritten():
    current = {"images": {"logo": {"src": "/img/logo.webp"}}, "photo_count": 3,
               "_scrape": {"written": ["images", "photo_count"]}}

    merged = es.merge_enrichment(current, {"images": {}, "photo_count": 0})

    assert merged.enrichment["images"] == {}
    assert merged.enrichment["photo_count"] == 0
    assert merged.written == ["images", "photo_count"]


def test_a_key_the_site_lost_is_dropped_not_kept():
    current = {"brand_colors": {"primary": "#111111"},
               "_scrape": {"written": ["brand_colors"]}}

    merged = es.merge_enrichment(current, {"photo_count": 0})

    assert "brand_colors" not in merged.enrichment


def test_a_value_the_scraper_never_wrote_is_never_overwritten():
    current = {"services": ["Написано руками"], "hours": ["Пн: 09:00"]}

    merged = es.merge_enrichment(current, {"services": ["С сайта"],
                                           "hours": ["Вт: 10:00"]})

    assert merged.enrichment["services"] == ["Написано руками"]
    assert merged.kept == ["hours", "services"] and merged.written == []


def test_what_the_scraper_wrote_last_time_it_may_rewrite():
    current = {"services": ["Старое с сайта"],
               "_scrape": {"written": ["services"]}}

    merged = es.merge_enrichment(current, {"services": ["Новое с сайта"]})

    assert merged.enrichment["services"] == ["Новое с сайта"]
    assert merged.written == ["services"] and merged.kept == []


def test_the_phone_of_a_card_with_contacts_is_not_touched():
    merged = es.merge_enrichment({}, {"phone": "+380440000011"},
                                 contact_types={"phone"})

    assert "phone" not in merged.enrichment and merged.kept == ["phone"]


def test_the_phone_of_a_card_without_contacts_is_promoted():
    merged = es.merge_enrichment({}, {"phone": "+380440000011"},
                                 contact_types=set())

    assert merged.enrichment["phone"] == "+380440000011"


def test_the_name_is_never_promoted():
    merged = es.merge_enrichment({}, {"name": "Совсем другое"})

    assert "name" not in merged.enrichment


def test_keys_of_other_owners_are_left_alone():
    current = {LOSS_KEY: {"lost": 4}, "review_count": 24}

    merged = es.merge_enrichment(current, {"services": ["С сайта"]})

    assert merged.enrichment[LOSS_KEY] == {"lost": 4}
    assert merged.enrichment["review_count"] == 24


# --- почта с сайта ------------------------------------------------------------

def found_email(*emails) -> str | None:
    found = es.found_fields(result(emails=list(emails)), {}, {},
                            looked_at_images=False)
    return found.get("email")


def test_an_address_hidden_behind_cyrillic_lookalikes_reaches_the_card_latin():
    """Ключ уходит прямиком в mailto клиентской страницы: чинить надо здесь."""
    assert found_email("shоp@lihtaryk.example") == "shop@lihtaryk.example"


def test_the_card_takes_the_first_address_that_is_really_latin():
    # «інфо» — не двойники, а кириллица: такой адрес пропускаем целиком
    assert found_email("інфо@lihtaryk.example",
                       "shop@lihtaryk.example") == "shop@lihtaryk.example"


def test_a_site_without_a_readable_address_gives_the_card_none():
    assert found_email("інфо@lihtaryk.example") is None
    assert found_email() is None


# --- минимум кадров: чистая функция -------------------------------------------

def frames(*names) -> dict:
    return {"images": {name: {"src": f"/img/{name}.webp", "width": 1200,
                              "height": 900} for name in names}}


def test_a_card_with_three_free_frames_needs_nothing():
    assert es.ambient_gap(frames("photo-2", "photo-3", "photo-4")) == 0


def test_the_gap_counts_what_the_composer_will_see_not_what_lies_in_the_bucket():
    """Логотип и именованные роли пулу не достаются — и нехватку не закрывают."""
    card = frames("logo", "hero_bg", "portrait", "photo-2")
    assert es.ambient_gap(card) == es.AMBIENT_TARGET - 1


def test_frames_taken_by_the_shop_window_do_not_count_as_free():
    card = frames("photo-2", "photo-3", "photo-4", "photo-5")
    card["products"] = [{"name": f"Товар {n}",
                         "image": {"src": f"/img/photo-{n}.webp", "width": 800,
                                   "height": 800}} for n in (3, 4, 5)]
    assert es.ambient_gap(card) == es.AMBIENT_TARGET - 1


def test_a_frame_of_a_single_product_is_not_free_either():
    """Витрина на один товар не собирается — предметный снимок всё равно занят."""
    card = frames("photo-2", "photo-3")
    card["products"] = [{"name": "Промінь 14",
                         "image": {"src": "/img/photo-2.webp", "width": 900,
                                   "height": 900}}]
    assert es.ambient_gap(card) == es.AMBIENT_TARGET - 1


def test_a_card_whose_pictures_were_never_examined_says_nothing():
    """Ключа images нет — сказать «кадров мало» не о чем: это не ноль кадров."""
    assert es.ambient_gap({"services": ["Заміна екрана"]}) == 0
    assert es.ambient_gap({}) == 0


def test_the_brief_names_the_niche_and_forbids_what_cannot_be_drawn():
    brief = es.ambient_brief("Кафе/ресторан")
    assert "зал" in brief and es.AMBIENT_RULE in brief
    assert es.AMBIENT_DEFAULT in es.ambient_brief("Ветеринарная клиника")


def test_every_niche_of_the_bot_has_a_subject_of_its_own():
    assert sorted(es.AMBIENT_SUBJECTS) == sorted(config.NICHES)


# --- прогон целиком -----------------------------------------------------------

async def test_an_empty_card_gets_everything(site_lead, scraped, r2):
    lead = await site_lead()
    scraped(result())

    got = await es.enrich_from_site(lead.id)

    assert got.ok and got.pages == 2, got.reason
    data = await enrichment_of(lead.id)
    assert data["services"] == ["Продаж ноутбуків", "Заміна екрана"]
    assert data["service_count"] == 2 and data["has_hours"] is True
    assert data["address"] == "вулиця Вигадана, 4, Вигаданськ"
    assert data["address_parts"]["locality"] == "Вигаданськ"
    assert data["brand_colors"] == {"primary": "#1f6f4a", "source": "meta"}
    assert data["old_site_state"] == "outdated"
    assert data["_scrape"]["written"] == got.written
    assert data["_scrape"]["found"]["name"] == "Ліхтарик"
    assert len(await events_of(lead.id, "site_scraped")) == 1


async def test_images_are_staged_under_the_lead_prefix(site_lead, scraped, r2):
    lead = await site_lead()
    scraped(result())

    got = await es.enrich_from_site(lead.id)

    names = ["hero_bg", "logo", "photo-2", "portrait"]
    assert got.staged == names
    assert sorted(r2.objects) == [f"_enrich/{lead.id}/img/{n}.webp"
                                  for n in names]
    data = await enrichment_of(lead.id)
    assert data["images"]["logo"] == {
        "src": "/img/logo.webp", "width": 400, "height": 120,
        "colors": site_images.dominant_colors(BLOBS[LOGO_URL]),
        "lightness": site_images.mean_lightness(BLOBS[LOGO_URL])}
    assert (data["images"]["hero_bg"]["width"]
            == site_images.ROLE_MAX_SIDE["background"])
    # инвариант сшивки: photo_count — контентные фото, логотип в них не входит
    assert data["photo_count"] == 3 == len(data["images"]) - 1
    # цвета и светлоту несёт только логотип: у остальных кадров их не считают
    assert all(set(item) == {"src", "width", "height"}
               for name, item in data["images"].items() if name != "logo")


async def test_a_site_without_a_single_free_frame_is_asked_for_ambient(site_lead,
                                                                       scraped,
                                                                       r2):
    """Фон, портрет и снимок товара пулу не достаются — свободных кадров ноль."""
    lead = await site_lead()
    scraped(result())

    got = await es.enrich_from_site(lead.id)

    assert got.ambient_need == es.AMBIENT_TARGET
    # ниша лида — стоматология, и промпт говорит именно про её помещение
    assert "кабинет" in got.ambient_brief and es.AMBIENT_RULE in got.ambient_brief


async def test_a_site_with_frames_to_spare_is_asked_for_nothing(site_lead,
                                                                scraped, r2):
    urls = [f"{SITE}zal-{n}.jpg" for n in range(4)]
    lead = await site_lead()
    scraped(result(products=[], images=[{"url": url, "weight": 30, "og": False,
                                         "width": 1200, "height": 900}
                                        for url in urls]),
            {LOGO_URL: BLOBS[LOGO_URL]} | {url: png(1200, 900) for url in urls})

    got = await es.enrich_from_site(lead.id)

    assert got.staged == ["logo", "photo-2", "photo-3", "photo-4", "portrait"]
    assert got.ambient_need == 0 and got.ambient_brief == ""


async def test_products_point_at_staged_files(site_lead, scraped, r2):
    lead = await site_lead()
    scraped(result())

    await es.enrich_from_site(lead.id)

    product, = (await enrichment_of(lead.id))["products"]
    assert product["name"] == "Промінь 14"
    assert product["price"] == "24 990 грн"
    assert product["image"] == {"src": "/img/photo-2.webp", "width": 900,
                                "height": 900}


async def test_a_product_whose_picture_did_not_make_it_has_none(site_lead,
                                                                scraped, r2):
    lead = await site_lead()
    scraped(result(products=[{"name": "Промінь 14", "price": "24 990 грн",
                              "image": f"{SITE}gone.jpg"}]))

    await es.enrich_from_site(lead.id)

    product, = (await enrichment_of(lead.id))["products"]
    # ссылка на чужой хост в превью не поедет: секцию отсеет гейт has_image
    assert "image" not in product


async def test_the_card_phone_wins_and_the_difference_is_reported(site_lead,
                                                                  scraped, r2):
    lead = await site_lead(phone="+380 44 111 22 33")
    scraped(result())

    got = await es.enrich_from_site(lead.id)

    assert "phone" not in await enrichment_of(lead.id)
    assert got.phone_diff == "+380440000011" and "phone" in got.kept


async def test_a_second_scrape_keeps_what_a_person_wrote(site_lead, scraped,
                                                         r2):
    lead = await site_lead()
    scraped(result(hours=[]))                     # часов на сайте не нашли
    assert (await es.enrich_from_site(lead.id)).ok
    await write_by_hand(lead.id, hours=["Пн–Сб: по домовленості"])
    scraped(result())                             # а на этот раз нашли

    got = await es.enrich_from_site(lead.id)

    data = await enrichment_of(lead.id)
    assert data["hours"] == ["Пн–Сб: по домовленості"] and "hours" in got.kept
    # а картинки скрейп по-прежнему ведёт сам
    assert data["images"] and "images" in got.written


async def test_a_repeat_scrape_changes_nothing_but_the_timestamp(site_lead,
                                                                 scraped, r2):
    lead = await site_lead()
    scraped(result())
    assert (await es.enrich_from_site(lead.id)).ok
    before = await enrichment_of(lead.id)

    assert (await es.enrich_from_site(lead.id)).ok

    after = await enrichment_of(lead.id)
    before["_scrape"].pop("at")
    after["_scrape"].pop("at")
    assert after == before


async def test_a_dead_site_leaves_the_card_byte_for_byte(site_lead, scraped,
                                                         r2):
    lead = await site_lead(enrichment={"services": ["Своё"]})
    scraped(site_scrape.ScrapeResult(ok=False, old_site_state="broken",
                                     reason="https: ClientConnectorError"))

    got = await es.enrich_from_site(lead.id)

    assert not got.ok and "ClientConnector" in got.reason
    assert await enrichment_of(lead.id) == {"services": ["Своё"]}
    assert not r2.objects and not await events_of(lead.id, "site_scraped")


async def test_a_blocked_site_is_reported_honestly(site_lead, scraped, r2):
    lead = await site_lead()
    scraped(site_scrape.ScrapeResult(ok=False,
                                     reason="сайт закрыт защитой от ботов"))

    got = await es.enrich_from_site(lead.id)

    assert not got.ok and got.reason == "сайт закрыт защитой от ботов"


async def test_a_lead_without_a_site_is_refused(make_lead, scraped):
    async with Session() as s, s.begin():
        lead = await make_lead(s)
    scraped(result())

    got = await es.enrich_from_site(lead.id)

    assert not got.ok and got.reason == "у лида нет сайта"


async def test_without_r2_the_text_still_arrives(site_lead, scraped,
                                                 monkeypatch):
    for name in draft_service.R2_ENV:
        monkeypatch.delenv(name, raising=False)
    lead = await site_lead()
    scraped(result())

    got = await es.enrich_from_site(lead.id)

    assert got.ok and got.staged == []
    assert "ключи R2" in got.images_reason
    data = await enrichment_of(lead.id)
    assert data["services"] and data["address"]
    # «фотографий ноль» — утверждение, которого мы не делали
    assert "images" not in data and "photo_count" not in data


async def test_a_site_without_usable_pictures_says_so(site_lead, scraped, r2):
    lead = await site_lead()
    scraped(result(logos=[], images=[], products=[]), blobs={})

    got = await es.enrich_from_site(lead.id)

    assert got.ok and got.staged == []
    assert "не нашлось" in got.images_reason
    # логотипа на сайте не было вовсе — объяснять тут нечего
    assert got.logo_note == ""
    data = await enrichment_of(lead.id)
    assert data["images"] == {} and data["photo_count"] == 0


async def test_a_logo_that_did_not_survive_the_download_says_so(site_lead,
                                                                scraped, r2):
    """Иначе отчёт говорит «логотипа нет» и о сайте без логотипа, и об этом."""
    lead = await site_lead()
    # кандидат в шапке был, но картинка мельче 64px — на страницу не годится
    scraped(result(images=[], products=[]), blobs={LOGO_URL: png(40, 20)})

    got = await es.enrich_from_site(lead.id)

    assert got.staged == [] and "кандидатов 1" in got.logo_note
    assert "взять руками" in got.logo_note


async def test_stale_staging_is_swept_before_the_new_one(site_lead, scraped,
                                                         r2):
    lead = await site_lead()
    scraped(result())
    assert (await es.enrich_from_site(lead.id)).ok
    ghost = f"_enrich/{lead.id}/img/photo-9.webp"
    r2.objects[ghost] = "c прошлого обхода".encode()

    assert (await es.enrich_from_site(lead.id)).ok

    # иначе призрак уехал бы в публикацию вместе с настоящими файлами
    assert ghost not in r2.objects


async def test_the_walk_is_logged_as_a_free_call(site_lead, scraped, r2):
    lead = await site_lead()
    scraped(result())

    await es.enrich_from_site(lead.id)

    async with Session() as s:
        rows = list(await s.scalars(
            select(CostLedger).where(CostLedger.lead_id == lead.id)
        ))
    assert [row.op for row in rows] == [es.SCRAPE_OP]
    assert rows[0].cost_usd == 0 and rows[0].api_calls == 1


async def test_one_lead_is_enriched_once_at_a_time(site_lead, monkeypatch, r2):
    lead = await site_lead()
    gate = asyncio.Event()

    async def slow_scrape(url, *, region=None, session=None):
        await gate.wait()
        return result()

    async def no_images(session, urls):
        return []

    monkeypatch.setattr(site_scrape, "scrape_site", slow_scrape)
    monkeypatch.setattr(site_scrape, "download_images", no_images)

    first = asyncio.create_task(es.enrich_from_site(lead.id))
    for _ in range(200):
        if es.enrich_busy(lead.id):
            break
        await asyncio.sleep(0.01)
    second = await es.enrich_from_site(lead.id)
    gate.set()

    assert (await first).ok
    assert not second.ok and second.reason == "этот лид уже обогащается"
    assert not es.enrich_busy(lead.id)


async def test_two_taps_at_once_start_only_one_walk(site_lead, monkeypatch, r2):
    """Два нажатия кнопки подряд: между проверкой и захватом лида нет щели."""
    lead = await site_lead()
    walks = []

    async def slow_scrape(url, *, region=None, session=None):
        walks.append(url)
        await asyncio.sleep(0)     # обход уступает управление, как настоящий
        return result()

    async def only_the_logo(session, urls):
        return [(LOGO_URL, BLOBS[LOGO_URL])]

    monkeypatch.setattr(site_scrape, "scrape_site", slow_scrape)
    monkeypatch.setattr(site_scrape, "download_images", only_the_logo)

    first, second = await asyncio.gather(es.enrich_from_site(lead.id),
                                         es.enrich_from_site(lead.id))

    assert first.ok and not second.ok
    assert second.reason == "этот лид уже обогащается"
    # оба прогона снесли бы стейджинг друг друга на полпути
    assert walks == [SITE]
    assert sorted(r2.objects) == [es.image_key(lead.id, "logo.webp")]
    assert list((await enrichment_of(lead.id))["images"]) == ["logo"]
    assert not es.enrich_busy(lead.id)


# --- рейтинг ------------------------------------------------------------------

async def test_the_rating_of_the_company_reaches_the_card(site_lead, scraped, r2):
    lead = await site_lead()
    scraped(result())

    got = await es.enrich_from_site(lead.id)

    assert (await enrichment_of(lead.id))["rating"] == {
        "value": 4.8, "count": 127, "source": "jsonld"}
    assert "rating" in got.written and got.rating == "4.8 (127 отзывов)"


async def test_a_rating_written_by_hand_survives_a_second_scrape(site_lead,
                                                                 scraped, r2):
    """Рейтинг ведут те же три правила, что услуги и часы: правка человека выше."""
    lead = await site_lead()
    scraped(result(rating={}))
    assert (await es.enrich_from_site(lead.id)).ok
    await write_by_hand(lead.id, rating={"value": 4.9, "count": 30,
                                         "source": "google"})
    scraped(result())

    got = await es.enrich_from_site(lead.id)

    assert (await enrichment_of(lead.id))["rating"]["source"] == "google"
    assert "rating" in got.kept and got.rating == "4.9 (30 отзывов)"


async def test_a_site_without_a_rating_reports_it_as_not_found(site_lead,
                                                               scraped, r2):
    lead = await site_lead()
    scraped(result(rating={}))

    got = await es.enrich_from_site(lead.id)

    assert "rating" not in await enrichment_of(lead.id)
    assert "рейтинг" in got.empty and got.rating == ""


def test_the_rating_note_counts_reviews_in_words():
    note = es.rating_note
    assert note({"value": 4.0, "count": 1}) == "4 (1 отзыв)"
    assert note({"value": 4.8, "count": 3}) == "4.8 (3 отзыва)"
    assert note({"value": 5, "count": 127}) == "5 (127 отзывов)"
    assert note({"value": 4.8}) == "" and note(None) == ""


# --- цвета бренда -------------------------------------------------------------

async def test_the_logo_gives_the_brand_colours_when_the_page_has_none(
        site_lead, scraped, r2):
    """У конструкторов theme-color пуст, и единственный след бренда — логотип."""
    lead = await site_lead()
    scraped(result(brand_colors={}, images=[], products=[]),
            blobs={LOGO_URL: two_tone_logo()})

    await es.enrich_from_site(lead.id)

    colors = (await enrichment_of(lead.id))["brand_colors"]
    palette = site_images.dominant_colors(two_tone_logo())
    assert len(palette) == 2 and colors["source"] == "logo"
    # primary — самый частый цвет логотипа, им залита большая часть картинки
    assert colors["primary"] == palette[0] == _hex(MUTED)
    # accent — самый насыщенный, а не первый по частоте: движок ставит его на
    # кнопки и ссылки, и приглушённый оттенок оставляет страницу без бренда
    assert colors["accent"] == palette[1] == _hex(VIVID)
    assert color.chroma(color.srgb(palette[1])) > color.chroma(
        color.srgb(palette[0]))


async def test_the_colours_of_the_logo_travel_with_the_logo_itself(site_lead,
                                                                   scraped, r2):
    """По ним движок решает, ляжет ли шапка на кадр первого экрана.

    Цвета бренда для этого не годятся: сайт объявляет их сам (meta theme-color)
    и о самой картинке они не говорят ничего.
    """
    lead = await site_lead()
    scraped(result(images=[], products=[]), blobs={LOGO_URL: two_tone_logo()})

    await es.enrich_from_site(lead.id)

    logo = (await enrichment_of(lead.id))["images"]["logo"]
    assert logo["colors"] == site_images.dominant_colors(two_tone_logo())
    # сквозь стык дорожек: движок читает цвета из той же записи
    assert draft_service._clean_image(logo)["colors"] == logo["colors"]


async def test_a_logo_of_one_grey_travels_by_its_lightness(site_lead, scraped,
                                                           r2):
    """Чёрно-белый логотип цветов не даёт вовсе, и без светлоты он был бы нем.

    Такой логотип у малого бизнеса самый частый, а на тёмном скриме первого
    экрана чёрные буквы пропадают ровно так же, как тёмно-бирюзовые.
    """
    lead = await site_lead()
    scraped(result(brand_colors={}, images=[], products=[]),
            blobs={LOGO_URL: png(400, 120, GREY)})

    await es.enrich_from_site(lead.id)

    data = await enrichment_of(lead.id)
    logo = data["images"]["logo"]
    assert "colors" not in logo and "brand_colors" not in data
    assert logo["lightness"] == site_images.mean_lightness(png(400, 120, GREY))
    # сквозь стык дорожек: шлюз черновика пропускает светлоту, и профиль по
    # ней одной судит о логотипе
    profile = Profile.from_dict({
        "domain_norm": "lead.example",
        "images": {"logo": draft_service._clean_image(logo)}})
    assert profile.feature("logo_is_dark").value is True


async def test_the_page_colours_win_over_the_logo(site_lead, scraped, r2):
    lead = await site_lead()
    scraped(result(images=[], products=[]), blobs={LOGO_URL: two_tone_logo()})

    await es.enrich_from_site(lead.id)

    # meta theme-color сайт объявляет о себе сам, логотип — догадка по пикселям
    assert (await enrichment_of(lead.id))["brand_colors"] == {
        "primary": "#1f6f4a", "source": "meta"}


async def test_without_a_logo_there_are_no_brand_colours(site_lead, scraped,
                                                          r2):
    lead = await site_lead()
    scraped(result(brand_colors={}, logos=[]),
            blobs={WIDE_URL: BLOBS[WIDE_URL]})

    await es.enrich_from_site(lead.id)

    assert "brand_colors" not in await enrichment_of(lead.id)


# --- амбиент: картинка, которую положили мы, а не сайт ------------------------
#
# Фон первого экрана иногда дорисовывают руками, и до этой волны повторное
# «Обогатить» стирало его вместе со всем стейджингом — так у боевого лида
# vortex потеряли hero_bg.

AMBIENT = {"src": "/img/hero_bg.webp", "width": 1920, "height": 1080}


def ambient_card() -> dict:
    """Карточка, в которой амбиент уже лежит: запись, счёт и служебный список."""
    return {"images": {"hero_bg": dict(AMBIENT)}, "photo_count": 1,
            es.AMBIENT_KEY: ["hero_bg"]}


def put_ambient(r2, lead_id: int) -> str:
    key = es.image_key(lead_id, "hero_bg.webp")
    r2.objects[key] = b"nakres ambient"
    return key


async def test_the_ambient_survives_a_second_enrichment(site_lead, scraped, r2):
    lead = await site_lead(enrichment=ambient_card())
    key = put_ambient(r2, lead.id)
    scraped(result(images=[], products=[]), blobs={LOGO_URL: BLOBS[LOGO_URL]})

    got = await es.enrich_from_site(lead.id)

    assert r2.objects.get(key) == b"nakres ambient"
    data = await enrichment_of(lead.id)
    assert data["images"]["hero_bg"] == AMBIENT
    # счёт прежней семантики: контентные фото без логотипа
    assert data["photo_count"] == 1 and set(data["images"]) == {"logo", "hero_bg"}
    assert data[es.AMBIENT_KEY] == ["hero_bg"] and got.ambient == ["hero_bg"]


async def test_a_real_photo_pushes_the_ambient_out(site_lead, scraped, r2):
    """Настоящее фото компании лучше нарисованного — правило основателя."""
    lead = await site_lead(enrichment=ambient_card())
    key = put_ambient(r2, lead.id)
    scraped(result())                             # у сайта своё широкое фото

    got = await es.enrich_from_site(lead.id)

    assert r2.objects[key] != b"nakres ambient"
    data = await enrichment_of(lead.id)
    assert data["images"]["hero_bg"]["width"] != AMBIENT["width"]
    assert data[es.AMBIENT_KEY] == [] and got.ambient == []


async def test_without_the_ambient_key_the_staging_is_swept_as_before(site_lead,
                                                                      scraped,
                                                                      r2):
    lead = await site_lead()
    scraped(result())
    assert (await es.enrich_from_site(lead.id)).ok
    stray = es.image_key(lead.id, "hero_bg-by-hand.webp")
    r2.objects[stray] = b"ne ambient"

    got = await es.enrich_from_site(lead.id)

    assert stray not in r2.objects and got.ambient == []
    assert es.AMBIENT_KEY not in await enrichment_of(lead.id)


async def test_closing_the_lead_takes_the_ambient_down_too(site_lead, scraped,
                                                           r2):
    """Амбиент лежит в том же префиксе — уборка закрытого лида сносит и его."""
    lead = await site_lead(enrichment=ambient_card(), status="rejected")
    key = put_ambient(r2, lead.id)
    scraped(result(images=[], products=[]), blobs={LOGO_URL: BLOBS[LOGO_URL]})
    assert (await es.enrich_from_site(lead.id)).ok
    assert key in r2.objects

    swept = await draft_service.sweep_staging()

    assert lead.id in swept
    assert not [k for k in r2.objects
                if k.startswith(es.staging_prefix(lead.id))]


def test_the_ambient_key_does_not_reach_the_engine_profile():
    """Служебный ключ с подчёркиванием в профиль не проходит — как и _scrape."""
    assert es.AMBIENT_KEY.startswith("_")
    assert es.AMBIENT_KEY not in draft_service.ENRICHMENT_FIELDS


# --- стейджинг не состоялся ---------------------------------------------------
#
# Ровно тот же инцидент с другой стороны: R2 не ответил, скрейп картинок не
# видел — и старое правило «прошлый скрейп писал, нынешний не видит» стирало из
# карточки фотографии, которые в бакете живы. Не видел и не смотрел — разное.

BLIND = RuntimeError("бакет недоступен")


def walked_card() -> dict:
    """Карточка после удачного обогащения: амбиент плюс журнал скрейпа."""
    return ambient_card() | {
        es.SCRAPE_KEY: {"written": ["images", "photo_count"]}}


async def test_a_failed_staging_keeps_the_pictures_of_the_card(site_lead,
                                                               scraped, r2):
    lead = await site_lead(enrichment=walked_card())
    key = put_ambient(r2, lead.id)
    scraped(result())
    r2.fail = BLIND

    got = await es.enrich_from_site(lead.id)

    assert got.ok and got.staged == []
    data = await enrichment_of(lead.id)
    assert data["images"] == {"hero_bg": AMBIENT} and data["photo_count"] == 1
    assert data[es.AMBIENT_KEY] == ["hero_bg"]
    # принадлежность скрейпу переносится вперёд: иначе следующий удачный
    # прогон счёл бы уцелевшее ручной правкой и больше не переписал
    assert "images" in data[es.SCRAPE_KEY]["written"]
    assert r2.objects[key] == b"nakres ambient"


async def test_without_r2_keys_the_pictures_of_the_card_stay_too(site_lead,
                                                                 scraped,
                                                                 monkeypatch):
    for name in draft_service.R2_ENV:
        monkeypatch.delenv(name, raising=False)
    lead = await site_lead(enrichment=walked_card())
    scraped(result())

    got = await es.enrich_from_site(lead.id)

    assert got.ok and got.staged == []
    data = await enrichment_of(lead.id)
    assert data["images"] == {"hero_bg": AMBIENT} and data["photo_count"] == 1
    assert "images" in data[es.SCRAPE_KEY]["written"]


async def test_a_good_run_after_a_failed_one_rewrites_as_usual(site_lead,
                                                               scraped, r2):
    lead = await site_lead(enrichment=walked_card())
    put_ambient(r2, lead.id)
    scraped(result())
    r2.fail = BLIND
    assert (await es.enrich_from_site(lead.id)).ok
    r2.fail = None

    got = await es.enrich_from_site(lead.id)

    assert got.ok and "hero_bg" in got.staged
    data = await enrichment_of(lead.id)
    assert data["images"]["hero_bg"]["width"] != AMBIENT["width"]
    assert data[es.AMBIENT_KEY] == []


async def test_a_good_run_after_a_failed_one_brings_the_ambient_back(site_lead,
                                                                     scraped,
                                                                     r2):
    lead = await site_lead(enrichment=walked_card())
    key = put_ambient(r2, lead.id)
    scraped(result(images=[], products=[]), blobs={LOGO_URL: BLOBS[LOGO_URL]})
    r2.fail = BLIND
    assert (await es.enrich_from_site(lead.id)).ok
    r2.fail = None

    got = await es.enrich_from_site(lead.id)

    assert got.ok and r2.objects.get(key) == b"nakres ambient"
    data = await enrichment_of(lead.id)
    assert data["images"]["hero_bg"] == AMBIENT
    assert set(data["images"]) == {"logo", "hero_bg"}
    assert data["photo_count"] == 1 and got.ambient == ["hero_bg"]


async def test_the_report_says_what_survived_the_failed_staging(site_lead,
                                                                scraped, r2):
    """Без этой строки отчёт молчит о том, цела ли работа предыдущих прогонов."""
    from handlers_admin import enrich_report

    lead = await site_lead(enrichment=walked_card())
    put_ambient(r2, lead.id)
    scraped(result())
    r2.fail = BLIND

    got = await es.enrich_from_site(lead.id)

    assert "прежнее в карточке сохранено: картинки, число фото" in got.images_reason
    assert "прежнее в карточке сохранено" in enrich_report(got)


def test_an_unexamined_key_is_neither_written_nor_dropped():
    current = {"products": [{"name": "Промінь 14"}],
               "_scrape": {"written": ["products"]}}

    merged = es.merge_enrichment(current, {}, unexamined=es.SCRAPER_OWNED)

    assert merged.enrichment["products"] == current["products"]
    assert merged.written == ["products"] and merged.kept == []


def test_an_unexamined_key_the_card_never_had_stays_absent():
    merged = es.merge_enrichment({}, {}, unexamined=es.SCRAPER_OWNED)

    assert merged.enrichment == {} and merged.written == []


# --- логотип, нарисованный прямо в HTML ---------------------------------------

def svg_logo(markup: str) -> dict:
    return {"url": "", "kind": "svg", "markup": markup, "weight": 35}


async def test_an_inline_svg_logo_takes_its_size_from_the_view_box(site_lead,
                                                                   scraped, r2):
    lead = await site_lead()
    scraped(result(logos=[svg_logo(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 64.4">'
        '<path d="M0 0h240v64H0z"/></svg>')], images=[], products=[]),
        blobs={})

    got = await es.enrich_from_site(lead.id)

    assert got.staged == ["logo"] and got.logo_note == ""
    logo = (await enrichment_of(lead.id))["images"]["logo"]
    assert (logo["width"], logo["height"]) == (240, 64)
    # сквозь стык дорожек: запись без размеров движок выбрасывает молча
    assert draft_service._clean_image(logo) == {"src": logo["src"],
                                                "width": 240, "height": 64}


async def test_an_inline_svg_logo_prefers_its_own_width_and_height(site_lead,
                                                                   scraped, r2):
    lead = await site_lead()
    scraped(result(logos=[svg_logo(
        '<svg xmlns="http://www.w3.org/2000/svg" width="180px" height="48px" '
        'viewBox="0 0 999 999"><path d="M0 0h180v48H0z"/></svg>')],
        images=[], products=[]), blobs={})

    await es.enrich_from_site(lead.id)

    logo = (await enrichment_of(lead.id))["images"]["logo"]
    assert (logo["width"], logo["height"]) == (180, 48)


async def test_an_inline_svg_without_a_size_is_left_to_hands(site_lead,
                                                             scraped, r2):
    lead = await site_lead()
    scraped(result(logos=[svg_logo(
        '<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="auto">'
        '<path d="M0 0h10v10H0z"/></svg>')], images=[], products=[]), blobs={})

    got = await es.enrich_from_site(lead.id)

    # «логотип есть» при пустой шапке превью — ровно то, чего быть не должно
    assert got.staged == [] and not r2.objects
    assert got.logo_note == "SVG-логотип без размеров — взять руками"
    assert (await enrichment_of(lead.id))["images"] == {}


async def test_an_inline_svg_the_sanitiser_refused_is_left_to_hands(site_lead,
                                                                    scraped,
                                                                    r2):
    """Битый SVG — не «логотипа нет»: у инлайнового кандидата ссылки нет вовсе.

    Растровая строка отчёта считает кандидатов по ссылкам, а их тут ноль, — и
    без своей строки такой сайт был бы неотличим от сайта без логотипа.
    """
    lead = await site_lead()
    scraped(result(logos=[svg_logo(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 64">'
        '<path d="M0 0h240v64H0z"></svg>')], images=[], products=[]), blobs={})

    got = await es.enrich_from_site(lead.id)

    assert got.staged == [] and not r2.objects
    assert got.logo_note == "SVG-логотип не прошёл проверку — взять руками"
    assert (await enrichment_of(lead.id))["images"] == {}


async def test_the_card_line_says_what_came_from_the_site(site_lead, scraped,
                                                          r2):
    lead = await site_lead()
    scraped(result())
    await es.enrich_from_site(lead.id)

    async with Session() as s:
        line = es.enrich_line(await s.get(Lead, lead.id))

    assert "страниц 2" in line and "логотип есть" in line
    assert "фото 3" in line and "услуг 2" in line and "товаров 1" in line


def test_a_card_nobody_scraped_has_no_line():
    assert es.enrich_line(None) == ""


def test_hours_written_by_hand_as_one_string_count_as_one_line():
    """Строку в карточке пишет человек, и len() дал бы число её символов."""
    lead = SimpleNamespace(enrichment={
        "_scrape": {"at": "2026-08-27T10:00:00", "pages": [SITE]},
        "hours": "Пн–Сб: по домовленості, неділя вихідний",
    })

    assert "строк часов 1" in es.enrich_line(lead)


# --- ИИ-ветка -----------------------------------------------------------------

THIN = {"services": [], "hours": [], "text_volume": "long"}
ANSWER = ('{"services": ["Ремонт даху", "Утеплення фасаду"], '
          '"hours": ["Пн–Пт: 08:00–17:00"]}')


async def test_the_model_is_not_asked_when_the_dom_did_its_job(site_lead,
                                                               scraped, r2,
                                                               enrich_model):
    lead = await site_lead()
    scraped(result())
    fake = enrich_model(ANSWER)

    got = await es.enrich_from_site(lead.id)

    assert got.ai_note == "" and fake.messages.calls == []


async def test_the_model_fills_what_the_dom_could_not(site_lead, scraped, r2,
                                                      enrich_model):
    lead = await site_lead()
    scraped(result(**THIN))
    fake = enrich_model(ANSWER)

    got = await es.enrich_from_site(lead.id)

    data = await enrichment_of(lead.id)
    assert data["services"] == ["Ремонт даху", "Утеплення фасаду"]
    assert data["hours"] == ["Пн–Пт: 08:00–17:00"]
    assert got.ai_note.startswith("дополнила: ")
    call = fake.messages.calls[0]
    prompt = call["messages"][0]["content"]
    assert prompt.startswith("<site>") and "Ноутбуки з гарантією" in prompt
    # системный промпт строкой, без блоков с cache_control: вызов на лида один
    assert isinstance(call["system"], str)


async def test_the_model_call_is_its_own_line_in_costs(site_lead, scraped, r2,
                                                       enrich_model):
    lead = await site_lead()
    scraped(result(**THIN))
    enrich_model(ANSWER)

    await es.enrich_from_site(lead.id)

    async with Session() as s:
        ops = list(await s.scalars(
            select(CostLedger.op).where(CostLedger.lead_id == lead.id)
        ))
    assert sorted(ops) == sorted([es.SCRAPE_OP, es.COST_OP])


async def test_the_cap_stops_the_model_before_the_call(site_lead, scraped, r2,
                                                       enrich_model,
                                                       monkeypatch):
    lead = await site_lead()
    scraped(result(**THIN))
    fake = enrich_model(ANSWER)

    async def reached():
        return True

    monkeypatch.setattr(costs, "cap_reached", reached)
    got = await es.enrich_from_site(lead.id)

    assert fake.messages.calls == [] and "кэп" in got.ai_note
    # деградация на результат DOM: обогащение всё равно состоялось
    assert got.ok and (await enrichment_of(lead.id))["images"]


async def test_the_flag_is_off_and_the_model_is_not_asked(site_lead, scraped,
                                                          r2, enrich_model,
                                                          monkeypatch):
    lead = await site_lead()
    scraped(result(**THIN))                       # ровно то, что зовёт модель
    fake = enrich_model(ANSWER)
    monkeypatch.setattr(config, "ENRICH_AI", False)

    got = await es.enrich_from_site(lead.id)

    assert fake.messages.calls == [] and "ENRICH_AI" in got.ai_note
    # обогащение состоялось: услуги и часы просто остались теми, что нашёл DOM
    assert got.ok and (await enrichment_of(lead.id))["images"]


async def test_without_a_key_the_branch_simply_is_not_there(site_lead, scraped,
                                                            r2, monkeypatch):
    monkeypatch.setattr(config, "ENRICH_AI", True)
    lead = await site_lead()
    scraped(result(**THIN))

    got = await es.enrich_from_site(lead.id)

    assert got.ok and "ANTHROPIC_API_KEY" in got.ai_note


async def test_a_broken_answer_degrades_to_the_dom(site_lead, scraped, r2,
                                                   enrich_model):
    lead = await site_lead()
    scraped(result(services=["Одна послуга"], hours=[], text_volume="long"))
    enrich_model("не json вовсе")

    got = await es.enrich_from_site(lead.id)

    assert got.ok and got.ai_note == "ответила не по формату"
    assert (await enrichment_of(lead.id))["services"] == ["Одна послуга"]


async def test_junk_inside_the_answer_is_filtered(site_lead, scraped, r2,
                                                  enrich_model):
    lead = await site_lead()
    scraped(result(**THIN))
    enrich_model('{"services": ["Ремонт даху", 12, "", "  ", null], '
                 '"hours": "не список"}')

    await es.enrich_from_site(lead.id)

    data = await enrichment_of(lead.id)
    assert data["services"] == ["Ремонт даху"] and "hours" not in data


async def test_a_long_answer_is_capped(site_lead, scraped, r2, enrich_model):
    lead = await site_lead()
    scraped(result(**THIN))
    enrich_model(json.dumps({"services": [f"Послуга {n}" for n in range(30)],
                             "hours": [f"День {n}: 09:00" for n in range(20)]}))

    await es.enrich_from_site(lead.id)

    data = await enrichment_of(lead.id)
    assert len(data["services"]) == site_scrape.MAX_SERVICES
    assert len(data["hours"]) == site_scrape.MAX_HOURS


# --- отчёт админу -------------------------------------------------------------

def test_the_report_shows_the_rating_with_its_numbers():
    """«рейтинг» одним словом не даёт сверить оценку с сайтом глазами."""
    from handlers_admin import enrich_report

    text = enrich_report(es.EnrichResult(
        ok=True, pages=2, written=["hours", "rating"],
        rating="4.8 (127 отзывов)"))

    assert "Записано: часы, рейтинг 4.8 (127 отзывов)" in text


def test_the_report_says_when_the_ambient_was_kept():
    from handlers_admin import enrich_report

    text = enrich_report(es.EnrichResult(ok=True, pages=1, staged=["logo"],
                                         ambient=["hero_bg"]))

    assert "Амбиент сохранён: hero_bg" in text


def test_the_report_asks_for_the_frames_it_lacks_and_says_what_to_draw():
    """Кадры рисует человек: бот называет число, промпт и команду выкладки."""
    from handlers_admin import enrich_report

    text = enrich_report(es.EnrichResult(
        ok=True, lead_id=417, pages=1, staged=["logo", "photo-2"],
        ambient_need=2, ambient_brief=es.ambient_brief("Салон красоты")))

    assert "Нужно 2 амбиент-кадра, промпт:" in text
    assert "кресло у зеркала" in text
    # команд столько, сколько кадров не хватает, и роль у каждой своя
    assert text.count("python -m ambient_stage 417") == 2
    assert "--role ambient-1" in text and "--role ambient-2" in text


def test_the_report_does_not_send_the_worker_over_a_frame_already_staged():
    """ambient-1 уже лежит: PUT идёт по имени роли, и повтор стёр бы этот кадр."""
    from handlers_admin import enrich_report

    text = enrich_report(es.EnrichResult(
        ok=True, lead_id=417, pages=1, ambient=["hero_bg", "ambient-1"],
        ambient_need=2, ambient_brief=es.ambient_brief("Салон красоты")))

    assert "--role ambient-1" not in text
    assert "--role ambient-2" in text and "--role ambient-3" in text


def test_a_report_about_a_page_with_enough_frames_asks_for_nothing():
    from handlers_admin import enrich_report

    text = enrich_report(es.EnrichResult(ok=True, pages=1, staged=["photo-2"]))

    assert "амбиент" not in text.lower()


# --- контракты ----------------------------------------------------------------

def test_prompt_version_and_op_are_pinned():
    """Версия промпта и строки в /costs — контракт: правятся вместе с промптом."""
    assert es.PROMPT_VERSION == "e1" and es.COST_OP == "enrich"
    assert es.SCRAPE_OP == "scrape" and es.SCRAPE_OP not in config.API_PRICES


def test_the_report_names_every_key_it_may_write():
    """Ключ без подписи уехал бы в отчёт админу схемным именем."""
    touched = set(es.SCRAPER_OWNED) | set(es.PROMOTED) | set(es.CONTACT_PROMOTED)
    derived = {"service_count", "has_hours", "has_address"}

    assert touched - derived <= set(es.FIELD_LABELS)


def test_the_staging_prefix_cannot_be_a_slug():
    """Подчёркивание не проходит проверку слага — снаружи стейджинга не видно."""
    assert es.staging_prefix(7) == f"{draft_service.ENRICH_PREFIX}/7/"
    assert not preview_hits.SLUG_RE.fullmatch(draft_service.ENRICH_PREFIX)


def test_the_scrape_result_stays_frozen():
    """ИИ-ветка правит улов не на месте: он frozen, и это нарочно."""
    found = result()

    patched = replace(found, services=["Другое"])

    assert found.services == ["Продаж ноутбуків", "Заміна екрана"]
    assert patched.services == ["Другое"]
