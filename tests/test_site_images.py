"""Картинки с чужого сайта: отбор, пережатие, роли, санитайзинг SVG.

Байты картинок рисует сам тест: файлов-фикстур для этого не нужно, а
проверяется ровно то, что модуль делает с размерами, альфой и форматом.
"""
import io
import random

import pytest
from PIL import Image

import site_images as si

ORANGE, BLUE = (194, 98, 26), (43, 74, 140)
SEED = 20260905


def raster(width, height, color=ORANGE, mode="RGB", fmt="PNG") -> bytes:
    img = Image.new(mode, (width, height), color)
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


def photo_like(width, height) -> bytes:
    """Кадр, который классификатор обязан считать снимком: шум по градиенту.

    Шум крупноблочный, а не попиксельный: загрублённая до 64×64 копия усреднила
    бы мелкое зерно в ровную заливку, и «фото» вышло бы графикой — ровно та
    ошибка, которую эти тесты и должны ловить. Зерно фиксировано, чтобы
    прогоны не расходились.
    """
    dice = random.Random(SEED)
    grid = Image.new("RGB", (si.GRAPHIC_GRID, si.GRAPHIC_GRID))
    grid.putdata([
        (_channel(40 + x * 3 + dice.randint(-40, 40)),
         _channel(90 + y * 2 + dice.randint(-40, 40)),
         _channel(150 - x * 2 + dice.randint(-40, 40)))
        for y in range(si.GRAPHIC_GRID) for x in range(si.GRAPHIC_GRID)
    ])
    return _png(grid.resize((width, height), Image.Resampling.NEAREST))


def flat_graphic(width, height) -> bytes:
    """Плашка: пара заливок поверх фона, ни шума, ни градиента."""
    img = Image.new("RGB", (width, height), (246, 246, 244))
    img.paste(Image.new("RGB", (width, height // 4), ORANGE), (0, 0))
    img.paste(Image.new("RGB", (width // 4, height // 4), BLUE),
              (width // 2, height // 2))
    return _png(img)


def icon(width, height, opaque=0.5) -> bytes:
    """Значок: непрозрачное пятно посреди пустоты, как PNG-лого «Free WiFi»."""
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    side = (round(width * opaque), round(height * opaque))
    img.paste(Image.new("RGBA", side, ORANGE + (255,)),
              ((width - side[0]) // 2, (height - side[1]) // 2))
    return _png(img)


def indexed_icon(width, height) -> bytes:
    """Тот же значок, но в палитре: прозрачность объявлена индексом, не альфой."""
    img = Image.new("P", (width, height), 0)
    img.putpalette([0, 0, 0] + list(ORANGE) + [0] * 762)
    img.paste(1, (width // 2 - 30, height // 2 - 20,
                  width // 2 + 30, height // 2 + 20))
    buf = io.BytesIO()
    img.save(buf, "PNG", transparency=0)
    return buf.getvalue()


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _channel(value: int) -> int:
    return max(0, min(255, value))


def opened(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


# --- отбор --------------------------------------------------------------------

def test_a_broken_file_is_not_an_image():
    assert si.probe_image(b"not an image at all") is None
    assert si.process_image(b"") is None
    assert si.dominant_colors(b"\x89PNG oops") == []


def test_probe_reads_size_and_alpha():
    assert si.probe_image(raster(800, 600)) == {"width": 800, "height": 600,
                                                "alpha": False}
    assert si.probe_image(raster(64, 64, mode="RGBA"))["alpha"] is True


@pytest.mark.parametrize("width,height,ok", [
    (200, 200, True), (199, 400, False), (400, 199, False),
    (2000, 400, False),                       # 5:1 — это баннер, а не фото
    (400, 2000, False),
    (1600, 400, True),                        # ровно 4:1 ещё проходит
])
def test_photo_gates(width, height, ok):
    assert si.fits({"width": width, "height": height}, "photo") is ok


def test_a_wide_wordmark_is_a_valid_logo():
    # логотип-леттеринг 600×80 — это 7,5:1, и отсеивать его по пропорциям нельзя
    assert si.fits({"width": 600, "height": 80}, "logo") is True
    assert si.fits({"width": 40, "height": 40}, "logo") is False


# --- пережатие ----------------------------------------------------------------

def test_photo_is_capped_and_becomes_webp():
    made = si.process_image(raster(3000, 2000), "photo")

    assert made["width"] == si.ROLE_MAX_SIDE["photo"] and made["height"] == 800
    assert made["content_type"] == "image/webp"
    assert opened(made["data"]).format == "WEBP"
    assert len(made["data"]) < len(raster(3000, 2000))


def test_background_keeps_a_larger_side_than_a_photo():
    wide = raster(4000, 1600)

    assert si.process_image(wide, "background")["width"] == 2000
    assert si.process_image(wide, "photo")["width"] == 1200


def test_logo_is_capped_at_its_own_side():
    made = si.process_image(raster(1500, 1500), "logo")

    assert made["width"] == si.ROLE_MAX_SIDE["logo"] == 512


def test_a_small_image_is_not_blown_up():
    made = si.process_image(raster(300, 200), "photo")

    assert (made["width"], made["height"]) == (300, 200)


def test_transparency_survives_the_conversion():
    logo = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    logo.paste(Image.new("RGBA", (100, 100), ORANGE + (255,)), (10, 10))
    buf = io.BytesIO()
    logo.save(buf, "PNG")

    made = si.process_image(buf.getvalue(), "logo")

    # без альфы логотип получил бы белую подложку на цветной шапке
    assert opened(made["data"]).mode in ("RGBA", "LA")


def test_role_names_map_to_processing_rules():
    assert si.role_of("logo") == "logo"
    assert si.role_of("hero_bg") == "background"
    assert si.role_of("portrait") == si.role_of("photo-3") == "photo"


# --- цвета --------------------------------------------------------------------

def test_dominant_colours_skip_white_and_transparent():
    canvas = Image.new("RGBA", (200, 200), (255, 255, 255, 255))
    canvas.paste(Image.new("RGBA", (120, 120), ORANGE + (255,)), (0, 0))
    canvas.paste(Image.new("RGBA", (40, 40), BLUE + (255,)), (150, 150))
    buf = io.BytesIO()
    canvas.save(buf, "PNG")

    colors = si.dominant_colors(buf.getvalue())

    assert len(colors) == 2 and colors[0].startswith("#c")
    assert all(color != "#ffffff" for color in colors)


def test_a_grey_logo_has_no_brand_colour():
    assert si.dominant_colors(raster(200, 200, (128, 128, 128))) == []


# --- светлота -----------------------------------------------------------------

def test_a_black_logo_is_at_the_bottom_of_the_scale_and_a_white_one_at_the_top():
    black = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    black.paste(Image.new("RGBA", (100, 100), (0, 0, 0, 255)), (50, 50))
    buf = io.BytesIO()
    black.save(buf, "PNG")

    assert si.mean_lightness(buf.getvalue()) == pytest.approx(0, abs=0.01)
    assert si.mean_lightness(raster(200, 200, (255, 255, 255))) == \
        pytest.approx(1, abs=0.01)


def test_a_grey_logo_has_no_colour_but_it_has_a_lightness():
    """Ради этого случая светлота и считается: цвета о таком логотипе молчат."""
    grey = raster(200, 200, (128, 128, 128))

    assert si.dominant_colors(grey) == []
    assert si.mean_lightness(grey) == pytest.approx(0.6, abs=0.01)


def test_a_logo_without_a_single_opaque_pixel_says_nothing():
    """За прозрачными пикселями стоит фон страницы — утверждать о них нечего."""
    assert si.mean_lightness(raster(200, 200, (0, 0, 0, 0), mode="RGBA")) is None
    assert si.mean_lightness(b"\x89PNG oops") is None


# --- графика против фото ------------------------------------------------------
#
# Инцидент 03.09: в галерею кав'ярні уехали рекламный баннер и PNG-значок
# «Free WiFi» — обычные <img>, крупнее 200px и внутри вилки аспектов, то есть
# для fits() безупречные. Пороги здесь выведены рассуждением, а не выборкой,
# поэтому тесты проверяют сторону порога и порядок метрик, а не сами числа.


def test_a_flat_fill_is_graphics_and_a_noisy_frame_is_not():
    plate = si.graphic_probe(flat_graphic(800, 600))
    frame = si.graphic_probe(photo_like(800, 600))

    assert plate["colors"] < si.GRAPHIC_MAX_COLORS <= frame["colors"]
    assert plate["flat"] >= si.GRAPHIC_FLAT > frame["flat"]
    assert plate["dominant"] >= si.GRAPHIC_DOMINANT > frame["dominant"]


def test_a_transparent_png_never_reaches_the_gallery():
    data = icon(600, 400)

    verdict = si.graphic_verdict(si.probe_image(data), si.graphic_probe(data))

    assert verdict["hard"] == "alpha"


def test_an_opaque_alpha_channel_is_judged_by_pixels():
    """Фото, экспортированное в PNG с пустым альфа-каналом, — обычное фото."""
    img = opened(photo_like(800, 600)).convert("RGBA")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    data = buf.getvalue()

    size = si.probe_image(data)
    verdict = si.graphic_verdict(size, si.graphic_probe(data))

    assert size["alpha"] is True
    assert verdict["hard"] == "" and verdict["soft"] is False


def test_an_indexed_gif_never_reaches_the_gallery():
    buf = io.BytesIO()
    opened(flat_graphic(800, 600)).convert("P").save(buf, "GIF")
    data = buf.getvalue()

    probe = si.graphic_probe(data)

    assert probe["indexed"] is True
    assert si.graphic_verdict(si.probe_image(data), probe)["hard"] == "indexed"


def test_a_strip_is_cut_even_though_fits_lets_it_through():
    """fits() не трогаем: 4:1 остаётся годным, полосу режет классификатор."""
    size = {"width": 1600, "height": 400, "alpha": False}

    assert si.fits(size, "photo") is True
    assert si.graphic_verdict(size, None)["hard"] == "strip"
    assert si.graphic_verdict({"width": 400, "height": 1600,
                               "alpha": False}, None)["hard"] == "strip"


def test_a_single_signal_is_not_enough_for_a_verdict():
    data = photo_like(800, 600)

    verdict = si.graphic_verdict(si.probe_image(data), si.graphic_probe(data),
                                 hint=True)

    assert verdict["score"] == si.W_HINT and verdict["soft"] is False


def test_two_signals_make_it_graphics():
    data = flat_graphic(800, 600)

    verdict = si.graphic_verdict(si.probe_image(data), si.graphic_probe(data))

    assert verdict["soft"] is True and verdict["hard"] == ""


def test_a_picture_without_pixels_to_judge_says_nothing():
    """Не открылось — None; открылось, но мерить нечего — пустые метрики."""
    assert si.graphic_probe(b"\x89PNG oops") is None

    probe = si.graphic_probe(icon(600, 400, opaque=0.1))

    assert probe["colors"] is None and probe["dominant"] is None
    assert probe["flat"] is None
    # ни альфы, ни палитры: разрежённость сама по себе ничего не доказывает
    verdict = si.graphic_verdict({"width": 600, "height": 400, "alpha": False},
                                 probe)

    assert verdict == {"hard": "", "score": 0, "soft": False}


def test_a_sparse_icon_on_a_transparent_background_is_still_a_sticker():
    """Значок «Free WiFi» с меткой на 6% кадра — та самая дыра инцидента."""
    data = icon(600, 400, opaque=0.1)
    size = si.probe_image(data)

    verdict = si.graphic_verdict(size, si.graphic_probe(data))

    assert size["alpha"] is True
    assert verdict["hard"] == "alpha"


def test_a_sparse_indexed_image_is_still_indexed():
    probe = si.graphic_probe(indexed_icon(600, 400))

    assert probe["indexed"] is True and probe["colors"] is None
    # альфу гасим руками: у настоящего такого PNG сработало бы её правило,
    # первое по порядку, а прочесть здесь нужно именно палитру
    verdict = si.graphic_verdict({"width": 600, "height": 400, "alpha": False},
                                 probe)

    assert verdict["hard"] == "indexed"


def test_graphics_never_take_the_hero_or_the_portrait():
    roles = si.assign_roles([photo(2400, 1200, "plashka.png", graphic=100),
                             photo(1000, 1200, "team.jpg")])

    assert "hero_bg" not in roles
    assert roles["portrait"]["url"] == "team.jpg"
    assert roles["photo-2"]["url"] == "plashka.png"


def test_graphics_are_the_first_to_fall_off_the_budget():
    roles = si.assign_roles(
        [photo(2000 - n, 1500, f"p{n}.jpg") for n in range(9)]
        + [photo(2400, 1600, "wide-plashka.png", graphic=70),
           photo(2300, 1500, "promo.png", graphic=100)]
    )

    assert len(roles) == si.MAX_STAGED
    assert all(not item.get("graphic") for item in roles.values())


def test_graphics_still_reach_the_gallery_when_there_is_nothing_else():
    """Мягкая метка не удаляет: лишняя картинка дешевле пустой страницы."""
    roles = si.assign_roles([photo(1200, 900, "promo.png", graphic=70)])

    assert list(roles) == ["photo-2"]
    assert roles["photo-2"]["url"] == "promo.png"


def test_a_candidate_without_the_flag_ranks_exactly_as_before():
    cands = [photo(2400, 1200, "wide.jpg"), photo(1200, 1600, "portrait.jpg",
                                                  og=True),
             photo(900, 900, "third.jpg"), photo(1200, 1600, "same.jpg")]

    order = [c["url"] for c in sorted(cands, key=si._photo_rank)]

    assert order == ["wide.jpg", "portrait.jpg", "same.jpg", "third.jpg"]


# --- SVG ----------------------------------------------------------------------

SAFE_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 40">'
            '<rect width="100" height="40" fill="#c2621a"/>'
            '<text x="4" y="28">Тепло</text></svg>')


def test_a_clean_logo_passes_through():
    clean = si.sanitize_svg(SAFE_SVG)

    assert clean is not None and "<rect" in clean and "#c2621a" in clean


def test_script_inside_the_logo_is_cut_out():
    markup = SAFE_SVG.replace("<rect", '<script>alert(1)</script><rect')

    clean = si.sanitize_svg(markup)

    assert clean is not None
    assert "script" not in clean and "alert" not in clean
    assert "<rect" in clean


def test_event_handlers_and_javascript_links_do_not_survive():
    markup = SAFE_SVG.replace(
        "<text", '<a href="javascript:alert(2)"><text onclick="alert(3)"'
    ).replace("</text>", "</text></a>")

    clean = si.sanitize_svg(markup)

    assert clean is not None
    assert "javascript:" not in clean and "onclick" not in clean


def test_foreign_object_is_cut_out():
    markup = SAFE_SVG.replace(
        "<rect", '<foreignObject><iframe src="https://evil.example/">'
                 "</iframe></foreignObject><rect"
    )

    clean = si.sanitize_svg(markup)

    assert clean is not None and "foreignObject" not in clean


def test_an_external_reference_is_dropped():
    markup = SAFE_SVG.replace(
        "<rect", '<image href="https://evil.example/track.png"/><rect'
    )

    clean = si.sanitize_svg(markup)

    assert "evil.example" not in clean


def test_a_doubtful_logo_is_refused_outright():
    assert si.sanitize_svg("<svg><rect></svg>") is None      # разметка битая
    assert si.sanitize_svg("<div>не svg</div>") is None
    assert si.sanitize_svg("") is None
    assert si.sanitize_svg("<svg>" + "<rect/>" * 40_000 + "</svg>") is None


def test_the_fixture_logo_is_cleaned():
    import site_scrape as ss
    from test_site_scrape import page

    logos = ss.logo_candidates(ss.soup_of(page("svg_logo.html")),
                               "https://teplo.example/")

    clean = si.sanitize_svg(logos[0]["markup"])

    assert "<script" in logos[0]["markup"]
    assert clean is not None
    assert "script" not in clean and "javascript:" not in clean
    assert "onclick" not in clean and "<rect" in clean
    # разметку приносит HTML-парсер, а он пишет viewBox строчными
    assert si.svg_size(clean) == {"width": 200, "height": 60}


def test_the_size_of_a_logo_comes_from_its_view_box():
    assert si.svg_size(SAFE_SVG) == {"width": 100, "height": 40}


def test_the_width_and_height_of_the_root_win_over_the_view_box():
    markup = SAFE_SVG.replace("<svg ", '<svg width="64px" height="20.6" ')

    assert si.svg_size(markup) == {"width": 64, "height": 21}


def test_a_logo_without_numbers_has_no_size():
    """Проценты и auto — не размер: место под логотип ими не удержать."""
    bare = '<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>'
    assert si.svg_size(bare) is None
    assert si.svg_size(bare.replace("<svg ", '<svg width="100%" height="auto" ')) is None
    assert si.svg_size(SAFE_SVG.replace('viewBox="0 0 100 40"',
                                        'viewBox="0 0 100"')) is None
    assert si.svg_size(SAFE_SVG.replace('viewBox="0 0 100 40"',
                                        'viewBox="0 0 0 0"')) is None
    assert si.svg_size("<svg><rect></svg>") is None


# --- роли ---------------------------------------------------------------------

def photo(width, height, url, **kw) -> dict:
    item = {"kind": "photo", "url": url, "width": width, "height": height,
            "og": kw.get("og", False), "product": kw.get("product", False)}
    if kw.get("graphic"):
        # ключа нет вовсе, пока его не поставили: так кандидат и приходит из
        # обогащения, и на этом держится «без метки — как раньше»
        item["graphic"] = kw["graphic"]
    return item


def test_roles_follow_the_contract_with_the_engine():
    roles = si.assign_roles([
        {"kind": "logo", "url": "l.png", "width": 240, "height": 80},
        photo(2400, 1200, "wide.jpg"),
        photo(1200, 1600, "portrait.jpg", og=True),
        photo(900, 900, "third.jpg"),
    ])

    assert set(roles) == {"logo", "hero_bg", "portrait", "photo-2"}
    assert roles["hero_bg"]["url"] == "wide.jpg"
    assert roles["portrait"]["url"] == "portrait.jpg"
    assert roles["photo-2"]["url"] == "third.jpg"


def test_a_single_photo_becomes_the_portrait_not_the_background():
    roles = si.assign_roles([photo(2400, 1000, "only.jpg")])

    assert set(roles) == {"portrait"}


def test_product_shots_do_not_take_the_hero():
    roles = si.assign_roles([
        photo(2000, 1000, "shop.jpg"),
        photo(1500, 1500, "box.jpg", product=True),
        photo(800, 600, "team.jpg"),
    ])

    assert roles["hero_bg"]["url"] == "shop.jpg"
    assert roles["portrait"]["url"] == "team.jpg"
    assert roles["photo-2"]["url"] == "box.jpg"


def test_a_shop_with_only_product_shots_gets_no_hero_and_no_portrait():
    """Шапка и портрет из витрины — то, из-за чего превью выглядит каталогом."""
    roles = si.assign_roles([photo(2400, 1000, "box.jpg", product=True),
                             photo(1000, 1000, "case.jpg", product=True)])

    # товары при этом не пропадают: их разберут товарные секции
    assert list(roles) == ["photo-2", "photo-3"]
    assert roles["photo-2"]["url"] == "box.jpg"
    assert roles["photo-3"]["url"] == "case.jpg"


def test_the_portrait_comes_only_from_a_non_product_photo():
    roles = si.assign_roles([photo(2000, 2000, "box.jpg", product=True),
                             photo(600, 800, "team.jpg")])

    assert roles["portrait"]["url"] == "team.jpg"
    assert roles["photo-2"]["url"] == "box.jpg"


def test_the_staging_budget_is_a_hard_ceiling():
    roles = si.assign_roles(
        [{"kind": "logo", "url": "l.png", "width": 240, "height": 80}]
        + [photo(1000 - n, 800, f"p{n}.jpg") for n in range(12)]
    )

    assert len(roles) == si.MAX_STAGED == 8
    assert len(si.photo_names(roles)) == 7
    assert "logo" not in si.photo_names(roles)
