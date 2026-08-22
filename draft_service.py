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

Неудачная пересборка не затирает уже собранный черновик: у опубликованного
превью в строке лежит r2_prefix, и потерять его из-за упавшего API нельзя.

Публикации отсюда нет: publish_draft() написан, но не вызывается ни одной
строкой кода — до деплоя Worker превью в интернет не уходит.
"""
import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select

import config
import slot_gen
from models import (
    DRAFT_TTL_DAYS, Contact, Draft, Lead, Session, Worker, draft_fresh,
    log_event,
)
from site_factory.engine import render
from site_factory.engine.checks import run_all
from site_factory.engine.compose import compose
from site_factory.engine.profile import Profile

log = logging.getLogger(__name__)

# Язык черновика — язык страны лида, а не язык карточки (8.29): страницу
# читает клиент компании, а не работник, который её нашёл.
UA_LANG, DEFAULT_LANG = "uk", "en"

# Признаки, которые обогащение вправе принести в профиль. Всё, чего нет в
# этом списке, движок не понимает, а чего нет в enrichment — неизвестно
# (unknown != false, Д13 §3 шаг 1).
ENRICHMENT_FIELDS = (
    "services", "service_count", "has_prices", "has_hours", "hours",
    "has_booking_url", "booking_url", "review_count", "google_rating",
    "has_address", "address", "address_parts", "photo_count", "text_volume",
    "old_site_state", "brand_colors", "images", "phone", "email", "name",
    "city", "niche",
)

# Описание черновика для <draft> письма: по одной формуле на вариант секции.
# Модель здесь не участвует — письмо должно называть то, что на странице
# действительно есть, а не то, что складно звучит.
SUMMARY_PARTS = {
    "hero_split_map": {"uk": "головна з картою і телефоном",
                       "en": "homepage with a map and phone"},
    "hero_photo_left": {"uk": "головна з фото і телефоном",
                        "en": "homepage with a photo and phone"},
    "hero_type_only": {"uk": "головна з телефоном угорі",
                       "en": "homepage with the phone on top"},
    "svc_cards_3": {"uk": "картки послуг", "en": "service cards"},
    "svc_list_icons": {"uk": "перелік послуг", "en": "a list of services"},
    "proof_stats_bar": {"uk": "оцінка і відгуки з Google",
                        "en": "the Google rating and reviews"},
    "cta_form_short": {"uk": "форма звернення", "en": "an enquiry form"},
    "footer_nap": {"uk": "контакти і години", "en": "contacts and hours"},
}
SUMMARY_MIN_WORDS, SUMMARY_MAX_WORDS = 5, 12

PREVIEW_HOST_SUFFIX = ".tobisitepreview.com"


@dataclass(frozen=True)
class BuildResult:
    """Итог сборки. missing непустой — лиду нужно обогащение, а не ремонт."""
    ok: bool
    draft_id: int | None = None
    status: str = ""
    reason: str = ""
    missing: list[str] = field(default_factory=list)
    checks: dict = field(default_factory=dict)
    notify_tg_id: int | None = None
    summary: str = ""

    @property
    def needs_enrichment(self) -> bool:
        return bool(self.missing)


async def build_draft(lead_id: int, *,
                      actor_tg_id: int = config.ADMIN_TG_ID) -> BuildResult:
    """Собрать черновик лида. Один активный черновик на лид, пересборка — в него."""
    async with Session() as s:
        lead = await s.get(Lead, lead_id)
        if lead is None or lead.deleted_at or lead.cancelled_at:
            return BuildResult(False, reason="лид недоступен")
        profile = await build_profile(s, lead)
        author = await s.get(Worker, lead.worker_id)

    composition = compose_for(profile)
    if not composition.ok:
        # тот же расклад ещё раз, но уже полным следом решения для recipe_json
        _, trace = render.render(profile)
        return await _save(lead, "failed", actor_tg_id, trace=trace,
                           missing=composition.needs_enrichment, author=author,
                           reason="данных не хватает на страницу")

    lang = str(profile.lang.value)
    slots = await slot_gen.fill_slots(profile, composition.sections, lang,
                                      lead_id=lead.id)
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
    return await _save(lead, status, actor_tg_id, trace=trace, checks=problems,
                       images=_image_ids(composition), reason=reason)


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


async def publish_draft(draft_id: int, html: str, slug: str,
                        bucket: str | None = None) -> str:
    """Выложить черновик в R2 и пометить строку published.

    НЕ ВЫЗЫВАЕТСЯ НИОТКУДА. Worker раздачи превью ещё не задеплоен (для этого
    нужен wrangler login в браузере основателя), а страница без раздачи — это
    ссылка в никуда в коммерческом письме. Пока публикация выполняется руками:
    tools/publish_r2.py делает те же PUT и печатает слаг для этой функции.
    Автоматической публикации в конвейере нет и не будет до деплоя Worker.
    """
    import boto3

    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://{_env('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com",
        aws_access_key_id=_env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_env("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )
    bucket = bucket or os.getenv("R2_BUCKET") or "tobisite-previews"
    await asyncio.to_thread(
        s3.put_object, Bucket=bucket, Key=f"{slug}/index.html",
        Body=html.encode(), ContentType="text/html; charset=utf-8",
        CacheControl="no-cache",
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
    return host


# --- внутреннее ---------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(config.TZ)


def _env(name: str) -> str:
    """Ключи R2 читаются здесь, а не в config: бот стартует и без них."""
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"не задана переменная окружения {name}")
    return value


async def _save(lead, status, actor_tg_id, *, trace=None, checks=None,
                images=None, missing=(), reason="", author=None) -> BuildResult:
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
            _fill_row(row, status, trace, checks, images)
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
        ok=status == "generated", draft_id=draft_id, status=status,
        reason=reason, missing=missing, checks=checks, summary=summary,
        notify_tg_id=_notify(author) if missing else None,
    )


def _fill_row(row, status, trace, checks, images):
    trace = trace or {}
    versions = trace.get("versions") or {}
    row.status = status
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


def _image_ids(composition) -> list[str]:
    return sorted({image["src"]
                   for section in composition.sections
                   for image in (section["images"] or {}).values()
                   if image.get("src")})


def _trace_lang(trace: dict) -> str:
    lang = ((trace.get("profile") or {}).get("lang") or {}).get("value")
    return lang if lang in ("uk", "en") else DEFAULT_LANG


def _palette(profile: Profile) -> dict:
    tokens = render.load_tokens()
    preset = render.resolve_preset(
        render.preset_for(profile.domain_norm, tokens), tokens
    )
    return preset["palette"]


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
