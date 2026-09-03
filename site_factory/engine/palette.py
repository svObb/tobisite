"""Бренд-цвета лида поверх палитры пресета, с гардом на контраст (решение D).

Из бренд-цветов заменяется только акцентная тройка. paper и ink остаются
пресетными: от них css/source.css считает производные тона (surface, line,
muted), и стоит подменить бумагу цветом с чужого логотипа — вместе с ней
поплывёт весь контраст страницы, а проверять его будет уже нечем.

    accent      — цвет бренда, срезанный до потолка насыщенности: он несёт декор
    accent_ink  — тот же тон, сдвинутый по светлоте в oklab до 4.5:1 по paper
    accent_on   — paper или ink, смотря что читается поверх accent_ink

Три причины молча вернуть палитру пресета:

    no_brand_color  цвета нет или он не читается как hex
    low_chroma      «фирменный серый»: логотип чёрно-белый, акцент из него
                    вышел бы грязным пятном, а не цветом бренда
    aa_unreachable  сдвиг светлоты упёрся в границу и до AA не дотянул

Молча — потому что это не брак лида: превью просто выглядит так, как задумано
в пресете. Причина уходит в след черновика (trace["palette"]), а не в текст.
Палитра бренда причину тоже несёт: chroma_capped значит, что цвет взят, но
его насыщенность пришлось срезать до потолка.

Детерминизм: ни одного случайного числа, шаг сдвига фиксирован, ответ зависит
только от аргументов.
"""
from __future__ import annotations

import re

from .color import (AA_TEXT, PIVOT_LUMINANCE, chroma, from_oklab, lightness,
                    luminance, ratio, srgb, to_hex, to_oklab, with_chroma)

# Ниже этой насыщенности в oklab цвет неотличим от серого. #808080 даёт 0,
# приглушённая пыльная терракота (#b98a74) — около 0.05.
MIN_CHROMA = 0.04

# Потолок насыщенности акцента. Выше 0.23 в oklch цвет на экране светится, а в
# светлой зоне (L > 0.78) флуоресцентными выглядят уже 0.18. Логотипы приносят
# такое регулярно, и срез — не отказ: тон и светлота остаются на месте, дальше
# работает обычный AA-гард.
MAX_CHROMA = 0.23
LIGHT_ZONE_CHROMA = 0.18
LIGHT_ZONE = 0.78

# Зона Гельмгольца–Кольрауша: насыщенная заливка средней светлоты выглядит
# светлее, чем меряется, и тёмный текст на ней читается хуже своей формулы.
# Ниже HK_MIN_RATIO светлый текст на такой заливке не читается вовсе — тогда
# выбор возвращается к прежнему правилу «что контрастнее».
HK_LIGHTNESS = (0.42, 0.78)
HK_CHROMA = 0.08
HK_MIN_RATIO = 3.0

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
CHROMA_CAPPED = "chroma_capped"


def brand_palette(brand_colors, preset_palette: dict) -> tuple[dict, str, str]:
    """(палитра, откуда, причина). У пресетной палитры причина — почему не бренд."""
    palette = dict(preset_palette)

    color = brand_color(brand_colors)
    if color is None:
        return palette, PRESET, "no_brand_color"
    if chroma(srgb(color)) < MIN_CHROMA:
        return palette, PRESET, "low_chroma"

    accent = cap_chroma(color)
    accent_ink = readable(accent, palette["paper"])
    if accent_ink is None:
        return palette, PRESET, "aa_unreachable"
    accent_on = text_on(accent_ink, palette["paper"], palette["ink"])
    if accent_on is None:
        return palette, PRESET, "aa_unreachable"

    capped = accent != to_hex(srgb(color))
    return (dict(palette, accent=accent, accent_ink=accent_ink,
                 accent_on=accent_on), BRAND, CHROMA_CAPPED if capped else "")


def cap_chroma(color: str) -> str:
    """Цвет бренда, срезанный до потолка насыщенности: тон и светлота на месте."""
    rgb = srgb(color)
    ceiling = LIGHT_ZONE_CHROMA if lightness(rgb) > LIGHT_ZONE else MAX_CHROMA
    if chroma(rgb) <= ceiling:
        return to_hex(rgb)
    return to_hex(with_chroma(rgb, ceiling))


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
    """Что писать поверх заливки accent_ink: из двух цветов пресета — лучший.

    Насыщенная заливка средней светлоты кажется глазу светлее, чем меряется
    (эффект Гельмгольца–Кольрауша), и тёмный текст на ней выглядит грязным
    даже с формально проходящим контрастом. На такой заливке берётся светлый
    из пары, как только он вообще читается.

    Результат всё равно проходит AA: светлый вариант, который читается по
    Гельмгольцу, но не дотягивает до 4.5, — это не палитра бренда, а
    aa_unreachable. Кнопка с грязным текстом на превью не лучше пресетной.
    """
    ground = srgb(fill)
    light, _ = sorted((paper, ink), key=lambda color: luminance(srgb(color)),
                      reverse=True)
    if _glows(ground) and ratio(srgb(light), ground) >= HK_MIN_RATIO:
        best = light
    else:
        best = max((paper, ink), key=lambda color: ratio(srgb(color), ground))
    return to_hex(srgb(best)) if ratio(srgb(best), ground) >= AA_TEXT else None


def _glows(fill) -> bool:
    """Заливка из зоны Гельмгольца–Кольрауша: насыщенная и средней светлоты."""
    low, high = HK_LIGHTNESS
    return low <= lightness(fill) <= high and chroma(fill) >= HK_CHROMA
