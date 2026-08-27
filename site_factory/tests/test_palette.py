"""Бренд-цвета лида: либо палитра проходит AA, либо её нет вовсе (решение D).

Главный инвариант этого файла — сетка «пресет × цвет»: что бы ни приехало из
логотипа, страница обязана остаться читаемой. Третьего исхода (палитра бренда
с проваленным контрастом) быть не может, поэтому проверяется именно «или-или».
"""
import colorsys

import pytest

from site_factory.engine.checks import a11y
from site_factory.engine.color import chroma, luminance, ratio, srgb
from site_factory.engine.palette import (MIN_CHROMA, brand_color, brand_palette,
                                         readable)
from site_factory.engine.render import load_tokens

# Двадцать цветов по кругу оттенков плюс серые: так выглядит то, что реально
# приезжает из чужих логотипов — от кричащего фирменного до чёрно-белого.
WHEEL = tuple("#" + "".join(f"{round(channel * 255):02x}" for channel in
                            colorsys.hsv_to_rgb(step / 20, 0.75, 0.8))
              for step in range(20))
GREYS = ("#ffffff", "#000000", "#808080", "#3a3d40", "#e8e8e8")
COLORS = WHEEL + GREYS

PRESETS = [preset for preset in load_tokens()["presets"]]
PALETTES = {preset["id"]: preset["palette"] for preset in PRESETS}


@pytest.mark.parametrize("preset_id", sorted(PALETTES))
def test_every_brand_color_leaves_the_page_readable(preset_id):
    preset = PALETTES[preset_id]
    for color in COLORS:
        palette, source, reason = brand_palette({"accent": color}, preset)
        problems = a11y.contrast_problems(palette)
        if source == "brand":
            assert problems == [], f"{preset_id} + {color}: {problems}"
            assert reason == ""
        else:
            assert palette == preset, f"{preset_id} + {color}: палитра не пресетная"
            assert reason in ("low_chroma", "aa_unreachable")


def test_brand_palette_is_deterministic():
    preset = PALETTES["corporate-trust"]
    for color in COLORS:
        first = brand_palette({"accent": color}, preset)
        second = brand_palette({"accent": color}, preset)
        assert first == second


def test_only_the_accent_trio_is_replaced():
    preset = PALETTES["editorial-warm"]
    palette, source, _ = brand_palette({"accent": "#1f6f4a"}, preset)
    assert source == "brand"
    assert palette["paper"] == preset["paper"]
    assert palette["ink"] == preset["ink"]
    assert palette["accent"] == "#1f6f4a"
    assert palette["accent_ink"] != preset["accent_ink"]
    assert palette["accent_on"] in (preset["paper"], preset["ink"])


def test_dark_paper_shifts_the_accent_into_the_light():
    """На чёрной бумаге тон бренда обязан светлеть, а не темнеть дальше."""
    paper = PALETTES["bold-trade"]["paper"]
    brand = "#7a3b00"
    accent_ink = readable(brand, paper)
    assert luminance(srgb(accent_ink)) > luminance(srgb(brand))
    assert ratio(srgb(accent_ink), srgb(paper)) >= a11y.AA_TEXT


def test_light_paper_shifts_the_accent_into_the_dark():
    paper = PALETTES["clinical-light"]["paper"]
    brand = "#ffd166"
    accent_ink = readable(brand, paper)
    assert luminance(srgb(accent_ink)) < luminance(srgb(brand))
    assert ratio(srgb(accent_ink), srgb(paper)) >= a11y.AA_TEXT


def test_accent_that_already_passes_stays_itself():
    preset = PALETTES["corporate-trust"]
    palette, source, _ = brand_palette("#35506f", preset)
    assert source == "brand"
    assert palette["accent"] == palette["accent_ink"] == "#35506f"


def test_grey_brand_falls_back_to_the_preset():
    """Логотип чёрно-белый: акцента в нём нет, и выдумывать его нечем."""
    preset = PALETTES["warm-table"]
    for grey in ("#8a8f94", "#333333", "#ffffff"):
        assert chroma(srgb(grey)) < MIN_CHROMA
        palette, source, reason = brand_palette({"accent": grey}, preset)
        assert (source, reason) == ("preset", "low_chroma")
        assert palette == preset


def test_missing_or_broken_colors_fall_back_quietly():
    preset = PALETTES["clinical-light"]
    for value in (None, {}, {"accent": ""}, {"accent": "не цвет"}, "rgb(1,2,3)",
                  [], 42, {"source": "logo"}):
        palette, source, reason = brand_palette(value, preset)
        assert (source, reason) == ("preset", "no_brand_color"), value
        assert palette == preset


def test_primary_is_taken_when_accent_is_absent():
    assert brand_color({"primary": "#c0392b", "source": "logo"}) == "#c0392b"
    assert brand_color({"accent": "#1abc9c", "primary": "#c0392b"}) == "#1abc9c"
    assert brand_color("#abc") == "#abc"
    assert brand_color(["#c0392b", "#1abc9c"]) == "#c0392b"


def test_short_hex_is_expanded():
    palette, source, _ = brand_palette("#0a6", PALETTES["editorial-warm"])
    assert source == "brand"
    assert palette["accent"] == "#00aa66"


def test_preset_palettes_are_their_own_fixed_point():
    """Пресет, поданный сам себе как бренд-цвет, обязан остаться собой."""
    for preset in PRESETS:
        palette, source, _ = brand_palette(preset["palette"]["accent"],
                                           preset["palette"])
        if source == "brand":
            assert palette["accent"] == preset["palette"]["accent"]
            assert a11y.contrast_problems(palette) == []
