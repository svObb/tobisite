"""Бренд-цвета лида поверх палитры пресета, с гардом на контраст (решение D).

Из бренд-цветов заменяется только акцентная тройка. paper и ink остаются
пресетными: от них css/source.css считает производные тона (surface, line,
muted), и стоит подменить бумагу цветом с чужого логотипа — вместе с ней
поплывёт весь контраст страницы, а проверять его будет уже нечем.

    accent      — цвет бренда как есть: он несёт только декор
    accent_ink  — тот же тон, сдвинутый по светлоте в oklab до 4.5:1 по paper
    accent_on   — paper или ink, смотря что читается поверх accent_ink

Три причины молча вернуть палитру пресета:

    no_brand_color  цвета нет или он не читается как hex
    low_chroma      «фирменный серый»: логотип чёрно-белый, акцент из него
                    вышел бы грязным пятном, а не цветом бренда
    aa_unreachable  сдвиг светлоты упёрся в границу и до AA не дотянул

Молча — потому что это не брак лида: превью просто выглядит так, как задумано
в пресете. Причина уходит в след черновика (trace["palette"]), а не в текст.

Детерминизм: ни одного случайного числа, шаг сдвига фиксирован, ответ зависит
только от аргументов.
"""
from __future__ import annotations

import re

from .color import (AA_TEXT, PIVOT_LUMINANCE, chroma, from_oklab, luminance,
                    ratio, srgb, to_hex, to_oklab)

# Ниже этой насыщенности в oklab цвет неотличим от серого. #808080 даёт 0,
# приглушённая пыльная терракота (#b98a74) — около 0.05.
MIN_CHROMA = 0.04

# Шаг сдвига светлоты в oklab и потолок числа шагов: вместе они покрывают всю
# шкалу 0..1, поэтому «не дотянули» означает упёрлись в границу цвета, а не
# в границу цикла.
LIGHTNESS_STEP = 0.01
MAX_STEPS = 100

# Порядок, в котором ищется акцент бренда в enrichment: скрейп кладёт
# brand_colors как {primary, accent, source}.
BRAND_KEYS = ("accent", "primary")

HEX = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

PRESET = "preset"
BRAND = "brand"


def brand_palette(brand_colors, preset_palette: dict) -> tuple[dict, str, str]:
    """(палитра, откуда, причина). Причина пуста, только когда взят бренд."""
    palette = dict(preset_palette)

    color = brand_color(brand_colors)
    if color is None:
        return palette, PRESET, "no_brand_color"
    if chroma(srgb(color)) < MIN_CHROMA:
        return palette, PRESET, "low_chroma"

    accent_ink = readable(color, palette["paper"])
    if accent_ink is None:
        return palette, PRESET, "aa_unreachable"
    accent_on = text_on(accent_ink, palette["paper"], palette["ink"])
    if accent_on is None:
        return palette, PRESET, "aa_unreachable"

    return (dict(palette, accent=to_hex(srgb(color)), accent_ink=accent_ink,
                 accent_on=accent_on), BRAND, "")


def brand_color(brand_colors) -> str | None:
    """Акцент бренда из enrichment. Всё, что не hex, — как будто цвета нет."""
    if isinstance(brand_colors, str):
        candidate = brand_colors
    elif isinstance(brand_colors, dict):
        candidate = next((brand_colors[key] for key in BRAND_KEYS
                          if isinstance(brand_colors.get(key), str)
                          and brand_colors[key]), None)
    elif isinstance(brand_colors, (list, tuple)):
        candidate = next((item for item in brand_colors
                          if isinstance(item, str)), None)
    else:
        candidate = None
    return candidate if candidate and HEX.match(candidate.strip()) else None


def readable(color: str, paper: str) -> str | None:
    """Тон бренда, сдвинутый по светлоте до AA по paper. None — не дотянул.

    Сдвиг идёт от бумаги: по светлой бумаге цвет темнеет, по тёмной светлеет.
    Шаг ноль — сам цвет бренда, поэтому акцент, который уже проходит AA,
    остаётся собой и accent_ink совпадает с accent.
    """
    ground = srgb(paper)
    lightness, green_red, blue_yellow = to_oklab(srgb(color))
    direction = -1 if luminance(ground) >= PIVOT_LUMINANCE else 1

    for step in range(MAX_STEPS + 1):
        shifted = lightness + direction * step * LIGHTNESS_STEP
        # Контраст считается по цвету, который реально уйдёт в HTML: округление
        # до байта меняет отношение в третьем знаке, и проверять надо его.
        candidate = to_hex(from_oklab((min(1.0, max(0.0, shifted)),
                                       green_red, blue_yellow)))
        if ratio(srgb(candidate), ground) >= AA_TEXT:
            return candidate
    return None


def text_on(fill: str, paper: str, ink: str) -> str | None:
    """Что писать поверх заливки accent_ink: из двух цветов пресета — лучший."""
    ground = srgb(fill)
    best = max((paper, ink), key=lambda color: ratio(srgb(color), ground))
    return to_hex(srgb(best)) if ratio(srgb(best), ground) >= AA_TEXT else None
