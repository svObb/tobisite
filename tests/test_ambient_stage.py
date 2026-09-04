"""Амбиент-фон в стейджинге лида: выкладка, карточка, отказы.

Ни сети, ни диска: байты рисует Pillow, бакет — FakeR2 из conftest. Модель
сюда не зовётся ни разу — картинку рисует человек, а модуль только пережимает
готовые байты.

Проверяется то, ради чего модуль написан: фон ложится в карточку так, чтобы
пережить повторное «Обогатить», а любой отказ не оставляет следа ни в базе, ни
в бакете.
"""
import io
import itertools

import pytest
from PIL import Image
from sqlalchemy import func, select

import ambient_stage
import draft_service
import enrich_service as es
import site_images
from models import Lead, LeadEvent, Session

SITE = "https://svitlo.example/"
_domains = itertools.count(1)


def png(width, height, color=(38, 52, 71)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def ambient_lead(make_lead):
    """Лид с сайтом; уже написанное обогащение задаётся вызовом."""
    async def _make(*, enrichment=None, **kw):
        async with Session() as s, s.begin():
            # домен уникален среди живых лидов — отсюда счётчик
            return await make_lead(
                s, website_url=SITE, enrichment=enrichment or {},
                domain_norm=f"svitlo-{next(_domains)}.example", **kw,
            )

    return _make


async def enrichment_of(lead_id: int) -> dict:
    async with Session() as s:
        return dict((await s.get(Lead, lead_id)).enrichment or {})


async def events_of(lead_id: int, name: str) -> list:
    async with Session() as s:
        return list(await s.scalars(
            select(LeadEvent).where(LeadEvent.lead_id == lead_id,
                                    LeadEvent.event == name)
        ))


def key_of(lead_id: int) -> str:
    return es.image_key(lead_id, "hero_bg.webp")


# --- фон ложится --------------------------------------------------------------

async def test_the_ambient_reaches_the_bucket_and_the_card(ambient_lead, r2):
    lead = await ambient_lead()

    report = await ambient_stage.stage_ambient(lead.id, png(1920, 1080))

    assert r2.objects[key_of(lead.id)][:4] == b"RIFF"      # webp, а не исходник
    data = await enrichment_of(lead.id)
    assert data["images"] == {"hero_bg": {"src": "/img/hero_bg.webp",
                                          "width": 1920, "height": 1080}}
    assert data["photo_count"] == 1
    assert data[es.AMBIENT_KEY] == ["hero_bg"]
    assert str(lead.id) in report and "hero_bg" in report
    assert len(await events_of(lead.id, "ambient_staged")) == 1


async def test_a_huge_background_is_capped_by_the_contract(ambient_lead, r2):
    """Пережатие — то же, что у скрейпа: длинная сторона не больше 2000."""
    lead = await ambient_lead()

    await ambient_stage.stage_ambient(lead.id, png(4000, 2250))

    record = (await enrichment_of(lead.id))["images"]["hero_bg"]
    assert record["width"] == site_images.ROLE_MAX_SIDE["background"]
    assert record["height"] == 1125


async def test_a_second_run_overwrites_without_doubling_the_ambient(ambient_lead,
                                                                   r2):
    lead = await ambient_lead()
    await ambient_stage.stage_ambient(lead.id, png(1920, 1080))

    await ambient_stage.stage_ambient(lead.id, png(1600, 900))

    assert list(r2.objects) == [key_of(lead.id)]
    data = await enrichment_of(lead.id)
    assert data["images"]["hero_bg"]["width"] == 1600
    assert data[es.AMBIENT_KEY] == ["hero_bg"]
    assert data["photo_count"] == 1


async def test_real_photos_keep_their_places_and_the_count_is_recounted(
        ambient_lead, r2):
    """Амбиент ложится рядом со снимками сайта, а photo_count считается заново."""
    lead = await ambient_lead(enrichment={
        "images": {"logo": {"src": "/img/logo.webp", "width": 400,
                            "height": 120},
                   "portrait": {"src": "/img/portrait.webp", "width": 1000,
                                "height": 1200},
                   "photo-2": {"src": "/img/photo-2.webp", "width": 900,
                               "height": 900}},
        "photo_count": 2,
    })

    await ambient_stage.stage_ambient(lead.id, png(1920, 1080))

    data = await enrichment_of(lead.id)
    assert sorted(data["images"]) == ["hero_bg", "logo", "photo-2", "portrait"]
    # логотип в счёт не идёт, амбиент идёт наравне со снимками
    assert data["photo_count"] == 3
    assert data[es.AMBIENT_KEY] == ["hero_bg"]


# --- отказы -------------------------------------------------------------------

async def test_broken_bytes_change_nothing(ambient_lead, r2):
    lead = await ambient_lead(enrichment={"services": ["Заміна проводки"]})

    with pytest.raises(ValueError, match="картинка"):
        await ambient_stage.stage_ambient(lead.id, b"eto ne kartinka")

    assert await enrichment_of(lead.id) == {"services": ["Заміна проводки"]}
    assert r2.objects == {}


async def test_a_picture_smaller_than_a_background_is_refused(ambient_lead, r2):
    lead = await ambient_lead()

    with pytest.raises(ValueError, match="не годится"):
        await ambient_stage.stage_ambient(lead.id, png(600, 400))

    assert await enrichment_of(lead.id) == {}
    assert r2.objects == {}


async def test_a_strip_is_not_a_background_either(ambient_lead, r2):
    """Полоска 5:1 шире порога, но пропорции фотографии она не проходит."""
    lead = await ambient_lead()

    with pytest.raises(ValueError, match="не годится"):
        await ambient_stage.stage_ambient(lead.id, png(2000, 400))

    assert r2.objects == {}


async def test_a_tall_frame_that_shrinks_below_the_sections_is_refused(
        ambient_lead, r2):
    """900px ширины исходником, 300px после пережатия по длинной стороне."""
    lead = await ambient_lead()

    with pytest.raises(ValueError, match="после пережатия"):
        await ambient_stage.stage_ambient(lead.id, png(900, 3600))

    assert r2.objects == {}


async def test_a_frame_of_the_pool_is_staged_under_its_own_name(ambient_lead, r2):
    """Кадр пула добивает нехватку: hero_bg вне пула, и её он не закрывает."""
    lead = await ambient_lead()

    await ambient_stage.stage_ambient(lead.id, png(1200, 1500),
                                      role="ambient-1")

    assert es.image_key(lead.id, "ambient-1.webp") in r2.objects
    data = await enrichment_of(lead.id)
    assert data["images"]["ambient-1"]["src"] == "/img/ambient-1.webp"
    assert data[es.AMBIENT_KEY] == ["ambient-1"]
    assert es.ambient_gap(data) == es.AMBIENT_TARGET - 1


async def test_a_role_of_the_scraper_is_not_ambient(ambient_lead, r2):
    """Портрет и photo-N раздаёт скрейп: амбиент в эти имена не пишет."""
    lead = await ambient_lead()

    for role in ("portrait", "photo-2"):
        with pytest.raises(ValueError, match=role):
            await ambient_stage.stage_ambient(lead.id, png(1920, 1080),
                                              role=role)

    assert await enrichment_of(lead.id) == {} and r2.objects == {}


async def test_without_r2_keys_nothing_is_written(ambient_lead, monkeypatch):
    for name in draft_service.R2_ENV:
        monkeypatch.delenv(name, raising=False)
    lead = await ambient_lead()

    with pytest.raises(RuntimeError, match="R2"):
        await ambient_stage.stage_ambient(lead.id, png(1920, 1080))

    assert await enrichment_of(lead.id) == {}


async def test_a_lead_that_is_not_there_is_refused(r2):
    async with Session() as s:
        missing = (await s.scalar(select(func.max(Lead.id))) or 0) + 1

    with pytest.raises(ValueError, match=f"нет лида {missing}"):
        await ambient_stage.stage_ambient(missing, png(1920, 1080))

    assert r2.objects == {}


async def test_a_closed_lead_is_refused(ambient_lead, r2):
    """Стейджинг закрытого лида подметает sweep_staging — класть в него нечего."""
    lead = await ambient_lead(status="rejected")

    with pytest.raises(ValueError, match="закрыт"):
        await ambient_stage.stage_ambient(lead.id, png(1920, 1080))

    assert await enrichment_of(lead.id) == {} and r2.objects == {}


async def test_a_lead_being_enriched_is_left_alone(ambient_lead, r2):
    """Занятость общая с «Обогатить»: перескрейп перекладывает те же файлы."""
    lead = await ambient_lead()
    assert es.hold_lead(lead.id)
    try:
        with pytest.raises(RuntimeError, match="обогащается"):
            await ambient_stage.stage_ambient(lead.id, png(1920, 1080))
    finally:
        es.release_lead(lead.id)

    assert r2.objects == {}
    assert await ambient_stage.stage_ambient(lead.id, png(1920, 1080))
    assert not es.enrich_busy(lead.id)
