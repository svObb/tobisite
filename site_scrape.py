"""Скрейп сайта лида: логотип, товары, услуги, часы, адрес, цвета (дорожка III).

Разбор отделён от сети ровно так же, как в scout/site_probe.py: всё выше
раздела «сеть» — чистые функции над байтами страницы, и их гоняют тесты на
фикстурах, не поднимая ни одного соединения.

Три правила, которые здесь важнее удобства:

* **ошибка сети — это данные.** Закрытый сайт, таймаут, 403 — всё это ok=False
  с причиной, а не исключение и не повод обходить защиту. Обходов антибота в
  этом модуле нет и не будет: чужой сайт мы читаем в гостях;
* **потолки везде.** Страниц не больше MAX_PAGES, картинок MAX_IMAGES, товаров
  MAX_PRODUCTS, услуг MAX_SERVICES: JSON-LD на чужом сайте бывает какой угодно
  длины, и разбирать его целиком мы не подряжались;
* **структура важнее эвристики.** Сначала JSON-LD и microdata, и только потом
  догадки по классам. Догадка, попавшая в enrichment, уедет на страницу
  клиента, поэтому «не уверен» значит «не пиши».

Адрес сюда приходит чужой, поэтому ходим мы не куда попало: `target_allowed`
пропускает только публичный веб, и каждый редирект проверяется отдельно —
иначе сайт лида перенаправил бы нас на 169.254.169.254 или на соседний порт
localhost, а прочитанное уехало бы в enrichment и оттуда в публичное превью.

Остаточный риск известен и принят: адрес мы резолвим сами, а соединение по
имени открывает aiohttp — между этими двумя шагами DNS успевает ответить
второй раз и другим адресом (DNS rebinding). Пиннинг IP потребовал бы своего
коннектора и подмены Host на каждом хопе; за защиту от чужого сайта, который
специально держит такой резолвер, эта цена не платится.
"""
import asyncio
import ipaddress
import json
import logging
import re
import socket
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urljoin, urlparse

import aiohttp
import phonenumbers
from bs4 import BeautifulSoup, UnicodeDammit

import config
from scout.site_probe import analyze_html

log = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=20)
UA = "tobisite-enrich/1.0"
MAX_HTML = 1_500_000
# Страниц на сайт: главная плюс до трёх внутренних (контакты, о нас, каталог).
MAX_PAGES = 4
# Пауза между страницами одного хоста: вежливость, а не оптимизация.
PAUSE_SEC = 0.5

MAX_IMAGES = 12
MAX_IMAGE_BYTES = 5_000_000
MAX_TOTAL_BYTES = 20_000_000

# Редиректы отслеживаем сами, чтобы проверить каждый адрес цепочки. Пять хопов
# хватает любому нормальному сайту (http -> https -> www -> язык -> слеш).
MAX_REDIRECTS = 5
REDIRECT_STATUS = (301, 302, 303, 307, 308)
# Порт: либо подразумеваемый схемой, либо явно записанный, но только вебовый.
# Сайт компании на 6379 или 9200 не живёт — там живёт чужой Redis.
ALLOWED_PORTS = (80, 443)
# Причина отказа гарда. Такие же данные, как таймаут: сайт не прочитан.
BLOCKED_TARGET = "blocked_target"

MAX_PRODUCTS = 12
MAX_SERVICES = 8
SERVICE_MAX_CHARS = 60
MAX_HOURS = 7
MAX_PHONES = 5
MAX_EMAILS = 5
MAX_LINKS = 3
# Узлов JSON-LD, которые вообще разбираются: каталог на тысячу позиций нам не
# нужен, а разложить его в память сайт может попросить запросто.
MAX_LD_NODES = 400
# Сколько текста уезжает в ИИ-ветку обогащения. Кириллица — около двух символов
# на токен, то есть это обещанные плану четыре тысячи токенов с запасом.
MAX_EXCERPT_CHARS = 8_000

# Признаки страницы-заглушки антибота. Ни один из них не повод что-то обходить:
# встретили — честно сказали, что сайт закрыт.
BLOCK_MARKERS = ("cf-chl", "just a moment", "challenge-platform",
                 "/cdn-cgi/challenge", "checking your browser",
                 "attention required! | cloudflare")

_SCHEME = re.compile(r"^[a-z+]+://", re.I)
_SPACES = re.compile(r"\s+")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_TIME_RANGE = re.compile(r"\d{1,2}[:.]\d{2}\s*[–—−-]\s*\d{1,2}[:.]\d{2}")
_HEX = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b")
_CSS_BRAND_VAR = re.compile(
    r"--(?P<name>[\w-]*(?:primary|brand|accent|main|theme)[\w-]*)\s*:\s*"
    r"(?P<value>#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}))\b"
)
# Картинки, которые фотографией не бывают: иконки, спрайты, платёжные значки,
# прозрачные пиксели. Логотип отсеивается тоже — у него своя роль.
_NOT_A_PHOTO = re.compile(
    r"(?:sprite|icon|favicon|logo|placeholder|avatar|flag|badge|payment|"
    r"visa|mastercard|pixel|blank|spacer|loader|spinner|arrow|bullet)",
    re.I,
)
_LOGO_HINT = re.compile(r"logo|логотип|лого|brand|wordmark", re.I)
_SERVICE_HINT = re.compile(r"servic|servis|posluh|poslug|uslug|услуг|послуг", re.I)
_SERVICE_HEADING = re.compile(
    r"послуг|услуг|service|leistung|sluzb|ponuka|що\s+ми\s+робимо|"
    r"что\s+мы\s+делаем|what\s+we\s+do",
    re.I,
)
_PRICE_HINT = re.compile(r"price|amount|cost|cena|цін|цен|вартіст|стоимост", re.I)
_TITLE_HINT = re.compile(r"title|name|heading|caption", re.I)
_ADDRESS_HINT = re.compile(r"address|adress|adresa|адрес|адрес[аи]|контакт", re.I)
# Анкоры внутренних страниц, на которых что-то есть: чем ближе к делу, тем выше.
_LINK_WEIGHTS = (
    (re.compile(r"контакт|contact|kontakt|звʼяз|связ", re.I), 40),
    (re.compile(r"катало|catalog|shop|магазин|товар|product|прайс|price|ціни|цены", re.I), 35),
    (re.compile(r"послуг|услуг|service|servis", re.I), 30),
    (re.compile(r"про\s|about|о\s+нас|о\s+компан|команд", re.I), 20),
)
# Типы JSON-LD, у которых name — это название компании, а не товара.
_BUSINESS_TYPES = {
    "organization", "localbusiness", "store", "website", "corporation",
    "dentist", "restaurant", "hotel", "automotiverepair", "legalservice",
    "healthandbeautybusiness", "homeandconstructionbusiness", "professionalservice",
}
_ADDRESS_KEYS = (
    ("streetAddress", "street"), ("addressLocality", "locality"),
    ("addressRegion", "region"), ("postalCode", "postal_code"),
    ("addressCountry", "country"),
)


# --- разбор: общее ------------------------------------------------------------

def decode(body: bytes) -> str:
    """Байты страницы в текст. Кодировку определяет UnicodeDammit.

    Кодировку берём отсюда, а не из заголовка ответа: половина сайтов, ради
    которых этот модуль и написан, объявляет её только в <meta>, а иногда врёт
    и там — тогда UnicodeDammit смотрит на сами байты.
    """
    if isinstance(body, str):
        return body
    dammit = UnicodeDammit(body, is_html=True)
    return dammit.unicode_markup or body.decode("utf-8", errors="ignore")


def soup_of(body) -> BeautifulSoup:
    return BeautifulSoup(decode(body), "lxml")


def visible_text(soup) -> str:
    """Текст страницы без скриптов и стилей — то, что видит человек."""
    copy = BeautifulSoup(str(soup), "lxml")
    for tag in copy(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()
    return _clean(copy.get_text(" "))


def text_volume(text: str) -> str:
    """Сколько на сайте прозы. Значения — из profile.TEXT_VOLUME_ORDER."""
    size = len(text)
    if size < 200:
        return "none"
    if size < 900:
        return "short"
    if size < 4000:
        return "medium"
    return "long"


def old_site_state(probe: dict, *, reachable: bool = True) -> str:
    """Состояние старого сайта. Значения — из profile.OLD_SITE_STATE_ORDER."""
    if not reachable:
        return "broken"
    if not probe.get("viewport"):
        return "not_mobile"
    year, builders = probe.get("copyright_year"), probe.get("builders") or []
    if builders or (year and year <= _this_year() - 2):
        return "outdated"
    return "ok"


def jsonld(soup) -> list[dict]:
    """Все объекты ld+json страницы, развёрнутые из @graph и вложенных списков."""
    nodes: list[dict] = []
    for tag in soup.find_all("script", attrs={"type": _LD_TYPE}):
        raw = (tag.string or tag.get_text() or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            # битый JSON-LD на чужом сайте — обычное дело, это не наша поломка
            continue
        _flatten_ld(data, nodes)
        if len(nodes) >= MAX_LD_NODES:
            break
    return nodes[:MAX_LD_NODES]


def site_name(soup, nodes=None) -> str:
    """Название компании: og:site_name → JSON-LD → <title>."""
    name = _meta(soup, "og:site_name")
    if name:
        return name[:120]
    for node in nodes if nodes is not None else jsonld(soup):
        if _ld_types(node) & _BUSINESS_TYPES:
            found = _text(node.get("name"))
            if found:
                return found[:120]
    title = _clean(soup.title.get_text() if soup.title else "")
    # «Компания — стоматология в Ужгороде»: название стоит первым, хвост — слоган
    head = re.split(r"\s*[|•·—–]\s*", title)[0].strip() if title else ""
    return (head or title)[:120]


def logo_candidates(soup, base: str) -> list[dict]:
    """Кандидаты в логотип, лучший первым.

    Инлайновый <svg> — отдельная роль: скачивать нечего, разметка уже здесь,
    и она пойдёт в санитайзер site_images.sanitize_svg.
    """
    found: list[dict] = []
    for tag in soup.find_all("img"):
        url = _img_url(tag, base)
        if not url:
            continue
        haystack = " ".join([
            tag.get("src") or "", tag.get("alt") or "", tag.get("title") or "",
            " ".join(tag.get("class") or []), tag.get("id") or "",
        ])
        if not _LOGO_HINT.search(haystack):
            continue
        found.append({"url": url, "kind": "img",
                      "weight": 40 if _in_header(tag) else 20})
    for tag in soup.find_all("svg"):
        haystack = " ".join([" ".join(tag.get("class") or []), tag.get("id") or "",
                             " ".join(_parent_classes(tag))])
        if not _LOGO_HINT.search(haystack):
            continue
        found.append({"url": "", "kind": "svg", "markup": str(tag),
                      "weight": 35 if _in_header(tag) else 15})
    for tag in soup.find_all("link", href=True):
        rel = " ".join(tag.get("rel") or []).lower()
        if "icon" not in rel:
            continue
        size = _icon_size(tag.get("sizes"))
        if "apple-touch" in rel:
            found.append({"url": urljoin(base, tag["href"]), "kind": "icon",
                          "weight": 30, "size": size})
        elif size >= 180:
            found.append({"url": urljoin(base, tag["href"]), "kind": "icon",
                          "weight": 25, "size": size})
    return _by_weight(found)


def image_candidates(soup, base: str) -> list[dict]:
    """Кандидаты в фотографии, лучшая первой. og:image идёт вперёд всех."""
    found: list[dict] = []
    for prop, weight in (("og:image", 60), ("twitter:image", 50)):
        url = _meta(soup, prop)
        if url:
            found.append({"url": urljoin(base, url), "weight": weight, "og": True,
                          "width": 0, "height": 0})
    for tag in soup.find_all("img"):
        url = _img_url(tag, base)
        if not url or _NOT_A_PHOTO.search(url):
            continue
        alt_class = " ".join([tag.get("alt") or "",
                              " ".join(tag.get("class") or [])])
        if _LOGO_HINT.search(alt_class):
            continue
        width, height = _int(tag.get("width")), _int(tag.get("height"))
        # размеры в атрибутах есть не всегда; когда есть — мелочь отсеиваем
        # здесь, чтобы не качать её вовсе, остальное отсеет site_images
        if (width and width < 200) or (height and height < 200):
            continue
        found.append({"url": url, "weight": 10 + min(width * height // 100_000, 20),
                      "og": False, "width": width, "height": height})
    for tag in soup.find_all("source", srcset=True):
        url = _from_srcset(tag["srcset"], base)
        if url and not _NOT_A_PHOTO.search(url):
            found.append({"url": url, "weight": 12, "og": False,
                          "width": 0, "height": 0})
    return _by_weight(found)[:MAX_IMAGES]


def theme_colors(soup) -> dict:
    """Цвета бренда со страницы: meta theme-color, затем CSS-переменные.

    Серое и белое отбрасываются: «фирменный серый» бренд-цветом не бывает, а
    палитра из него вышла бы неотличимой от пресетной.
    """
    picked: list[str] = []
    source = ""
    for tag in soup.find_all("meta", attrs={"name": re.compile(r"^theme-color$", re.I)}):
        value = _hex(tag.get("content"))
        if value and not _is_neutral(value):
            picked.append(value)
            source = source or "meta"
    css = " ".join(tag.get_text() for tag in soup.find_all("style"))
    for style in soup.find_all(style=True):
        css += " " + style["style"]
    for match in _CSS_BRAND_VAR.finditer(css):
        value = _hex(match.group("value"))
        if value and not _is_neutral(value) and value not in picked:
            picked.append(value)
            source = source or "css"
    if not picked:
        return {}
    colors = {"primary": picked[0], "source": source}
    if len(picked) > 1:
        colors["accent"] = picked[1]
    return colors


def phones(text: str, region: str | None = None) -> list[str]:
    """Телефоны страницы в E.164. Невалидные номера не берём вовсе.

    Регион нужен местным номерам без кода страны; без него в улов попадают
    только номера, записанные международно, — и это правильнее, чем угадывать.
    """
    found: list[str] = []
    for match in phonenumbers.PhoneNumberMatcher(text, region or "ZZ"):
        if not phonenumbers.is_valid_number(match.number):
            continue
        value = phonenumbers.format_number(
            match.number, phonenumbers.PhoneNumberFormat.E164
        )
        if value not in found:
            found.append(value)
        if len(found) >= MAX_PHONES:
            break
    return found


def emails(soup, text: str = "") -> list[str]:
    """Почты страницы: сперва mailto-ссылки, потом текст."""
    found: list[str] = []
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if href.lower().startswith("mailto:"):
            found += _EMAIL.findall(href)
    found += _EMAIL.findall(text or visible_text(soup))
    out: list[str] = []
    for value in found:
        value = value.lower().strip(".")
        if value not in out:
            out.append(value)
        if len(out) >= MAX_EMAILS:
            break
    return out


def address(soup, nodes=None) -> dict:
    """Адрес: JSON-LD PostalAddress → microdata → <address> в подвале.

    parts заполняются ТОЛЬКО из структурированных источников: разложить строку
    подвала на улицу и город без ошибок нельзя, а ошибка уедет в schema.org
    готовой страницы.
    """
    for node in nodes if nodes is not None else jsonld(soup):
        parts = _address_parts(node.get("address"))
        if parts:
            return {"display": _address_line(parts), "parts": parts}
    parts = _microdata_address(soup)
    if parts:
        return {"display": _address_line(parts), "parts": parts}
    for tag in soup.find_all("address"):
        line = _clean(tag.get_text(" "))
        if 8 <= len(line) <= 200:
            return {"display": line, "parts": {}}
    for tag in soup.select("[class*='address'], [id*='address']"):
        line = _clean(tag.get_text(" "))
        if 8 <= len(line) <= 200 and any(ch.isdigit() for ch in line):
            return {"display": line, "parts": {}}
    return {}


def hours(soup, nodes=None) -> list[str]:
    """Часы работы: openingHoursSpecification → таблица «день + чч:мм».

    Строки уходят на страницу как есть, поэтому и день, и время берутся с
    сайта: своих названий дней мы не подставляем.
    """
    lines: list[str] = []
    for node in nodes if nodes is not None else jsonld(soup):
        for spec in _as_list(node.get("openingHoursSpecification")):
            line = _hours_line(spec)
            if line and line not in lines:
                lines.append(line)
    if lines:
        return lines[:MAX_HOURS]
    for row in soup.find_all("tr"):
        cells = [_clean(c.get_text(" ")) for c in row.find_all(["td", "th"])]
        cells = [c for c in cells if c]
        if len(cells) != 2 or not _has_time(cells[1]) or len(cells[0]) > 40:
            continue
        line = f"{cells[0]}: {cells[1]}"
        if line not in lines:
            lines.append(line)
        if len(lines) >= MAX_HOURS:
            break
    return lines[:MAX_HOURS]


def services(soup, nodes=None) -> list[str]:
    """Услуги: JSON-LD-каталог, затем блоки с «услугами» в классе или заголовке."""
    found: list[str] = []
    for node in nodes if nodes is not None else jsonld(soup):
        for offer in _as_list(node.get("hasOfferCatalog")):
            for item in _as_list(offer.get("itemListElement")):
                _add_service(found, _text(
                    (item.get("itemOffered") or {}).get("name")
                    if isinstance(item.get("itemOffered"), dict) else item.get("name")
                ))
        if "service" in _ld_types(node):
            _add_service(found, _text(node.get("name")))
    if len(found) >= MAX_SERVICES:
        return found[:MAX_SERVICES]
    for container in _service_containers(soup):
        for tag in container.find_all(["h2", "h3", "h4", "li", "dt"]):
            _add_service(found, _clean(tag.get_text(" ")))
            if len(found) >= MAX_SERVICES:
                return found
    return found[:MAX_SERVICES]


def products(soup, base: str, nodes=None) -> list[dict]:
    """Товары: JSON-LD Product/ItemList, затем карточки каталога.

    У товара обязано быть название; цена и картинка — по наличию. Цена берётся
    строкой с сайта: пересчитывать и переформатировать чужие цены мы не вправе.
    """
    found: list[dict] = []
    for node in nodes if nodes is not None else jsonld(soup):
        types = _ld_types(node)
        if "product" in types:
            _add_product(found, _ld_product(node, base))
        elif "itemlist" in types:
            for item in _as_list(node.get("itemListElement")):
                inner = item.get("item") if isinstance(item.get("item"), dict) else item
                if isinstance(inner, dict) and "product" in _ld_types(inner):
                    _add_product(found, _ld_product(inner, base))
        if len(found) >= MAX_PRODUCTS:
            return found[:MAX_PRODUCTS]
    taken: set[int] = set()
    for card in soup.select(_PRODUCT_CARDS):
        if any(id(parent) in taken for parent in card.parents):
            continue
        item = _card_product(card, base)
        if item is None:
            continue
        taken.add(id(card))
        _add_product(found, item)
        if len(found) >= MAX_PRODUCTS:
            break
    return found[:MAX_PRODUCTS]


def pick_internal_links(soup, base: str) -> list[str]:
    """До трёх внутренних страниц, где вероятнее всего лежит недостающее."""
    host = urlparse(base).netloc.lower()
    scored: dict[str, int] = {}
    for tag in soup.find_all("a", href=True):
        url = urljoin(base, tag["href"].strip())
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or parsed.netloc.lower() != host:
            continue
        url = parsed._replace(fragment="").geturl()
        if url.rstrip("/") == base.rstrip("/"):
            continue
        anchor = f"{_clean(tag.get_text(' '))} {parsed.path}"
        weight = max((w for rx, w in _LINK_WEIGHTS if rx.search(anchor)), default=0)
        if weight and weight > scored.get(url, 0):
            scored[url] = weight
    best = sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))
    return [url for url, _ in best[:MAX_LINKS]]


def text_excerpts(soup) -> list[str]:
    """Куски прозы для ИИ-ветки: заголовки и абзацы, без разметки и потолком.

    В модель уезжает текст, а не HTML: разметка чужого сайта — это тысячи
    токенов, из которых ни один не про услуги компании.
    """
    picked: list[str] = []
    size = 0
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "dd"]):
        line = _clean(tag.get_text(" "))
        if len(line) < 12 or line in picked:
            continue
        picked.append(line)
        size += len(line) + 1
        if size >= MAX_EXCERPT_CHARS:
            break
    return picked


# --- сеть ---------------------------------------------------------------------

@dataclass(frozen=True)
class Page:
    """Одна загруженная страница. ok=False — причина в error, и это данные."""
    url: str = ""
    ok: bool = False
    status: int | None = None
    blocked: bool = False
    error: str = ""
    body: bytes = b""


@dataclass(frozen=True)
class ScrapeResult:
    """Что нашлось на сайте. ok=False — не нашлось ничего, reason объясняет."""
    ok: bool = False
    url: str = ""
    reason: str = ""
    pages: list[str] = field(default_factory=list)
    name: str = ""
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    address: dict = field(default_factory=dict)
    hours: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    products: list[dict] = field(default_factory=list)
    logos: list[dict] = field(default_factory=list)
    images: list[dict] = field(default_factory=list)
    brand_colors: dict = field(default_factory=dict)
    text_volume: str = ""
    old_site_state: str = ""
    excerpts: list[str] = field(default_factory=list)


async def resolve_host(host: str) -> list[str]:
    """Адреса хоста. Отдельной функцией — её подменяют тесты вместо DNS."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def target_allowed(url: str) -> bool:
    """Пускать ли бота по этому адресу. False — читаем это как сетевую ошибку.

    Пропускаем только публичный веб: схема http/https, порт схемы или 80/443,
    и все адреса, на которые резолвится хост, — глобальные. Хватает одного
    внутреннего адреса в ответе резолвера, чтобы отказать целиком: выбирать,
    по какому из них пойдёт aiohttp, мы не можем. Адрес, записанный числами,
    проверяется как есть — резолвить в нём нечего.
    """
    parts = urlparse(url)
    if parts.scheme not in ("http", "https"):
        return False
    host = parts.hostname
    if not host:
        return False
    try:
        port = parts.port
    except ValueError:      # порт не число — такого адреса не существует
        return False
    if port is not None and port not in ALLOWED_PORTS:
        return False
    literal = _address(host)
    if literal is not None:
        return _is_public(literal)
    try:
        found = await resolve_host(host)
    except (OSError, UnicodeError, ValueError) as e:
        log.info("хост %s не резолвится: %s", host, e.__class__.__name__)
        return False
    addresses = [_address(value) for value in found]
    return bool(addresses) and all(a is not None and _is_public(a)
                                   for a in addresses)


async def fetch_page(session, url: str) -> Page:
    """Открыть страницу. Без схемы — сперва https, потом http (паттерн probe).

    Ошибка сети возвращается полем, а не исключением: сайт, который не
    открылся, — это ответ на вопрос обогащения, а не сбой бота. Адрес, который
    не пропустил target_allowed, — такой же ответ, с error=BLOCKED_TARGET.
    """
    schemes = ("https", "http") if not _SCHEME.match(url) else (None,)
    error = ""
    for scheme in schemes:
        target = url if scheme is None else f"{scheme}://{_bare(url)}"
        page = await _follow(session, target, scheme or "страница")
        if page.ok or page.blocked:
            return page
        error = page.error or error
    return Page(url=url, error=error or "страница не открылась")


async def _follow(session, url: str, label: str) -> Page:
    """Пройти цепочку редиректов руками, проверяя гардом каждый её адрес.

    aiohttp ходит по Location сам, но тогда проверить успевает только первый
    адрес — а увести на внутренний хост можно и вторым.
    """
    target = url
    for _ in range(MAX_REDIRECTS + 1):
        if not await target_allowed(target):
            return Page(url=target, error=BLOCKED_TARGET)
        try:
            async with session.get(
                target, headers={"User-Agent": UA}, allow_redirects=False
            ) as resp:
                status, final = resp.status, str(resp.url)
                location = resp.headers.get("Location")
                body = b"" if status in REDIRECT_STATUS else \
                    await resp.content.read(MAX_HTML)
        except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError,
                UnicodeError, ValueError) as e:
            return Page(url=target, error=f"{label}: {e.__class__.__name__}")
        if status in REDIRECT_STATUS:
            # перенаправление без Location идти некуда: это не страница
            if not location:
                return Page(url=final, status=status, error=f"HTTP {status}")
            target = urljoin(final, location)
            continue
        if is_blocked(status, body):
            return Page(url=final, status=status, blocked=True,
                        error="сайт закрыт защитой от ботов")
        if status >= 400:
            return Page(url=final, status=status, error=f"HTTP {status}")
        return Page(url=final, ok=True, status=status, body=body)
    return Page(url=target, error="слишком много перенаправлений")


def is_blocked(status: int | None, body: bytes) -> bool:
    """Страница-заглушка антибота. Обходить её мы не будем — только назвать."""
    head = decode(body[:20_000]).lower()
    if any(marker in head for marker in BLOCK_MARKERS):
        return True
    return status == 403


async def scrape_site(url: str, *, region: str | None = None,
                      session=None) -> ScrapeResult:
    """Пройти сайт лида и собрать всё, что на нём есть.

    Страницы читаются последовательно и с паузой: параллельный обход чужого
    сайта — это уже нагрузка, а не чтение.
    """
    if session is not None:
        return await _walk(session, url, region)
    async with aiohttp.ClientSession(timeout=TIMEOUT) as own:
        return await _walk(own, url, region)


async def download_images(session, urls) -> list[tuple[str, bytes]]:
    """Скачать картинки под тремя потолками: штук, размер каждой и общий вес.

    Адреса и их редиректы проходят тот же гард, что и страницы: картинка с
    внутреннего адреса уехала бы прямиком в публичное превью.
    """
    downloaded: list[tuple[str, bytes]] = []
    total = 0
    for url in list(dict.fromkeys(urls))[:MAX_IMAGES]:
        data = await _fetch_image(session, url)
        if not data:
            continue
        if total + len(data) > MAX_TOTAL_BYTES:
            break
        downloaded.append((url, data))
        total += len(data)
    return downloaded


async def _fetch_image(session, url: str) -> bytes | None:
    """Байты одной картинки. None — не скачалась, не годится или не пущена."""
    target = url
    for _ in range(MAX_REDIRECTS + 1):
        if not await target_allowed(target):
            log.info("картинка %s пропущена: %s", target, BLOCKED_TARGET)
            return None
        try:
            async with session.get(target, headers={"User-Agent": UA},
                                   allow_redirects=False) as resp:
                status, final = resp.status, str(resp.url)
                location = resp.headers.get("Location")
                if status in REDIRECT_STATUS:
                    if not location:
                        return None
                    target = urljoin(final, location)
                    continue
                if status >= 400:
                    return None
                data = await resp.content.read(MAX_IMAGE_BYTES + 1)
        except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError,
                UnicodeError, ValueError) as e:
            log.info("картинка %s не скачалась: %s", url, e.__class__.__name__)
            return None
        return data if len(data) <= MAX_IMAGE_BYTES else None
    return None


# --- внутреннее ---------------------------------------------------------------

_LD_TYPE = re.compile(r"ld\+json", re.I)
_PRODUCT_CARDS = (
    "li.product, .product, [class*='product-card'], [class*='product_card'], "
    "[class*='product-item'], [class*='product_item'], [itemtype$='Product']"
)


async def _walk(session, url: str, region: str | None) -> ScrapeResult:
    home = await fetch_page(session, url)
    if not home.ok:
        return ScrapeResult(url=url, reason=home.error,
                            old_site_state="broken" if not home.blocked else "")
    soup = soup_of(home.body)
    base = home.url
    nodes = jsonld(soup)
    text = visible_text(soup)
    probe = analyze_html(decode(home.body))

    result = {
        "pages": [base],
        "name": site_name(soup, nodes),
        "phones": phones(text, region),
        "emails": emails(soup, text),
        "address": address(soup, nodes),
        "hours": hours(soup, nodes),
        "services": services(soup, nodes),
        "products": products(soup, base, nodes),
        "logos": logo_candidates(soup, base),
        "images": image_candidates(soup, base),
        "brand_colors": theme_colors(soup),
        "text_volume": text_volume(text),
        "old_site_state": old_site_state(probe),
        "excerpts": text_excerpts(soup),
    }
    for link in pick_internal_links(soup, base)[:MAX_PAGES - 1]:
        await asyncio.sleep(PAUSE_SEC)
        page = await fetch_page(session, link)
        if not page.ok:
            continue
        inner = soup_of(page.body)
        result["pages"].append(page.url)
        _merge_page(result, inner, page.url, region)
    result["images"] = result["images"][:MAX_IMAGES]
    result["excerpts"] = _cap_excerpts(result["excerpts"])
    return ScrapeResult(ok=True, url=base, **result)


def _merge_page(result: dict, soup, base: str, region: str | None):
    """Внутренняя страница дополняет главную, но не переписывает её.

    Логотип и цвета берутся только с главной: шапка там та же, а вот подвал
    каталога вполне может нести чужой логотип платёжной системы.
    """
    nodes = jsonld(soup)
    text = visible_text(soup)
    for value in phones(text, region):
        if value not in result["phones"] and len(result["phones"]) < MAX_PHONES:
            result["phones"].append(value)
    for value in emails(soup, text):
        if value not in result["emails"] and len(result["emails"]) < MAX_EMAILS:
            result["emails"].append(value)
    if not result["address"]:
        result["address"] = address(soup, nodes)
    if not result["hours"]:
        result["hours"] = hours(soup, nodes)
    for value in services(soup, nodes):
        if value not in result["services"] and len(result["services"]) < MAX_SERVICES:
            result["services"].append(value)
    names = {item["name"] for item in result["products"]}
    for item in products(soup, base, nodes):
        if item["name"] not in names and len(result["products"]) < MAX_PRODUCTS:
            result["products"].append(item)
            names.add(item["name"])
    urls = {item["url"] for item in result["images"]}
    for item in image_candidates(soup, base):
        if item["url"] not in urls:
            result["images"].append(item)
            urls.add(item["url"])
    result["images"].sort(key=lambda item: -item["weight"])
    result["excerpts"] += [line for line in text_excerpts(soup)
                           if line not in result["excerpts"]]


def _cap_excerpts(lines: list[str]) -> list[str]:
    out, size = [], 0
    for line in lines:
        if size + len(line) > MAX_EXCERPT_CHARS:
            break
        out.append(line)
        size += len(line) + 1
    return out


def _flatten_ld(data, out: list[dict], depth: int = 0):
    if depth > 6 or len(out) >= MAX_LD_NODES:
        return
    if isinstance(data, list):
        for item in data:
            _flatten_ld(item, out, depth + 1)
        return
    if not isinstance(data, dict):
        return
    out.append(data)
    for key in ("@graph", "mainEntity", "itemListElement"):
        if key in data:
            _flatten_ld(data[key], out, depth + 1)


def _ld_types(node) -> set[str]:
    if not isinstance(node, dict):
        return set()
    raw = node.get("@type") or node.get("type") or []
    values = raw if isinstance(raw, list) else [raw]
    return {str(v).rsplit("/", 1)[-1].lower() for v in values if v}


def _ld_product(node: dict, base: str) -> dict | None:
    name = _text(node.get("name"))
    if not name:
        return None
    item = {"name": name[:120]}
    price = _ld_price(node.get("offers"))
    if price:
        item["price"] = price
    image = _ld_image(node.get("image"))
    if image:
        item["image"] = urljoin(base, image)
    return item


def _ld_price(offers) -> str:
    for offer in _as_list(offers):
        price = _text(offer.get("price") or offer.get("lowPrice"))
        if not price:
            continue
        currency = _text(offer.get("priceCurrency"))
        return f"{price} {currency}".strip()[:32]
    return ""


def _ld_image(value) -> str:
    for item in _as_list(value) or ([value] if isinstance(value, str) else []):
        if isinstance(item, str) and item.strip():
            return item.strip()
        if isinstance(item, dict):
            url = _text(item.get("url") or item.get("contentUrl"))
            if url:
                return url
    return ""


def _add_product(found: list[dict], item: dict | None):
    if item and all(item["name"] != known["name"] for known in found):
        found.append(item)


def _card_product(card, base: str) -> dict | None:
    """Товар из карточки каталога: имя, картинка и цена обязаны быть рядом."""
    if len(card.get_text(" ", strip=True)) > 400:
        return None
    img = card.find("img")
    price = next((tag for tag in card.find_all(True)
                  if _PRICE_HINT.search(" ".join(tag.get("class") or []))), None)
    if img is None or price is None:
        return None
    name = _card_name(card, img)
    if not name:
        return None
    item = {"name": name[:120]}
    value = _clean(price.get_text(" "))
    if value and len(value) <= 32:
        item["price"] = value
    url = _img_url(img, base)
    if url:
        item["image"] = url
    return item


def _card_name(card, img) -> str:
    for tag in card.find_all(["h2", "h3", "h4", "h5", "a", "span", "div"]):
        classes = " ".join(tag.get("class") or [])
        if tag.name.startswith("h") or _TITLE_HINT.search(classes):
            name = _clean(tag.get_text(" "))
            if 2 <= len(name) <= 120:
                return name
    return _clean(img.get("alt") or "")


def _service_containers(soup):
    """Блоки, в которых на сайте лежат услуги: по классу и по заголовку."""
    seen, out = set(), []
    for tag in soup.find_all(True):
        marks = " ".join([" ".join(tag.get("class") or []), tag.get("id") or ""])
        if marks.strip() and _SERVICE_HINT.search(marks) and id(tag) not in seen:
            seen.add(id(tag))
            out.append(tag)
    for tag in soup.find_all(["h1", "h2", "h3"]):
        if not _SERVICE_HEADING.search(tag.get_text(" ")):
            continue
        holder = tag.find_parent(["section", "div", "article", "main"]) or tag.parent
        if holder is not None and id(holder) not in seen:
            seen.add(id(holder))
            out.append(holder)
    return out


def _add_service(found: list[str], line: str):
    line = _clean(line)
    if not (3 <= len(line) <= SERVICE_MAX_CHARS):
        return
    if line not in found and len(found) < MAX_SERVICES:
        found.append(line)


def _address_parts(value) -> dict:
    if isinstance(value, str):
        return {}
    for node in _as_list(value):
        parts = {}
        for key, name in _ADDRESS_KEYS:
            text = _text(node.get(key))
            if text:
                parts[name] = text[:120]
        if parts.get("street") or parts.get("locality"):
            return parts
    return {}


def _microdata_address(soup) -> dict:
    parts = {}
    for key, name in _ADDRESS_KEYS:
        tag = soup.find(attrs={"itemprop": key})
        if tag is None:
            continue
        text = _clean(tag.get("content") or tag.get_text(" "))
        if text:
            parts[name] = text[:120]
    return parts if parts.get("street") or parts.get("locality") else {}


def _address_line(parts: dict) -> str:
    order = ("street", "locality", "region", "postal_code")
    return ", ".join(parts[key] for key in order if parts.get(key))[:200]


def _hours_line(spec) -> str:
    if not isinstance(spec, dict):
        return ""
    days = [str(day).rsplit("/", 1)[-1] for day in _as_list_raw(spec.get("dayOfWeek"))]
    opens, closes = _text(spec.get("opens")), _text(spec.get("closes"))
    if not days or not opens or not closes:
        return ""
    return f"{', '.join(days)}: {opens}–{closes}"[:120]


def _has_time(value: str) -> bool:
    return bool(_TIME_RANGE.search(value) or re.search(r"\d{1,2}[:.]\d{2}", value))


def _img_url(tag, base: str) -> str:
    """Адрес картинки с оглядкой на ленивую загрузку. data: не берём вовсе."""
    for attr in ("src", "data-src", "data-lazy-src", "data-original"):
        value = (tag.get(attr) or "").strip()
        if value and not value.lower().startswith("data:"):
            return urljoin(base, value)
    if tag.get("srcset"):
        return _from_srcset(tag["srcset"], base)
    return ""


def _from_srcset(value: str, base: str) -> str:
    """Самый широкий вариант srcset: мелкие нам всё равно не подойдут."""
    best, best_width = "", -1
    for part in value.split(","):
        chunk = part.strip().split()
        if not chunk or chunk[0].lower().startswith("data:"):
            continue
        width = _int(chunk[1].rstrip("wx")) if len(chunk) > 1 else 0
        if width > best_width:
            best, best_width = urljoin(base, chunk[0]), width
    return best


def _in_header(tag) -> bool:
    return any(parent.name in ("header", "nav") for parent in tag.parents)


def _parent_classes(tag) -> list[str]:
    parent = tag.parent
    return (parent.get("class") or []) + ([parent.get("id")] if parent
                                          and parent.get("id") else [])


def _icon_size(value) -> int:
    if not value:
        return 0
    sizes = [_int(part.split("x")[0]) for part in str(value).lower().split()]
    return max(sizes or [0])


def _by_weight(items: list[dict]) -> list[dict]:
    out, seen = [], set()
    for item in sorted(items, key=lambda i: -i["weight"]):
        key = item.get("url") or item.get("markup", "")
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _meta(soup, prop: str) -> str:
    for attr in ("property", "name"):
        tag = soup.find("meta", attrs={attr: re.compile(f"^{prop}$", re.I)})
        if tag is not None and tag.get("content"):
            return _clean(tag["content"])
    return ""


def _hex(value) -> str:
    match = _HEX.search(str(value or ""))
    if not match:
        return ""
    color = match.group(0).lower()
    if len(color) == 4:
        color = "#" + "".join(ch * 2 for ch in color[1:])
    return color


def _is_neutral(color: str) -> bool:
    """Серое, белое и чёрное — не бренд, а фон. Такому в accent не место."""
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return max(r, g, b) - min(r, g, b) < 24


def _as_list(value) -> list[dict]:
    items = value if isinstance(value, list) else [value]
    return [item for item in items if isinstance(item, dict)]


def _as_list_raw(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _text(value) -> str:
    if isinstance(value, (int, float)):
        return str(value)
    return _clean(value) if isinstance(value, str) else ""


def _clean(value: str) -> str:
    return _SPACES.sub(" ", str(value or "")).strip()


def _int(value) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _bare(url: str) -> str:
    return _SCHEME.sub("", url.strip())


def _address(value: str):
    """Строка в объект адреса. None — это не адрес, а имя хоста.

    Зона IPv6 (`fe80::1%eth0`) адресом тоже не считается: разбирать её незачем,
    а любой хост со ссылочной областью гард всё равно не пропустит.
    """
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _is_public(address) -> bool:
    """Адрес из публичного интернета. Всё прочее — чужая внутренняя сеть.

    is_global тут основной критерий, остальные проверки — явные имена того,
    ради чего гард и написан: метаданные облака (169.254.169.254), сам бот
    (127.0.0.1) и соседи по приватной сети.
    """
    if address.is_private or address.is_loopback or address.is_link_local:
        return False
    if address.is_reserved or address.is_multicast or address.is_unspecified:
        return False
    return address.is_global


def _this_year() -> int:
    return datetime.now(config.TZ).year
