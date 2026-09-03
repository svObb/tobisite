"""Сборка страницы: рецепт -> роли -> варианты, и лестница деградации (§3).

Пять ступеней: понижение внутри роли по downgrade_ladder, замена роли, выброс
optional-роли, порог min_sections, запрет на подмену. Если после лестницы
секций меньше min_sections — черновик не генерится: лид получает
needs_enrichment со списком недостающего, а не кривую страницу.

Здесь же собирается recipe_json — полный след решения, включая отвергнутые
альтернативы и их score (§5).

Как ступени лежат в коде:

1. **Понижение внутри роли.** downgrade_ladder рецепта задаёт список вариантов
   роли в порядке предпочтения. Гейт снимает те, для которых нет данных,
   скоринг выбирает лучший из оставшихся. Чтобы порядок лестницы и результат
   скоринга не спорили, рецепт обязан держать niche_affinity убывающей вдоль
   лестницы — это проверяет tests/test_recipes.py.
2. **Замена роли** — role_substitutes: {роль: [роли-заместители]}. Заместитель
   встаёт на место выбывшей роли и снимается со своей позиции, если она была
   дальше в roles_order (одна и та же секция не выводится дважды).
3. **Выброс роли** — только если она в optional_roles.
4. **Порог.** Черновик не собирается, если секций меньше min_sections или
   выбыла обязательная роль: страница без hero или без футера с NAP хуже,
   чем честное «дозаполните лид».
5. **Запрет на подмену** живёт не здесь, а в slots.py и checks/: движку нечем
   заменить отсутствующий факт, и это правильно.

Ничьи в скоринге разрешает один random.Random(seed) на весь вызов. Порядок
розыгрышей — порядок ролей в рецепте, поэтому он воспроизводим для профиля,
но не пытайтесь предсказать его для отдельно взятой роли.

Тем же порядком ролей задан приоритет на фотографии: пул беспредметных
снимков один на страницу, и курсор по нему сквозной (engine/photos). Секция,
выбывшая позже на пустом тексте модели (apply_free_texts), свои кадры
обратно не отдаёт — это принятая цена, как и резерв товарной сетки.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from . import gates, photos, slots
from .profile import Profile
from .score import choose, score

FALLBACK_HINT = "_fallback"

# Куда ведёт вторая кнопка первого экрана: первый содержательный раздел
# страницы, а не форма. Какой из них первый, решает порядок секций на странице,
# а не порядок в этом множестве. Подпись кнопки — заголовок той же секции (см.
# link_sections), поэтому обещание кнопки и результат клика совпадают всегда.
SECONDARY_ROLES = frozenset({"products", "services", "about", "info", "cta"})

# Роль, которая идёт в контрастном тоне. Ровно одна на страницу: две таких
# секции — уже не ритм, а вторая тема внутри одной страницы. Первый экран и
# форму не берём: у первого свой фон, у формы — кнопка, заливка которой
# считается от бумаги пресета.
CONTRAST_ROLES = ("about", "proof", "info")


@dataclass
class Composition:
    sections: list = field(default_factory=list)
    roles: list = field(default_factory=list)
    needs_enrichment: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.needs_enrichment


def compose(profile: Profile, recipe: dict, library: dict, seed: int,
            recent_variants=()) -> Composition:
    rng = random.Random(seed)
    order = list(recipe["roles_order"])
    optional = set(recipe.get("optional_roles") or [])
    substitutes = recipe.get("role_substitutes") or {}

    sections: list[dict] = []
    trace: list[dict] = []
    taken: set[str] = set()
    # Курсор пула: кадры, разобранные секциями выше по странице. Роли идут в
    # порядке roles_order, поэтому приоритет на снимки задаёт он же — галерея
    # выбирает раньше блока о компании, и один кадр дважды на странице не
    # встречается по построению.
    taken_photos: set[str] = set()

    for position in order:
        if position in taken:
            trace.append({"role": position, "status": "used_earlier",
                          "chosen": None, "candidates": [], "rejected": []})
            continue

        section, record = _fill(position, profile, recipe, library, rng,
                                recent_variants, taken_photos)
        if section is None:
            for substitute in substitutes.get(position, []):
                if substitute in taken:
                    continue
                section, sub_record = _fill(substitute, profile, recipe, library,
                                            rng, recent_variants, taken_photos)
                record.setdefault("substitutes_tried", []).append(sub_record)
                if section is not None:
                    record["substituted_by"] = substitute
                    taken.add(substitute)
                    break

        if section is None:
            record["status"] = "dropped" if position in optional else "missing"
        else:
            record["status"] = "substituted" if record.get("substituted_by") else "filled"
            sections.append(section)
            taken_photos |= photos.claimed(section)
        taken.add(position)
        trace.append(record)

    unfilled = [r for r in trace if r["status"] in ("missing", "dropped")]
    short = len(sections) < recipe["min_sections"]
    blocking = [r for r in trace if r["status"] == "missing"]
    if blocking or short:
        return Composition([], trace, _enrichment(recipe, blocking or unfilled))

    link_sections(sections)
    return Composition(sections, trace, [])


def apply_free_texts(composition: Composition, texts: dict) -> list[str]:
    """Слоты, написанные моделью, поверх заготовок. Возвращает выбывшие роли.

    Модель никогда не видит HTML: сюда приходит готовый JSON слотов, и дальше
    работают те же правила, что и для фактов. Секция, у которой обязательный
    слот остался пустым, выбывает — судьбу страницы решает enough().
    """
    survivors, dropped = [], []
    for section in composition.sections:
        if slots.apply_free_texts(section, texts):
            survivors.append(section)
        else:
            dropped.append(section["role"])
    composition.sections = survivors
    return dropped


def enough(composition: Composition, recipe: dict, dropped: list[str]) -> bool:
    """Страница ещё собирается: обязательные роли на месте, секций хватает."""
    optional = set(recipe.get("optional_roles") or [])
    return (not set(dropped) - optional
            and len(composition.sections) >= recipe["min_sections"])


def _fill(role: str, profile: Profile, recipe: dict, library: dict,
          rng: random.Random, recent_variants, taken_photos=()):
    """Одна роль: гейты -> слоты -> скоринг. None, если годных вариантов нет.

    taken_photos — курсор пула на момент этой роли: и гейт, и слоты видят
    только те кадры, которые секции выше по странице ещё не забрали.
    """
    record = {"role": role, "chosen": None, "candidates": [], "rejected": []}
    admitted = []

    for variant in (recipe.get("downgrade_ladder") or {}).get(role, []):
        contract = library[variant]
        if contract["role"] != role:
            raise ValueError(f"{recipe['id']}: вариант {variant!r} не роли {role!r}")
        verdict = gates.check(contract, profile, taken_photos)
        if not verdict.ok:
            record["rejected"].append(_rejection(variant, "gate", verdict.reasons))
            continue
        filled = slots.build(contract, profile, recipe, taken_photos)
        if not filled.ok:
            record["rejected"].append(_rejection(variant, "slots", filled.reasons))
            continue
        admitted.append((contract, filled))

    if not admitted:
        return None, record

    scores = [score(contract, profile, recipe, recent_variants)
              for contract, _ in admitted]
    record["candidates"] = [s.as_dict() for s in scores]
    winner = choose(scores, rng)
    record["chosen"] = winner.variant
    record["score"] = winner.as_dict()

    contract, filled = next(pair for pair in admitted
                            if pair[0]["id"] == winner.variant)
    section = {
        "id": contract["role"],
        "role": contract["role"],
        "variant": contract["id"],
        "template": contract["template"],
        "slots": filled.slots,
        "images": filled.images,
        "contract": contract,
    }
    return section, record


def link_sections(sections: list[dict]) -> None:
    """Связи между секциями: якорь и подпись второй кнопки, контрастный тон.

    Считается по составу страницы, а не по рецепту, поэтому вызывается ещё раз
    после того, как тексты модели вывели часть секций из состава: и якорь, и
    тон обязаны указывать на секцию, которая на странице осталась. Якорь ищется
    в порядке самих секций — «первый раздел» это первый сверху, а не первый в
    списке ролей.

    Подпись второй кнопки — заголовок секции, к которой она ведёт. Своей
    заготовки у неё нет и модель её не пишет: кнопка, обещающая одно и
    прокручивающая к другому, — это баг, а не текст.
    """
    if not sections:
        return
    target = next((section for section in sections
                   if section["role"] in SECONDARY_ROLES
                   and section["slots"].get("section_title")), None)
    values = {
        "secondary_target": target["id"] if target else sections[-1]["id"],
        "secondary_label": target["slots"]["section_title"] if target else None,
    }

    roles = {section["role"] for section in sections}
    tone = next((name for name in CONTRAST_ROLES if name in roles), None)
    for section in sections:
        section["tone"] = "contrast" if section["role"] == tone else None
        slots.apply_composer(section, values)


def _rejection(variant: str, stage: str, reasons) -> dict:
    return {"variant": variant, "stage": stage,
            "reasons": [r.as_dict() for r in reasons]}


def _enrichment(recipe: dict, records: list[dict]) -> list[str]:
    """Что именно просить у работника: подсказки по полям, которые всё сломали."""
    hints = recipe.get("enrichment_hints") or {}
    needed: list[str] = []
    for record in records:
        for rejection in record["rejected"] + _substitute_rejections(record):
            for reason in rejection["reasons"]:
                field = gates.HINT_FIELDS.get(reason["field"], reason["field"])
                hint = hints.get(field)
                if hint and hint not in needed:
                    needed.append(hint)
    if not needed:
        fallback = hints.get(FALLBACK_HINT)
        if fallback:
            needed.append(fallback)
    return needed


def _substitute_rejections(record: dict) -> list[dict]:
    return [rejection
            for attempt in record.get("substitutes_tried", [])
            for rejection in attempt["rejected"]]
