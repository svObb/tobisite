"""Проверка живого превью: страница, форма заявки и скорость (10.9, 10.17).

Сеть настоящая, поэтому это утилита, а не автопроверка: ни run_all, ни CI её
не зовут — прогон тестов не должен зависеть от чужой сети.

Запускать из корня проекта:

    python tools/preview_check.py https://pravo-i-dilo.tobisitepreview.com/
    python tools/preview_check.py <url> --skip-speed   # без PageSpeed, быстро

Заявка уходит с заголовком X-Tobisite-Test: 1 — Worker проходит валидацию, но
в админ-чат ничего не пишет. PageSpeed без ключа отвечает медленно и с квотой
~1 запрос/сек; ключ берётся из PAGESPEED_API_KEY.
"""
import argparse
import asyncio
import os
import pathlib
import re
import sys
import time
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import aiohttp  # noqa: E402

from scout.site_probe import (  # noqa: E402
    PSI_TIMEOUT, parse_psi_metrics, psi_raw,
)
from site_factory.engine.checks.form_e2e import check_live  # noqa: E402

# 10.17: превью обязано показать главное за 3 секунды на мобильном 4G.
# Меряет это Lighthouse mobile, он же дросселирует сеть до 4G.
SPEED_BUDGET_MS = 3000
PAGE_TIMEOUT = aiohttp.ClientTimeout(total=30)

SCRIPT_SRC = re.compile(r'<script[^>]+\bsrc="([^"]+)"')
IMG_SRC = re.compile(r'<img[^>]+\bsrc="([^"]+)"')


def speed_problems(metrics: dict) -> list[str]:
    """Вердикт по бюджету. Не измерено — это тоже не «в порядке»."""
    lcp = metrics.get("lcp_ms")
    if lcp is None:
        return ["PageSpeed не измерил LCP — прогнать ещё раз"]
    if lcp > SPEED_BUDGET_MS:
        return [f"LCP {secs(lcp)} против бюджета {secs(SPEED_BUDGET_MS)}"]
    return []


def secs(ms) -> str:
    return "—" if ms is None else f"{ms / 1000:.1f} с"


async def check_page(session, url: str) -> tuple[list[str], str]:
    started = time.monotonic()
    try:
        async with session.get(url) as resp:
            body = await resp.read()
            status, ctype = resp.status, resp.headers.get("content-type", "")
    except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError) as e:
        return [f"страница не открылась: {e.__class__.__name__}"], ""
    print(f"  страница: HTTP {status}, {len(body) / 1024:.0f} КБ, "
          f"{time.monotonic() - started:.2f} с")
    problems = []
    if status != 200:
        problems.append(f"страница отдала HTTP {status}")
    if "text/html" not in ctype:
        problems.append(f"страница не HTML: {ctype!r}")
    return problems, body.decode("utf-8", "replace")


def assets_of(html: str):
    """Что страница дотягивает сама: скрипты и первый кадр.

    Первая картинка — она же LCP-кадр первого экрана: страница откроется и без
    неё, а мерить будет нечего. Остальные картинки грузятся лениво и на
    открытие превью не влияют. data-URI пропускаем: это уже в разметке.
    """
    for src in SCRIPT_SRC.findall(html):
        yield "javascript", src
    for src in IMG_SRC.findall(html):
        if not src.startswith("data:"):
            yield "image", src
            return


async def check_assets(session, url: str, html: str) -> list[str]:
    problems = []
    for kind, src in assets_of(html):
        target = urllib.parse.urljoin(url, src)
        try:
            async with session.get(target) as resp:
                body = await resp.read()
                status = resp.status
                ctype = resp.headers.get("content-type", "")
        except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError) as e:
            problems.append(f"{src}: не открылся ({e.__class__.__name__})")
            continue
        print(f"  {src}: HTTP {status}, {len(body) / 1024:.0f} КБ, {ctype}")
        if status != 200:
            problems.append(f"{src}: HTTP {status}")
        elif kind not in ctype:
            problems.append(f"{src}: content-type {ctype!r}, ожидался {kind}")
    return problems


async def check_speed(url: str, api_key: str) -> list[str]:
    async with aiohttp.ClientSession(timeout=PSI_TIMEOUT) as session:
        metrics = parse_psi_metrics(await psi_raw(session, url, api_key))
    print(f"  скорость (PageSpeed mobile): LCP {secs(metrics['lcp_ms'])}, "
          f"Speed Index {secs(metrics['si_ms'])}, "
          f"performance {metrics['score'] if metrics['score'] is not None else '—'}")
    return speed_problems(metrics)


async def run(url: str, skip_speed: bool) -> int:
    print(f"Превью: {url}")
    problems = []
    async with aiohttp.ClientSession(timeout=PAGE_TIMEOUT) as session:
        page, html = await check_page(session, url)
        problems += page
        problems += await check_assets(session, url, html)
        form = await check_live(url, session)
        print("  форма: " + ("заявка принята, в чат не ушла" if not form
                             else "; ".join(form)))
        problems += form
    if not skip_speed:
        problems += await check_speed(url, os.getenv("PAGESPEED_API_KEY", ""))
    if problems:
        print("\nНе в порядке:")
        for problem in problems:
            print(f"  ✗ {problem}")
        return 1
    print("\nВсё в порядке.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("url", help="адрес превью, например https://slug"
                                    ".tobisitepreview.com/")
    parser.add_argument("--skip-speed", action="store_true",
                        help="без PageSpeed: он думает 15–40 секунд")
    args = parser.parse_args()
    return asyncio.run(run(args.url.rstrip("/") + "/", args.skip_speed))


if __name__ == "__main__":
    sys.exit(main())
