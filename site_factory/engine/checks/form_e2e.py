"""Форма заявки доходит до админ-чата.

Проверок две, и они отвечают на разные вопросы.

* check(html) — статическая: форма есть, ведёт на /api/lead методом post и
  несёт honeypot. Сети не касается, идёт в run_all до всякой публикации.
* check_live(url) — настоящий POST на живое превью с заголовком
  X-Tobisite-Test: 1 (10.9): Worker проходит ту же валидацию, отвечает
  {"ok": true, "test": true} и в админ-чат ничего не пишет.

check_live в run_all не входит намеренно: автопроверки идут до публикации,
когда стучаться ещё некуда, а CI не должен зависеть от чужой сети. Её зовёт
tools/preview_check.py по уже выложенному превью.
"""
from __future__ import annotations

import asyncio
import re

import aiohttp

ACTION = "/api/lead"
METHOD = "post"
HONEYPOT = "company_website"
REQUIRED_FIELDS = ("name", "phone")

FORM = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.S)

TEST_HEADER = {"X-Tobisite-Test": "1"}
LIVE_TIMEOUT = 15
# заявка проходит валидацию Worker (имя ≥2, телефон ≥5), но никуда не уходит
PROBE_LEAD = {"name": "tobisite check", "phone": "+380000000000"}


def check(html: str) -> list[str]:
    forms = FORM.findall(html)
    if not forms:
        return [f"на странице нет формы с action={ACTION}"]

    problems = []
    for attrs, body in forms:
        if f'action="{ACTION}"' not in attrs:
            problems.append(f"форма без action=\"{ACTION}\": {attrs.strip()[:60]!r}")
        if f'method="{METHOD}"' not in attrs.lower():
            problems.append(f"форма без method=\"{METHOD}\": {attrs.strip()[:60]!r}")
        if f'name="{HONEYPOT}"' not in body:
            problems.append(f"в форме нет honeypot-поля {HONEYPOT!r}")
        problems += [f"в форме нет поля {field!r}" for field in REQUIRED_FIELDS
                     if f'name="{field}"' not in body]
    return problems


async def check_live(url: str, session=None) -> list[str]:
    """Тестовая заявка на живое превью: сеть трогает только эта функция."""
    if session is None:
        timeout = aiohttp.ClientTimeout(total=LIVE_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as own:
            return await check_live(url, own)
    target = url.rstrip("/") + ACTION
    try:
        async with session.post(target, json=PROBE_LEAD,
                                headers=TEST_HEADER) as resp:
            # content_type=None: ошибку Cloudflare отдаёт и текстом
            return check_answer(resp.status, await resp.json(content_type=None),
                                target)
    except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError,
            ValueError) as e:
        return [f"{target}: {e.__class__.__name__}"]


def check_answer(status: int, body, target: str = ACTION) -> list[str]:
    """Ответ Worker на тестовую заявку: 200 и {"ok": true, "test": true}."""
    if status != 200:
        return [f"{target}: HTTP {status}"]
    if not isinstance(body, dict):
        return [f"{target}: ответ не JSON"]
    problems = []
    if body.get("ok") is not True:
        problems.append(f"{target}: ok={body.get('ok')!r}, "
                        f"ошибка {body.get('error')!r}")
    if body.get("test") is not True:
        # без test:true заявка ушла в админ-чат: заголовок не долетел, и
        # каждая такая проверка — сообщение о несуществующем клиенте
        problems.append(f"{target}: тест-заголовок не сработал, "
                        f"заявка ушла в чат")
    return problems
