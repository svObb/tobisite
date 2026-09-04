"""Детерминизм и след решения: тот же лид -> тот же сайт (§3)."""
import hashlib
import json
import re

from site_factory.engine import niches
from site_factory.engine.profile import Profile
from site_factory.engine.render import (BASE_SCRIPTS, PRESET_DEFAULTS,
                                        SECTION_ROLES, load_library,
                                        load_tokens, preset_for, recipe_id_for,
                                        render, resolve_preset, seed_for,
                                        track_for)

from .conftest import (BRAND_SHOP, GENERIC_LIGHT, GENERIC_RICH, LAWYER_POOR,
                       LAWYER_RICH)

# Домены-однодневки, на которых видно, что пресет меняется вместе с доменом.
DOMAINS = ["alfa.example", "beta.example", "gamma.example", "delta.example",
           "epsilon.example", "zeta.example", "eta.example", "theta.example"]

# Семь ниш бота словами карточки плюс слово вне таблицы: на них проверяется
# формула выбора пресета.
NICHES = ["Юрист", "Стоматология", "Автосервис", "Кафе/ресторан",
          "Салон красоты", "Гостиница", "Строительство", "Bakery"]

# Порядок первых восьми пресетов заморожен: от индекса в пуле зависит, какой
# дизайн уже ушёл лиду в письме. Новые пресеты дописываются только в конец.
FROZEN_PRESETS = ("corporate-trust", "editorial-warm", "clinical-light",
                  "deep-premium", "bold-trade", "warm-table",
                  "salon-monochrome", "friendly-pop")

# Ширины экрана, на которых меряется шкала кеглей: от узкого телефона до
# широкого ноутбука.
VIEWPORTS = range(320, 1681, 80)

# Корневой кегль страница не переопределяет (css/source.css), значит rem — это
# дефолтные 16 px браузера, и vw с rem сравнимы.
REM_PX = 16


def preset_ids():
    return tuple(preset["id"] for preset in load_tokens()["presets"])


def test_two_renders_are_byte_identical(buildable_profile):
    first, first_trace = render(buildable_profile)
    second, second_trace = render(buildable_profile)
    assert first == second
    assert json.dumps(first_trace, ensure_ascii=False, sort_keys=True) == \
        json.dumps(second_trace, ensure_ascii=False, sort_keys=True)


def test_seed_comes_from_domain(lawyer_rich):
    _, trace = render(lawyer_rich)
    digest = hashlib.sha256(lawyer_rich.domain_norm.encode()).hexdigest()
    assert trace["seed"] == seed_for(lawyer_rich.domain_norm)
    assert trace["seed"] == int(digest[:8], 16)


def test_preset_comes_from_the_pool_of_the_niche():
    """Ниша решает, каким сайт вообще может быть, домен — каким он будет."""
    ids = preset_ids()
    for niche in NICHES:
        pool = niches.pool_for(_key(niche), ids)
        for domain in DOMAINS:
            profile = Profile.from_dict(
                {"domain_norm": domain, "niche": niche, "lang": "uk"})
            digest = int(hashlib.sha256(domain.encode()).hexdigest(), 16)
            assert preset_for(profile)["id"] == pool[digest % len(pool)]


def test_library_is_version_five_with_sixteen_presets():
    """Первые восемь пресетов заморожены — слот-ключи лидов считают их по месту.

    Версия библиотеки — пятая: шкала кеглей, тональный ритм и разметка секций
    сменились разом, и черновики, собранные на четвёртой, обязаны стать stale
    (draft_service._stale_library), а не публиковаться вперемешку с новыми.
    """
    ids = preset_ids()
    assert load_tokens()["version"] == 5
    assert len(ids) == 16
    assert ids[:len(FROZEN_PRESETS)] == FROZEN_PRESETS
    assert len(set(ids)) == len(ids)


def test_the_type_scale_keeps_a_step_between_the_headings():
    """Шаг шкалы не меньше 1.25×: h1 и h2 одного кегля — это документ, а не сайт.

    Меряется на всей ширине экрана, а не по концам clamp'а: из своих минимумов
    h1 и h2 выходят на разных ширинах и в максимумы упираются на разных, так
    что сойтись они могут посреди диапазона, где оба конца ещё в порядке.
    """
    for name, scale in load_tokens()["type_scale"].items():
        for width in VIEWPORTS:
            h1 = _font_size(scale["h1"], width)
            h2 = _font_size(scale["h2"], width)
            assert h1 / h2 >= 1.25, f"{name}: шаг шкалы на {width}px"
            assert h2 > _font_size(scale["lede"], width), \
                f"{name}: h2 не крупнее лида на {width}px"


def test_every_preset_is_reachable():
    """Пресет, до которого не доходит ни одна ниша, — мёртвый вес библиотеки.

    Считается по самим пулам: фолбэк незнакомой ниши отдаёт полный список
    и покрыл бы любой пресет, потому в union он не участвует.
    """
    ids = preset_ids()
    pooled = {pid for pool in niches.pools(ids).values() for pid in pool}
    assert pooled == set(ids)


def test_unknown_niche_gets_the_whole_library():
    """Слово вне таблицы не сужает выбор — лид получает все пресеты."""
    ids = preset_ids()
    assert niches.pool_for(_key("Bakery"), ids) == ids


def test_preset_index_is_sha256_not_builtin_hash():
    """Встроенный hash() солится на запуск — тот же лид получил бы другой дизайн."""
    pool = niches.pool_for("cafe", preset_ids())
    assert len(pool) > 1, "проверять формулу на пуле из одного пресета нечем"
    for domain in DOMAINS:
        digest = int(hashlib.sha256(domain.encode()).hexdigest(), 16)
        profile = Profile.from_dict(
            {"domain_norm": domain, "niche": "Кафе/ресторан", "lang": "uk"})
        assert preset_for(profile)["id"] == pool[digest % len(pool)]


def test_domain_changes_preset_and_page():
    """Домен решает пресет и ничьи в скоринге.

    На одних и тех же данных варианты секций совпадают (их выбирает score, а не
    домен), поэтому страниц ровно столько, сколько выпало разных пресетов.
    Полного покрытия здесь не требуется: пул ниши уже, чем список пресетов, и
    покрытие проверяет юнит preset_for выше.
    """
    pages, presets, seeds = set(), set(), set()
    for domain in DOMAINS:
        html, trace = render(Profile.from_dict(dict(GENERIC_RICH,
                                                    domain_norm=domain)))
        pages.add(html)
        presets.add(trace["preset"])
        seeds.add(trace["seed"])
    assert len(presets) > 1
    assert len(pages) == len(presets)
    assert len(seeds) == len(DOMAINS)


def test_track_and_recipe(lawyer_rich, lawyer_light, generic_light, lawyer_poor):
    assert track_for(lawyer_rich) == "rich"
    assert recipe_id_for(lawyer_rich) == "lawyer_rich"
    assert track_for(lawyer_light) == "light"
    assert recipe_id_for(lawyer_light) == "lawyer_light"
    assert recipe_id_for(generic_light) == "generic_light"
    # photo_count не спрашивали — это не «нет фото», но трек всё равно light.
    assert not lawyer_poor.photo_count.known
    assert track_for(lawyer_poor) == "light"


def test_trace_keeps_rejected_candidates_and_versions(lawyer_rich):
    _, trace = render(lawyer_rich)
    hero = next(role for role in trace["roles"] if role["role"] == "hero")
    losers = [c["variant"] for c in hero["candidates"] if c["variant"] != hero["chosen"]]
    assert losers, "в следе нет отвергнутых альтернатив со score"
    assert hero["chosen"] in [c["variant"] for c in hero["candidates"]]
    assert trace["versions"] == {"engine": 2, "library": load_tokens()["version"],
                                 "recipe": 3}
    assert trace["profile"]["photo_count"] == {"value": 6, "known": True}
    assert trace["profile"]["brand_colors"] == {"value": None, "known": False}


def test_trace_says_where_the_palette_came_from(lawyer_rich, brand_shop, brand_ugly):
    _, plain = render(lawyer_rich)
    assert plain["palette"] == {"source": "preset", "reason": "no_brand_color"}
    _, branded = render(brand_shop)
    assert branded["palette"] == {"source": "brand", "reason": ""}
    _, grey = render(brand_ugly)
    assert grey["palette"] == {"source": "preset", "reason": "low_chroma"}


def test_brand_colors_reach_the_page(brand_shop):
    html, _ = render(brand_shop)
    assert brand_shop.brand_colors.value["accent"] in html


def test_trace_is_json_serialisable(any_profile):
    _, trace = render(any_profile)
    assert json.loads(json.dumps(trace, ensure_ascii=False))["seed"] == trace["seed"]


def test_needs_enrichment_has_no_html(lawyer_poor):
    html, trace = render(lawyer_poor)
    assert html is None
    assert trace["needs_enrichment"]
    assert trace["sections"] == []


def test_language_switches_texts(generic_light):
    html, _ = render(generic_light)
    assert html is not None
    assert "Skip to content" in html
    assert "Перейти" not in html


def test_every_page_gets_the_base_scripts(buildable_profile):
    """Плавный скролл и появление секций — на каждом черновике.

    Заодно и граница CSP: скрипты только свои и только файлами, единственный
    инлайн на странице — JSON-LD, а это данные, а не код.
    """
    html, _ = render(buildable_profile)
    assert html is not None
    tags = re.findall(r"<script[^>]*>", html)
    loaded = [tag for tag in tags if "src=" in tag]
    assert loaded[:len(BASE_SCRIPTS)] == \
        [f'<script defer src="/assets/{name}.js">' for name in BASE_SCRIPTS]
    assert all(re.fullmatch(r'<script defer src="/assets/[a-z]+\.js">', tag)
               for tag in loaded)
    assert all("application/ld+json" in tag for tag in tags if "src=" not in tag)


def test_parallax_script_comes_only_with_the_section_that_asks(lawyer_light,
                                                               lawyer_rich,
                                                               brand_shop):
    """Скрипт секции просит сама секция — флагом js в контракте.

    Просят его оба первых экрана со снимком: и фон (hero_bg_photo), и кадр
    слева (hero_photo_left). Страница без единой картинки не просит ничего.
    """
    plain, plain_trace = render(lawyer_light)
    assert plain is not None
    assert not [v for v in plain_trace["sections"] if v.startswith("hero_")
                and v != "hero_type_only"]
    assert "/assets/parallax.js" not in plain

    photo, photo_trace = render(brand_shop)
    assert photo is not None
    assert "hero_bg_photo" in photo_trace["sections"]
    assert '<script defer src="/assets/parallax.js"></script>' in photo

    framed, framed_trace = render(lawyer_rich)
    assert "hero_photo_left" in framed_trace["sections"]
    assert '<script defer src="/assets/parallax.js"></script>' in framed


def test_the_background_photo_is_preloaded_before_the_stylesheet(brand_shop):
    """Фон первого экрана — LCP страницы: запрос за ним обязан уйти до CSS."""
    html, _ = render(brand_shop)
    preload = re.search(r'<link rel="preload" as="image"[^>]*>', html)
    assert preload, "фонового фото нет в preload"
    assert 'href="/img/hero_bg.webp"' in preload.group(0)
    assert 'fetchpriority="high"' in preload.group(0)
    assert preload.start() < html.index('<link rel="stylesheet"')


def test_a_page_without_a_background_photo_preloads_no_image(lawyer_rich):
    """Ключ preload_images необязателен: у секции без него ничего не меняется."""
    html, trace = render(lawyer_rich)
    assert "hero_bg_photo" not in trace["sections"]
    assert 'rel="preload" as="image"' not in html


def test_derived_features_read_the_new_enrichment(brand_shop):
    """Производные, на которых стоят полоса галереи и proof: имена и рейтинг."""
    photos = Profile.from_dict(dict(
        BRAND_SHOP,
        images={f"photo-{n}": {"src": f"/img/photo-{n}.webp", "width": 1200,
                               "height": 900} for n in range(2, 7)},
        products=[{"name": f"Товар {n}",
                   "image": {"src": f"/img/photo-{n}.webp", "width": 800,
                             "height": 800}} for n in (3, 5, 6)]))
    assert photos.feature("product_image_names").value == \
        frozenset({"photo-3", "photo-5", "photo-6"})
    assert photos.feature("nonproduct_photo_count").value == 2
    assert photos.free_photos() == ["photo-2", "photo-4"]
    # товары не спрашивали — снимки свободны все: товарной секции всё равно нет
    assert brand_shop.feature("product_image_names").known
    assert not Profile.from_dict(LAWYER_RICH).feature("product_image_names").known


def test_recent_variants_lower_the_score(lawyer_rich):
    _, plain = render(lawyer_rich)
    _, penalised = render(lawyer_rich, recent_variants=("hero_photo_left",))
    hero_before = _score_of(plain, "hero", "hero_photo_left")
    hero_after = _score_of(penalised, "hero", "hero_photo_left")
    assert hero_after["novelty"] == 0.0
    assert round(hero_before["total"] - hero_after["total"], 6) == 2.0


def test_unknown_profile_field_stays_unknown():
    profile = Profile.from_dict(dict(LAWYER_POOR))
    assert profile.feature("has_phone") == profile.feature("has_address")
    assert not profile.feature("has_phone").known
    # спрошено и пусто — это уже известно и это false
    empty = Profile.from_dict(dict(GENERIC_LIGHT))
    assert empty.feature("service_count").known
    assert empty.feature("service_count").value == 0


def test_lawyer_fixture_keeps_its_niche():
    assert Profile.from_dict(LAWYER_RICH).niche_key == "lawyer"


def test_preset_without_diversity_tokens_still_resolves():
    """Токены разнообразия необязательны: пресет без них — валидный пресет."""
    tokens = load_tokens()
    bare = {key: value for key, value in tokens["presets"][0].items()
            if key not in PRESET_DEFAULTS}
    resolved = resolve_preset(bare, tokens)
    assert {key: resolved[key] for key in PRESET_DEFAULTS} == PRESET_DEFAULTS
    assert resolved["scale"] == tokens["type_scale"]["regular"]
    assert resolved["shadow"] == tokens["elevation_scale"]["flat"]


def test_diversity_tokens_reach_the_page(any_profile):
    html, trace = render(any_profile)
    if html is None:
        return
    preset = next(p for p in load_tokens()["presets"] if p["id"] == trace["preset"])
    assert f'data-btn="{preset.get("button", "fill")}"' in html
    assert f'data-align="{preset.get("align", "left")}"' in html
    assert f'data-elev="{preset.get("elevation", "flat")}"' in html
    assert "--h1:clamp" in html


def test_library_survives_roles_without_variants():
    """Роль без папки — роль, для которой ещё не написано ни одного варианта."""
    library = load_library()
    assert {contract["role"] for contract in library.values()} <= set(SECTION_ROLES)
    assert "footer_nap" in library


def _clamp(value: str) -> tuple[float, float, float]:
    """«clamp(2.4rem, 6.5vw, 5.25rem)» -> (2.4, 6.5, 5.25)."""
    inside = value[value.index("(") + 1:value.rindex(")")]
    return tuple(float(re.sub(r"[a-z]+$", "", part.strip()))
                 for part in inside.split(","))


def _font_size(value: str, viewport: int) -> float:
    """Кегль clamp'а в пикселях на экране такой ширины."""
    low, vw, high = _clamp(value)
    return min(max(low * REM_PX, vw * viewport / 100), high * REM_PX)


def _key(niche: str) -> str:
    key = niches.key_for(niche)
    assert key is not None
    return key


def _score_of(trace, role, variant):
    record = next(r for r in trace["roles"] if r["role"] == role)
    return next(c for c in record["candidates"] if c["variant"] == variant)
