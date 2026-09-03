"""Заголовки (один h1 в hero, дальше h2), alt у картинок, контраст пресета.

Контраст считается по контракту палитры из tokens/presets.yaml: accent текста
не несёт, его несут accent_ink (>= 4.5:1 по paper) и accent_on (>= 4.5:1 по
accent_ink).

Производные тона (surface, line, muted) в presets.yaml не хранятся — их
считает css/source.css через color-mix(in oklab, ...). Здесь то же смешение
повторено на Python (engine/color.py), иначе проверять текст цвета muted было
бы нечем. Числа процентов обязаны совпадать с css/source.css: разъедутся —
разъедется и проверка. line не проверяется: это линейка, текста он не несёт.

Контрастная секция страницы (data-tone="contrast") подменяет внутри себя всю
эту семью тонов, поэтому её пары проверяются отдельно и тем же порогом:
цвета считает engine/palette.contrast_tones, и на страницу уходят ровно они.
"""
from __future__ import annotations

import re

from ..color import AA_TEXT, MUTED_MIX, SURFACE_MIX, mix_oklab, ratio, srgb
from ..palette import contrast_tones

# Акцент, подмешанный в бумагу под кнопкой data-btn="soft". Тоже копия из
# css/source.css: подложка кнопки несёт текст, значит её надо проверять.
BUTTON_SOFT_MIX = 0.14

__all__ = ["AA_TEXT", "BUTTON_SOFT_MIX", "MUTED_MIX", "SURFACE_MIX", "check",
           "contrast_problems", "contrast_tones", "derived", "ratio"]

HEADING = re.compile(r"<h([1-6])\b")
IMG = re.compile(r"<img\b([^>]*)>")
HTML_LANG = re.compile(r'<html\b[^>]*\blang="([^"]*)"')


def check(html: str, palette: dict | None = None) -> list[str]:
    problems = _headings(html) + _images(html) + _lang(html)
    if palette:
        problems += contrast_problems(palette)
    return problems


def contrast_problems(palette: dict) -> list[str]:
    """Пары, которые несут текст. Всё, что ниже 4.5:1, — брак пресета."""
    tones = derived(palette)
    pairs = (
        ("ink/paper", tones["ink"], tones["paper"]),
        ("accent_ink/paper", tones["accent_ink"], tones["paper"]),
        ("accent_on/accent_ink", tones["accent_on"], tones["accent_ink"]),
        ("muted/paper", tones["muted"], tones["paper"]),
        ("muted/surface", tones["muted"], tones["surface"]),
        ("paper/ink", tones["paper"], tones["ink"]),
        ("ink/btn-soft", tones["ink"], tones["btn_soft"]),
        ("tone ink/bg", tones["tone_ink"], tones["tone_bg"]),
        ("tone muted/bg", tones["tone_muted"], tones["tone_bg"]),
        ("tone muted/surface", tones["tone_muted"], tones["tone_surface"]),
        ("tone accent/bg", tones["tone_accent"], tones["tone_bg"]),
    )
    return [f"контраст {name}: {ratio(a, b):.2f}:1 при норме {AA_TEXT}:1"
            for name, a, b in pairs if ratio(a, b) < AA_TEXT]


def derived(palette: dict) -> dict:
    """Палитра пресета плюс тона, которые css/source.css считает через oklab."""
    tones = {name: srgb(value) for name, value in palette.items()}
    tones["surface"] = mix_oklab(tones["ink"], tones["paper"], SURFACE_MIX)
    tones["muted"] = mix_oklab(tones["ink"], tones["paper"], MUTED_MIX)
    tones["btn_soft"] = mix_oklab(tones["accent"], tones["paper"], BUTTON_SOFT_MIX)
    tones.update({f"tone_{name}": srgb(value)
                  for name, value in contrast_tones(palette).items()})
    return tones


def _headings(html: str) -> list[str]:
    levels = [int(level) for level in HEADING.findall(html)]
    problems = []
    if levels.count(1) != 1:
        problems.append(f"h1 на странице {levels.count(1)}, должен быть ровно один")
    if levels and levels[0] != 1:
        problems.append(f"первый заголовок страницы — h{levels[0]}, а не h1")
    for previous, current in zip(levels, levels[1:]):
        if current > previous + 1:
            problems.append(f"пропуск в заголовках: h{previous} -> h{current}")
    return problems


def _images(html: str) -> list[str]:
    problems = []
    for attrs in IMG.findall(html):
        alt = re.search(r'\balt="([^"]*)"', attrs)
        if alt is None:
            problems.append(f"<img> без alt: {attrs.strip()[:60]!r}")
        elif not alt.group(1).strip() and 'aria-hidden="true"' not in attrs:
            problems.append(f"<img> с пустым alt и без aria-hidden: "
                            f"{attrs.strip()[:60]!r}")
    return problems


def _lang(html: str) -> list[str]:
    found = HTML_LANG.search(html)
    if not found or not found.group(1).strip():
        return ["у <html> нет атрибута lang"]
    return []


