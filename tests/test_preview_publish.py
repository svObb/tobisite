"""Публикация превью: слаг, выкладка в R2, адрес у черновика и лида (10.11–10.13).

Сети нет: клиент R2 подменяет фикстура r2 из conftest, PUT'ы складываются в
список. Без ключей R2 фикстура не ставится, и тогда проверяется обратное —
сборка работает как раньше, а публикации просто нет.
"""
import itertools
from datetime import datetime, timedelta

from sqlalchemy import select

import ambient_stage
import config
import draft_service
import site_images
from models import PREVIEW_TTL_DAYS, Draft, Lead, LeadEvent, Session
from site_factory.engine import render
from test_draft_service import shop_enrichment


async def _draft(lead_id: int) -> Draft:
    async with Session() as s:
        return (await s.scalars(
            select(Draft).where(Draft.lead_id == lead_id)
        )).one()


async def _events(lead_id: int, event: str) -> list[LeadEvent]:
    async with Session() as s:
        return list(await s.scalars(
            select(LeadEvent).where(LeadEvent.lead_id == lead_id,
                                    LeadEvent.event == event)
        ))


async def _lead(lead_id: int) -> Lead:
    async with Session() as s:
        return await s.get(Lead, lead_id)


async def test_built_draft_becomes_a_live_preview(slot_answer, draft_lead, r2):
    lead = await draft_lead(name="Право і Діло")
    await slot_answer(lead)

    result = await draft_service.build_draft(lead.id)

    assert result.ok and result.preview_url == "https://pravo-i-dilo" \
                                               ".tobisitepreview.com/"
    assert not result.publish_reason
    put, = r2.puts
    assert put["Key"] == "pravo-i-dilo/index.html"
    assert put["ContentType"] == "text/html; charset=utf-8"
    assert put["Bucket"] == draft_service.DEFAULT_BUCKET
    assert b"<html" in put["Body"].lower()

    row = await _draft(lead.id)
    assert row.status == "published" and row.published_at
    assert row.r2_prefix == "pravo-i-dilo"
    assert row.preview_host == "pravo-i-dilo.tobisitepreview.com"
    assert row.slots_json                      # страницу можно собрать заново
    # ссылка на лиде и адрес черновика — одно и то же, иначе письмо уйдёт не туда
    assert (await _lead(lead.id)).draft_url == row.preview_url
    assert len(await _events(lead.id, "preview_published")) == 1


async def test_second_company_with_the_same_name_gets_its_own_slug(
        slot_answer, draft_lead, r2):
    first = await draft_lead(name="Зубна Фея")
    await slot_answer(first)
    assert (await draft_service.build_draft(first.id)).ok
    second = await draft_lead(name="Зубна Фея")
    await slot_answer(second)

    result = await draft_service.build_draft(second.id)

    assert (await _draft(first.id)).r2_prefix == "zubna-feia"
    assert (await _draft(second.id)).r2_prefix == "zubna-feia-2"
    assert result.preview_url == "https://zubna-feia-2.tobisitepreview.com/"


async def test_dead_r2_does_not_cancel_the_draft(slot_answer, draft_lead, r2):
    lead = await draft_lead()
    await slot_answer(lead)
    r2.fail = RuntimeError("бакета нет")

    result = await draft_service.build_draft(lead.id)

    assert result.ok and result.status == "generated"
    assert not result.preview_url and "бакета нет" in result.publish_reason
    row = await _draft(lead.id)
    # слаг остаётся закреплённым за черновиком, а адреса превью нет: страницы
    # в бакете тоже нет, и ссылке взяться неоткуда
    assert row.status == "generated" and row.r2_prefix
    assert row.preview_host is None
    assert (await _lead(lead.id)).draft_url is None


async def test_repeat_after_a_failed_upload_uses_the_same_slug(slot_answer,
                                                               draft_lead, r2):
    lead = await draft_lead(name="Кава і Пара")
    await slot_answer(lead)
    r2.fail = RuntimeError("бакета нет")
    assert not (await draft_service.build_draft(lead.id)).preview_url
    reserved = (await _draft(lead.id)).r2_prefix
    r2.fail = None

    result = await draft_service.publish_preview(lead.id)

    assert result.ok and result.slug == reserved
    assert list(r2.objects) == [f"{reserved}/index.html"]
    assert (await _draft(lead.id)).status == "published"


async def _new_draft(lead_id: int) -> int:
    async with Session() as s, s.begin():
        row = Draft(lead_id=lead_id, status="generated")
        s.add(row)
        await s.flush()
        return row.id


async def test_racing_drafts_do_not_share_one_slug(draft_lead, monkeypatch):
    first, second = await draft_lead(name="Гонка"), await draft_lead(name="Гонка")
    ids = [await _new_draft(first.id), await _new_draft(second.id)]
    real, calls = draft_service.free_slug, itertools.count()

    async def same_slug(session, lead):
        # обе сборки успели выбрать слаг раньше, чем соседняя записала свой
        return "honka-race" if next(calls) < 2 else await real(session, lead)

    monkeypatch.setattr(draft_service, "free_slug", same_slug)

    slugs = [await draft_service.reserve_slug(ids[0], first),
             await draft_service.reserve_slug(ids[1], second)]

    assert slugs[0] == "honka-race" and slugs[1] != slugs[0]
    assert (await _draft(second.id)).r2_prefix == slugs[1]
    # слаг закреплён до выкладки: страницы под ним ещё нет
    assert (await _draft(first.id)).status == "publishing"


async def test_draft_from_another_library_is_not_published(slot_answer,
                                                           draft_lead, r2):
    lead = await draft_lead()
    await slot_answer(lead)
    assert (await draft_service.build_draft(lead.id)).ok
    async with Session() as s, s.begin():
        (await s.get(Draft, (await _draft(lead.id)).id)).library_version = "0"

    result = await draft_service.publish_preview(lead.id)

    assert not result.ok and "пересоберите" in result.reason
    assert len(r2.puts) == 1                   # второй выкладки не было


async def test_slot_without_saved_text_drops_the_section(draft_lead, slot_plan):
    lead = await draft_lead()
    plan = await slot_plan(lead)
    texts = {spec["slot"]: "Рядок" for spec in plan.specs}
    lost = next(s["slot"] for s in plan.specs if s["role"] == "hero")
    texts.pop(lost)

    html, trace = render.render(plan.profile, free_texts=texts)

    # заготовка рецепта на месте пропавшего текста означала бы рыбу на превью
    assert html is None and "hero" in trace["dropped_sections"]


async def test_republish_keeps_the_slug_and_does_not_ask_the_model(
        slot_answer, draft_lead, r2):
    lead = await draft_lead()
    state = await slot_answer(lead)
    built = await draft_service.build_draft(lead.id)
    calls = len(state.fake.messages.calls)

    result = await draft_service.publish_preview(lead.id)

    # тот же адрес: ссылка из уже отправленного письма обязана открываться
    assert result.ok and result.url == built.preview_url
    assert len(r2.puts) == 2 and r2.puts[0]["Body"] == r2.puts[1]["Body"]
    assert len(state.fake.messages.calls) == calls


async def test_draft_without_saved_slots_is_not_published(slot_answer,
                                                          draft_lead, r2):
    lead = await draft_lead()
    await slot_answer(lead)
    assert (await draft_service.build_draft(lead.id)).ok
    async with Session() as s, s.begin():
        (await s.get(Draft, (await _draft(lead.id)).id)).slots_json = None

    result = await draft_service.publish_preview(lead.id)

    assert not result.ok and "без слотов" in result.reason
    assert len(r2.puts) == 1                   # второй выкладки не было


# --- уборка превью (10.14, 10.15) ---------------------------------------------

async def _published(slot_answer, draft_lead, **kw) -> Lead:
    lead = await draft_lead(**kw)
    await slot_answer(lead)
    built = await draft_service.build_draft(lead.id)
    assert built.ok and built.preview_url, built.publish_reason
    return lead


async def _age(lead_id: int, days: int):
    async with Session() as s, s.begin():
        draft = await s.scalar(select(Draft).where(Draft.lead_id == lead_id))
        draft.published_at = datetime.now(config.TZ) - timedelta(days=days)


async def _set_status(lead_id: int, status: str):
    async with Session() as s, s.begin():
        (await s.get(Lead, lead_id)).status = status


async def test_preview_past_its_term_is_taken_down(slot_answer, draft_lead, r2):
    lead = await _published(slot_answer, draft_lead)
    slug = (await _draft(lead.id)).r2_prefix
    await _age(lead.id, PREVIEW_TTL_DAYS + 1)

    result = await draft_service.expire_previews()

    assert (lead.id, slug, f"срок {PREVIEW_TTL_DAYS} дней") in result.deleted
    assert f"{slug}/index.html" not in r2.objects
    row = await _draft(lead.id)
    assert row.status == "expired" and row.r2_prefix == slug
    # ссылка вела бы в 404 — с лида она снята, а событие осталось
    assert (await _lead(lead.id)).draft_url is None
    assert len(await _events(lead.id, "preview_expired")) == 1


async def test_sold_lead_keeps_its_preview(slot_answer, draft_lead, r2):
    lead = await _published(slot_answer, draft_lead)
    slug = (await _draft(lead.id)).r2_prefix
    await _age(lead.id, PREVIEW_TTL_DAYS * 3)
    await _set_status(lead.id, "sold")

    result = await draft_service.expire_previews()

    assert result.kept_sold and lead.id not in [x[0] for x in result.deleted]
    assert f"{slug}/index.html" in r2.objects
    assert (await _draft(lead.id)).status == "published"


async def test_closed_lead_loses_its_preview_at_once(slot_answer, draft_lead,
                                                     r2):
    lead = await _published(slot_answer, draft_lead)
    slug = (await _draft(lead.id)).r2_prefix
    await _set_status(lead.id, "refused")

    result = await draft_service.expire_previews()

    assert (lead.id, slug, "лид закрыт") in result.deleted
    assert f"{slug}/index.html" not in r2.objects


async def test_live_preview_is_not_touched(slot_answer, draft_lead, r2):
    lead = await _published(slot_answer, draft_lead)
    slug = (await _draft(lead.id)).r2_prefix

    result = await draft_service.expire_previews()

    assert lead.id not in [x[0] for x in result.deleted]
    assert f"{slug}/index.html" in r2.objects
    assert (await _draft(lead.id)).status == "published"


async def test_failed_deletion_keeps_the_row_as_it_was(slot_answer, draft_lead,
                                                       r2):
    lead = await _published(slot_answer, draft_lead)
    url = (await _lead(lead.id)).draft_url
    await _age(lead.id, PREVIEW_TTL_DAYS + 1)
    r2.fail = RuntimeError("R2 не отвечает")

    result = await draft_service.expire_previews()

    assert any(f"#{lead.id}" in line for line in result.failed)
    assert (await _draft(lead.id)).status == "published"
    assert (await _lead(lead.id)).draft_url == url


async def test_gc_survives_a_database_failure_in_the_middle(
        slot_answer, draft_lead, r2, monkeypatch):
    first = await _published(slot_answer, draft_lead, name="Перша Гілка")
    second = await _published(slot_answer, draft_lead, name="Друга Гілка")
    await _age(first.id, PREVIEW_TTL_DAYS + 1)
    await _age(second.id, PREVIEW_TTL_DAYS + 1)
    real = draft_service._mark_expired

    async def flaky(draft_id, lead_id, why, actor_tg_id):
        if lead_id == first.id:
            raise RuntimeError("база отвалилась")
        await real(draft_id, lead_id, why, actor_tg_id)

    monkeypatch.setattr(draft_service, "_mark_expired", flaky)

    result = await draft_service.expire_previews()

    # соседнее превью разобрано, а сбой виден в отчёте, а не только в логах
    done = [x[0] for x in result.deleted]
    assert second.id in done and first.id not in done
    assert any(f"#{first.id}" in line for line in result.failed)
    assert (await _draft(first.id)).status == "published"
    assert (await _draft(second.id)).status == "expired"


async def test_preview_the_bucket_refused_to_delete_stays_published(
        slot_answer, draft_lead, r2):
    lead = await _published(slot_answer, draft_lead)
    slug = (await _draft(lead.id)).r2_prefix
    await _age(lead.id, PREVIEW_TTL_DAYS + 1)
    r2.refuse.add(f"{slug}/index.html")

    result = await draft_service.expire_previews()

    # 200 с Errors внутри — не удаление: страница открывается, значит превью живо
    assert any(f"#{lead.id}" in line for line in result.failed)
    assert f"{slug}/index.html" in r2.objects
    assert (await _draft(lead.id)).status == "published"
    assert (await _lead(lead.id)).draft_url


async def test_slug_of_a_removed_preview_is_not_given_away(slot_answer,
                                                           draft_lead, r2):
    first = await _published(slot_answer, draft_lead, name="Мовна Школа")
    await _age(first.id, PREVIEW_TTL_DAYS + 1)
    assert (await draft_service.expire_previews()).deleted
    second = await _published(slot_answer, draft_lead, name="Мовна Школа")

    # сохранённая клиентом ссылка не должна однажды открыть чужой сайт
    assert (await _draft(second.id)).r2_prefix == "movna-shkola-2"


# --- картинки с сайта лида (дорожка III) --------------------------------------

def _stage(r2, lead_id: int, *names: str) -> list[str]:
    """Стейджинг обогащения: файлы лежат там, куда их кладёт enrich_service."""
    prefix = f"{draft_service.enrich_prefix(lead_id)}{draft_service.IMG_DIR}/"
    keys = [prefix + name for name in names]
    for key, name in zip(keys, names):
        r2.objects[key] = f"байты {name}".encode()
        r2.ops.append(("put", key))
    return keys


async def test_staged_images_reach_the_preview_before_the_page(slot_answer,
                                                               draft_lead, r2):
    lead = await draft_lead()
    _stage(r2, lead.id, "logo.webp", "portrait.webp")
    await slot_answer(lead)

    result = await draft_service.build_draft(lead.id)

    assert result.ok and result.preview_url, result.publish_reason
    slug = (await _draft(lead.id)).r2_prefix
    assert f"{slug}/img/logo.webp" in r2.objects
    assert (r2.objects[f"{slug}/img/portrait.webp"]
            == "байты portrait.webp".encode())
    # иначе первый открывший превью увидит страницу с дырами вместо фотографий
    copies = [i for i, (kind, _) in enumerate(r2.ops) if kind == "copy"]
    page = r2.ops.index(("put", f"{slug}/index.html"))
    assert copies and max(copies) < page
    # стейджинг остаётся на месте: пересборка копирует те же файлы заново
    assert f"{draft_service.enrich_prefix(lead.id)}img/logo.webp" in r2.objects


async def test_a_picture_that_did_not_copy_cancels_the_publication(
        slot_answer, draft_lead, r2):
    lead = await draft_lead()
    staged = _stage(r2, lead.id, "logo.webp", "hero_bg.webp")
    r2.refuse_copy.add(staged[1])
    await slot_answer(lead)

    result = await draft_service.build_draft(lead.id)

    # страница со ссылками на несуществующие файлы хуже отсутствия страницы
    assert result.ok and not result.preview_url
    assert "картинки не скопированы" in result.publish_reason
    row = await _draft(lead.id)
    assert row.status == "generated" and row.r2_prefix
    assert f"{row.r2_prefix}/index.html" not in r2.objects
    assert (await _lead(lead.id)).draft_url is None


async def test_republishing_wipes_the_pictures_of_the_previous_scrape(
        slot_answer, draft_lead, r2):
    lead = await draft_lead()
    _stage(r2, lead.id, "logo.webp")
    await slot_answer(lead)
    assert (await draft_service.build_draft(lead.id)).preview_url
    slug = (await _draft(lead.id)).r2_prefix
    r2.objects[f"{slug}/img/photo-5.webp"] = "c прошлого обхода".encode()
    mark = len(r2.ops)

    assert (await draft_service.publish_preview(lead.id)).ok

    # прошлый скрейп нашёл больше фото, чем нынешний, — лишнее уехало бы призраком
    assert f"{slug}/img/photo-5.webp" not in r2.objects
    assert f"{slug}/img/logo.webp" in r2.objects
    # и убрано оно ПОСЛЕ копий: пока они идут, старая страница ещё живая
    kinds = [kind for kind, _ in r2.ops[mark:]]
    assert kinds.index("delete") > max(i for i, k in enumerate(kinds)
                                       if k == "copy")


async def test_a_failed_recopy_leaves_the_live_pictures_alone(slot_answer,
                                                              draft_lead, r2):
    lead = await draft_lead()
    _stage(r2, lead.id, "logo.webp", "portrait.webp")
    await slot_answer(lead)
    assert (await draft_service.build_draft(lead.id)).preview_url
    slug = (await _draft(lead.id)).r2_prefix
    # файл прошлой публикации, которого в новом манифесте нет: снеси мы папку
    # первой, он исчез бы вместе с ней ещё до неудачной копии
    r2.objects[f"{slug}/img/photo-5.webp"] = "c прошлого обхода".encode()
    live = {key: value for key, value in r2.objects.items()
            if key.startswith(f"{slug}/{draft_service.IMG_DIR}/")}
    r2.refuse_copy.add(f"{draft_service.enrich_prefix(lead.id)}img/portrait.webp")
    mark = len(r2.ops)

    result = await draft_service.publish_preview(lead.id)

    # ссылку на превью клиент уже получил: страницы без фотографий он не увидит
    assert not result.ok and "картинки не скопированы" in result.reason
    # копия оборвалась не на первом же файле — и всё равно ничего не потеряно
    assert ("copy", f"{slug}/img/logo.webp") in r2.ops[mark:]
    assert not [key for kind, key in r2.ops[mark:] if kind == "delete"]
    assert {key: value for key, value in r2.objects.items()
            if key.startswith(f"{slug}/{draft_service.IMG_DIR}/")} == live
    # прежняя публикация цела целиком: страница на месте и ссылается на файлы,
    # которые в бакете есть. Слаг за черновиком остаётся — по нему и повторим
    assert f"{slug}/index.html" in r2.objects
    row = await _draft(lead.id)
    assert row.status == "published" and row.r2_prefix == slug


async def test_closing_a_lead_takes_down_the_staging_too(slot_answer,
                                                         draft_lead, r2):
    lead = await _published(slot_answer, draft_lead)
    slug = (await _draft(lead.id)).r2_prefix
    _stage(r2, lead.id, "logo.webp", "portrait.webp")
    await _set_status(lead.id, "refused")

    result = await draft_service.expire_previews()

    assert (lead.id, slug, "лид закрыт") in result.deleted
    assert lead.id in result.swept
    assert not [k for k in r2.objects if k.startswith(f"{slug}/")]
    assert not [k for k in r2.objects
                if k.startswith(draft_service.enrich_prefix(lead.id))]


async def test_staging_of_a_live_lead_is_not_swept(slot_answer, draft_lead, r2):
    lead = await _published(slot_answer, draft_lead)
    _stage(r2, lead.id, "logo.webp")

    result = await draft_service.expire_previews()

    assert lead.id not in result.swept
    assert f"{draft_service.enrich_prefix(lead.id)}img/logo.webp" in r2.objects


async def test_staging_without_a_preview_is_swept_all_the_same(make_lead, r2):
    async with Session() as s, s.begin():
        lead = await make_lead(s, status="rejected")
    _stage(r2, lead.id, "logo.webp")

    result = await draft_service.expire_previews()

    # черновика у лида нет и не будет, а картинки в бакете остались бы навсегда
    assert lead.id in result.swept and not r2.objects


# --- бюджет картинок превью (дорожка II, media_manifest) ----------------------

def _img(name: str) -> str:
    return f'<img src="/img/{name}" alt="" width="8" height="8">'


def test_manifest_starts_with_the_pictures_of_the_page():
    staged = [f"_enrich/7/img/{name}" for name in
              ("hero_bg.webp", "logo.webp", "photo-2.webp", "portrait.webp")]
    page = _img("portrait.webp") + _img("photo-2.webp")

    manifest = draft_service.media_manifest(staged, page)

    # сначала то, на что ссылается страница, и в порядке появления в разметке
    assert [key.rsplit("/", 1)[-1] for key in manifest] == [
        "portrait.webp", "photo-2.webp", "logo.webp", "hero_bg.webp"]


def test_the_media_budget_covers_the_whole_staging():
    """12 — потолок скрейпа (логотип и семь снимков) плюс четыре амбиент-роли.

    Пересечение контрактов бывает только в меньшую сторону: hero_bg скрейпа и
    амбиента — одно имя файла. Разъедутся контракты — разъедется и бюджет,
    и публикация начнёт отказывать страницам, которые стейджинг честно собрал.
    """
    assert draft_service.MEDIA_BUDGET == (
        site_images.MAX_STAGED + len(ambient_stage.AMBIENT_ROLES))


def test_manifest_holds_the_budget_of_the_whole_staging():
    staged = [f"_enrich/7/img/{name}" for name in
              ["logo.webp"] + [f"photo-{n}.webp" for n in range(2, 16)]]

    manifest = draft_service.media_manifest(staged)

    assert len(manifest) == draft_service.MEDIA_BUDGET == 12
    names = [key.rsplit("/", 1)[-1] for key in manifest]
    # логотип вперёд, дальше галерея по номерам: лишнее остаётся в стейджинге
    assert names[0] == "logo.webp" and names[1] == "photo-2.webp"
    assert "photo-14.webp" not in names and "photo-15.webp" not in names


def test_manifest_refuses_a_page_that_asks_for_more_than_the_budget():
    names = [f"photo-{n}.webp" for n in range(2, 16)]
    staged = [f"_enrich/7/img/{name}" for name in names]
    page = "".join(_img(name) for name in names)

    try:
        draft_service.media_manifest(staged, page)
    except ValueError as e:
        assert "бюджете" in str(e)
    else:
        raise AssertionError("страница без части своих картинок не публикуется")


def test_manifest_ignores_pictures_the_staging_does_not_have():
    manifest = draft_service.media_manifest(["_enrich/7/img/logo.webp"],
                                            _img("portrait.webp"))

    # ссылку вписали руками, файла нет: страница выкладывается как есть
    assert manifest == ["_enrich/7/img/logo.webp"]


def test_page_images_are_read_in_order_and_once():
    html = _img("logo.webp") + _img("photo-2.webp") + _img("logo.webp")
    assert draft_service.page_images(html) == ["logo.webp", "photo-2.webp"]
    assert draft_service.page_images("") == []


async def test_publication_copies_the_pictures_the_page_needs(slot_answer,
                                                              draft_lead, r2):
    lead = await draft_lead(enrichment=shop_enrichment())
    _stage(r2, lead.id, "logo.webp", "portrait.webp",
           *[f"photo-{n}.webp" for n in range(2, 16)])
    await slot_answer(lead)

    result = await draft_service.build_draft(lead.id)

    assert result.ok and result.preview_url, result.publish_reason
    slug = (await _draft(lead.id)).r2_prefix
    copied = {key.rsplit("/", 1)[-1] for key in r2.objects
              if key.startswith(f"{slug}/{draft_service.IMG_DIR}/")}
    assert len(copied) == draft_service.MEDIA_BUDGET
    # ни одной ссылки на файл, которого в папке превью нет
    html = r2.objects[f"{slug}/index.html"].decode()
    assert draft_service.page_images(html)
    assert set(draft_service.page_images(html)) <= copied
    # логотип идёт раньше галереи, а хвост стейджинга в бюджет не влез
    assert "logo.webp" in copied and "photo-15.webp" not in copied


async def test_without_r2_keys_the_pipeline_works_as_before(slot_answer,
                                                            draft_lead,
                                                            monkeypatch):
    for name in draft_service.R2_ENV:
        monkeypatch.delenv(name, raising=False)
    lead = await draft_lead()
    await slot_answer(lead)

    result = await draft_service.build_draft(lead.id)

    assert result.ok and not result.preview_url and not result.publish_reason
    assert (await _draft(lead.id)).status == "generated"
    assert not (await draft_service.publish_preview(lead.id)).ok
