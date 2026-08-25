"""Проверка сайта БЕЗ ИИ (15.10–15.11): статус, HTTPS, viewport, вес,
год копирайта, отпечатки конструкторов.

Разбор HTML отделён от сети (analyze_html — чистая функция): его гоняют тесты,
не поднимая ни одного соединения.
"""
import asyncio
import logging
import re

import aiohttp

log = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=20)
UA = "Mozilla/5.0 (compatible; tobisite-probe/1.0)"
MAX_HTML = 1_500_000  # дальше не читаем: вес всё равно уже «тяжёлый»

_VIEWPORT = re.compile(r'<meta[^>]+name=["\']viewport', re.I)
# год рядом с © / &copy; / "copyright", берём максимальный найденный
_COPY_BLOCK = re.compile(r"(?:©|&copy;|copyright)[^<\n]{0,80}", re.I)
_YEAR = re.compile(r"(19|20)\d{2}")

# подстрока в HTML → метка конструктора; ловим дешёвые/устаревшие площадки
_BUILDERS = [
    ("static.parastorage.com", "wix"),
    ("wix.com", "wix"),
    ("godaddysites", "godaddy"),
    ("ucoz", "ucoz"),
    ("narod.ru", "narod"),
    ("weebly", "weebly"),
    ("wp-content", "wordpress"),
]


def analyze_html(html: str) -> dict:
    found = {label for needle, label in _BUILDERS if needle in html.lower()}
    year = None
    for m in _COPY_BLOCK.finditer(html):
        for y in _YEAR.finditer(m.group(0)):
            year = max(year or 0, int(y.group(0)))
    return {
        "viewport": bool(_VIEWPORT.search(html)),
        "copyright_year": year,
        "builders": sorted(found),
        "size_bytes": len(html.encode("utf-8", errors="ignore")),
    }


def _with_scheme(url: str, scheme: str) -> str:
    bare = re.sub(r"^[a-z+]+://", "", url.strip(), flags=re.I)
    return f"{scheme}://{bare}"


async def probe(session: aiohttp.ClientSession, url: str) -> dict:
    """Открывает сайт: сперва https, при неудаче http.

    Возвращает dict: reachable, https, status, error + поля analyze_html.
    Любая ошибка сети — это ДАННЫЕ (сайт мёртв = хороший лид), не исключение.
    """
    result = {
        "reachable": False, "https": False, "status": None, "error": None,
        "viewport": False, "copyright_year": None, "builders": [],
        "size_bytes": 0,
    }
    for scheme in ("https", "http"):
        target = _with_scheme(url, scheme)
        try:
            async with session.get(
                target, headers={"User-Agent": UA}, allow_redirects=True
            ) as resp:
                body = (await resp.content.read(MAX_HTML)).decode(
                    resp.charset or "utf-8", errors="ignore"
                )
            result["reachable"] = resp.status < 500
            result["status"] = resp.status
            result["https"] = str(resp.url).startswith("https://")
            result.update(analyze_html(body))
            return result
        except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError,
                UnicodeError, ValueError) as e:
            result["error"] = f"{scheme}: {e.__class__.__name__}"
    return result


async def probe_many(urls: list[str], *, concurrency: int = 8) -> dict[str, dict]:
    """Пачка проверок с потолком одновременности — не DDoS-ить чужие сайты."""
    sem = asyncio.Semaphore(concurrency)
    results: dict[str, dict] = {}

    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        async def one(u: str):
            async with sem:
                results[u] = await probe(session, u)

        await asyncio.gather(*(one(u) for u in dict.fromkeys(urls)))
    return results


# --- PageSpeed Insights (15.12) ----------------------------------------------

PSI_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
PSI_TIMEOUT = aiohttp.ClientTimeout(total=75)  # Lighthouse думает 15–40 сек


def parse_psi(data: dict) -> int | None:
    """Оценка performance 0–100 из ответа PSI; None, если Lighthouse не осилил."""
    try:
        score = data["lighthouseResult"]["categories"]["performance"]["score"]
    except (KeyError, TypeError):
        return None
    return round(score * 100) if isinstance(score, (int, float)) else None


def parse_psi_metrics(data: dict) -> dict:
    """Скор плюс LCP и Speed Index в миллисекундах (10.17).

    Lighthouse mobile гоняет страницу через дросселирование 4G, поэтому его
    LCP и есть ответ на вопрос «за сколько превью показывает главное».
    None в поле значит «не измерено», а не «быстро».
    """
    audits = ((data or {}).get("lighthouseResult") or {}).get("audits") or {}

    def ms(name: str) -> int | None:
        value = (audits.get(name) or {}).get("numericValue")
        return round(value) if isinstance(value, (int, float)) else None

    return {"score": parse_psi(data), "lcp_ms": ms("largest-contentful-paint"),
            "si_ms": ms("speed-index")}


async def psi_raw(session: aiohttp.ClientSession, url: str,
                  api_key: str = "") -> dict:
    """Ответ PageSpeed целиком. Пустой словарь — не получилось (это данные)."""
    params = {"url": _with_scheme(url, "https"), "strategy": "mobile",
              "category": "performance"}
    if api_key:
        params["key"] = api_key
    try:
        async with session.get(PSI_URL, params=params) as resp:
            if resp.status != 200:
                log.info("PSI %s → HTTP %s", url, resp.status)
                return {}
            return await resp.json()
    except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError,
            ValueError) as e:
        log.info("PSI %s → %s", url, e.__class__.__name__)
        return {}


async def psi_score(session: aiohttp.ClientSession, url: str,
                    api_key: str = "") -> int | None:
    """Мобильный performance-скор сайта. Ошибка — это данные (None), не исключение."""
    return parse_psi(await psi_raw(session, url, api_key))


async def psi_many(urls: list[str], *, api_key: str = "",
                   concurrency: int = 2) -> dict[str, int | None]:
    """PSI по списку URL. Параллельность 2: без ключа квота ~1 запрос/сек."""
    sem = asyncio.Semaphore(concurrency)
    async with aiohttp.ClientSession(timeout=PSI_TIMEOUT) as session:
        async def one(u: str):
            async with sem:
                return u, await psi_score(session, u, api_key)

        return dict(await asyncio.gather(*(one(u) for u in dict.fromkeys(urls))))
