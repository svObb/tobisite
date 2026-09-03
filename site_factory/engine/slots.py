"""Слоты: контракт между секцией и данными. Модель никогда не видит HTML (§2).

Контракт варианта секции — YAML рядом с .j2. Правила, которые держит этот
модуль:

* type: fact — только из белого списка профиля (карточка лида, Google Maps,
  старый сайт). type: free — пишет модель; в MVP это плейсхолдеры из рецепта.
* Движок обязан положить в контекст ВСЕ объявленные слоты. Неизвестный слот
  с optional: true кладётся как None — шаблон проверяет его через if. Слот без
  optional отсутствовать не может: под StrictUndefined это ошибка сборки.
* repeat-слоты с общим ключом group приходят одним списком: пара
  {name: service_name, group: services} + {name: service_blurb, group: services}
  превращается в s.services = [{name, blurb}, ...]. Имя ключа в элементе — имя
  слота без singular-приставки группы (services -> service_).
* группу ведёт ровно один fact-слот без source: item — он решает, сколько
  элементов будет. Остальные fact-слоты группы объявляют source: item и берут
  свои значения из того же элемента драйвера: {name: product_name} даёт цену
  через {name: product_price, source: item} и картинку через product_image.
  Так товар «название + цена + фото» помещается в контракт, не заводя второй
  источник длины списка.
* group_filter на слоте-драйвере отсеивает элементы до проверки repeat: у
  товарной сетки без картинок нет смысла, и лучше пусть выбудет весь вариант,
  чем встанут пустые рамки.
* image_names перечисляет картинки поимённо, и каждая обязана лежать в белом
  списке профиля. image_pool: free_photos, наоборот, просит image_slots любых
  свободных снимков — тех, что не заняты ни именованной ролью, ни товарной
  сеткой. Так полоса галереи не повторяет фотографии витрины.
* max_chars — ограничение для генератора слотов, не для вёрстки. Молча резать
  текст нельзя: если заготовка или факт не влезли, вариант выбывает по гейту
  с причиной too_long, и роль берёт следующую ступень лестницы. Словарь
  (картинка) мимо max_chars: считать символы в {src, width, height} нечего.

Белый список фактов — таблица FACT_SOURCES ниже, и только она. Слот type: fact,
которого в таблице нет, — ошибка контракта, а не повод что-нибудь придумать.

source: composer — единственное исключение: значение знает не профиль, а
compose.py (якорь соседней секции). Такие слоты заполняет apply_composer после
того, как состав страницы окончателен.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from .gates import FACT_MISSING, NO_DEFAULT, TOO_FEW, TOO_LONG, Reason
from .profile import Profile

COMMON = "_common"        # заготовки, общие для всех вариантов рецепта
PAGE = "_page"            # заготовки уровня страницы: title/description/ui
RESERVED = (COMMON, PAGE)

FREE_PHOTOS = "free_photos"   # единственный пул картинок (image_pool контракта)

_MISSING = object()


@dataclass(frozen=True)
class Filled:
    slots: dict
    images: dict
    reasons: tuple[Reason, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.reasons


def build(contract: dict, profile: Profile, recipe: dict) -> Filled:
    """Собрать JSON слотов варианта. Пустой Filled.reasons — вариант годен."""
    if not profile.lang.known or not profile.lang.value:
        return Filled({}, {}, (Reason("lang", FACT_MISSING,
                                      "язык лида неизвестен"),))
    lang = str(profile.lang.value)
    if not (recipe.get("free_defaults") or {}).get(lang):
        return Filled({}, {}, (Reason("lang", NO_DEFAULT,
                                      f"в рецепте нет заготовок для языка {lang!r}"),))

    slots: dict[str, Any] = {}
    reasons: list[Reason] = []
    groups: dict[str, list[dict]] = {}

    for spec in contract.get("slots") or []:
        group = spec.get("group")
        if group:
            groups.setdefault(group, []).append(spec)
            continue
        value, trouble = _scalar(spec, contract, profile, recipe, lang)
        slots[spec["name"]] = value
        reasons.extend(trouble)

    for group, specs in groups.items():
        items, trouble = _group(group, specs, contract, profile, recipe, lang)
        slots[group] = items
        reasons.extend(trouble)

    return Filled(slots, _images(contract, profile), tuple(reasons))


def free_specs(contract: dict) -> list[dict]:
    """Скалярные free-слоты варианта — ровно то, что пишет модель.

    Повторяющиеся free-слоты (blurb услуги, подпись показателя) сюда не входят:
    у них по значению на каждый элемент группы. Слоты группы отдаёт
    group_free_specs, и ключ у них свой, с индексом элемента.
    """
    return [spec for spec in contract.get("slots") or []
            if spec["type"] == "free" and not spec.get("group")
            and not spec.get("repeat") and spec.get("source") != "composer"]


def group_free_specs(contract: dict) -> list[dict]:
    """Повторяющиеся free-слоты варианта: по значению на каждый элемент группы.

    Ключ такого текста несёт индекс — «вариант.слот[0]». Позиция элемента
    в группе детерминирована: композиция это чистая функция профиля и seed,
    а тексты ложатся поверх неё уже готовой.
    """
    return [spec for spec in contract.get("slots") or []
            if spec["type"] == "free" and spec.get("group")]


def apply_free_texts(section: dict, texts: dict) -> bool:
    """Тексты модели поверх заготовок рецепта. False — секцию рендерить нечем.

    Ключ текста — «вариант.слот»: одно и то же имя слота живёт в разных
    секциях с разным смыслом и разным лимитом, и плоский ключ склеил бы
    заголовок первого экрана с заголовком формы. У слотов группы к ключу
    добавляется индекс элемента: «svc_cards_3.service_blurb[1]».

    Скалярного слота нет в словаре — текста для него нет, и заготовка рецепта
    его не подменяет: слот-генерация всегда отдаёт ключ на каждый free-слот
    композиции, а недостающий ключ означает, что тексты собраны для другой
    композиции (публикация старого черновика на обновлённой библиотеке).
    Тихий плейсхолдер в этом месте — рыба рецепта на живом превью.

    С групповыми наоборот: недостающий ключ оставляет заготовку рецепта на
    месте. Тексты старых черновиков блёрбов не содержат вовсе, и требовать их
    задним числом значило бы уронить каждую такую страницу.

    Пустая строка или превышение max_chars — слот пуст: молча резать текст
    нельзя (тот же запрет, что и в _measure), а пустой обязательный слот
    выводит секцию из состава страницы целиком (§3 ступень 4). Пустой блёрб
    элемента секцию не валит: карточка услуги без пояснения — карточка.
    """
    ok = True
    for spec in free_specs(section["contract"]):
        key = f"{section['variant']}.{spec['name']}"
        value = str(texts.get(key) or "").strip()
        max_chars = spec.get("max_chars")
        if max_chars and len(value) > max_chars:
            value = ""
        if not value and not spec.get("optional"):
            ok = False
        section["slots"][spec["name"]] = value or None

    for spec in group_free_specs(section["contract"]):
        field = _item_key(spec["group"], spec["name"])
        max_chars = spec.get("max_chars")
        for index, row in enumerate(section["slots"].get(spec["group"]) or []):
            key = f"{section['variant']}.{spec['name']}[{index}]"
            if key not in texts:
                continue
            value = str(texts.get(key) or "").strip()
            if max_chars and len(value) > max_chars:
                value = ""
            row[field] = value or None
    return ok


def apply_composer(section: dict, values: dict) -> None:
    """Заполнить composer-слоты, когда состав страницы уже окончателен."""
    for spec in section["contract"].get("slots") or []:
        if spec.get("source") != "composer":
            continue
        value = values.get(spec["name"])
        if value is None and not spec.get("optional"):
            raise ValueError(f"{section['id']}: composer не дал слот {spec['name']!r}")
        max_chars = spec.get("max_chars")
        if value is not None and max_chars and len(value) > max_chars:
            raise ValueError(f"{section['id']}: слот {spec['name']!r} длиннее "
                             f"{max_chars} символов")
        section["slots"][spec["name"]] = value


def page_defaults(recipe: dict, lang: str) -> dict:
    return ((recipe.get("free_defaults") or {}).get(lang) or {}).get(PAGE) or {}


def tel_href(phone: str) -> str:
    """tel: из телефона профиля — только плюс и цифры, ничего не выдумывая."""
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    plus = "+" if str(phone).lstrip().startswith("+") else ""
    return f"tel:{plus}{digits}"


# --- факты -----------------------------------------------------------------

@dataclass(frozen=True)
class FactSource:
    """field — какое поле профиля дозаполнить, если факта нет."""

    field: str
    build: Callable[[Profile, str], Any]


def _phone_href(profile: Profile, lang: str):
    return tel_href(profile.phone.value) if profile.phone.known else None


def _plain(name: str):
    def read(profile: Profile, lang: str):
        feature = getattr(profile, name)
        return feature.value if feature.known else None
    return read


def _services(profile: Profile, lang: str):
    if not profile.services.known:
        return None
    return [{"key": str(i), "value": str(v)}
            for i, v in enumerate(profile.services.value or [])]


def _products(profile: Profile, lang: str):
    """Товары лида. Цена и картинка ждут слотов с source: item."""
    if not profile.products.known:
        return None
    rows = []
    for index, item in enumerate(profile.products.value or []):
        name = str((item or {}).get("name") or "").strip()
        if not name:
            continue        # товар без названия нечем показать и нечем назвать
        rows.append({"key": str(index), "value": name,
                     "price": item.get("price"), "image": item.get("image")})
    return rows


def _hours_list(profile: Profile):
    """Часы профиля списком строк. None — часов не спрашивали.

    Строка приходит одним элементом, а не рассыпается по буквам: расписание
    ручного ввода добирается сюда целиком, и посимвольная итерация нарисовала
    бы таблицу часов из отдельных знаков. Резать такую строку — дело шлюза
    (draft_service._clean_hours), где ещё видно, откуда она взялась.
    """
    if not profile.hours.known:
        return None
    value = profile.hours.value
    if isinstance(value, str):
        return [value] if value.strip() else []
    return list(value or [])


def _hours_rows(profile: Profile, lang: str):
    lines = _hours_list(profile)
    if lines is None:
        return None
    return [{"key": str(i), "value": str(v)} for i, v in enumerate(lines)]


def _hour_days(profile: Profile, lang: str):
    """Часы таблицей: день слева, время справа.

    Строку часов пишет сам бизнес, поэтому режем её по первому «: », а если
    двоеточия нет — по пробелу перед началом времени («Пн-Пт 9:00-19:00»). Не
    режется ни так, ни так — вся строка уходит в день, а время остаётся
    пустым: выдумывать расписание движку нечем.
    """
    lines = _hours_list(profile)
    if lines is None:
        return None
    rows = []
    for index, line in enumerate(lines):
        day, time = _split_hours(str(line))
        rows.append({"key": str(index), "value": day, "time": time})
    return rows


# «Пн-Пт 9:00-19:00»: день — всё до пробела, за которым начинается время.
_HOURS_TIME = re.compile(r"^(?P<day>.+?)\s+(?P<time>\d{1,2}[:.]\d{2}.*)$")


def _split_hours(text: str) -> tuple[str, str | None]:
    day, separator, time = text.partition(": ")
    if separator:
        return day, time.strip()
    found = _HOURS_TIME.match(text)
    if found:
        return found["day"].strip(), found["time"].strip()
    return text, None


def _hours_line(profile: Profile, lang: str):
    rows = _hours_rows(profile, lang)
    return " · ".join(item["value"] for item in rows) if rows else None


def _stats(profile: Profile, lang: str):
    stats = profile.proof_stats()
    if not stats:
        return None
    return [{"key": s["key"], "value": _stat_text(s, lang)} for s in stats]


def _stat_text(stat: dict, lang: str) -> str:
    if stat["key"] == "rating":
        return _decimal(stat["value"], lang)
    return str(int(stat["value"]))


def _decimal(value, lang: str) -> str:
    """Оценка с одним знаком: «4.8» латиницей, «4,8» на украинском."""
    text = f"{float(value):.1f}"
    return text if lang == "en" else text.replace(".", ",")


def _rating(profile: Profile) -> dict | None:
    """Рейтинг со страницы лида. None — его нет или он не показуем."""
    return profile.rating.value if profile.feature("has_rating").value else None


# Подпись под показателями: у каждого источника своя формулировка. Источника
# в таблице нет — подписи не будет вовсе: назвать гугловскими цифры, снятые
# с сайта лида, нельзя, а обтекаемое «за відкритими даними» не сообщает ничего.
RATING_SOURCE_NOTES: dict[str, dict[str, str]] = {
    "google": {"uk": "Дані з профілю Google Business.",
               "en": "Figures from the Google Business profile."},
    "jsonld": {"uk": "Дані з сайту компанії.",
               "en": "Figures from the company's own website."},
}


def _rating_source_note(profile: Profile, lang: str):
    source = profile.stats_source() or ""
    return RATING_SOURCE_NOTES.get(source, {}).get(lang)


def _rating_value(profile: Profile, lang: str):
    rating = _rating(profile)
    return None if rating is None else _decimal(rating["value"], lang)


def _rating_count(profile: Profile, lang: str):
    rating = _rating(profile)
    return None if rating is None else str(int(rating["count"]))


FACT_SOURCES: dict[str, FactSource] = {
    "business_name": FactSource("name", _plain("name")),
    "phone": FactSource("phone", _plain("phone")),
    "phone_href": FactSource("phone", _phone_href),
    "email": FactSource("email", _plain("email")),
    "address": FactSource("address", _plain("address")),
    "service_name": FactSource("services", _services),
    "product_name": FactSource("products", _products),
    "hour_day": FactSource("hours", _hour_days),
    "stat_value": FactSource("proof_stats", _stats),
    # Оценка и число отзывов поштучно: proof-полоса берёт их списком через
    # stat_value, а секции, которой нужна одна цифра, — этими двумя слотами.
    "rating_value": FactSource("rating", _rating_value),
    "rating_count": FactSource("rating", _rating_count),
    "rating_source_note": FactSource("rating", _rating_source_note),
}

# Отборы элементов группы. Имя из group_filter слота-драйвера; фильтр видит
# элемент целиком, поэтому судит по ключам, которых нет ни у одного признака
# профиля (картинка конкретного товара).
GROUP_FILTERS: dict[str, Callable[[dict], bool]] = {
    "has_image": lambda item: bool(item.get("image")),
}

# Слоты, которые в одном контракте повторяются, а в другом идут строкой.
FACT_SOURCES_REPEAT: dict[str, FactSource] = {
    "hours": FactSource("hours", _hours_rows),
}
FACT_SOURCES_SCALAR: dict[str, FactSource] = {
    "hours": FactSource("hours", _hours_line),
}


def _source(spec: dict) -> FactSource:
    name = spec["name"]
    table = FACT_SOURCES_REPEAT if spec.get("repeat") else FACT_SOURCES_SCALAR
    source = table.get(name) or FACT_SOURCES.get(name)
    if source is None:
        raise ValueError(f"слот {name!r} объявлен фактом, но его нет в белом списке")
    return source


# --- сборка ----------------------------------------------------------------

def _scalar(spec, contract, profile, recipe, lang):
    name = spec["name"]
    if spec.get("source") == "composer":
        return None, ()          # заполнит apply_composer
    if spec.get("repeat"):
        return _repeat_plain(spec, profile, lang)

    if spec["type"] == "fact":
        source = _source(spec)
        value = source.build(profile, lang)
        if value is None:
            return _absent(spec, Reason(source.field, FACT_MISSING,
                                        f"нет данных для слота {name!r}"))
    else:
        value = _default(recipe, lang, contract["id"], name)
        if value is _MISSING:
            return _absent(spec, Reason(f"free:{name}", NO_DEFAULT,
                                        f"в рецепте нет заготовки для слота {name!r}"))
    return _measure(spec, value)


def _absent(spec, reason: Reason):
    """Слот нечем заполнить: optional уходит в None, обязательный валит вариант."""
    return (None, ()) if spec.get("optional") else (None, (reason,))


def _repeat_plain(spec, profile: Profile, lang: str):
    """Повтор без группы: footer_nap.hours — просто список строк."""
    name = spec["name"]
    source = _source(spec)
    items = source.build(profile, lang)
    if not items:
        return _absent(spec, Reason(source.field, FACT_MISSING,
                                    f"нет данных для слота {name!r}"))
    low, high = _repeat_range(spec["repeat"])
    items = items[:high]
    if len(items) < low:
        return None, (Reason(source.field, TOO_FEW,
                             f"{name}: {len(items)} значений, нужно от {low}"),)
    values, reasons = [], []
    for item in items:
        value, trouble = _measure(spec, item["value"])
        values.append(value)
        reasons.extend(trouble)
    return values, tuple(reasons)


def _group(group, specs, contract, profile, recipe, lang):
    driver_specs = [s for s in specs
                    if s["type"] == "fact" and s.get("source") != "item"]
    if len(driver_specs) != 1:
        raise ValueError(f"{contract['id']}: группа {group!r} обязана иметь "
                         f"ровно один fact-слот без source: item")
    driver = driver_specs[0]
    source = _source(driver)
    items = source.build(profile, lang)
    if not items:
        return None, (Reason(source.field, FACT_MISSING,
                             f"нет данных для группы {group!r}"),)

    low, high = _repeat_range(driver["repeat"])
    items = _filtered(driver, items)[:high]
    if len(items) < low:
        return None, (Reason(source.field, TOO_FEW,
                             f"{group}: {len(items)} значений, нужно от {low}"),)

    reasons: list[Reason] = []
    rows = []
    for index, item in enumerate(items):
        row = {}
        for spec in specs:
            key = _item_key(group, spec["name"])
            if spec is driver:
                value, trouble = _measure(spec, item["value"])
            elif spec.get("source") == "item":
                value, trouble = _item_value(spec, source, item, key)
            else:
                value, trouble = _free_item(spec, contract, recipe, lang,
                                            item["key"], index)
            row[key] = value
            reasons.extend(trouble)
        rows.append(row)
    return rows, tuple(reasons)


def _filtered(driver, items):
    """group_filter драйвера: отсев элементов до проверки repeat."""
    name = driver.get("group_filter")
    if not name:
        return items
    keep = GROUP_FILTERS.get(name)
    if keep is None:
        raise ValueError(f"неизвестный group_filter {name!r}")
    return [item for item in items if keep(item)]


def _item_value(spec, source: FactSource, item: dict, key: str):
    """Дополнительный ключ элемента драйвера: цена товара, время работы."""
    value = item.get(key)
    if value is None:
        return _absent(spec, Reason(source.field, FACT_MISSING,
                                    f"в элементе группы нет ключа {key!r}"))
    return _measure(spec, value)


def _free_item(spec, contract, recipe, lang, item_key, index):
    """Заготовка для повторяющегося free-слота.

    Словарь — берётся по ключу элемента (показатели: rating/reviews), список —
    крутится по кругу (услуги: у каждой своя строка), строка — одна на всех.
    """
    default = _default(recipe, lang, contract["id"], spec["name"])
    if default is _MISSING:
        return None, (Reason(f"free:{spec['name']}", NO_DEFAULT,
                             f"в рецепте нет заготовки для слота {spec['name']!r}"),)
    if isinstance(default, dict):
        value = default.get(item_key, _MISSING)
        if value is _MISSING:
            return None, (Reason(f"free:{spec['name']}", NO_DEFAULT,
                                 f"нет заготовки {spec['name']}[{item_key!r}]"),)
    elif isinstance(default, list):
        value = default[index % len(default)] if default else _MISSING
        if value is _MISSING:
            return None, (Reason(f"free:{spec['name']}", NO_DEFAULT,
                                 f"пустой список заготовок {spec['name']!r}"),)
    else:
        value = default
    return _measure(spec, value)


def _measure(spec, value):
    max_chars = spec.get("max_chars")
    if isinstance(value, dict):
        return value, ()      # картинка: считать символы в {src, width, height} нечего
    if value is not None and max_chars and len(str(value)) > max_chars:
        return value, (Reason(f"slot:{spec['name']}", TOO_LONG,
                              f"{spec['name']}: {len(str(value))} символов при "
                              f"лимите {max_chars}"),)
    return value, ()


def _default(recipe, lang, variant, slot):
    table = (recipe.get("free_defaults") or {}).get(lang) or {}
    per_variant = table.get(variant) or {}
    if slot in per_variant:
        return per_variant[slot]
    return (table.get(COMMON) or {}).get(slot, _MISSING)


def _item_key(group: str, slot_name: str) -> str:
    singular = group[:-1] if group.endswith("s") else group
    prefix = f"{singular}_"
    return slot_name[len(prefix):] if slot_name.startswith(prefix) else slot_name


def _repeat_range(spec) -> tuple[int, int]:
    text = str(spec)
    low, _, high = text.partition("..")
    return int(low or 1), int(high or 99)


def _images(contract: dict, profile: Profile) -> dict:
    """Картинки секции: поимённо из image_names или пулом из image_pool.

    Пул отдаёт первые image_slots свободных снимков в порядке profile —
    он детерминирован, как и весь остальной подбор.
    """
    available = (profile.images.value if profile.images.known else {}) or {}
    if contract.get("image_pool") == FREE_PHOTOS:
        names = profile.free_photos()[:contract.get("image_slots") or 0]
    else:
        names = contract.get("image_names") or []
    return {name: available[name] for name in names if name in available}
