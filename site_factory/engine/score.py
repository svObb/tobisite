"""Скоринг вариантов внутри роли (§3 шаг 4).

    score = 3*data_fit + 2*niche_affinity + 2*novelty - 1*perf_cost

novelty штрафует варианты из последних 30 черновиков той же ниши и того же
города. Ничьи разрешает random.Random(seed) — модель не выбирает никогда.

Слагаемые считаются так:

* **data_fit** — доля выполненных условий `prefers` контракта, 0..1. Признак,
  которого мы не знаем, условие не выполняет (то же правило, что в гейтах), и
  считается он тем же gates.feature_for: вариант с image_pool видит пул по
  остатку страницы и в гейте, и здесь. У варианта без `prefers` data_fit = 0:
  подходящесть надо заявить, она не выдаётся по умолчанию.
* **niche_affinity** — вес варианта в рецепте, 0..1. Порядок downgrade_ladder
  и убывание этих весов рецепт обязан держать согласованными: гейт снимает
  верхнюю ступень, вес поднимает следующую.
* **novelty** — 1, если варианта нет в recent_variants, иначе 0. Список
  последних черновиков ниши/города придёт из БД на этапе 6; в MVP это параметр
  со значением по умолчанию.
* **perf_cost** — вес секции: est_dom_nodes/100 плюс 0,5 за картинку. Числа
  из контракта, шкала подобрана так, чтобы самая тяжёлая секция стоила около
  одного балла и не могла перебить data_fit.

Ничья считается по округлённому до 6 знаков total: разница в 1e-15 от
порядка сложения float — не преимущество варианта, а шум.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from .gates import feature_for, satisfies
from .profile import Profile

WEIGHT_DATA_FIT = 3
WEIGHT_NICHE_AFFINITY = 2
WEIGHT_NOVELTY = 2
WEIGHT_PERF_COST = -1

TIE_PRECISION = 6


@dataclass(frozen=True)
class Score:
    variant: str
    total: float
    data_fit: float
    niche_affinity: float
    novelty: float
    perf_cost: float

    def as_dict(self) -> dict:
        return {
            "variant": self.variant,
            "total": self.total,
            "data_fit": self.data_fit,
            "niche_affinity": self.niche_affinity,
            "novelty": self.novelty,
            "perf_cost": self.perf_cost,
        }


def score(contract: dict, profile: Profile, recipe: dict,
          recent_variants=(), taken=()) -> Score:
    variant = contract["id"]
    data_fit = _data_fit(contract, profile, taken)
    affinity = float((recipe.get("niche_affinity") or {}).get(variant, 0.0))
    novelty = 0.0 if variant in recent_variants else 1.0
    perf_cost = _perf_cost(contract)
    total = (WEIGHT_DATA_FIT * data_fit
             + WEIGHT_NICHE_AFFINITY * affinity
             + WEIGHT_NOVELTY * novelty
             + WEIGHT_PERF_COST * perf_cost)
    return Score(variant, round(total, TIE_PRECISION), round(data_fit, 4),
                 affinity, novelty, round(perf_cost, 4))


def choose(scores, rng: random.Random) -> Score:
    """Победитель роли. Ничью разрешает rng, и только он."""
    ordered = sorted(scores, key=lambda s: (-s.total, s.variant))
    best = ordered[0].total
    tied = [s for s in ordered if s.total == best]
    return tied[0] if len(tied) == 1 else rng.choice(tied)


def _data_fit(contract: dict, profile: Profile, taken=()) -> float:
    prefers = contract.get("prefers") or {}
    if not prefers:
        return 0.0
    hits = 0
    for name, condition in prefers.items():
        feature = feature_for(name, contract, profile, taken)
        if feature.known and satisfies(condition, feature):
            hits += 1
    return hits / len(prefers)


def _perf_cost(contract: dict) -> float:
    perf = contract.get("perf") or {}
    return perf.get("est_dom_nodes", 0) / 100 + 0.5 * perf.get("images", 0)
