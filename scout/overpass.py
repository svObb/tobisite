"""Источник Overpass/OSM: бесплатный, первый для отладки конвейера (15.3).

Ограничение, о котором надо помнить: область ищется по имени
(area["name"="Košice"]), и город надо писать так, как он назван в OSM —
обычно на местном языке. Одноимённый город в другой стране теоретически
может подмешаться; от дублей спасает дедуп по телефону и домену, а v1
скаута — инструмент отладки, не боевой источник.
"""
import logging

import aiohttp

from scout.types import RawBiz

log = logging.getLogger(__name__)

ENDPOINT = "https://overpass-api.de/api/interpreter"
# Общий инстанс перегружен — запасной обязателен, иначе прогон падает целиком
FALLBACK = "https://overpass.kumi.systems/api/interpreter"
UA = "tobisite-scout/1.0 (lead research; contact via telegram bot)"
MAX_RESULTS = 200

_PHONE_KEYS = ("phone", "contact:phone", "phone:mobile", "contact:mobile")
_SITE_KEYS = ("website", "contact:website", "url")


def build_query(tags: list[tuple[str, str]], city: str) -> str:
    city = city.replace('"', "").strip()
    lines = "\n".join(
        f'  nwr["{k}"="{v}"](area.a);' for k, v in tags
    )
    return (
        '[out:json][timeout:60];\n'
        f'area["name"="{city}"]->.a;\n'
        "(\n"
        f"{lines}\n"
        ");\n"
        f"out tags center {MAX_RESULTS};"
    )


def parse_elements(payload: dict, *, city: str, source: str = "overpass") -> list[RawBiz]:
    """JSON Overpass → карточки. Без имени карточка бесполезна — пропускаем."""
    out = []
    for el in payload.get("elements", []):
        tags = el.get("tags") or {}
        name = (tags.get("name") or "").strip()
        if not name:
            continue
        phone = next(
            (tags[k].strip() for k in _PHONE_KEYS if tags.get(k)), None
        )
        website = next(
            (tags[k].strip() for k in _SITE_KEYS if tags.get(k)), None
        )
        street = " ".join(filter(None, (
            tags.get("addr:street"), tags.get("addr:housenumber"),
        ))) or None
        out.append(RawBiz(
            name=name,
            phone=phone,
            website=website,
            address=street,
            city=(tags.get("addr:city") or city).strip(),
            source=source,
            source_url=f"https://www.openstreetmap.org/{el.get('type')}/{el.get('id')}",
        ))
    return out


async def fetch(tags: list[tuple[str, str]], city: str) -> list[RawBiz]:
    query = build_query(tags, city)
    timeout = aiohttp.ClientTimeout(total=90)
    last_err = None
    async with aiohttp.ClientSession(
        timeout=timeout, headers={"User-Agent": UA}
    ) as session:
        for endpoint in (ENDPOINT, FALLBACK):
            try:
                async with session.post(endpoint, data={"data": query}) as resp:
                    if resp.status != 200:
                        last_err = f"HTTP {resp.status} от {endpoint}"
                        continue
                    payload = await resp.json(content_type=None)
                return parse_elements(payload, city=city)
            except aiohttp.ClientError as e:
                last_err = f"{endpoint}: {e}"
            except TimeoutError as e:
                last_err = f"{endpoint}: таймаут ({e})"
    raise RuntimeError(f"Overpass недоступен: {last_err}")
