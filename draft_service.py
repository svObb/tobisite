"""Сборка черновика сайта лида: профиль -> композиция -> слоты -> HTML (Д13 §3).

Порядок жёсткий и весь детерминированный, кроме одного шага:

    профиль лида (leads + contacts + enrichment)
    -> compose: рецепт, гейты, скоринг, seed = sha256(domain_norm)
    -> slot_gen: модель пишет free-слоты (единственный недетерминированный шаг)
    -> render: тот же профиль, тот же seed, та же композиция + JSON слотов
    -> checks: NAP, заглушки, скролл, a11y, форма
    -> строка drafts со следом решения

Три исхода, и они разные:

* **generated** — страница собралась и прошла проверки;
* **needs_enrichment** — данных не хватило ещё на этапе композиции: лид
  получает список того, что дозаполнить, а работник — сообщение. Черновика
  нет, и просить у модели нечего (Д13 §3 ступень 4);
* **failed** — данные были, но собрать не вышло: модель недоступна, слоты
  пустые после перегенерации или автопроверки нашли брак. Работника такое не
  касается — чинить это нам.

Единственный недетерминированный шаг выключается: лежат в enrichment готовые
тексты слотов под ключом `_dev_free_texts` — модель не зовётся вовсе, и вся
сборка становится чистой функцией от карточки лида.

Неудачная пересборка не затирает уже собранный черновик: у опубликованного
превью в строке лежит r2_prefix, и потерять его из-за упавшего API нельзя.

Собранный черновик тут же уезжает в R2 и становится живым превью (10.11–10.13):
одна команда админа превращает карточку лида в адрес, который можно открыть.
Публикация не обязательна — без ключей R2 её просто нет, а черновик остаётся
в базе и в письме, как раньше.
"""
import asyncio
import logging
import os
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import config
import slot_gen
from models import (
    DRAFT_TTL_DAYS, PREVIEW_TTL_DAYS, Contact, Draft, Lead, Session, Worker,
    draft_fresh, log_event,
)
from site_factory.engine import render
from site_factory.engine.checks import run_all
from site_factory.engine.compose import compose
from site_factory.engine.profile import Profile
from tools.slugify_preview import unique_slug

log = logging.getLogger(__name__)

# Язык черновика — язык страны лида, а не язык карточки (8.29): страницу
# читает клиент компании, а не работник, который её нашёл.
UA_LANG, DEFAULT_LANG = "uk", "en"

# Признаки, которые обогащение вправе принести в профиль. Всё, чего нет в
# этом списке, движок не понимает, а чего нет в enrichment — неизвестно
# (unknown != false, Д13 §3 шаг 1). Служебные ключи обогащения начинаются с
# подчёркивания (`_scrape`, `_dev_free_texts`) и в профиль не проходят именно
# потому, что их здесь нет.
ENRICHMENT_FIELDS = (
    "services", "service_count", "has_prices", "has_hours", "hours",
    "has_booking_url", "booking_url", "review_count", "google_rating",
    "has_address", "address", "address_parts", "photo_count", "text_volume",
    "old_site_state", "brand_colors", "images", "products", "phone", "email",
    "name", "city", "niche",
)

# Готовые тексты free-слотов прямо в карточке лида: {«вариант.слот»: строка}.
# Ключ есть — модель за черновиком не зовётся вовсе (режим экономии API).
DEV_TEXTS_KEY = "_dev_free_texts"

# Описание черновика для <draft> письма: по одной формуле на вариант секции.
# Модель здесь не участвует — письмо должно называть то, что на странице
# действительно есть, а не то, что складно звучит. Порядок — как на странице;
# вариант без формулы означал бы секцию, о которой письмо промолчит, поэтому
# полноту таблицы держит тест против load_library().
SUMMARY_PARTS = {
    "header_logo": {"uk": "шапка з логотипом",
                    "en": "a header with the logo"},
    "header_wordmark": {"uk": "шапка з назвою",
                        "en": "a header with the name"},
    "hero_bg_photo": {"uk": "головна з фото на весь екран",
                      "en": "homepage with a full-width photo"},
    "hero_split_map": {"uk": "головна з картою і телефоном",
                       "en": "homepage with a map and phone"},
    "hero_photo_left": {"uk": "головна з фото і телефоном",
                        "en": "homepage with a photo and phone"},
    "hero_type_only": {"uk": "головна з телефоном угорі",
                       "en": "homepage with the phone on top"},
    "products_grid": {"uk": "товари з фото", "en": "products with photos"},
    "products_list": {"uk": "перелік товарів", "en": "a list of products"},
    "svc_cards_3": {"uk": "картки послуг", "en": "service cards"},
    "svc_list_icons": {"uk": "перелік послуг", "en": "a list of services"},
    "svc_two_col_rule": {"uk": "послуги рядками",
                         "en": "services line by line"},
    "gallery_strip": {"uk": "смуга фотографій", "en": "a strip of photos"},
    "proof_stats_bar": {"uk": "оцінка і відгуки з Google",
                        "en": "the Google rating and reviews"},
    "about_note": {"uk": "абзац про компанію",
                   "en": "a note about the company"},
    "info_hours_card": {"uk": "години роботи таблицею",
                        "en": "opening hours in a table"},
    "cta_form_short": {"uk": "форма звернення", "en": "an enquiry form"},
    "footer_nap": {"uk": "контакти і години", "en": "contacts and hours"},
}
SUMMARY_MIN_WORDS, SUMMARY_MAX_WORDS = 5, 12

PREVIEW_HOST_SUFFIX = ".tobisitepreview.com"
# Стейджинг картинок обогащения (дорожка III). Подчёркивание не проходит
# проверку слага в воркере, поэтому снаружи этих файлов не видно — до тех пор,
# пока публикация не скопирует их в `<slug>/img/` уже на стороне бакета.
ENRICH_PREFIX = "_enrich"
IMG_DIR = "img"
# Сколько картинок уезжает в папку превью: логотип и ещё семь. Столько же
# кладёт в стейджинг скрейп (site_images.MAX_STAGED), но потолок стоит и здесь:
# в бакете может лежать стейджинг, снятый по прежним правилам, а платит за
# лишние килобайты LCP превью.
MEDIA_BUDGET = 8
# Порядок копирования картинок, на которые страница не ссылается: сначала то,
# что видно раньше всего, если секция с ним однажды выиграет пересборку.
MEDIA_ORDER = ("logo", "hero_bg", "portrait")
# Всё, что страница дотягивает из собственной папки. Других путей у картинок
# превью не бывает: на чужой хост движок не ссылается.
PAGE_IMG = re.compile(rf'<img[^>]+\bsrc="/{IMG_DIR}/([^"/]+)"')
# Ключи бакета живут в окружении, а не в config: бот стартует и без них,
# и тогда публикации просто нет.
R2_ENV = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
DEFAULT_BUCKET = "tobisite-previews"
# Лид закрыт — превью больше некому показывать (10.14). sold сюда не входит
# намеренно: его страница становится основой боевого сайта (10.15).
CLOSED_STATUSES = ("refused", "rejected")
# Черновики, за которыми может стоять объект в бакете: publishing — тот, чей
# PUT не досчитался ответа, и его страница тоже подлежит уборке.
IN_BUCKET_STATUSES = ("publishing", "published")
# Сколько раз подряд подбирать слаг, проиграв гонку за него. Больше — это уже
# не гонка, а сломанный уникальный индекс, и молча крутиться незачем.
SLUG_TRIES = 5


@dataclass(frozen=True)
class BuildResult:
    """Итог сборки. missing непустой — лиду нужно обогащение, а не ремонт."""
    ok: bool
    draft_id: int | None = None
    lead_id: int | None = None
    status: str = ""
    reason: str = ""
    missing: list[str] = field(default_factory=list)
    checks: dict = field(default_factory=dict)
    notify_tg_id: int | None = None
    summary: str = ""
    preview_url: str = ""
    # почему превью не выложено; пусто и при успехе, и когда публикации нет
    publish_reason: str = ""

    @property
    def needs_enrichment(self) -> bool:
        return bool(self.missing)


@dataclass(frozen=True)
class PublishResult:
    """Итог публикации превью."""
    ok: bool
    url: str = ""
    slug: str = ""
    reason: str = ""


@dataclass(frozen=True)
class GcResult:
    """Итог уборки превью: что снесено, что сохранено и что не вышло."""
    deleted: list[tuple[int, str, str]] = field(default_factory=list)
    kept_sold: int = 0
    failed: list[str] = field(default_factory=list)
    # стейджинг обогащения лидов, у которых превью уже не будет
    swept: list[int] = field(default_factory=list)


async def build_draft(lead_id: int, *,
                      actor_tg_id: int = config.ADMIN_TG_ID) -> BuildResult:
    """Собрать черновик лида. Один активный черновик на лид, пересборка — в него."""
    async with Session() as s:
        lead = await s.get(Lead, lead_id)
        if lead is None or lead.deleted_at or lead.cancelled_at:
            return BuildResult(False, reason="лид недоступен")
        profile = await build_profile(s, lead)
        author = await s.get(Worker, lead.worker_id)
        ready_texts = (lead.enrichment or {}).get(DEV_TEXTS_KEY)

    composition = compose_for(profile)
    if not composition.ok:
        # тот же расклад ещё раз, но уже полным следом решения для recipe_json
        _, trace = render.render(profile)
        return await _save(lead, "failed", actor_tg_id, trace=trace,
                           missing=composition.needs_enrichment, author=author,
                           reason="данных не хватает на страницу")

    lang = str(profile.lang.value)
    if ready_texts is None:
        slots = await slot_gen.fill_slots(profile, composition.sections, lang,
                                          lead_id=lead.id)
    else:
        slots = _ready_slots(ready_texts, composition.sections)
    if not slots.ok:
        return await _save(lead, "failed", actor_tg_id, reason=slots.reason)

    html, trace = render.render(profile, free_texts=slots.texts)
    trace["empty_slots"] = list(slots.empty)
    if html is None:
        return await _save(lead, "failed", actor_tg_id, trace=trace,
                           reason=trace.get("failed", "страница не собралась"))

    problems = run_all(html, profile, _palette(profile))
    status = "failed" if problems else "generated"
    reason = f"автопроверки: {', '.join(sorted(problems))}" if problems else ""
    built = await _save(lead, status, actor_tg_id, trace=trace, checks=problems,
                        images=_image_ids(composition), reason=reason,
                        slots=slots.texts)
    if not built.ok or not r2_ready():
        return built
    # публикация уже собранного черновика: не вышла — черновик всё равно есть
    published = await _publish(lead, built.draft_id, html, actor_tg_id)
    return replace(built, preview_url=published.url,
                   publish_reason=published.reason)


def compose_for(profile: Profile, recent_variants=()):
    """Композиция профиля — ровно та, которую соберёт render: seed один и тот же."""
    recipe = render.load_recipe(render.recipe_id_for(profile))
    return compose(profile, recipe, render.load_library(),
                   render.seed_for(profile.domain_norm), recent_variants)


async def build_profile(session, lead) -> Profile:
    """Профиль движка из строки лида, его контактов и enrichment (Д13 §3 шаг 1).

    Ключ в словаре есть — признак известен, ключа нет — неизвестен. Поэтому
    поля добавляются только когда значение действительно есть: пустой телефон
    и неспрошенный телефон это разные вещи, и гейт обязан их различать.

    domain_norm обязателен — из него движок считает seed и пресет. У лида без
    сайта его нет, и ключом становится сам лид: дизайн всё равно должен быть
    постоянным между пересборками.
    """
    data = {"domain_norm": lead.domain_norm or f"lead-{lead.id}",
            "lang": lang_of(lead)}
    for key, value in (("name", lead.name), ("city", lead.city),
                       ("niche", lead.niche),
                       ("country", config.COUNTRY_ISO.get(lead.country)
                        or lead.country)):
        if value:
            data[key] = value
    rating = _rating(lead.google_rating)
    if rating is not None:
        data["google_rating"] = rating
    data |= await _contacts(session, lead.id)
    # обогащение идёт последним: работник заполнял его уже под черновик
    data |= {k: v for k, v in (lead.enrichment or {}).items()
             if k in ENRICHMENT_FIELDS}
    # картинки, товары и часы приходят и от скрейпа, и из формы дозаполнения,
    # а шаблон секции ставит их поля в разметку без проверок
    for key, clean in (("images", _clean_images), ("products", _clean_products),
                       ("hours", _clean_hours)):
        if key in data:
            data[key] = clean(data[key])
    return Profile.from_dict(data)


def lang_of(lead) -> str:
    """Язык черновика: uk для Украины, en для остальных стран (8.29)."""
    iso = config.COUNTRY_ISO.get(lead.country)
    return UA_LANG if iso == "UA" else DEFAULT_LANG


def draft_summary(draft, lang: str | None = None) -> str:
    """5–12 слов о том, что в черновике, для тега <draft> письма.

    Детерминированно из состава композиции: модель к описанию собственного
    черновика не подпускается — иначе письмо пообещает секцию, которой на
    странице нет.
    """
    trace = (draft.recipe_json if draft is not None else None) or {}
    lang = lang or _trace_lang(trace)
    parts = [SUMMARY_PARTS[variant][lang]
             for variant in trace.get("sections") or []
             if variant in SUMMARY_PARTS and lang in SUMMARY_PARTS[variant]]
    if not parts:
        return ""
    chosen, words = [parts[0]], len(parts[0].split())
    for part in parts[1:]:
        count = len(part.split())
        if words >= SUMMARY_MIN_WORDS and words + count > SUMMARY_MAX_WORDS:
            break
        chosen.append(part)
        words += count
    return ", ".join(chosen)


async def fresh_draft(session, lead_id: int) -> Draft | None:
    """Живой черновик лида: собран, не удалён, не просрочен."""
    draft = await session.scalar(
        select(Draft).where(Draft.lead_id == lead_id, Draft.deleted_at.is_(None))
    )
    return draft if draft_fresh(draft) else None


async def summary_for(lead, lang: str | None) -> str:
    """Описание черновика лида для письма. Пусто — черновика нет, спросим руками."""
    if lang is None:
        return ""
    async with Session() as s:
        draft = await fresh_draft(s, lead.id)
    return draft_summary(draft, lang) if draft is not None else ""


def r2_ready() -> bool:
    """Ключи бакета заданы. Нет — публикации нет, и это не поломка."""
    return all((os.getenv(name) or "").strip() for name in R2_ENV)


async def free_slug(session, lead) -> str:
    """Свободный слаг лида: транслит названия, суффикс при коллизии (10.12).

    Занятость считается по drafts: два превью под одним слагом — это одна
    страница, затёртая чужой компанией. Слаги, положенные в бакет руками
    (tools/publish_r2.py), здесь не видны — руками их и разводить.
    """
    taken = set(await session.scalars(
        select(Draft.r2_prefix).where(Draft.r2_prefix.is_not(None))
    ))
    return unique_slug(lead.name or f"lead-{lead.id}", taken.__contains__)


async def reserve_slug(draft_id: int, lead) -> str:
    """Закрепить слаг за черновиком ДО выкладки в R2 (10.12).

    Свободный слаг, выбранный по таблице, свободен только до чужого commit'а:
    два параллельных «Собрать черновик» для компаний с одинаковым транслитом
    названия получают один и тот же вариант. Проигравшего гонку ловит
    уникальный индекс — он берёт следующий вариант и пробует снова.

    Слаг остаётся за черновиком навсегда, в том числе после неудачного PUT:
    повтор публикации обязан лечь по тому же адресу, что ушёл в письме.
    """
    for _ in range(SLUG_TRIES):
        async with Session() as s:
            draft = await s.get(Draft, draft_id)
            if draft is None:
                raise ValueError(f"нет черновика {draft_id}")
            if draft.r2_prefix:
                return draft.r2_prefix
            slug = await free_slug(s, lead)
        try:
            async with Session() as s, s.begin():
                draft = await s.get(Draft, draft_id, with_for_update=True)
                if draft.r2_prefix:
                    return draft.r2_prefix
                draft.r2_prefix = slug
                draft.status = "publishing"
        except IntegrityError:
            log.info("лид %s: слаг %s занят, беру следующий", lead.id, slug)
            continue
        return slug
    raise RuntimeError(f"слаг {slug} не закрепился за черновиком {draft_id}")


async def publish_draft(draft_id: int, html: str, slug: str,
                        bucket: str | None = None, *,
                        actor_tg_id: int = config.ADMIN_TG_ID) -> str:
    """Выложить страницу в R2 и записать адрес превью в базу (10.11, 10.13).

    Здесь выкладывается только index.html: bundle.css и шрифты отдаёт сам
    Worker из своих [assets], а картинки лида уже лежат в `<slug>/img/` —
    их положил copy_staged_images ДО этого вызова, чтобы страница не успела
    открыться раньше собственных картинок.

    Запись в базу — одной транзакцией с адресом на лиде: разойдись они, лид
    остался бы со ссылкой на страницу, которой нет, или наоборот.
    """
    await asyncio.to_thread(
        s3_client().put_object, Bucket=bucket_name(bucket),
        Key=f"{slug}/index.html", Body=html.encode(),
        ContentType="text/html; charset=utf-8", CacheControl="no-cache",
    )
    host = f"{slug}{PREVIEW_HOST_SUFFIX}"
    async with Session() as s, s.begin():
        draft = await s.get(Draft, draft_id)
        if draft is None:
            raise ValueError(f"нет черновика {draft_id}")
        draft.r2_prefix = slug
        draft.preview_host = host
        draft.status = "published"
        draft.published_at = _now()
        lead = await s.get(Lead, draft.lead_id)
        if lead is not None:
            lead.draft_url = draft.preview_url
        log_event(s, draft.lead_id, "preview_published", actor_tg_id, new=host)
    return host


async def publish_preview(lead_id: int, *,
                          actor_tg_id: int = config.ADMIN_TG_ID) -> PublishResult:
    """Опубликовать живой черновик лида заново (ручная команда).

    Страница пересобирается из сохранённых слотов: модель второй раз не зовётся,
    а правки карточки, сделанные после сборки, в превью попадают. Слаг у лида
    один навсегда — иначе ссылка из уже отправленного письма умрёт.

    Библиотека секций с тех пор могла обновиться, и тогда сохранённые тексты
    относятся к другим вариантам секций. Такой черновик пересобирают, а не
    публикуют: иначе на превью молча уехали бы заготовки рецепта.
    """
    if not r2_ready():
        return PublishResult(False, reason="не заданы ключи R2")
    async with Session() as s:
        lead = await s.get(Lead, lead_id)
        if lead is None or lead.deleted_at or lead.cancelled_at:
            return PublishResult(False, reason="лид недоступен")
        draft = await fresh_draft(s, lead_id)
        if draft is None:
            return PublishResult(False, reason="живого черновика нет")
        if not draft.slots_json:
            # черновики старше 0013: текстов слотов в базе нет, а звать модель
            # ради публикации значит выложить не ту страницу, что в письме
            return PublishResult(False, reason="черновик собран без слотов, "
                                               "пересоберите его")
        stale = _stale_library(draft)
        if stale:
            return PublishResult(False, reason=stale)
        draft_id, slots = draft.id, dict(draft.slots_json)
        profile = await build_profile(s, lead)

    html, trace = render.render(profile, free_texts=slots)
    if html is None:
        return PublishResult(False,
                             reason=trace.get("failed", "страница не собралась"))
    problems = run_all(html, profile, _palette(profile))
    if problems:
        return PublishResult(False,
                             reason=f"автопроверки: {', '.join(sorted(problems))}")
    return await _publish(lead, draft_id, html, actor_tg_id)


async def expire_previews(*,
                          actor_tg_id: int = config.ADMIN_TG_ID) -> GcResult:
    """Убрать превью, которым вышел срок, и превью закрытых лидов (10.14).

    Превью проданного лида не трогается никогда (10.15): именно эта страница
    становится основой боевого сайта. Ровно поэтому уборку делает бот, а не
    lifecycle-правило бакета — правило про статус лида ничего не знает.

    Слаг у снесённого превью остаётся в строке черновика: он больше не
    выдаётся другой компании, иначе сохранённая клиентом ссылка однажды
    открыла бы чужой сайт.
    """
    if not r2_ready():
        return GcResult(failed=["не заданы ключи R2"])
    async with Session() as s:
        rows = list(await s.execute(
            select(Draft, Lead).join(Lead, Lead.id == Draft.lead_id)
            .where(Draft.status.in_(IN_BUCKET_STATUSES),
                   Draft.r2_prefix.is_not(None))
        ))
    deadline = _now() - timedelta(days=PREVIEW_TTL_DAYS)
    deleted, failed, sold = [], [], 0
    for draft, lead in rows:
        if lead.status == "sold":
            sold += 1
            continue
        why = _expire_reason(draft, lead, deadline)
        if not why:
            continue
        try:
            # снос и отметка о нём — одна операция: упади база после удаления,
            # остальные превью всё равно должны быть разобраны, а этот лид —
            # попасть в failed, а не потеряться в тишине до следующего прогона
            await _delete_prefix(draft.r2_prefix)
            await _mark_expired(draft.id, lead.id, why, actor_tg_id)
        except Exception as e:
            log.exception("лид %s: превью %s не снесено", lead.id,
                          draft.r2_prefix)
            failed.append(f"#{lead.id}: {e}")
            continue
        deleted.append((lead.id, draft.r2_prefix, why))
    try:
        swept = await sweep_staging()
    except Exception as e:
        log.exception("стейджинг обогащения не подметён")
        failed.append(f"стейджинг: {e}")
        swept = []
    return GcResult(deleted=deleted, kept_sold=sold, failed=failed, swept=swept)


async def sweep_staging() -> list[int]:
    """Снести стейджинг обогащения у лидов, которым превью уже не показывать.

    Ходит по всему префиксу, а не по строкам черновиков: обогащение бывает и у
    лида, черновик которого так и не собрали, и его картинки иначе остались бы
    в бакете навсегда. Второй эшелон — 180-дневное lifecycle-правило бакета.
    """
    keys = await list_keys(f"{ENRICH_PREFIX}/", limit=10_000)
    staged: dict[int, list[str]] = {}
    for key in keys:
        parts = key.split("/")
        if len(parts) > 2 and parts[1].isdigit():
            staged.setdefault(int(parts[1]), []).append(key)
    if not staged:
        return []
    async with Session() as s:
        alive = set(await s.scalars(
            select(Lead.id).where(Lead.id.in_(staged),
                                  Lead.deleted_at.is_(None),
                                  Lead.cancelled_at.is_(None),
                                  Lead.status.not_in(CLOSED_STATUSES))
        ))
    gone = []
    for lead_id in sorted(set(staged) - alive):
        await delete_keys(staged[lead_id])
        gone.append(lead_id)
    return gone


# --- внутреннее ---------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(config.TZ)


def _env(name: str) -> str:
    """Ключи R2 читаются здесь, а не в config: бот стартует и без них."""
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"не задана переменная окружения {name}")
    return value


_s3 = None


def s3_client():
    """Клиент R2, один на процесс: boto3 держит в нём пул соединений.

    boto3 импортируется лениво — бот без ключей R2 работает как раньше, и
    тянуть ради этого botocore на старте незачем.
    """
    global _s3
    if _s3 is None:
        import boto3
        _s3 = boto3.client(
            "s3",
            endpoint_url=f"https://{_env('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com",
            aws_access_key_id=_env("R2_ACCESS_KEY_ID"),
            aws_secret_access_key=_env("R2_SECRET_ACCESS_KEY"),
            region_name="auto",
        )
    return _s3


def bucket_name(name: str | None = None) -> str:
    return name or os.getenv("R2_BUCKET") or DEFAULT_BUCKET


def enrich_prefix(lead_id: int) -> str:
    """Где лежат картинки, снятые с сайта лида, до публикации превью."""
    return f"{ENRICH_PREFIX}/{lead_id}/"


def page_images(html: str) -> list[str]:
    """Имена файлов, на которые ссылается страница, в порядке появления."""
    names = []
    for name in PAGE_IMG.findall(html or ""):
        if name not in names:
            names.append(name)
    return names


def media_manifest(staged, html: str = "",
                   budget: int = MEDIA_BUDGET) -> list[str]:
    """Какие ключи стейджинга едут в папку превью и в каком порядке.

    Первыми идут те, на которые ссылается сама страница: превью с дырой на
    месте фотографии хуже превью без лишнего файла, и порядок появления в
    разметке — это порядок логотип, фон первого экрана, товары, галерея.
    Остаток бюджета добирают прочие картинки стейджинга — их подхватит
    пересборка, если состав страницы изменится.

    Страница просит больше картинок, чем помещается в бюджет, — это не повод
    выложить её без части файлов: публикация обрывается тут же.
    """
    by_name = {key.rsplit("/", 1)[-1]: key for key in staged}
    wanted = page_images(html)
    manifest = [by_name[name] for name in wanted if name in by_name]
    if len(manifest) > budget:
        raise ValueError(f"страница ссылается на {len(manifest)} картинок "
                         f"при бюджете {budget}")
    missing = [name for name in wanted if name not in by_name]
    if missing:
        # картинку вписали в карточку руками, а файла в стейджинге нет:
        # страница соберётся, но на её месте останется пусто
        log.warning("в стейджинге нет картинок страницы: %s", ", ".join(missing))
    for name in sorted(set(by_name) - set(wanted), key=_media_rank):
        if len(manifest) >= budget:
            break
        manifest.append(by_name[name])
    return manifest


async def copy_staged_images(lead_id: int, slug: str,
                             html: str = "") -> list[str]:
    """Перенести картинки лида из стейджинга в папку превью (дорожка III).

    Копирует бакет сам (CopyObject): байты через бота не проходят вовсе.
    Что именно копировать, решает media_manifest по самой странице. Не
    скопировалось — исключение: публиковать страницу, которая ссылается на
    картинки, которых в бакете нет, хуже, чем не публиковать вовсе.

    Порядок «сначала копии, потом уборка» — не вкусовщина. Слаг у повторной
    публикации тот же, и по нему уже ходит клиент, которому мы отправили
    ссылку. Снеси мы папку первой — оборвавшаяся на середине копия оставила бы
    живую страницу с дырами вместо фотографий насовсем: старых файлов уже нет,
    новые не доехали, а index.html так и остался прежним. Копия поверх
    одноимённого файла безопасна: пока новая страница не выложена, старая
    ссылается на те же имена.

    Хвост прошлой публикации (файлы, которых в нынешнем манифесте нет)
    сносится последним и сбоем публикации не считается: после нового
    index.html на него уже ничто не ссылается, а не убранное доберёт
    expire_previews.
    """
    staged = await list_keys(f"{enrich_prefix(lead_id)}{IMG_DIR}/", limit=100)
    manifest = media_manifest(staged, html)
    old = await list_keys(f"{slug}/{IMG_DIR}/", limit=1000)
    s3, bucket, copied = s3_client(), bucket_name(), []
    for key in manifest:
        target = f"{slug}/{IMG_DIR}/{key.rsplit('/', 1)[-1]}"
        await asyncio.to_thread(
            s3.copy_object, Bucket=bucket,
            CopySource={"Bucket": bucket, "Key": key}, Key=target,
        )
        copied.append(target)
    stale = [key for key in old if key not in copied]
    if stale:
        try:
            await delete_keys(stale)
        except Exception:
            log.warning("лид %s: хвост прошлой публикации не убран под %s/%s/",
                        lead_id, slug, IMG_DIR, exc_info=True)
    return copied


async def list_keys(prefix: str, limit: int = 1000) -> list[str]:
    """Ключи под префиксом. Бакет отдаёт их страницами по 1000."""
    s3, keys, token = s3_client(), [], None
    while len(keys) < limit:
        kw = {"Bucket": bucket_name(), "Prefix": prefix,
              "MaxKeys": min(1000, limit - len(keys))}
        if token:
            kw["ContinuationToken"] = token
        page = await asyncio.to_thread(s3.list_objects_v2, **kw)
        keys += [o["Key"] for o in page.get("Contents") or []]
        token = page.get("NextContinuationToken")
        if not token:
            break
    return keys


async def delete_keys(keys) -> int:
    """Удаление пачками по 1000: столько принимает delete_objects за раз.

    Возвращает число действительно удалённых ключей: пачка отвечает 200 и с
    Errors внутри, и посчитать неудалённое удалённым — значит решить, что
    превью снесено, когда оно живо.
    """
    s3, keys, gone = s3_client(), list(keys), 0
    for start in range(0, len(keys), 1000):
        chunk = keys[start:start + 1000]
        answer = await asyncio.to_thread(
            s3.delete_objects, Bucket=bucket_name(),
            Delete={"Objects": [{"Key": k} for k in chunk]},
        )
        errors = answer.get("Errors") or []
        for err in errors:
            log.warning("R2 не удалил %s: %s", err.get("Key"),
                        err.get("Message") or err.get("Code"))
        gone += len(chunk) - len(errors)
    return gone


async def _publish(lead, draft_id: int, html: str,
                   actor_tg_id: int) -> PublishResult:
    """Резерв слага -> картинки -> страница -> запись адреса.

    Порядок именно такой: слаг в базе появляется раньше объекта в бакете, и
    объекта без строки о нём не остаётся даже при упавшей транзакции. Картинки
    идут раньше index.html — иначе первый же открывший превью увидит страницу
    с дырами на месте фотографий.

    Сбой R2 черновик не отменяет: он остаётся в базе, а выкладку повторит
    /publish по тому же слагу.
    """
    try:
        slug = await reserve_slug(draft_id, lead)
    except Exception as e:
        log.exception("лид %s: слаг не закреплён", lead.id)
        return PublishResult(False, reason=f"слаг не закреплён: {e}")
    try:
        await copy_staged_images(lead.id, slug, html)
    except Exception as e:
        log.exception("лид %s: картинки превью не скопированы", lead.id)
        await _release_slug(draft_id)
        return PublishResult(False, slug=slug,
                             reason=f"картинки не скопированы: {e}")
    try:
        host = await publish_draft(draft_id, html, slug, actor_tg_id=actor_tg_id)
    except Exception as e:
        # сеть, ключи, бакет — исход один: превью нет, а черновик в базе есть
        log.exception("лид %s: превью не выложено", lead.id)
        await _release_slug(draft_id)
        return PublishResult(False, slug=slug, reason=f"R2 недоступен: {e}")
    return PublishResult(True, url=f"https://{host}/", slug=slug)


async def _release_slug(draft_id: int):
    """Вернуть черновик из publishing в собранный: слаг за ним остаётся.

    Не вышло и это — черновик останется в publishing, и его подберёт /publish:
    страница и слаг на месте, повторная выкладка ляжет по тому же адресу.
    """
    try:
        async with Session() as s, s.begin():
            draft = await s.get(Draft, draft_id)
            if draft is not None and draft.status == "publishing":
                draft.status = "generated"
    except Exception:
        log.exception("черновик %s: статус publishing не снят", draft_id)


def _stale_library(draft) -> str:
    """Почему черновик нельзя выложить как есть. Пусто — версия та же.

    Версия библиотеки решает, какие варианты секций выиграют композицию, а
    ключ сохранённого текста — это «вариант.слот». Разъехались версии —
    разъедутся и ключи, и часть страницы соберётся из заготовок рецепта.
    """
    current = str(render.load_tokens()["version"])
    if (draft.library_version or "") == current:
        return ""
    return (f"черновик собран на библиотеке {draft.library_version or '—'}, "
            f"сейчас {current} — пересоберите черновик")


def _expire_reason(draft, lead, deadline: datetime) -> str:
    """Почему превью пора убрать. Пусто — рано."""
    if lead.deleted_at or lead.cancelled_at or lead.status in CLOSED_STATUSES:
        return "лид закрыт"
    if draft.published_at and draft.published_at < deadline:
        return f"срок {PREVIEW_TTL_DAYS} дней"
    return ""


async def _delete_prefix(slug: str) -> int:
    """Снести всё под префиксом слага: превью из одного файла бывает не всегда
    (руками через tools/publish_r2.py уезжает вся папка).

    Удалилось не всё — это неудача целиком: страница ещё открывается, и
    помечать превью снесённым нельзя.
    """
    keys = await list_keys(f"{slug}/", limit=10_000)
    gone = await delete_keys(keys)
    if gone < len(keys):
        raise RuntimeError(f"{slug}: осталось объектов {len(keys) - gone}")
    return gone


async def _mark_expired(draft_id: int, lead_id: int, why: str,
                        actor_tg_id: int):
    """Строка черновика и ссылка на лиде — одной транзакцией с событием."""
    async with Session() as s, s.begin():
        draft = await s.get(Draft, draft_id)
        lead = await s.get(Lead, lead_id)
        draft.status = "expired"
        # ссылка ведёт в 404: в карточке и в письме ей больше не место
        if lead is not None and lead.draft_url == draft.preview_url:
            lead.draft_url = None
        log_event(s, lead_id, "preview_expired", actor_tg_id,
                  old=draft.preview_host, new=why)


async def _save(lead, status, actor_tg_id, *, trace=None, checks=None,
                images=None, missing=(), reason="", author=None,
                slots=None) -> BuildResult:
    """Строка drafts, флаги обогащения на лиде и событие — одной транзакцией."""
    missing = list(missing)
    checks = dict(checks or {})
    async with Session() as s, s.begin():
        row = await s.scalar(
            select(Draft)
            .where(Draft.lead_id == lead.id, Draft.deleted_at.is_(None))
            .with_for_update()
        )
        # упавшая пересборка не трогает уже собранный черновик: его r2_prefix
        # и его страницу мы бы потеряли из-за недоступного API
        keep = status == "failed" and draft_fresh(row)
        if row is None:
            row = Draft(lead_id=lead.id)
            s.add(row)
        if not keep:
            _fill_row(row, status, trace, checks, images, slots)
        # флаг обогащения трогаем только там, где знаем ответ: собралось —
        # снять, не хватило данных — поставить. Упавший API про данные лида
        # не говорит ничего
        if missing or status == "generated":
            fresh = await s.get(Lead, lead.id)
            fresh.needs_enrichment = bool(missing)
            fresh.enrichment_request = _request_text(missing) if missing else None
        log_event(s, lead.id, f"draft_{status}", actor_tg_id,
                  new=reason[:200] or None)
        await s.flush()
        draft_id = row.id
    summary = draft_summary(row, lang_of(lead)) if status == "generated" else ""
    if status == "generated":
        log.info("лид %s: черновик %s собран: %s", lead.id, draft_id,
                 ", ".join(row.section_variants or []))
    return BuildResult(
        ok=status == "generated", draft_id=draft_id, lead_id=lead.id,
        status=status, reason=reason, missing=missing, checks=checks,
        summary=summary, notify_tg_id=_notify(author) if missing else None,
    )


def _fill_row(row, status, trace, checks, images, slots=None):
    trace = trace or {}
    versions = trace.get("versions") or {}
    row.status = status
    row.slots_json = dict(slots or {})
    row.library_version = str(versions.get("library") or "")
    row.seed = trace.get("seed")
    row.recipe_id = trace.get("recipe")
    row.token_preset = trace.get("preset")
    row.section_variants = list(trace.get("sections") or [])
    row.image_ids = list(images or [])
    row.recipe_json = trace
    row.checks_json = checks
    row.generated_at = _now()
    row.expires_at = _now() + timedelta(days=DRAFT_TTL_DAYS)


def _notify(author) -> int | None:
    """Кому идёт просьба дозаполнить: тому, кто лид нашёл, если он ещё с нами."""
    if author is None or author.deleted_at or not author.is_active:
        return None
    return author.tg_id


def _request_text(missing) -> str:
    return "\n".join(f"• {hint}" for hint in missing)


def _ready_slots(texts, sections) -> slot_gen.SlotResult:
    """Готовые тексты слотов вместо вызова модели (дельта 27.08, экономия API).

    Тексты пишет подписочный агент и кладёт их в карточку лида ключом
    DEV_TEXTS_KEY; бот их только расставляет по композиции. Пропущенный,
    слишком длинный или вообще не строковый ключ — ровно то же, что пустой
    ответ модели: слот остаётся пустым, и дальше судьбу секции решает лестница
    деградации site_factory.

    Не-строку тут именно выбрасывают, а не приводят к строке: словарь, попавший
    в карточку вместо текста, уехал бы на страницу клиента питоновским repr.

    Групповые слоты (блёрб услуги, подпись показателя) необязательны: ключа
    нет — элемент остаётся с заготовкой рецепта, и в empty_slots такой слот не
    пишется. Пустой он там ни о чём не говорит: секцию он всё равно не валит.
    Сюда они берутся все, включая привязанные к цифре: подпись под цифрой из
    Google пишет человек, который эту цифру видит, — модель её не видит и в
    свой список слотов не получает.
    """
    texts = texts if isinstance(texts, dict) else {}
    final, empty = {}, []
    for spec in slot_gen.slot_specs(sections, include_fact_bound=True):
        raw = texts.get(spec["slot"])
        if spec.get("grouped") and raw is None:
            continue
        value = raw.strip() if isinstance(raw, str) else ""
        limit = spec.get("max_chars")
        if limit and len(value) > limit:
            value = ""
        final[spec["slot"]] = value
        if not value and not spec.get("grouped"):
            empty.append(spec["slot"])
    return slot_gen.SlotResult(True, texts=final, empty=empty,
                               model="", prompt_version="")


def _media_rank(name: str) -> tuple:
    """Порядок картинок вне страницы: роли по MEDIA_ORDER, потом photo-2, -3…"""
    stem = name.rsplit(".", 1)[0]
    if stem in MEDIA_ORDER:
        return (MEDIA_ORDER.index(stem), 0, name)
    number = stem.rpartition("-")[2]
    return (len(MEDIA_ORDER), int(number) if number.isdigit() else 99, name)


def _image_ids(composition) -> list[str]:
    return sorted({image["src"]
                   for section in composition.sections
                   for image in (section["images"] or {}).values()
                   if image.get("src")})


def _trace_lang(trace: dict) -> str:
    lang = ((trace.get("profile") or {}).get("lang") or {}).get("value")
    return lang if lang in ("uk", "en") else DEFAULT_LANG


def _palette(profile: Profile) -> dict:
    return render.palette_for(profile)


def _clean_images(value) -> dict:
    """Белый список картинок в форме, которую ждёт движок: {имя: {src,w,h}}.

    Размеры обязательны наравне со ссылкой: они держат место под фото, пока оно
    грузится, и без них страница дёргается на глазах у клиента. Запись, где нет
    хотя бы одного из трёх полей, — не картинка, и до разметки она не доходит.
    """
    out = {}
    for name, item in (value or {}).items():
        image = _clean_image(item)
        if image:
            out[str(name)] = image
    return out


def _clean_image(item) -> dict | None:
    if not isinstance(item, dict):
        return None
    src = str(item.get("src") or "").strip()
    width, height = _size(item.get("width")), _size(item.get("height"))
    if not (src and width and height):
        return None
    return {"src": src, "width": width, "height": height}


def _clean_products(value) -> list[dict]:
    """Товары в форме движка: [{name, price?, image?}].

    Товар без названия показать нечем; цена — строка ровно та, что пишет сам
    бизнес; картинка проходит те же три поля, что и остальные снимки.
    """
    out = []
    for item in value or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        row = {"name": name}
        price = str(item.get("price") or "").strip()
        if price:
            row["price"] = price
        image = _clean_image(item.get("image"))
        if image:
            row["image"] = image
        out.append(row)
    return out


# Кусок расписания стоит отдельной строкой, если сам называет свой день:
# «Пн-Пт: 09:00-19:00» через «: » или «Сб 10:00-17:00» через пробел перед
# временем. Ровно то же деление потом делает движковый _split_hours.
_HOURS_DAY_TIME = re.compile(r"\S\s+\d{1,2}[:.]\d{2}")


def _clean_hours(value) -> list[str]:
    """Часы в форме движка: список строк, по строке на день.

    Ручную строку из карточки работник пишет одной строкой, поэтому «;» режет
    её всегда. Запятая режет только тогда, когда каждый кусок сам называет
    свой день: «Пн-Пт 9:00-19:00, Сб 10:00-17:00» — два дня, а «Сб, Нд:
    вихідний» и «Пн-Пт: 09:00-13:00, 14:00-18:00» (обед) — по одной строке,
    и резать их значит выбросить половину расписания.

    Авто-скрейп уже отдаёт list[str] и пересплиту не подлежит вовсе.
    """
    if isinstance(value, str):
        parts = _split_hours_line(value)
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        return []
    return [str(part).strip() for part in parts if str(part).strip()]


def _split_hours_line(value: str) -> list[str]:
    out = []
    for chunk in value.split(";"):
        pieces = chunk.split(",")
        if len(pieces) > 1 and all(_names_its_day(piece) for piece in pieces):
            out.extend(pieces)
        else:
            out.append(chunk)
    return out


def _names_its_day(piece: str) -> bool:
    return ": " in piece or bool(_HOURS_DAY_TIME.search(piece))


def _size(value) -> int | None:
    try:
        size = int(value)
    except (TypeError, ValueError):
        return None
    return size if size > 0 else None


def _rating(value) -> float | None:
    try:
        return float(str(value or "").replace(",", "."))
    except ValueError:
        return None


async def _contacts(session, lead_id: int) -> dict:
    """Телефон и почта лида — первые непустые. Остальные каналы движку не нужны."""
    rows = await session.execute(
        select(Contact.ctype, Contact.value)
        .where(Contact.lead_id == lead_id, Contact.deleted_at.is_(None))
        .order_by(Contact.id)
    )
    found = {}
    for ctype, value in rows:
        key = {"phone": "phone", "email": "email"}.get(ctype)
        if key and value and key not in found:
            found[key] = value
    return found
