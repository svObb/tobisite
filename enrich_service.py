"""Обогащение карточки лида с его собственного сайта (дорожка III).

Одна кнопка в карточке: бот открывает сайт компании, забирает оттуда логотип,
фотографии, товары, услуги, часы, адрес и бренд-цвета, кладёт картинки в
стейджинг R2 и дописывает `leads.enrichment`. Дальше этими данными пользуется
сборка черновика — код сборки при этом не меняется вовсе.

Три правила слияния, и они разные, потому что разного происхождения данные:

* **scraper-owned** (`images`, `photo_count`, `brand_colors`, `products`) —
  ведёт скрейп. Повторное нажатие переписывает их целиком: на сайте сменили
  логотип — сменится и у нас;
* **promote-if-missing-or-ours** (`services`, `hours`, `address`…) — скрейп их
  только предлагает. Значение, которого он в прошлый раз не писал, — ручное, и
  оно переживает любое число перескрейпов. Список написанного лежит в
  `_scrape.written`;
* **никогда** — `name`. Название компании в карточке правил человек, а в
  <title> чужого сайта лежит что угодно.

Служебный ключ `_scrape` начинается с подчёркивания, поэтому в профиль движка
он не просачивается: build_profile берёт только ENRICHMENT_FIELDS.

Картинки живут в бакете под `_enrich/<lead_id>/img/`. Ни диска, ни /tmp:
файловая система бота read-only, а tmpfs умирает вместе с контейнером.
Подчёркивание в префиксе не проходит проверку слага в воркере — снаружи этих
файлов не видно, пока публикация не скопирует их в `<slug>/img/`.
"""
import asyncio
import json
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal

import aiohttp
import anthropic
from sqlalchemy import select

import config
import costs
import draft_service
import site_images
import site_scrape
from models import Contact, Lead, Session, log_event

log = logging.getLogger(__name__)

# Бесплатная строка в /costs: без неё неоткуда узнать, сколько сайтов бот
# обошёл. Платит обогащение только за ИИ-ветку, и та пишется своим op.
SCRAPE_OP = "scrape"
COST_OP = "enrich"

MODEL = "claude-haiku-4-5-20251001"
PROMPT_VERSION = "e1"
MAX_TOKENS = 1500
# Прайс Haiku 4.5, $/1M токенов.
PRICE_IN, PRICE_OUT = Decimal("1"), Decimal("5")
MILLION = Decimal("1000000")

STAGING_ROOT = "_enrich"
IMG_DIR = "img"

# Ключи, которые ведёт скрейп: перескрейп переписывает их целиком.
SCRAPER_OWNED = ("images", "photo_count", "brand_colors", "products")
# Ключи, которые скрейп только предлагает: ручное значение переживает перескрейп.
PROMOTED = ("services", "service_count", "hours", "has_hours", "address",
            "address_parts", "has_address", "text_volume", "old_site_state")
# Контакты особые: enrichment перекрывает контакты лида при сборке профиля, а
# телефон из подвала чужого шаблона бывает не тот. Поэтому предлагаем их только
# лиду, у которого контакта такого типа нет вовсе.
CONTACT_PROMOTED = ("phone", "email")
SCRAPE_KEY = "_scrape"
# Что отчёт называет найденным и ненайденным: людскими словами, а не ключами
# схемы — отчёт читает человек, а производные вроде service_count ему не нужны.
REPORT_FIELDS = (("services", "услуги"), ("hours", "часы"),
                 ("address", "адрес"), ("phone", "телефон"),
                 ("email", "почта"), ("products", "товары"),
                 ("brand_colors", "цвета бренда"))
# Производных признаков (service_count, has_hours) в этой таблице нет
# намеренно: они едут вместе со своим полем и отдельной строкой в отчёте были
# бы шумом.
FIELD_LABELS = {
    "services": "услуги", "hours": "часы", "address": "адрес",
    "address_parts": "адрес по частям", "phone": "телефон", "email": "почта",
    "images": "картинки", "photo_count": "число фото", "products": "товары",
    "brand_colors": "цвета бренда", "text_volume": "объём текста",
    "old_site_state": "состояние сайта",
}

# Сколько кандидатов каждого вида доходит до скачивания. Дальше потолки
# download_images: 12 файлов, 5 МБ на файл, 20 МБ всего.
LOGO_CANDIDATES = 2
PHOTO_CANDIDATES = 6
PRODUCT_CANDIDATES = 6

SYSTEM_PROMPT = """Ты разбираешь текст сайта небольшой местной компании и достаёшь из него две вещи: перечень услуг и часы работы. Больше ничего.

ЧТО ТЕБЕ ДАЮТ
Куски текста с сайта компании: заголовки, абзацы, пункты списков. Разметки нет, порядок кусков — порядок страницы. Часть кусков к делу не относится: меню навигации, юридические строки, названия соседних разделов.

УСЛУГИ
— то, за что компании платят деньги: «Лікування карієсу», «Заміна масла», «Ремонт даху»;
— бери формулировку с сайта, не переписывай её своими словами;
— до восьми услуг, каждая до шестидесяти символов;
— не услуги: «Про нас», «Контакти», «Головна», «Наші переваги», «Замовити дзвінок», названия городов, имена сотрудников, слоганы;
— на сайте нет перечня услуг — верни пустой список. Пустой список лучше выдуманного.

ЧАСЫ РАБОТЫ
— строки вида «день: время», ровно как они написаны на сайте: «Пн–Пт: 09:00–18:00», «Сб: 10:00–15:00»;
— до семи строк;
— время бери с сайта до минуты; ни одной цифры, которой в тексте нет;
— «цілодобово», «без вихідних» и подобное — это строка целиком, без времени;
— часов на сайте нет — верни пустой список.

ЧЕГО НЕ ДЕЛАТЬ
— не придумывать ни услуг, ни часов, ни цен;
— не переводить: язык сайта — язык ответа;
— не добавлять пояснений, не писать markdown;
— не отвечать ничем, кроме JSON.

ФОРМАТ ВЫВОДА
{"services": ["строка", ...], "hours": ["строка", ...]}

ПРИМЕР
Текст: «Стоматологія Лінія. Головна. Про нас. Контакти. Ми лікуємо зуби дорослим і дітям. Лікування карієсу без болю. Професійна гігієна порожнини рота. Протезування коронками. Працюємо з понеділка по пʼятницю з 9:00 до 18:00, у суботу з 10:00 до 15:00. Неділя — вихідний.»
{"services": ["Лікування карієсу", "Професійна гігієна порожнини рота", "Протезування коронками"], "hours": ["Пн–Пт: 09:00–18:00", "Сб: 10:00–15:00"]}"""


@dataclass(frozen=True)
class EnrichResult:
    """Итог обогащения. ok=False — enrichment не тронут, reason объясняет."""
    ok: bool = False
    lead_id: int | None = None
    reason: str = ""
    url: str = ""
    pages: int = 0
    written: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    empty: list[str] = field(default_factory=list)
    staged: list[str] = field(default_factory=list)
    images_reason: str = ""
    phone_diff: str = ""
    ai_note: str = ""
    logo_note: str = ""


@dataclass(frozen=True)
class MergeResult:
    """Слияние найденного с тем, что в карточке уже было."""
    enrichment: dict
    written: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)


# Один прогон на лида: обогащение сносит и перекладывает его картинки, и два
# параллельных нажатия оставили бы в бакете половину старых, половину новых.
_running: set[int] = set()


def enrich_busy(lead_id: int) -> bool:
    return lead_id in _running


def staging_prefix(lead_id: int) -> str:
    return f"{STAGING_ROOT}/{lead_id}/"


def image_key(lead_id: int, filename: str) -> str:
    return f"{staging_prefix(lead_id)}{IMG_DIR}/{filename}"


async def enrich_from_site(lead_id: int, *,
                           actor_tg_id: int = config.ADMIN_TG_ID) -> EnrichResult:
    """Обойти сайт лида и дописать его карточку. Проверка лида не требуется.

    Занятость проверяется и занимается одним куском без единого await между:
    в однопоточном asyncio такой кусок не прерывается, и второе нажатие кнопки
    упирается в занятость, а не проскакивает, пока первое ждёт ответа базы.
    """
    if lead_id in _running:
        return EnrichResult(reason="этот лид уже обогащается", lead_id=lead_id)
    _running.add(lead_id)
    try:
        async with Session() as s:
            lead = await s.get(Lead, lead_id)
            if lead is None or lead.deleted_at or lead.cancelled_at:
                return EnrichResult(reason="лид недоступен", lead_id=lead_id)
            url = (lead.website_url or lead.domain_norm or "").strip()
            country, enrichment = lead.country, dict(lead.enrichment or {})
            types = set(await s.scalars(
                select(Contact.ctype).where(Contact.lead_id == lead_id,
                                            Contact.deleted_at.is_(None))
            ))
        if not url:
            return EnrichResult(reason="у лида нет сайта", lead_id=lead_id)
        return await _run(lead_id, url, country, enrichment, types, actor_tg_id)
    finally:
        _running.discard(lead_id)


def merge_enrichment(current: dict, found: dict, *,
                     contact_types=()) -> MergeResult:
    """Слить найденное с карточкой по трём правилам (см. докстринг модуля).

    Чистая функция: ни базы, ни сети — её и гоняют тесты идемпотентности.
    """
    result = dict(current or {})
    previous = set(((result.get(SCRAPE_KEY) or {}).get("written")) or [])
    written, kept = [], []
    for key in SCRAPER_OWNED:
        if key in found:
            result[key] = found[key]
            written.append(key)
        elif key in previous:
            # прошлый скрейп это писал, нынешний не видит — значит на сайте
            # этого больше нет, и держать устаревшее было бы враньём
            result.pop(key, None)
    for key in PROMOTED:
        if key not in found:
            continue
        if key in result and key not in previous:
            kept.append(key)
            continue
        result[key] = found[key]
        written.append(key)
    for key in CONTACT_PROMOTED:
        if key not in found:
            continue
        if key in contact_types or (key in result and key not in previous):
            kept.append(key)
            continue
        result[key] = found[key]
        written.append(key)
    return MergeResult(result, sorted(written), sorted(kept))


def found_fields(scrape, staged: dict, colors: dict, *,
                 looked_at_images: bool = True) -> dict:
    """Что скрейп предлагает записать в enrichment. Пустого здесь не бывает.

    Ключ появляется, только если значение действительно есть: пустой список
    услуг и неспрошенные услуги для гейтов движка — разные вещи. Ровно поэтому
    images и photo_count пишутся, только когда картинки правда смотрели: без
    ключей R2 «фотографий ноль» было бы утверждением, которого мы не делали.
    """
    found: dict = {}
    if scrape.services:
        found["services"] = list(scrape.services)
        found["service_count"] = len(scrape.services)
    if scrape.hours:
        found["hours"] = list(scrape.hours)
        found["has_hours"] = True
    if scrape.address.get("display"):
        found["address"] = scrape.address["display"]
        found["has_address"] = True
        if scrape.address.get("parts"):
            found["address_parts"] = dict(scrape.address["parts"])
    if scrape.text_volume:
        found["text_volume"] = scrape.text_volume
    if scrape.old_site_state:
        found["old_site_state"] = scrape.old_site_state
    if scrape.phones:
        found["phone"] = scrape.phones[0]
    if scrape.emails:
        found["email"] = scrape.emails[0]
    if colors:
        found["brand_colors"] = dict(colors)
    if looked_at_images:
        # images и photo_count пишутся всегда вместе: инвариант «photo_count
        # равен числу выложенных контентных фото» держится этой строкой
        found["images"] = {
            name: {"src": item["src"], "width": item["width"],
                   "height": item["height"]}
            for name, item in staged.items()
        }
        found["photo_count"] = len(site_images.photo_names(staged))
    products = _products_with_images(scrape.products, staged)
    if products:
        found["products"] = products
    return found


def enrich_line(lead) -> str:
    """Строка карточки о том, что бот взял с сайта. Пусто — обогащения не было."""
    enrichment = getattr(lead, "enrichment", None) or {}
    journal = enrichment.get(SCRAPE_KEY) or {}
    if not journal:
        return ""
    images = enrichment.get("images") or {}
    parts = [f"страниц {len(journal.get('pages') or [])}"]
    if images:
        parts.append("логотип есть" if "logo" in images else "логотипа нет")
        parts.append(f"фото {len([n for n in images if n != 'logo'])}")
    for key, label in (("services", "услуг"), ("products", "товаров"),
                       ("hours", "строк часов")):
        values = enrichment.get(key)
        if values:
            parts.append(f"{label} {len(values)}")
    return f"{journal.get('at', '')[:16]}: " + ", ".join(parts)


# --- внутреннее ---------------------------------------------------------------

async def _run(lead_id, url, country, enrichment, contact_types,
               actor_tg_id) -> EnrichResult:
    region = config.COUNTRY_ISO.get(country) or None
    async with aiohttp.ClientSession(timeout=site_scrape.TIMEOUT) as session:
        scrape = await site_scrape.scrape_site(url, region=region,
                                               session=session)
        await costs.log_api(op=SCRAPE_OP, lead_id=lead_id,
                            note=f"обход {url}: страниц {len(scrape.pages)}")
        if not scrape.ok:
            return EnrichResult(reason=scrape.reason or "сайт не открылся",
                                lead_id=lead_id, url=url)
        scrape, ai_note = await _ask_model(scrape, lead_id)
        staged, images_reason, looked, logo_note = await _stage(
            session, lead_id, scrape)

    colors = _brand_colors(scrape, staged)
    found = found_fields(scrape, staged, colors, looked_at_images=looked)
    merged = merge_enrichment(enrichment, found, contact_types=contact_types)
    journal = {
        "url": scrape.url,
        "at": datetime.now(config.TZ).isoformat(timespec="seconds"),
        "pages": list(scrape.pages),
        "written": merged.written,
        "found": {"phone": scrape.phones[0] if scrape.phones else "",
                  "name": scrape.name},
        "products": [dict(item) for item in scrape.products],
        "ai": ai_note,
    }
    enriched = dict(merged.enrichment)
    enriched[SCRAPE_KEY] = journal
    async with Session() as s, s.begin():
        lead = await s.get(Lead, lead_id)
        if lead is None:
            return EnrichResult(reason="лид недоступен", lead_id=lead_id)
        # присваиванием: JSONB меняется целым значением, правка вложенного
        # словаря на месте до базы не доедет
        lead.enrichment = enriched
        log_event(s, lead_id, "site_scraped", actor_tg_id,
                  new=", ".join(merged.written)[:200] or "ничего")
    return EnrichResult(
        ok=True, lead_id=lead_id, url=scrape.url, pages=len(scrape.pages),
        written=merged.written, kept=merged.kept,
        empty=[label for key, label in REPORT_FIELDS if key not in found],
        staged=sorted(staged), images_reason=images_reason,
        phone_diff=_phone_diff(scrape, contact_types), ai_note=ai_note,
        logo_note=logo_note,
    )


def _phone_diff(scrape, contact_types) -> str:
    """Найденный телефон, который мы НЕ записали: у лида уже есть свой."""
    if not scrape.phones or "phone" not in contact_types:
        return ""
    return scrape.phones[0]


def _brand_colors(scrape, staged: dict) -> dict:
    """Цвета бренда: со страницы, а если их там нет — с логотипа."""
    if scrape.brand_colors:
        return scrape.brand_colors
    logo = staged.get("logo")
    if logo is None or not logo.get("colors"):
        return {}
    colors = logo["colors"]
    picked = {"primary": colors[0], "source": "logo"}
    if len(colors) > 1:
        picked["accent"] = colors[1]
    return picked


def _products_with_images(products, staged: dict) -> list[dict]:
    """Товары для секций движка: картинка — ссылка на выложенный файл.

    Товар, чья картинка в стейджинг не попала, остаётся без неё: гейт
    `has_image` отсеет его сам, а ссылка на чужой хост в превью не поедет.
    """
    by_url = {item["source"]: (name, item) for name, item in staged.items()
              if item.get("source")}
    out = []
    for product in products:
        row = {"name": product["name"]}
        if product.get("price"):
            row["price"] = product["price"]
        found = by_url.get(product.get("image") or "")
        if found is not None:
            _, item = found
            row["image"] = {"src": item["src"], "width": item["width"],
                            "height": item["height"]}
        out.append(row)
    return out


async def _stage(session, lead_id: int, scrape) -> tuple[dict, str, bool, str]:
    """Скачать, пережать и выложить картинки.

    Возвращает ({имя: запись}, причину пропуска, смотрели ли вообще, строку о
    логотипе). Третий флаг отделяет «на сайте фотографий нет» от «мы не
    проверяли»: первое пишется в enrichment нулём, второе не пишется никак.
    Четвёртая — то, что админу придётся доделать руками.
    """
    if not draft_service.r2_ready():
        return {}, "не заданы ключи R2 — картинки пропущены", False, ""
    product_images = [p["image"] for p in scrape.products if p.get("image")]
    wanted = (
        [(c["url"], "logo", False, False)
         for c in scrape.logos if c.get("url")][:LOGO_CANDIDATES]
        + [(c["url"], "photo", c.get("og", False), False)
           for c in scrape.images][:PHOTO_CANDIDATES]
        + [(url, "photo", False, True)
           for url in product_images][:PRODUCT_CANDIDATES]
    )
    kinds = {}
    for url, kind, og, product in wanted:
        kinds.setdefault(url, (kind, og, product))
    blobs = await site_scrape.download_images(session, list(kinds))
    candidates, bodies = [], {}
    for url, data in blobs:
        kind, og, product = kinds[url]
        size = site_images.probe_image(data)
        if size is None or not site_images.fits(size, kind):
            continue
        bodies[url] = data
        candidates.append({"url": url, "kind": kind, "og": og,
                           "product": product, **size})
    roles = site_images.assign_roles(candidates)
    files = _render_files(roles, bodies)
    logo_note = ""
    if "logo" not in files:
        svg, logo_note = _inline_logo(scrape)
        if svg is not None:
            files = {"logo": svg} | files
    if not files:
        return ({}, "картинок, годных для страницы, на сайте не нашлось",
                True, logo_note)
    try:
        await _put_all(lead_id, files)
    except Exception as e:
        log.exception("лид %s: стейджинг картинок не удался", lead_id)
        return {}, f"картинки не выложены: {e}", False, logo_note
    return ({name: _record(item) for name, item in files.items()},
            "", True, logo_note)


def _render_files(roles: dict, bodies: dict) -> dict:
    """Роли → готовые к выкладке файлы. Не пережалось — роли просто нет."""
    files = {}
    for name, candidate in roles.items():
        data = bodies.get(candidate["url"])
        if data is None:
            continue
        made = site_images.process_image(data, site_images.role_of(name))
        if made is None:
            continue
        made["filename"] = f"{name}.webp"
        made["source"] = candidate["url"]
        if name == "logo":
            made["colors"] = site_images.dominant_colors(data)
        files[name] = made
    return files


def _inline_logo(scrape) -> tuple[dict | None, str]:
    """Логотип, нарисованный прямо в HTML. Санитайзер сомнений не прощает.

    Второе значение — строка для отчёта админу. Размеры берём из самой
    очищенной разметки: без них движок выбросит запись при сборке страницы, и
    получилось бы «логотип есть» в отчёте при пустой шапке на превью.
    """
    note = ""
    for candidate in scrape.logos:
        if candidate.get("kind") != "svg":
            continue
        clean = site_images.sanitize_svg(candidate.get("markup") or "")
        if clean is None:
            continue
        size = site_images.svg_size(clean)
        if size is None:
            note = "SVG-логотип без размеров — взять руками"
            continue
        return {"data": clean.encode(), "content_type": "image/svg+xml",
                "filename": "logo.svg", **size,
                "source": "", "colors": []}, ""
    return None, note


def _record(item: dict) -> dict:
    record = {"src": f"/{IMG_DIR}/{item['filename']}", "width": item["width"],
              "height": item["height"], "source": item["source"]}
    if item.get("colors"):
        record["colors"] = item["colors"]
    return record


async def _put_all(lead_id: int, files: dict):
    """Снести прежний стейджинг лида и выложить новый.

    Сносим целиком: имена ролей фиксированные, но прошлый скрейп мог оставить
    photo-5, которого в этот раз нет, и он уехал бы в публикацию призраком.
    """
    old = await draft_service.list_keys(staging_prefix(lead_id), limit=10_000)
    if old:
        await draft_service.delete_keys(old)
    s3 = draft_service.s3_client()
    for item in files.values():
        await asyncio.to_thread(
            s3.put_object, Bucket=draft_service.bucket_name(),
            Key=image_key(lead_id, item["filename"]), Body=item["data"],
            ContentType=item["content_type"], CacheControl="no-cache",
        )


# --- ИИ-ветка -----------------------------------------------------------------

async def _ask_model(scrape, lead_id: int) -> tuple:
    """Дозаполнить услуги и часы моделью. (результат скрейпа, строка отчёта).

    Ветка необязательная и вдобавок за флагом `ENRICH_AI` (по умолчанию
    выключена). Даже включённая, она берётся за дело только там, где разбор
    DOM объективно не справился: услуг меньше двух при живой прозе или часы не
    распарсились. На сайте без текста спрашивать нечего, и денег мы на него не
    тратим.

    Любая неудача — деградация на результат DOM: страница соберётся из того,
    что нашли своими руками, и в отчёте будет написано, почему модели не было.
    """
    need = _model_needed(scrape)
    if not need:
        return scrape, ""
    if not config.ENRICH_AI:
        return scrape, "не звалась — ENRICH_AI выключен"
    if not config.ANTHROPIC_API_KEY:
        return scrape, "не звалась — нет ANTHROPIC_API_KEY"
    if await costs.cap_reached():
        return scrape, "не звалась — месячный кэп расходов исчерпан"
    try:
        response = await client().messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            # кэш префикса здесь не ставится намеренно: вызов на лида ровно
            # один, а живёт кэш пять минут — платить за запись было бы не за что
            system=SYSTEM_PROMPT,
            thinking={"type": "disabled"},
            messages=[{"role": "user", "content": _user_prompt(scrape)}],
        )
    except anthropic.APIError as e:
        log.warning("обогащение %s: ошибка API: %s", lead_id, e)
        return scrape, f"недоступна — {e.__class__.__name__}"
    await _log_cost(response.usage, lead_id=lead_id)
    if getattr(response, "stop_reason", "") == "max_tokens":
        return scrape, "ответ обрезан лимитом токенов"
    parsed = _parse(response)
    if parsed is None:
        return scrape, "ответила не по формату"
    added, patch = [], {}
    for key in need:
        values = parsed.get(key) or []
        if not values:
            continue
        patch[key] = values
        added.append(f"{_LABELS[key]} {len(values)}")
    if not patch:
        return scrape, "ничего не добавила"
    return replace(scrape, **patch), "дополнила: " + ", ".join(added)


_LABELS = {"services": "услуг", "hours": "строк часов"}


def _model_needed(scrape) -> list[str]:
    need = []
    if len(scrape.services) < 2 and scrape.text_volume in ("medium", "long"):
        need.append("services")
    if not scrape.hours:
        need.append("hours")
    return need


def _user_prompt(scrape) -> str:
    return ("<site>" + "\n".join(scrape.excerpts) + "</site>\n\n"
            "Достань услуги и часы работы. Ответь JSON.")


def _parse(response) -> dict | None:
    """Строгий разбор: не JSON, не те типы, мусор в списках — значит нет ответа."""
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    out = {}
    for key, cap, width in (("services", site_scrape.MAX_SERVICES,
                             site_scrape.SERVICE_MAX_CHARS),
                            ("hours", site_scrape.MAX_HOURS, 120)):
        rows = data.get(key)
        if not isinstance(rows, list):
            continue
        clean = []
        for row in rows:
            if not isinstance(row, str):
                continue
            line = " ".join(row.split())[:width]
            if len(line) >= 3 and line not in clean:
                clean.append(line)
        out[key] = clean[:cap]
    return out or None


_client = None


def client():
    """Ленивый клиент: без ключа сюда не доходит ни один вызов."""
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


async def _log_cost(usage, *, lead_id: int):
    cost = (Decimal(usage.input_tokens) * PRICE_IN
            + Decimal(usage.output_tokens) * PRICE_OUT) / MILLION
    await costs.log_cost(op=COST_OP, model=MODEL, cost_usd=cost,
                         input_tokens=usage.input_tokens,
                         output_tokens=usage.output_tokens,
                         lead_id=lead_id,
                         note=f"обогащение, промпт {PROMPT_VERSION}")
