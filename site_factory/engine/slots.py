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

from dataclasses import dataclass
from typing import Any, Callable

from .gates import FACT_MISSING, NO_DEFAULT, TOO_FEW, TOO_LONG, Reason
from .profile import Profile

COMMON = "_common"        # заготовки, общие для всех вариантов рецепта
PAGE = "_page"            # заготовки уровня страницы: title/description/ui
RESERVED = (COMMON, PAGE)

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
    у них по значению на каждый элемент группы, а контракт слот-генерации —
    одна строка на слот. Их по-прежнему закрывают заготовки рецепта.
    """
    return [spec for spec in contract.get("slots") or []
            if spec["type"] == "free" and not spec.get("group")
            and not spec.get("repeat") and spec.get("source") != "composer"]


def apply_free_texts(section: dict, texts: dict) -> bool:
    """Тексты модели поверх заготовок рецепта. False — секцию рендерить нечем.

    Ключ текста — «вариант.слот»: одно и то же имя слота живёт в разных
    секциях с разным смыслом и разным лимитом, и плоский ключ склеил бы
    заголовок первого экрана с заголовком формы.

    Слота нет в словаре — текста для него нет, и заготовка рецепта его не
    подменяет: слот-генерация всегда отдаёт ключ на каждый free-слот
    композиции, а недостающий ключ означает, что тексты собраны для другой
    композиции (публикация старого черновика на обновлённой библиотеке).
    Тихий плейсхолдер в этом месте — рыба рецепта на живом превью.

    Пустая строка или превышение max_chars — слот пуст: молча резать текст
    нельзя (тот же запрет, что и в _measure), а пустой обязательный слот
    выводит секцию из состава страницы целиком (§3 ступень 4).
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


def _hours_rows(profile: Profile, lang: str):
    if not profile.hours.known:
        return None
    return [{"key": str(i), "value": str(v)}
            for i, v in enumerate(profile.hours.value or [])]


def _hour_days(profile: Profile, lang: str):
    """Часы таблицей: день слева, время справа.

    Строку часов пишет сам бизнес, поэтому единственное, что здесь можно
    сделать, — разрезать её по первому «: ». Не режется — вся строка уходит в
    день, а время остаётся пустым: выдумывать расписание движку нечем.
    """
    if not profile.hours.known:
        return None
    rows = []
    for index, line in enumerate(profile.hours.value or []):
        text = str(line)
        day, separator, time = text.partition(": ")
        rows.append({"key": str(index),
                     "value": day if separator else text,
                     "time": time.strip() if separator else None})
    return rows


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
        text = f"{float(stat['value']):.1f}"
        return text if lang == "en" else text.replace(".", ",")
    return str(int(stat["value"]))


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
    names = contract.get("image_names") or []
    available = (profile.images.value if profile.images.known else {}) or {}
    return {name: available[name] for name in names if name in available}
