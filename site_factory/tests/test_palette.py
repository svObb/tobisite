"""Бренд-цвета лида: либо палитра проходит AA, либо её нет вовсе (решение D).

Главный инвариант этого файла — сетка «пресет × цвет»: что бы ни приехало из
логотипа, страница обязана остаться читаемой. Третьего исхода (палитра бренда
с проваленным контрастом) быть не может, поэтому проверяется именно «или-или».
"""
import colorsys
import re

import pytest

from site_factory.engine.checks import a11y
from site_factory.engine.color import (chroma, lightness, luminance, mix_oklab,
                                       ratio, srgb, to_hex)
from site_factory.engine.palette import (CHROMA_CAPPED, HK_CHROMA, LIGHT_ZONE,
                                         LIGHT_ZONE_CHROMA, MAX_CHROMA,
                                         MIN_CHROMA, brand_color, brand_palette,
                                         cap_chroma, readable)
from site_factory.engine.render import ROOT, load_tokens

# Двадцать цветов по кругу оттенков плюс серые: так выглядит то, что реально
# приезжает из чужих логотипов — от кричащего фирменного до чёрно-белого.
WHEEL = tuple("#" + "".join(f"{round(channel * 255):02x}" for channel in
                            colorsys.hsv_to_rgb(step / 20, 0.75, 0.8))
              for step in range(20))
GREYS = ("#ffffff", "#000000", "#808080", "#3a3d40", "#e8e8e8")
# Кислотные логотипы: неон выше потолка хромы и насыщенный красный средней
# светлоты — на нём и виден эффект Гельмгольца–Кольрауша.
HOT = ("#ff0040", "#ff1493", "#e53e3e", "#39ff14")
COLORS = WHEEL + GREYS + HOT

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
            assert reason in ("", CHROMA_CAPPED)
            assert chroma(srgb(palette["accent"])) <= MAX_CHROMA + 1e-3
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


def test_an_oversaturated_brand_colour_is_cut_to_the_ceiling():
    """Кислотный логотип не отменяет бренд: срезаем хрому, тон оставляем."""
    preset = PALETTES["clinical-light"]
    palette, source, reason = brand_palette({"accent": "#ff0040"}, preset)
    assert (source, reason) == ("brand", CHROMA_CAPPED)
    accent = srgb(palette["accent"])
    assert chroma(srgb("#ff0040")) > MAX_CHROMA
    assert round(chroma(accent), 3) <= MAX_CHROMA
    assert round(lightness(accent), 2) == round(lightness(srgb("#ff0040")), 2)
    assert a11y.contrast_problems(palette) == []


def test_the_light_zone_has_a_lower_ceiling():
    """Выше L 0.78 флуоресцентным выглядит уже 0.18, а не 0.23."""
    pale = "#ffe600"
    assert lightness(srgb(pale)) > LIGHT_ZONE
    assert round(chroma(srgb(cap_chroma(pale))), 3) <= LIGHT_ZONE_CHROMA


def test_a_saturated_fill_takes_the_light_text():
    """Гельмгольц–Кольрауш: тёмный текст формально проходит, а выглядит грязным.

    Чернила пресетов чуть светлее чистого чёрного и до 4.5 по такой заливке
    не дотягивают — чтобы тёмный вариант формально проходил и правило было
    видно, чернила здесь чёрные.
    """
    preset = dict(PALETTES["clinical-light"], ink="#000000")
    palette, source, _ = brand_palette({"accent": "#ff4d4d"}, preset)
    fill = srgb(palette["accent_ink"])

    assert source == "brand"
    assert ratio(srgb(preset["ink"]), fill) >= a11y.AA_TEXT
    assert ratio(srgb(preset["ink"]), fill) > ratio(srgb(preset["paper"]), fill)
    assert palette["accent_on"] == preset["paper"]
    assert a11y.contrast_problems(palette) == []


def test_a_muted_fill_keeps_the_old_rule():
    """Вне зоны эффекта побеждает контраст, а не светлота."""
    preset = dict(PALETTES["clinical-light"], ink="#000000")
    palette, source, _ = brand_palette({"accent": "#b28686"}, preset)
    fill = srgb(palette["accent_ink"])

    assert source == "brand"
    assert MIN_CHROMA <= chroma(fill) < HK_CHROMA
    assert palette["accent_on"] == max((preset["paper"], preset["ink"]),
                                       key=lambda color: ratio(srgb(color), fill))


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
    """Пресет, поданный сам себе как бренд-цвет, обязан остаться собой.

    Ровно до потолка хромы: акцент неонового пресета ярче гарда, и гард
    срезает его так же, как срезал бы тот же цвет с чужого логотипа.
    """
    for preset in PRESETS:
        accent = preset["palette"]["accent"]
        palette, source, _ = brand_palette(accent, preset["palette"])
        if source == "brand":
            assert palette["accent"] == cap_chroma(accent)
            assert a11y.contrast_problems(palette) == []


# --- шапка поверх чужого кадра ------------------------------------------------

# Плотность подложки .header-overlay и доля бумаги в её приглушённом тоне —
# числа из css/source.css. Читаются оттуда же, чтобы правка стиля падала здесь,
# а не на первом экране лида.
OVERLAY_SCRIM = re.compile(
    r"\.header-overlay\s*\{.*?--color-muted:\s*color-mix\(in oklab, "
    r"var\(--paper\) (\d+)%.*?var\(--ink\) (\d+)%, transparent\)", re.S)

# Кадр лида непредсказуем: под подложкой может оказаться и белая стена, и
# ночная витрина, и ровный полутон.
ANY_FRAME = ((1.0, 1.0, 1.0), (0.0, 0.0, 0.0), (0.5, 0.5, 0.5))


def overlay_mixes() -> tuple[float, float]:
    source = (ROOT / "css" / "source.css").read_text(encoding="utf-8")
    muted, density = OVERLAY_SCRIM.search(source).groups()
    return int(muted) / 100, int(density) / 100


@pytest.mark.parametrize("preset_id", sorted(PALETTES))
def test_the_header_over_a_photo_stays_readable_on_any_frame(preset_id):
    """Обещание комментария в source.css: буквы шапки держат AA на любом кадре.

    checks/a11y.py этого не считает — он берёт сплошные пары палитры, а тут
    полупрозрачная подложка поверх чужого снимка. Считаем здесь, той же
    колор-математикой, которой браузер считает color-mix.
    """
    paper_mix, density = overlay_mixes()
    ink, paper = srgb(PALETTES[preset_id]["ink"]), srgb(PALETTES[preset_id]["paper"])
    muted = mix_oklab(paper, ink, paper_mix)

    for frame in ANY_FRAME:
        ground = mix_oklab(ink, frame, density)
        assert ratio(paper, ground) >= a11y.AA_TEXT, preset_id
        assert ratio(muted, ground) >= a11y.AA_TEXT, preset_id


def test_the_menu_of_the_overlay_header_still_changes_colour_on_hover():
    """hover ведёт из --color-muted в --color-ink: цвета обязаны быть разными."""
    paper_mix, _ = overlay_mixes()
    assert paper_mix < 1.0
    for palette in PALETTES.values():
        ink, paper = srgb(palette["ink"]), srgb(palette["paper"])
        assert to_hex(mix_oklab(paper, ink, paper_mix)) != to_hex(paper)
