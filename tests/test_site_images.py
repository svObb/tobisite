"""Картинки с чужого сайта: отбор, пережатие, роли, санитайзинг SVG.

Байты картинок рисует сам тест: файлов-фикстур для этого не нужно, а
проверяется ровно то, что модуль делает с размерами, альфой и форматом.
"""
import io

import pytest
from PIL import Image

import site_images as si

ORANGE, BLUE = (194, 98, 26), (43, 74, 140)


def raster(width, height, color=ORANGE, mode="RGB", fmt="PNG") -> bytes:
    img = Image.new(mode, (width, height), color)
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


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
    return {"kind": "photo", "url": url, "width": width, "height": height,
            "og": kw.get("og", False), "product": kw.get("product", False)}


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
