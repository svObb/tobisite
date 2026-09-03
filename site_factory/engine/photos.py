"""Пул беспредметных снимков страницы: кто свободен и кому какой кадр достался.

Пул один на всю страницу (`profile.free_photos`), и курсор по нему сквозной:
роли разбирают кадры в том порядке, в каком стоят в `roles_order`, а
`compose._fill` протаскивает через гейты и слоты множество уже разобранных
имён. Поэтому один снимок дважды на странице не встречается по построению, а
не по проверке постфактум.

Ключи контракта, которые читает этот модуль:

    image_pool: free_photos   вариант берёт кадры пулом, а не поимённо
    image_slots: N            сколько кадров он покажет — это потолок
    pool_min: N               без скольких он не живёт (по умолчанию image_slots)
    pool_min_width: N         кадры уже этой ширины варианту не годятся вовсе
    pool_pick: widest         вперёд идёт самый широкий кадр остатка

pool_min нужен там, где секция тянется: коллаж рисует и три кадра, и пять, —
потолок у него пять, а порог три. Порог проверяет гейт, потолок режет выдачу.

pool_min_width — жёсткий отсев, а не предпочтение: снимок 600px под фон
секции не годится ничем, и вариант с ним честно выбывает по гейту, вместо
того чтобы растянуть его на всю ширину экрана.
"""
from __future__ import annotations

FREE_PHOTOS = "free_photos"   # единственный пул картинок (image_pool контракта)
WIDEST = "widest"             # pool_pick: самый широкий кадр остатка вперёд


def uses_pool(contract: dict) -> bool:
    return contract.get("image_pool") == FREE_PHOTOS


def floor(contract: dict) -> int:
    """Сколько кадров варианту нужно обязательно."""
    slots = contract.get("image_slots") or 0
    return int(contract.get("pool_min", slots))


def remaining(profile, taken=()) -> list[str]:
    """Свободные кадры страницы за вычетом тех, что разобрали секции выше."""
    used = set(taken)
    return [name for name in profile.free_photos() if name not in used]


def available(contract: dict, profile, taken=()) -> list[str]:
    """Кадры остатка, годные варианту: те, что не уже его pool_min_width."""
    least = contract.get("pool_min_width") or 0
    if not least:
        return remaining(profile, taken)
    return [name for name in remaining(profile, taken)
            if _width(profile, name) >= least]


def picked(contract: dict, profile, taken=()) -> list[str]:
    """Кадры, которые вариант заберёт: срез годных по image_slots.

    Порядок остатка — номерной (profile.free_photos), и он же порядок выдачи.
    pool_pick: widest переставляет его один раз: секции, где кадр идёт фоном
    во всю ширину, нужен самый широкий снимок лида, а не первый по номеру.
    Сортировка устойчивая, поэтому кадры одной ширины остаются в номерном
    порядке.
    """
    names = available(contract, profile, taken)
    if contract.get("pool_pick") == WIDEST:
        names = sorted(names, key=lambda name: -_width(profile, name))
    return names[:contract.get("image_slots") or 0]


def claimed(section: dict) -> set[str]:
    """Кадры пула, которые секция забрала со страницы.

    Именованные картинки (logo, hero_bg, portrait, map) сюда не входят: они и
    так вне пула, и занимать их незачем.
    """
    return set(section["images"]) if uses_pool(section["contract"]) else set()


def _width(profile, name: str) -> int:
    images = (profile.images.value if profile.images.known else {}) or {}
    return int((images.get(name) or {}).get("width") or 0)
