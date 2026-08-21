"""Эвристика ширин: страница обязана жить на 360px без горизонтального скролла.

Честно о том, что это такое. Модуль не раскладывает страницу и не считает
реальных ширин — он читает HTML строкой и ищет четыре типовые причины, по
которым превью уезжает вбок:

1. инлайновый style с фиксированной шириной больше вьюпорта;
2. произвольная ширина Tailwind вида w-[480px] больше вьюпорта;
3. картинка (img, video, iframe) без класса, ограничивающего ширину:
   width="1600" без w-full или max-w-* растянет страницу ровно на 1600px;
4. непарные контейнерные теги — незакрытый div или figure ломает вложенность,
   и содержимое вываливается из своей колонки.

Чего эвристика НЕ видит: длинное неразрывное слово или ссылку без break-words,
таблицу без обёртки со скроллом, absolute-позиционирование за край, реальную
ширину шрифта. Настоящая проверка — скриншот на 360px; в MVP её нет, и модуль
не притворяется, что заменяет её. Он ловит регрессии в шаблонах, а не даёт
гарантию.
"""
from __future__ import annotations

import re

VIEWPORT_PX = 360

MEDIA_TAGS = ("img", "video", "iframe")
CONTAINER_TAGS = ("div", "section", "figure", "picture", "ul", "ol", "form",
                  "main", "footer", "dl")

TAG = re.compile(r"<(?P<name>[a-z]+)(?P<attrs>[^>]*)>")
STYLE_WIDTH = re.compile(r"(?:min-)?width\s*:\s*(\d+(?:\.\d+)?)px", re.I)
ARBITRARY_WIDTH = re.compile(r"\b(?:min-)?w-\[(\d+(?:\.\d+)?)px\]")
WIDTH_ATTR = re.compile(r'\bwidth="(\d+)"')
FLUID_CLASSES = ("w-full", "w-auto", "max-w-full", "max-w-[", "w-screen")


def check(html: str, viewport: int = VIEWPORT_PX) -> list[str]:
    problems = []
    for tag in TAG.finditer(html):
        name, attrs = tag.group("name"), tag.group("attrs")
        fluid = any(token in attrs for token in FLUID_CLASSES)

        for value in STYLE_WIDTH.findall(attrs):
            if float(value) > viewport:
                problems.append(f"<{name}>: инлайновая ширина {value}px больше "
                                f"вьюпорта {viewport}px")
        for value in ARBITRARY_WIDTH.findall(attrs):
            if float(value) > viewport:
                problems.append(f"<{name}>: класс w-[{value}px] больше вьюпорта "
                                f"{viewport}px")
        if name in MEDIA_TAGS:
            fixed = WIDTH_ATTR.search(attrs)
            if not fluid:
                problems.append(f"<{name}>: нет класса, ограничивающего ширину "
                                f"({', '.join(FLUID_CLASSES)})")
            elif fixed and int(fixed.group(1)) > viewport and "w-full" not in attrs:
                problems.append(f"<{name}>: width={fixed.group(1)} без w-full")

    problems += _unbalanced(html)
    return problems


def _unbalanced(html: str) -> list[str]:
    problems = []
    for name in CONTAINER_TAGS:
        opened = len(re.findall(rf"<{name}\b", html))
        closed = len(re.findall(rf"</{name}>", html))
        if opened != closed:
            problems.append(f"<{name}>: открыт {opened} раз, закрыт {closed}")
    return problems
