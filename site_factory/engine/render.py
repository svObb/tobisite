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
    пресет        = int(sha256(domain_norm), 16) % len(presets)
    трек          = rich, если photo_count >= 3 и он известен, иначе light
    рецепт        = <ниша>_<трек>, ниша вне покрытия -> generic

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

from . import slots
from .compose import apply_free_texts, compose, enough
from .profile import Profile

ROOT = pathlib.Path(__file__).resolve().parent.parent
ASSETS_BASE = "/assets"          # общий префикс превью; @font-face в бандле
ENGINE_VERSION = 1

SECTION_ROLES = ("hero", "services", "proof", "cta", "footer")


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
    preset = resolve_preset(preset_for(profile.domain_norm, tokens), tokens)
    composition = compose(profile, recipe, library, seed, recent_variants)

    trace = {
        "schema": 1,
        "domain_norm": profile.domain_norm,
        "seed": seed,
        "track": track_for(profile),
        "recipe": recipe["id"],
        "preset": preset["id"],
        "versions": {
            "engine": ENGINE_VERSION,
            "library": tokens["version"],
            "recipe": recipe.get("version", 1),
        },
        "profile": profile.as_trace(),
        "recent_variants": list(recent_variants),
        "roles": composition.roles,
        "sections": [s["variant"] for s in composition.sections],
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

    lang = str(profile.lang.value)
    html = environment(root).get_template("base/layout.html.j2").render(
        preset=preset,
        site=site_context(profile, recipe, lang),
        facts=facts_context(profile, recipe),
        sections=composition.sections,
    )
    return Draft(html, trace)


def seed_for(domain_norm: str) -> int:
    return int(hashlib.sha256(domain_norm.encode()).hexdigest()[:8], 16)


def preset_for(domain_norm: str, tokens: dict) -> dict:
    presets = tokens["presets"]
    index = int(hashlib.sha256(domain_norm.encode()).hexdigest(), 16) % len(presets)
    return presets[index]


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
    """Пресет из presets.yaml в форму, которую ждёт base/layout.html.j2."""
    fonts = {
        role: dict(tokens["fonts"][preset["type"][role]], family=preset["type"][role])
        for role in ("display", "body")
    }
    return dict(preset, fonts=fonts, space=tokens["density_scale"][preset["density"]])


def site_context(profile: Profile, recipe: dict, lang: str) -> dict:
    page = slots.page_defaults(recipe, lang)
    name = profile.name.value if profile.name.known else None
    city = profile.city.value if profile.city.known else None
    return {
        "lang": lang,
        "title": f"{name} — {city}" if name and city else (name or ""),
        "description": page.get("description"),
        "assets_base": ASSETS_BASE,
        "ui": {"skip_to_content": page.get("skip_to_content", "")},
    }


def facts_context(profile: Profile, recipe: dict) -> dict:
    """Белый список для schema.org. Неизвестное просто не выводится.

    openingHours сюда не попадает намеренно: часы приходят строкой на языке
    лида, а schema.org ждёт машинный формат — переводить одно в другое значит
    угадывать, чего движку нельзя.
    """
    facts = {"business_type": recipe.get("schema_type", "LocalBusiness")}
    for key, feature in (("name", profile.name),
                         ("telephone", profile.phone),
                         ("email", profile.email),
                         ("rating", profile.google_rating),
                         ("review_count", profile.review_count)):
        if feature.known and feature.value is not None:
            facts[key] = feature.value
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
    return env


@functools.lru_cache(maxsize=8)
def load_tokens(root: pathlib.Path = ROOT) -> dict:
    return _read(root / "tokens" / "presets.yaml")


@functools.lru_cache(maxsize=8)
def load_library(root: pathlib.Path = ROOT) -> dict:
    """Контракты вариантов секций: id -> контракт с путём к шаблону."""
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
