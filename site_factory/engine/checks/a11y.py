"""Заголовки (один h1 в hero, дальше h2), alt у картинок, контраст пресета.

Контраст считается по контракту палитры из tokens/presets.yaml: accent текста
не несёт, его несут accent_ink (>= 4.5:1 по paper) и accent_on (>= 4.5:1 по
accent_ink).

Производные тона (surface, line, muted) в presets.yaml не хранятся — их
считает css/source.css через color-mix(in oklab, ...). Здесь то же смешение
повторено на Python, иначе проверять текст цвета muted было бы нечем. Числа
процентов обязаны совпадать с css/source.css: разъедутся — разъедется и
проверка. line не проверяется: это линейка, текста он не несёт.
"""
from __future__ import annotations

import re

# Проценты ink в производных тонах — копия из css/source.css.
SURFACE_MIX = 0.05
MUTED_MIX = 0.72

AA_TEXT = 4.5

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
    )
    return [f"контраст {name}: {ratio(a, b):.2f}:1 при норме {AA_TEXT}:1"
            for name, a, b in pairs if ratio(a, b) < AA_TEXT]


def derived(palette: dict) -> dict:
    """Палитра пресета плюс тона, которые css/source.css считает через oklab."""
    tones = {name: _srgb(value) for name, value in palette.items()}
    tones["surface"] = _mix_oklab(tones["ink"], tones["paper"], SURFACE_MIX)
    tones["muted"] = _mix_oklab(tones["ink"], tones["paper"], MUTED_MIX)
    return tones


def ratio(first, second) -> float:
    """Контраст по WCAG 2.1: (L1 + 0.05) / (L2 + 0.05)."""
    light, dark = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


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


def _srgb(value: str) -> tuple[float, float, float]:
    digits = value.lstrip("#")
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    return tuple(int(digits[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _to_linear(channel: float) -> float:
    return (channel / 12.92 if channel <= 0.04045
            else ((channel + 0.055) / 1.055) ** 2.4)


def _from_linear(channel: float) -> float:
    return (channel * 12.92 if channel <= 0.0031308
            else 1.055 * channel ** (1 / 2.4) - 0.055)


def _luminance(color) -> float:
    red, green, blue = (_to_linear(channel) for channel in color)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _to_oklab(color):
    red, green, blue = (_to_linear(channel) for channel in color)
    long_ = (0.4122214708 * red + 0.5363325363 * green + 0.0514459929 * blue) ** (1 / 3)
    medium = (0.2119034982 * red + 0.6806995451 * green + 0.1073969566 * blue) ** (1 / 3)
    short = (0.0883024619 * red + 0.2817188376 * green + 0.6299787005 * blue) ** (1 / 3)
    return (0.2104542553 * long_ + 0.7936177850 * medium - 0.0040720468 * short,
            1.9779984951 * long_ - 2.4285922050 * medium + 0.4505937099 * short,
            0.0259040371 * long_ + 0.7827717662 * medium - 0.8086757660 * short)


def _from_oklab(lab):
    lightness, green_red, blue_yellow = lab
    long_ = (lightness + 0.3963377774 * green_red + 0.2158037573 * blue_yellow) ** 3
    medium = (lightness - 0.1055613458 * green_red - 0.0638541728 * blue_yellow) ** 3
    short = (lightness - 0.0894841775 * green_red - 1.2914855480 * blue_yellow) ** 3
    linear = (4.0767416621 * long_ - 3.3077115913 * medium + 0.2309699292 * short,
              -1.2684380046 * long_ + 2.6097574011 * medium - 0.3413193965 * short,
              -0.0041960863 * long_ - 0.7034186147 * medium + 1.7076147010 * short)
    return tuple(min(1.0, max(0.0, _from_linear(channel))) for channel in linear)


def _mix_oklab(first, second, weight: float):
    """color-mix(in oklab, first weight%, second) — как его считает браузер."""
    left, right = _to_oklab(first), _to_oklab(second)
    return _from_oklab(tuple(a * weight + b * (1 - weight)
                             for a, b in zip(left, right)))
