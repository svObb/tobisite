"""NAP: телефон, email и адрес обязаны побайтово совпасть с профилем (§3).

Несовпадение валит сборку. Проверка двусторонняя:

* каждое известное значение профиля обязано быть на странице — как есть,
  байт в байт (с поправкой на HTML-экранирование, которое делает Jinja);
* каждый href="tel:" и href="mailto:" обязан вести на телефон и почту из
  профиля — чужая ссылка не пройдёт, даже если рядом лежит правильный текст.

Требование «значение обязано быть на странице» опирается на то, что footer_nap
входит обязательной ролью в каждый рецепт, а его контракт требует телефон и
адрес. Если однажды это изменится, проверка сработает ложно — и это правильное
направление отказа: лучше остановить сборку и посмотреть, чем однажды
отправить клиенту чужой телефон.
"""
from __future__ import annotations

import re

from markupsafe import escape

from ..slots import tel_href

TEL_HREF = re.compile(r'href="tel:([^"]*)"')
MAILTO_HREF = re.compile(r'href="mailto:([^"]*)"')

NAP_FIELDS = (("name", "название"), ("phone", "телефон"),
              ("email", "email"), ("address", "адрес"))


def check(html: str, profile) -> list[str]:
    problems = []
    for field, title in NAP_FIELDS:
        feature = getattr(profile, field)
        if not feature.known or not feature.value:
            continue
        if not _present(html, str(feature.value)):
            problems.append(f"{title} профиля не найден на странице: "
                            f"{feature.value!r}")

    if profile.phone.known and profile.phone.value:
        expected = tel_href(profile.phone.value)[len("tel:"):]
        problems += [f"чужой tel: на странице: {found!r}, в профиле {expected!r}"
                     for found in TEL_HREF.findall(html) if found != expected]
    elif TEL_HREF.search(html):
        problems.append("на странице есть tel:, а в профиле телефона нет")

    if profile.email.known and profile.email.value:
        expected = str(profile.email.value)
        problems += [f"чужой mailto: на странице: {found!r}, в профиле {expected!r}"
                     for found in MAILTO_HREF.findall(html) if found != expected]
    elif MAILTO_HREF.search(html):
        problems.append("на странице есть mailto:, а в профиле почты нет")

    return problems


def _present(html: str, value: str) -> bool:
    """Сырое значение или оно же после экранирования Jinja — но не «похожее»."""
    return value in html or str(escape(value)) in html
