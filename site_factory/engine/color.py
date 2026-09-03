"""Колор-математика движка: sRGB, яркость, oklab, контраст по WCAG.

Одна реализация на весь пакет. Ею checks/a11y.py проверяет контраст готовой
страницы и ею же engine/palette.py доводит бренд-цвет лида до AA: разъедутся
две копии — гард палитры начнёт пропускать то, что проверка потом забракует.

mix_oklab повторяет color-mix(in oklab, ...) браузера: css/source.css считает
им производные тона (surface, line, muted), и Python обязан получать те же
числа. Проценты смешения живут в checks/a11y.py рядом с проверкой — здесь
только математика, без единой доли процента политики.

Все функции принимают и отдают цвет в двух формах: строка "#rrggbb" на входе
у srgb()/to_hex() и тройка float 0..1 внутри. Промежуточных представлений нет.
"""
from __future__ import annotations

AA_TEXT = 4.5

# Яркость цвета, который одинаково контрастен с чёрным и белым. Дальше от него
# в любую сторону — контраст растёт; это и есть направление сдвига в palette.py.
PIVOT_LUMINANCE = 0.1791


def srgb(value: str) -> tuple[float, float, float]:
    """"#abc" или "#aabbcc" -> каналы 0..1."""
    digits = str(value).lstrip("#")
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    return tuple(int(digits[i:i + 2], 16) / 255 for i in (0, 2, 4))


def to_hex(color) -> str:
    """Каналы 0..1 -> "#rrggbb". Здесь и только здесь цвет квантуется в байты."""
    return "#" + "".join(f"{round(min(1.0, max(0.0, channel)) * 255):02x}"
                         for channel in color)


def luminance(color) -> float:
    red, green, blue = (_to_linear(channel) for channel in color)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def ratio(first, second) -> float:
    """Контраст по WCAG 2.1: (L1 + 0.05) / (L2 + 0.05)."""
    light, dark = sorted((luminance(first), luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def to_oklab(color) -> tuple[float, float, float]:
    red, green, blue = (_to_linear(channel) for channel in color)
    long_ = (0.4122214708 * red + 0.5363325363 * green + 0.0514459929 * blue) ** (1 / 3)
    medium = (0.2119034982 * red + 0.6806995451 * green + 0.1073969566 * blue) ** (1 / 3)
    short = (0.0883024619 * red + 0.2817188376 * green + 0.6299787005 * blue) ** (1 / 3)
    return (0.2104542553 * long_ + 0.7936177850 * medium - 0.0040720468 * short,
            1.9779984951 * long_ - 2.4285922050 * medium + 0.4505937099 * short,
            0.0259040371 * long_ + 0.7827717662 * medium - 0.8086757660 * short)


def from_oklab(lab) -> tuple[float, float, float]:
    lightness, green_red, blue_yellow = lab
    long_ = (lightness + 0.3963377774 * green_red + 0.2158037573 * blue_yellow) ** 3
    medium = (lightness - 0.1055613458 * green_red - 0.0638541728 * blue_yellow) ** 3
    short = (lightness - 0.0894841775 * green_red - 1.2914855480 * blue_yellow) ** 3
    linear = (4.0767416621 * long_ - 3.3077115913 * medium + 0.2309699292 * short,
              -1.2684380046 * long_ + 2.6097574011 * medium - 0.3413193965 * short,
              -0.0041960863 * long_ - 0.7034186147 * medium + 1.7076147010 * short)
    return tuple(min(1.0, max(0.0, _from_linear(channel))) for channel in linear)


def mix_oklab(first, second, weight: float):
    """color-mix(in oklab, first weight%, second) — как его считает браузер."""
    left, right = to_oklab(first), to_oklab(second)
    return from_oklab(tuple(a * weight + b * (1 - weight)
                            for a, b in zip(left, right)))


def chroma(color) -> float:
    """Насыщенность в oklab (она же C в oklch). У серого, чёрного и белого — ноль."""
    _, green_red, blue_yellow = to_oklab(color)
    return (green_red ** 2 + blue_yellow ** 2) ** 0.5


def lightness(color) -> float:
    """Светлота L в oklab (она же L в oklch): 0 — чёрный, 1 — белый."""
    return to_oklab(color)[0]


def with_chroma(color, value: float):
    """Тот же цвет с другой насыщенностью: L и тон oklch остаются на месте.

    Срез хромы в palette.py — шаг вдоль радиуса oklch: направление (тон) и
    светлота не меняются, короче становится только вектор (a, b). У серого
    направления нет, и менять ему нечего.
    """
    lightness_, green_red, blue_yellow = to_oklab(color)
    current = (green_red ** 2 + blue_yellow ** 2) ** 0.5
    if not current:
        return from_oklab((lightness_, green_red, blue_yellow))
    scale = value / current
    return from_oklab((lightness_, green_red * scale, blue_yellow * scale))


def _to_linear(channel: float) -> float:
    return (channel / 12.92 if channel <= 0.04045
            else ((channel + 0.055) / 1.055) ** 2.4)


def _from_linear(channel: float) -> float:
    return (channel * 12.92 if channel <= 0.0031308
            else 1.055 * channel ** (1 / 2.4) - 0.055)
