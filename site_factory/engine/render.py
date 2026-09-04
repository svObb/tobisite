"""Рендер Jinja2: композиция + слоты -> один self-contained index.html.

Что делает этот модуль:

* Резолвит пресет из tokens/presets.yaml в форму, которую ждёт
  base/layout.html.j2: семейства шрифтов -> stack и gwfh_id из реестра fonts,
  density -> density_scale. Раньше эта логика жила в tests/smoke_render.py —
  теперь она только здесь, а смоук импортирует её отсюда.
* Держит StrictUndefined: пропущенный слот обязан валить сборку, а не
  вытекать пустой строкой на страницу клиента.
* Выкидывает из globals окружения lipsum — генератор lorem ipsum в
  шаблонизаторе соседствует с запретом на lorem в checks/placeholders.py.
* Не запускает ни одного подпроцесса: bundle.css собран заранее, рендер
  черновика это чистая строка (§2).

Детерминизм (§3):

    seed          = int(sha256(domain_norm)[:8], 16)
    пул           = пресеты ниши из tokens/niches.yaml (нет ниши -> все)
    пресет        = пул[int(sha256(domain_norm), 16) % len(пула)]
    трек          = rich, если photo_count >= 3 и он известен, иначе light
    рецепт        = <ниша>_<трек>, ниша вне покрытия -> generic

Ниша решает, каким сайт вообще может быть, домен — каким он будет.

Встроенный hash() здесь использовать нельзя ни в одном из двух мест: он
солится на каждый запуск процесса, и тот же лид получил бы другой дизайн при
следующей перегенерации.

Возврат — Draft(html, recipe_json). Когда данных не хватило, html = None,
а recipe_json["needs_enrichment"] содержит конкретный список для работника.
"""
from __future__ import annotations

import functools
import hashlib
import pathlib
from typing import NamedTuple

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from . import niches, slots
from .color import PIVOT_LUMINANCE, luminance, srgb
from .compose import apply_free_texts, compose, enough, link_sections
from .naming import split_product_name
from .palette import brand_palette, contrast_tones
from .profile import Profile

ROOT = pathlib.Path(__file__).resolve().parent.parent
ASSETS_BASE = "/assets"          # общий префикс превью; @font-face в бандле
ENGINE_VERSION = 3
OVERLAY = "overlay"              # ключ header контракта: шапка ложится на секцию

SECTION_ROLES = ("header", "hero", "products", "services", "gallery", "proof",
                 "about", "info", "cta", "footer")

# Скрипты каждой страницы превью, в порядке подключения: сначала вендорный
# Lenis, потом наш preview.js — он рассчитывает, что библиотека уже
# определена, и defer этот порядок сохраняет. Файлы лежат в site_factory/js
# и копируются в build/ шагом tools/build_css.py.
BASE_SCRIPTS = ("lenis", "preview")

# Токены разнообразия пресета: у пресета они необязательны, а в шаблоне обязаны
# быть всегда. Значения по умолчанию — то, как выглядели первые четыре пресета
# до того, как эти токены появились.
PRESET_DEFAULTS = {"type_scale": "regular", "button": "fill",
                   "elevation": "flat", "align": "left"}


class Draft(NamedTuple):
    html: str | None
    recipe_json: dict


def render(profile: Profile, recent_variants=(), root: pathlib.Path = ROOT,
           free_texts: dict | None = None) -> Draft:
    """Черновик лида. free_texts — слоты, написанные моделью (JSON, не HTML).

    Без free_texts free-слоты закрывают заготовки рецепта: так работает смоук
    библиотеки. Бот отдаёт сюда JSON слот-генерации, собранный по композиции
    этого же профиля — она детерминирована seed'ом и совпадает с здешней.
    """
    tokens = load_tokens(root)
    library = load_library(root)
    recipe = load_recipe(recipe_id_for(profile, root), root)

    seed = seed_for(profile.domain_norm)
    chosen = preset_for(profile, tokens, root)
    palette, palette_source, palette_reason = brand_palette(_brand_colors(profile),
                                                            chosen["palette"])
    preset = dict(resolve_preset(chosen, tokens), palette=palette,
                  contrast=contrast_tones(palette))
    composition = compose(profile, recipe, library, seed, recent_variants)

    trace = {
        "schema": 1,
        "domain_norm": profile.domain_norm,
        "seed": seed,
        "track": track_for(profile),
        "recipe": recipe["id"],
        "preset": preset["id"],
        "palette": {"source": palette_source, "reason": palette_reason},
        "versions": {
            "engine": ENGINE_VERSION,
            "library": tokens["version"],
            "recipe": recipe.get("version", 1),
        },
        "profile": profile.as_trace(),
        "recent_variants": list(recent_variants),
        "roles": composition.roles,
        "sections": [s["variant"] for s in composition.sections],
        "tone": _tone(composition.sections),
        "needs_enrichment": composition.needs_enrichment,
    }
    if not composition.ok:
        return Draft(None, trace)

    if free_texts is not None:
        dropped = apply_free_texts(composition, free_texts)
        trace["dropped_sections"] = dropped
        trace["sections"] = [s["variant"] for s in composition.sections]
        if not enough(composition, recipe, dropped):
            # текста нет — секции нет; страницы из двух секций не бывает
            trace["failed"] = f"секции без текста: {', '.join(dropped)}"
            return Draft(None, trace)
        # заголовки секций теперь от модели, а часть секций выбыла: якорь
        # второй кнопки и контрастный тон пересчитываются по тому, что осталось
        link_sections(composition.sections)
        trace["tone"] = _tone(composition.sections)

    lang = str(profile.lang.value)
    html = environment(root).get_template("base/layout.html.j2").render(
        preset=preset,
        site=site_context(profile, recipe, lang, composition.sections),
        facts=facts_context(profile, recipe),
        sections=composition.sections,
    )
    return Draft(html, trace)


def _tone(sections) -> str | None:
    """Роль секции, которая идёт в контрастном тоне. None — такой на странице нет."""
    return next((section["role"] for section in sections if section.get("tone")),
                None)


def seed_for(domain_norm: str) -> int:
    return int(hashlib.sha256(domain_norm.encode()).hexdigest()[:8], 16)


def preset_for(profile: Profile, tokens: dict | None = None,
               root: pathlib.Path = ROOT) -> dict:
    """Пресет лида: пул его ниши, а внутри пула — хеш домена."""
    tokens = tokens or load_tokens(root)
    by_id = {preset["id"]: preset for preset in tokens["presets"]}
    pool = _legible(niches.pool_for(profile.niche_key, tuple(by_id), root),
                    by_id, profile)
    index = int(hashlib.sha256(profile.domain_norm.encode()).hexdigest(), 16) % len(pool)
    return by_id[pool[index]]


def palette_for(profile: Profile, root: pathlib.Path = ROOT) -> dict:
    """Палитра, которая уйдёт в HTML этого лида, — с бренд-цветами, если они есть.

    Публичная, потому что проверки (checks/a11y.py) обязаны считать контраст
    ровно той палитры, которая на странице, а не той, что записана в пресете.
    """
    tokens = load_tokens(root)
    preset = preset_for(profile, tokens, root)
    return brand_palette(_brand_colors(profile), preset["palette"])[0]


def _brand_colors(profile: Profile):
    return profile.brand_colors.value if profile.brand_colors.known else None


def _legible(pool: tuple[str, ...], by_id: dict, profile: Profile) -> tuple[str, ...]:
    """Тёмные пресеты вон, когда у лида есть логотип или бренд-цвета.

    И логотип, и фирменный цвет нарисованы под белый фон: на чёрной бумаге
    логотип пропадает, а бренд-акцент приходится высветлять до неузнаваемости.
    Судим по самой бумаге, а не по mood: у arch-05 mood dual-theme, а paper
    почти чёрный. Если светлых в пуле не осталось, пул остаётся как был —
    пустого выбора не бывает.
    """
    if not (profile.feature("has_logo").value
            or profile.feature("has_brand_colors").value):
        return pool
    light = tuple(preset_id for preset_id in pool
                  if luminance(srgb(by_id[preset_id]["palette"]["paper"]))
                  >= PIVOT_LUMINANCE)
    return light or pool


def track_for(profile: Profile) -> str:
    photos = profile.photo_count
    return "rich" if photos.known and (photos.value or 0) >= 3 else "light"


def recipe_id_for(profile: Profile, root: pathlib.Path = ROOT) -> str:
    track = track_for(profile)
    niche = profile.niche_key or "generic"
    if not (root / "recipes" / f"{niche}_{track}.yaml").exists():
        niche = "generic"
    return f"{niche}_{track}"


def resolve_preset(preset: dict, tokens: dict) -> dict:
    """Пресет из presets.yaml в форму, которую ждёт base/layout.html.j2.

    Токены разнообразия (type_scale, button, elevation, align) у пресета
    необязательны: пропущенный берётся из PRESET_DEFAULTS. Шаблон о такой
    необязательности не знает — до него доходят уже все четыре.
    """
    fonts = {
        role: dict(tokens["fonts"][preset["type"][role]], family=preset["type"][role])
        for role in ("display", "body")
    }
    options = {key: preset.get(key, default)
               for key, default in PRESET_DEFAULTS.items()}
    return dict(preset, **options, fonts=fonts,
                space=tokens["density_scale"][preset["density"]],
                scale=tokens["type_scale"][options["type_scale"]],
                shadow=tokens["elevation_scale"][options["elevation"]],
                # тона контрастной секции считаются от палитры пресета; у лида
                # с бренд-цветами render пересчитывает их по его палитре
                contrast=contrast_tones(preset["palette"]))


def site_context(profile: Profile, recipe: dict, lang: str, sections=()) -> dict:
    page = slots.page_defaults(recipe, lang)
    name = profile.name.value if profile.name.known else None
    city = profile.city.value if profile.city.known else None
    return {
        "lang": lang,
        "title": f"{name} — {city}" if name and city else (name or ""),
        "description": page.get("description"),
        "assets_base": ASSETS_BASE,
        "scripts": scripts_for(sections),
        "preload_images": preload_for(sections),
        "header_overlay": header_overlay(sections),
        "ui": {"skip_to_content": page.get("skip_to_content", ""),
               "nav_label": page.get("nav_label", "")},
    }


def header_overlay(sections) -> bool:
    """Ложится ли шапка на первую секцию вместо того, чтобы стоять над ней.

    Просит об этом сама секция — ключом header: overlay своего контракта, — и
    просят только те первые экраны, у которых кадр идёт во всю ширину: тогда
    фотография начинается от кромки окна, а не под полосой шапки. Роль header
    в порядке страницы идёт первой, поэтому «первая секция» здесь — первая
    после неё.
    """
    body = [section for section in sections if section["role"] != "header"]
    return bool(body) and (body[0].get("contract") or {}).get("header") == OVERLAY


def preload_for(sections) -> list[str]:
    """Картинки, за которыми браузер обязан пойти до разбора CSS.

    Фоновое фото первого экрана — LCP-элемент страницы, но <img> внутри секции
    браузер находит только после stylesheet, и запрос уходит на треть времени
    LCP позже, чем мог бы. Список собирается из тех секций, что реально попали
    на страницу: секция без ключа preload_images не кладёт в него ничего.
    """
    sources: list[str] = []
    for section in sections:
        names = (section.get("contract") or {}).get("preload_images") or []
        for name in names:
            image = (section.get("images") or {}).get(name)
            if image and image["src"] not in sources:
                sources.append(image["src"])
    return sources


def scripts_for(sections) -> list[str]:
    """Скрипты превью: общие для всех страниц плюс те, что просят сами секции.

    Общие — BASE_SCRIPTS: плавный скролл и появление секций положены каждому
    черновику. Флагом js своего контракта секция добирает только то, что
    нужно ей одной: параллакс едет туда, где есть фоновое фото, и никуда
    больше.
    """
    names: list[str] = list(BASE_SCRIPTS)
    for section in sections:
        flag = (section.get("contract") or {}).get("js")
        for name in ([flag] if isinstance(flag, str) else flag or []):
            if name not in names:
                names.append(name)
    return [f"{ASSETS_BASE}/{name}.js" for name in names]


def facts_context(profile: Profile, recipe: dict) -> dict:
    """Белый список для schema.org. Неизвестное просто не выводится.

    openingHours сюда не попадает намеренно: часы приходят строкой на языке
    лида, а schema.org ждёт машинный формат — переводить одно в другое значит
    угадывать, чего движку нельзя.
    """
    facts = {"business_type": recipe.get("schema_type", "LocalBusiness")}
    for key, feature in (("name", profile.name),
                         ("telephone", profile.phone),
                         ("email", profile.email)):
        if feature.known and feature.value is not None:
            facts[key] = feature.value
    # Оценка берётся там же, где её берёт proof-полоса: иначе на одной
    # странице стояли бы две разные оценки одного бизнеса.
    stats = {row["key"]: row["value"] for row in profile.proof_stats()}
    for key, source in (("rating", "rating"), ("review_count", "reviews")):
        if source in stats:
            facts[key] = stats[source]
    if profile.address_parts.known and profile.address_parts.value:
        facts["address"] = dict(profile.address_parts.value)
    return facts


@functools.lru_cache(maxsize=8)
def environment(root: pathlib.Path = ROOT) -> Environment:
    env = Environment(
        loader=FileSystemLoader(root),
        autoescape=True,
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    env.globals.pop("lipsum", None)  # генератор lorem ipsum рядом с запретом на lorem
    # Разрез названия товара — чистая функция, и шаблон зовёт именно её, а не
    # свою цепочку фильтров: правило разреза одно на всю библиотеку.
    env.globals["split_product_name"] = split_product_name
    return env


@functools.lru_cache(maxsize=8)
def load_tokens(root: pathlib.Path = ROOT) -> dict:
    return _read(root / "tokens" / "presets.yaml")


@functools.lru_cache(maxsize=8)
def load_library(root: pathlib.Path = ROOT) -> dict:
    """Контракты вариантов секций: id -> контракт с путём к шаблону.

    Роль без папки — не ошибка, а роль, для которой ещё не написано ни одного
    варианта: glob по несуществующему каталогу просто пуст. Рецепт такую роль
    в лестницу не положит, и до библиотеки дело не дойдёт.
    """
    library = {}
    for role in SECTION_ROLES:
        for path in sorted((root / "sections" / role).glob("*.yaml")):
            contract = _read(path)
            template = path.parent / f"{path.stem}.html.j2"
            contract["template"] = template.relative_to(root).as_posix()
            if contract["id"] != path.stem or contract["role"] != role:
                raise ValueError(f"{path}: id/role контракта не совпадают с файлом")
            library[contract["id"]] = contract
    return library


@functools.lru_cache(maxsize=16)
def load_recipe(recipe_id: str, root: pathlib.Path = ROOT) -> dict:
    recipe = _read(root / "recipes" / f"{recipe_id}.yaml")
    if recipe["id"] != recipe_id:
        raise ValueError(f"{recipe_id}: id внутри рецепта не совпадает с именем файла")
    return recipe


def _read(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))
