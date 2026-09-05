"""Жёсткие гейты: отсев вариантов по requires против профиля (§3 шаг 3).

Это отсев, а не штраф: вариант либо годен, либо нет. Штрафы живут в score.py.

Два правила, из-за которых модуль выглядит строже, чем кажется нужным:

* **unknown не проходит гейт.** Признак, которого мы не знаем, не может
  подтвердить requires. Иначе первый же лид без заполненного enrichment
  получит секцию, для которой у нас нет данных, — а именно от этого
  предостерегает §3 шаг 1 (unknown != false).
* **Картинки — тоже requires.** Вариант с image_names требует, чтобы каждая
  картинка лежала в белом списке профиля; вариант с image_pool — чтобы
  свободных снимков хватило на его порог (engine/photos). Секции с пустой
  рамкой вместо фото не бывает: это ступень 5 лестницы, запрет на подмену.
* **Пул считается по остатку.** taken — имена кадров, которые уже разобрали
  секции выше по странице. Для варианта с image_pool признак
  nonproduct_photo_count это остаток пула, а не весь пул: три снимка, из
  которых два забрала галерея, — это один снимок, и вариант, которому нужно
  два, обязан выбыть здесь, а не встать на страницу с чужими кадрами. Остаток
  считается глазами самого варианта (photos.offered): галерея дорисованных
  кадров сверх добора не видит, и считать их ей здесь тоже нечего.

Язык условий (он же используется в score.py для prefers):

    true / false        значение приводится к bool и сравнивается
    ">=3", "<=2", ">1"  число или место в перечислении (ORDERED_ENUMS)
    "3..", "..5", "2..6"  диапазон, границы включительно
    [a, b]              членство в списке
    "ok"                равенство

Причина отказа пишется в след (recipe_json) полем field: по нему compose.py
собирает конкретный список для needs_enrichment.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import photos
from .profile import ORDERED_ENUMS, Feature, Profile, known

# Виды отказов. Первые два ставит этот модуль, остальные — slots.py: список
# общий, потому что для лестницы деградации все они значат одно — вариант выбыл.
UNKNOWN_FIELD = "unknown"
MISMATCH = "mismatch"
MISSING_IMAGE = "missing_image"
FACT_MISSING = "fact_missing"
TOO_LONG = "too_long"
NO_DEFAULT = "no_default"
TOO_FEW = "too_few"


# Признак, который вариант с image_pool считает по остатку пула, а не по всему
# белому списку профиля (см. _feature).
POOL_COUNT = "nonproduct_photo_count"

# Признак контракта -> поле профиля, которое дозаполняет работник. Контракт
# спрашивает has_address, а бот просит «адрес»: таблица переводит одно в другое
# для needs_enrichment (compose.py).
HINT_FIELDS = {
    "has_phone": "phone",
    "has_address": "address",
    "has_hours": "hours",
    "has_logo": "images",
    "service_count": "services",
    "product_count": "products",
    "products_with_images": "products",
    "nonproduct_photo_count": "photo_count",
    "proof_stats_count": "proof_stats",
}


@dataclass(frozen=True)
class Reason:
    """Почему вариант выбыл. field — что дозаполнить, чтобы он вернулся."""

    field: str
    kind: str
    detail: str

    def as_dict(self) -> dict:
        return {"field": self.field, "kind": self.kind, "detail": self.detail}


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reasons: tuple[Reason, ...] = ()


def check(contract: dict, profile: Profile, taken=()) -> Verdict:
    """Пропустить вариант или отсеять его вместе с причинами.

    taken — кадры пула, разобранные секциями выше по странице.
    """
    reasons: list[Reason] = []
    for name, condition in (contract.get("requires") or {}).items():
        feature = feature_for(name, contract, profile, taken)
        if not feature.known:
            reasons.append(Reason(name, UNKNOWN_FIELD,
                                  f"{name} неизвестен, требуется {condition!r}"))
        elif not satisfies(condition, feature):
            reasons.append(Reason(name, MISMATCH,
                                  f"{name}={feature.value!r}, требуется {condition!r}"))
    reasons.extend(_image_reasons(contract, profile, taken))
    return Verdict(not reasons, tuple(reasons))


def feature_for(name: str, contract: dict, profile: Profile, taken=()) -> Feature:
    """Признак профиля глазами варианта: пул он видит по остатку страницы.

    Публичная, потому что тем же признаком считает score.py: у гейта и у
    скоринга одно и то же поле обязано значить одно и то же (profile.py).
    """
    if name != POOL_COUNT or not photos.uses_pool(contract):
        return profile.feature(name)
    if not profile.images.known:
        return profile.feature(name)
    return known(len(photos.offered(contract, profile, taken)))


def _image_reasons(contract: dict, profile: Profile, taken) -> list[Reason]:
    if contract.get("image_pool"):
        return _pool_reasons(contract, profile, taken)
    names = contract.get("image_names") or []
    if not names:
        return []
    available = profile.images.value if profile.images.known else {}
    return [Reason("images", MISSING_IMAGE, f"нет картинки {name!r} в профиле")
            for name in names if name not in (available or {})]


def _pool_reasons(contract: dict, profile: Profile, taken) -> list[Reason]:
    """Секция берёт картинки пулом: важно их число, а не имена."""
    if not profile.images.known:
        return [Reason("images", UNKNOWN_FIELD, "картинки лида неизвестны")]
    needed = photos.floor(contract)
    free = photos.available(contract, profile, taken)
    if len(free) >= needed:
        return []
    least = contract.get("pool_min_width")
    width = f" шириной от {least}px" if least else ""
    return [Reason("images", MISSING_IMAGE,
                   f"свободных фотографий{width} {len(free)}, нужно {needed}")]


def satisfies(condition, feature: Feature) -> bool:
    """Условие contracts-языка против известного признака."""
    if isinstance(condition, bool):
        return bool(feature.value) is condition
    if isinstance(condition, (list, tuple)):
        return feature.value in condition
    if isinstance(condition, (int, float)):
        return feature.value == condition
    text = str(condition).strip()
    if ".." in text:
        low, _, high = text.partition("..")
        return ((not low or _at_least(feature.value, low))
                and (not high or _at_most(feature.value, high)))
    for prefix, test in ((">=", _at_least), ("<=", _at_most),
                         (">", _greater), ("<", _less)):
        if text.startswith(prefix):
            return test(feature.value, text[len(prefix):].strip())
    return feature.value == condition


def _ranks(value, operand: str) -> tuple[float, float]:
    """(значение, порог) в сравнимых числах: сами числа или места в перечислении."""
    try:
        return float(value), float(operand)
    except (TypeError, ValueError):
        pass
    for order in ORDERED_ENUMS:
        if operand in order and value in order:
            return float(order.index(value)), float(order.index(operand))
    raise ValueError(f"нечем сравнить {value!r} с {operand!r}")


def _at_least(value, operand):
    left, right = _ranks(value, operand)
    return left >= right


def _at_most(value, operand):
    left, right = _ranks(value, operand)
    return left <= right


def _greater(value, operand):
    left, right = _ranks(value, operand)
    return left > right


def _less(value, operand):
    left, right = _ranks(value, operand)
    return left < right
