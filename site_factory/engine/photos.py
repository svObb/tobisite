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
    pool_leave: N             сколько кадров оставить секциям ниже по странице
    pool_min_width: N         кадры уже этой ширины варианту не годятся вовсе
    pool_pick: widest         вперёд идёт самый широкий кадр остатка

pool_min нужен там, где секция тянется: коллаж рисует и три кадра, и пять, —
потолок у него пять, а порог три. Порог проверяет гейт, потолок режет выдачу.

pool_leave — вежливость тянущейся секции к тем, кто идёт следом: коллаж стоит
выше блока «о компании» и без него забирал бы весь пул, оставляя странице один
абзац без единой фотографии. Порог сильнее вежливости: отдать последние кадры
и выбыть самому — хуже, чем не оставить их вовсе.

pool_min_width — жёсткий отсев, а не предпочтение: снимок 600px под фон
секции не годится ничем, и вариант с ним честно выбывает по гейту, вместо
того чтобы растянуть его на всю ширину экрана.

Кадры `ambient-N` в пуле лежат вместе с остальными, но галерее достаются не
так: см. offered().
"""
from __future__ import annotations

FREE_PHOTOS = "free_photos"   # единственный пул картинок (image_pool контракта)
WIDEST = "widest"             # pool_pick: самый широкий кадр остатка вперёд
AMBIENT = "ambient-"          # префикс кадров, дорисованных под нехватку пула
GALLERY = "gallery"           # роль, где кадр читается как фотография объекта

# Сколько кадров нужно галерее, чтобы быть галереей: порог самой требовательной
# из них — кладки. Добор дорисованными считается для роли целиком, а не для
# каждого варианта отдельно: считай его вариант по своему порогу, заявление и
# кладка спорили бы за роль, глядя на пулы разного размера. Числом, потому что
# библиотеки здесь нет и не будет (её читает render, а он импортирует этот
# модуль); вывод числа из контрактов держит тест в tests/test_photos.py.
#
# Порог этот — про добор, и работает он только пока добирать есть к чему: у
# лида без единого своего снимка галерея до тройки не дотягивается вовсе, см.
# offered().
GRID_MIN = 3


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


def is_ambient(name: str) -> bool:
    """Кадр дорисован под нехватку пула, а не снят у самого лида."""
    return str(name).startswith(AMBIENT)


def has_ambient(images) -> bool:
    """Есть ли дорисованный кадр среди картинок секции. Зовут и шаблоны галерей."""
    return any(is_ambient(name) for name in images or ())


def offered(contract: dict, profile, taken=()) -> list[str]:
    """Кадры остатка, которые вариант вправе увидеть. Реальные всегда впереди.

    Сгенерированный кадр не доказательство — он закрывает дыру, а не
    рассказывает о бизнесе. Поэтому галерее он достаётся только добором к
    снимкам самого лида: пока их хватает на GRID_MIN, дорисованных она не видит
    вовсе, а не хватает — берёт ровно столько, сколько не хватает до порога.

    Своих снимков нет ни одного — добирать не к чему, и галерее предлагается
    ровно один дорисованный кадр: во всю ширину под фразой он остаётся фоном,
    чем и задуман, а кладка или полоса целиком из нарисованного — уже не
    заплатка, а сфабрикованное доказательство.

    Остальным ролям дорисованный кадр годится как прежде: фоном первого экрана
    и полотном рядом с текстом он и задуман, а фотографией объекта там никто
    его не выдаёт.
    """
    names = remaining(profile, taken)
    if contract.get("role") != GALLERY:
        return names
    real = [name for name in names if not is_ambient(name)]
    drawn = [name for name in names if is_ambient(name)]
    gap = max(GRID_MIN - len(real), 0) if real else 1
    return real + drawn[:gap]


def available(contract: dict, profile, taken=()) -> list[str]:
    """Кадры остатка, годные варианту: те, что не уже его pool_min_width."""
    least = contract.get("pool_min_width") or 0
    names = offered(contract, profile, taken)
    if not least:
        return names
    return [name for name in names if _width(profile, name) >= least]


def picked(contract: dict, profile, taken=()) -> list[str]:
    """Кадры, которые вариант заберёт из остатка.

    Порядок остатка — номерной (profile.free_photos), и он же порядок выдачи.
    pool_pick: widest переставляет его один раз: секции, где кадр идёт фоном
    во всю ширину, нужен самый широкий снимок лида, а не первый по номеру.
    Сортировка устойчивая, поэтому кадры одной ширины остаются в номерном
    порядке.
    """
    names = available(contract, profile, taken)
    if contract.get("pool_pick") == WIDEST:
        names = sorted(names, key=lambda name: -_width(profile, name))
    return names[:_take(contract, len(names))]


def _take(contract: dict, free: int) -> int:
    """Сколько кадров вариант возьмёт из free годных: потолок с оглядкой назад.

    Без pool_leave это просто image_slots. С ним вариант отдаёт лишние кадры
    секциям ниже по странице, но не опускается ниже своего порога: секция,
    выбывшая из-за собственной вежливости, не оставит кадры никому.
    """
    ceiling = contract.get("image_slots") or 0
    leave = contract.get("pool_leave") or 0
    if not leave:
        return ceiling
    return max(min(free - leave, ceiling), floor(contract))


def strictest_min_width(library: dict) -> int:
    """Самый строгий pool_min_width библиотеки: уже него кадр не берёт никто.

    Публичная, потому что вне движка есть ровно один вопрос к пулу — годится
    ли кадр, который человек кладёт в стейджинг руками (bot/ambient_stage).
    Ответ на него обязан считаться по контрактам, а не второй копией числа.
    """
    return max((int(contract.get("pool_min_width") or 0)
                for contract in library.values()), default=0)


def ambient_fill(section: dict) -> int:
    """Сколько кадров секция добрала дорисованными — число для следа решения."""
    return sum(1 for name in section["images"] if is_ambient(name))


def claimed(section: dict) -> set[str]:
    """Кадры пула, которые секция забрала со страницы.

    Именованные картинки (logo, hero_bg, portrait, map) сюда не входят: они и
    так вне пула, и занимать их незачем.
    """
    return set(section["images"]) if uses_pool(section["contract"]) else set()


def _width(profile, name: str) -> int:
    images = (profile.images.value if profile.images.known else {}) or {}
    return int((images.get(name) or {}).get("width") or 0)
