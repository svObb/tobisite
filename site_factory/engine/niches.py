"""Таблицы ниш: слово работника -> ключ, ключ -> пул пресетов и тип schema.org.

Все три таблицы — данные (tokens/niches.yaml), а не код: ниш скоро станет
полсотни, и каждая новая не должна быть коммитом в движок.

    key_for           "Кафе/ресторан" -> "cafe". Слово вне таблицы остаётся
                      собой: рецепта под него нет, его обслуживает generic.
    pool_for          "cafe" -> пресеты, которыми ниша может быть собрана. Ниша
                      вне таблицы получает все пресеты в порядке presets.yaml.
    schema_type_for   "cafe" -> "Restaurant" для JSON-LD страницы. Ниши нет в
                      таблице — тип берётся из рецепта.

Порядок пула — часть контракта (пресет выбирается как хеш домена по модулю
длины пула), поэтому валидация здесь жёсткая: id, которого нет в presets.yaml,
валит загрузку сразу, а не выдаёт лиду пресет-призрак на публикации.

Модуль не импортирует ничего из engine: его читают и profile.py, и render.py,
а профиль не должен знать про рендер.
"""
from __future__ import annotations

import functools
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load(root: pathlib.Path = ROOT) -> dict:
    """Все таблицы файла.

    Кеш вынесен в _read намеренно: lru_cache различает вызовы load() и
    load(ROOT) по самим аргументам и держал бы два разбора одного файла.
    """
    return _read(root)


@functools.lru_cache(maxsize=8)
def _read(root: pathlib.Path) -> dict:
    data = yaml.safe_load((root / "tokens" / "niches.yaml").read_text(encoding="utf-8"))
    return {"aliases": data.get("aliases") or {},
            "pools": data.get("pools") or {},
            "schema_types": data.get("schema_types") or {}}


def key_for(niche: str | None, root: pathlib.Path = ROOT) -> str | None:
    """Ключ ниши по слову карточки. Регистр и пробелы по краям не считаются."""
    word = str(niche or "").strip().lower()
    if not word:
        return None
    return load(root)["aliases"].get(word, word)


def pool_for(niche_key: str | None, preset_ids: tuple[str, ...],
             root: pathlib.Path = ROOT) -> tuple[str, ...]:
    """Пул ниши. Ниши нет в таблице — весь список пресетов, как он лежит."""
    return pools(preset_ids, root).get(niche_key) or tuple(preset_ids)


def pools(preset_ids: tuple[str, ...],
          root: pathlib.Path = ROOT) -> dict[str, tuple[str, ...]]:
    """Все пулы разом, проверенные против списка пресетов."""
    return _validated(tuple(preset_ids), root)


def schema_type_for(niche_key: str | None, root: pathlib.Path = ROOT) -> str | None:
    """Тип schema.org ниши. None — ниши в таблице нет, тип возьмёт рецепт."""
    return schema_types(root).get(niche_key)


def schema_types(root: pathlib.Path = ROOT) -> dict[str, str]:
    """Раздел schema_types, проверенный против списка ниш."""
    return _schema_types(root)


@functools.lru_cache(maxsize=8)
def _schema_types(root: pathlib.Path) -> dict[str, str]:
    """Ниша без пула — опечатка: тип попал бы в JSON-LD, а пресет бы не выбрался."""
    data = load(root)
    unknown = sorted(set(data["schema_types"]) - set(data["pools"]))
    if unknown:
        raise ValueError(f"tokens/niches.yaml: schema_types называет ниши, "
                         f"которых нет в pools: {', '.join(unknown)}")
    return {key: str(value) for key, value in data["schema_types"].items()}


@functools.lru_cache(maxsize=8)
def _validated(preset_ids: tuple[str, ...],
               root: pathlib.Path) -> dict[str, tuple[str, ...]]:
    known = set(preset_ids)
    table = {}
    for niche, ids in load(root)["pools"].items():
        if not ids:
            raise ValueError(f"tokens/niches.yaml: пустой пул {niche!r}")
        unknown = [preset_id for preset_id in ids if preset_id not in known]
        if unknown:
            raise ValueError(f"tokens/niches.yaml: пул {niche!r} ссылается на "
                             f"пресеты, которых нет: {', '.join(unknown)}")
        table[niche] = tuple(ids)
    return table
