"""Валит сборку на lorem, [НАЗВАНИЕ], {{ и href="#" в готовом HTML (§3).

Ступень 5 лестницы деградации — запрет на подмену. Клиенту нельзя показать
страницу, где вместо его данных стоит рыба, скобка редактора или ссылка
в никуда: такое превью хуже, чем отсутствие превью.

Список закрытый и намеренно грубый: {{ и }} ловят неразвёрнутый Jinja,
href="#" — кнопку, которая никуда не ведёт, TODO/FIXME — забытую заметку
разработчика, [В ВЕРХНЕМ РЕГИСТРЕ] — редакторскую заглушку в любом алфавите.

Единственное исключение — блок JSON-LD: вложенный объект schema.org честно
заканчивается на }}, и по фигурным скобкам его не проверяем. По остальным
шаблонам проверяем: название компании попадает и туда тоже.
"""
from __future__ import annotations

import re

LD_JSON = re.compile(r'<script type="application/ld\+json">.*?</script>', re.S)

# (шаблон, что нашли, пропускать ли блок JSON-LD)
PATTERNS = (
    (re.compile(r"\blorem\b", re.I), "рыба lorem", False),
    (re.compile(r"\{\{|\}\}"), "неразвёрнутый шаблон Jinja", True),
    (re.compile(r"\{%|%\}"), "неразвёрнутый блок Jinja", True),
    (re.compile(r'href="#"'), 'ссылка href="#" в никуда', False),
    (re.compile(r"\bTODO\b|\bFIXME\b"), "заметка разработчика", False),
    # [НАЗВАНИЕ], [CITY], [НАЗВА КОМПАНІЇ] — заглушка в квадратных скобках.
    (re.compile(r"\[[^\]\n]*[A-ZА-ЯЁІЇЄҐ]{3,}[^\]\n]*\]"), "заглушка в скобках", False),
)


def check(html: str) -> list[str]:
    without_json = LD_JSON.sub("", html)
    problems = []
    for pattern, title, skip_json in PATTERNS:
        haystack = without_json if skip_json else html
        found = pattern.search(haystack)
        if found:
            problems.append(f"{title}: {_context(haystack, found)!r}")
    return problems


def _context(html: str, found: re.Match, width: int = 40) -> str:
    start = max(0, found.start() - width)
    return html[start:found.end() + width].replace("\n", " ")
