"""Профиль лида из leads + contacts + leads.enrichment (13-шаблоны §3 шаг 1).

Признаки: photo_count, service_count, has_prices, has_hours, has_booking_url,
review_count, google_rating, rating, has_address, text_volume, old_site_state,
brand_colors, products, ниша, язык, страна.

Главное правило: у каждого поля отдельный флаг known, и unknown != false.
Иначе система однажды соберёт сайт без телефона потому, что телефон не
спрашивали, а не потому, что его нет.

Отсюда правило конструктора: **ключ есть в словаре — признак известен, ключа
нет — неизвестен**. `{"has_prices": False}` значит «спросили, прайса нет»;
отсутствие ключа значит «не спрашивали», и гейт такой вариант не пропустит.

Кроме сырых полей профиль отдаёт производные признаки (`feature`), которыми
оперируют контракты секций: has_phone, has_address, service_count из списка
услуг, product_count и products_with_images, has_logo, logo_is_dark,
has_brand_colors, has_rating, proof_stats_count, nonproduct_photo_count,
нормализованная ниша.
Производные считаются в одном месте, чтобы гейт и скоринг не разошлись в
трактовке.

from_lead (сборка из БД) появится вместе с этапом 6 — здесь только from_dict
для фикстур: пакет не импортирует ни моделей бота, ни драйвера базы.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Callable

from . import niches
from .color import lightness, srgb
from .palette import HEX


@dataclass(frozen=True)
class Feature:
    """Признак профиля: значение и знаем ли мы его. unknown != false."""

    value: Any = None
    known: bool = False

    def as_dict(self) -> dict:
        return {"value": self.value, "known": self.known}


UNKNOWN = Feature()


def known(value: Any) -> Feature:
    return Feature(value, True)


# Порядок перечислений, по которому сравниваются условия вида ">=medium".
TEXT_VOLUME_ORDER = ("none", "short", "medium", "long")
OLD_SITE_STATE_ORDER = ("none", "broken", "not_mobile", "outdated", "ok")
ORDERED_ENUMS = (TEXT_VOLUME_ORDER, OLD_SITE_STATE_ORDER)

# Картинки белого списка, у которых на странице своё место: логотип в шапке,
# hero_bg под первым экраном, portrait и map — в своих первых экранах. Полоса
# галереи берёт снимки пулом и эти имена не трогает: иначе фон первого экрана
# встал бы на страницу вторым экземпляром.
NAMED_IMAGES = ("logo", "hero_bg", "portrait", "map")

# Ниже этой светлоты (L в oklab) логотип считается тёмным — и когда светлота
# снята со всей картинки (site_images.mean_lightness), и когда её приходится
# считать по цвету. Середина шкалы: такой логотип нарисован под белый фон, и
# на тёмном скриме первого экрана он пропадает — шапке приходится вставать
# своей полосой (render.header_overlay).
DARK_LOGO_LIGHTNESS = 0.5

# Карточку лида наполняет выгрузка Google Maps, поэтому у карточных оценки и
# числа отзывов источник ровно один. У скрейпа источник свой и приходит вместе
# с цифрами (site_scrape.rating).
CARD_RATING_SOURCE = "google"

# Ниша лида приходит словом на языке работника; рецепты и контракты знают
# только ключи. Таблица переехала в tokens/niches.yaml — ниш скоро полсотни,
# и каждая новая не должна быть коммитом в движок. Всё, чего нет в таблице,
# обслуживает generic (§ рамка этапа 5).
NICHE_ALIASES = niches.load()["aliases"]


@dataclass(frozen=True)
class Profile:
    """Профиль лида. Всё, кроме domain_norm, — Feature с флагом known."""

    # Идентичность лида. domain_norm — ключ детерминизма: из него движок
    # считает seed и пресет, поэтому он обязателен и не бывает unknown.
    domain_norm: str

    niche: Feature = UNKNOWN
    lang: Feature = UNKNOWN
    country: Feature = UNKNOWN
    city: Feature = UNKNOWN

    # NAP. address — строка, которая уйдёт на страницу побайтово (её сверяет
    # checks/nap.py); address_parts — разбор для schema.org, и только для него.
    name: Feature = UNKNOWN
    phone: Feature = UNKNOWN
    email: Feature = UNKNOWN
    address: Feature = UNKNOWN
    address_parts: Feature = UNKNOWN

    # Признаки §3 шаг 1.
    photo_count: Feature = UNKNOWN
    service_count: Feature = UNKNOWN
    services: Feature = UNKNOWN
    has_prices: Feature = UNKNOWN
    has_hours: Feature = UNKNOWN
    hours: Feature = UNKNOWN
    has_booking_url: Feature = UNKNOWN
    booking_url: Feature = UNKNOWN
    review_count: Feature = UNKNOWN
    google_rating: Feature = UNKNOWN
    # Оценка со страницы лида (разметка JSON-LD): {value, count, source?}.
    # Она старше карточной google_rating — её снял скрейп с самого сайта.
    rating: Feature = UNKNOWN
    has_address: Feature = UNKNOWN
    text_volume: Feature = UNKNOWN
    old_site_state: Feature = UNKNOWN
    brand_colors: Feature = UNKNOWN

    # Белый список готовых картинок: имя из image_names контракта (или любое
    # photo-N для пула) -> {src, width, height}. Чего здесь нет — того на
    # странице не будет.
    images: Feature = UNKNOWN

    # Товары со страниц лида: [{name, price?, image?{src, width, height}}].
    # Цена — строка ровно в том виде, в каком её пишет сам бизнес: движок цифр
    # не форматирует и валюту не подставляет.
    products: Feature = UNKNOWN

    @classmethod
    def from_dict(cls, data: dict) -> "Profile":
        raw = dict(data)
        domain_norm = raw.pop("domain_norm")
        allowed = {f.name for f in dataclasses.fields(cls)} - {"domain_norm"}
        unexpected = set(raw) - allowed
        if unexpected:
            raise ValueError(f"неизвестные поля профиля: {sorted(unexpected)}")
        if "rating" in raw:
            rating = clean_rating(raw.pop("rating"))
            if rating is not None:
                raw["rating"] = rating
        return cls(domain_norm=domain_norm,
                   **{name: known(value) for name, value in raw.items()})

    @property
    def niche_key(self) -> str | None:
        """Ниша, приведённая к ключу рецепта. None, если ниша неизвестна."""
        if not self.niche.known:
            return None
        return niches.key_for(self.niche.value)

    def proof_stats(self) -> list[dict]:
        """Показатели для proof-секции. Только цифры, которые лид уже показывает.

        Рейтинг со страницы лида старше карточного google_rating: скрейп снял
        его вместе с числом отзывов и источником, а карточку работник
        заполняет по памяти. Оба поля читаются здесь и только здесь — иначе
        proof-секция и JSON-LD одной страницы назвали бы разные оценки.
        """
        rating, reviews = self._rating_pair()
        stats = []
        if rating is not None:
            stats.append({"key": "rating", "value": rating})
        if reviews is not None:
            stats.append({"key": "reviews", "value": reviews})
        return stats

    def stats_source(self) -> str | None:
        """Откуда взяты цифры proof_stats(). None — сказать нечего.

        Подпись под показателями называет источник, поэтому его выбирает та же
        ветка, что и сами цифры: подпись «дані з профілю Google» под оценкой,
        снятой с сайта лида, была бы враньём на странице клиента.
        """
        if self.feature("has_rating").value:
            return str(self.rating.value.get("source") or "") or None
        return CARD_RATING_SOURCE if self.proof_stats() else None

    def free_photos(self) -> list[str]:
        """Контентные снимки, свободные под секции с фотографиями, — по номеру.

        Свободный значит: не логотип, не занят именованной ролью (NAMED_IMAGES)
        и не снят как товар. Товары стейджинг кладёт под теми же именами
        photo-N, и без этого отбора галерея повторяла бы витрину.

        Товарный кадр не свободен никогда (product_image_names) — независимо от
        того, попала товарная секция на страницу или нет.

        Товары неизвестны — все снимки считаются свободными: без известных
        товаров на странице нет и товарной съёмки, прятать нечего.
        """
        taken = self.feature("product_image_names").value or frozenset()
        names = [name for name in (self.images.value or {})
                 if name not in NAMED_IMAGES and name not in taken]
        return sorted(names, key=_photo_order)

    def _rating_pair(self):
        if self.feature("has_rating").value:
            return self.rating.value["value"], self.rating.value["count"]
        return (self.google_rating.value if self.google_rating.known else None,
                self.review_count.value if self.review_count.known else None)

    def feature(self, name: str) -> Feature:
        """Признак по имени из контракта секции: сырое поле или производное."""
        derived = _DERIVED.get(name)
        if derived is not None:
            return derived(self)
        return getattr(self, name, UNKNOWN)

    def as_trace(self) -> dict:
        """JSON-совместимый снимок профиля для recipe_json."""
        out = {"domain_norm": self.domain_norm}
        for f in dataclasses.fields(self):
            if f.name == "domain_norm":
                continue
            out[f.name] = getattr(self, f.name).as_dict()
        return out


def clean_rating(value) -> dict | None:
    """Рейтинг лида в форме движка или None, если показывать нечего.

    Контракт скрейпа — {"value": 0<v<=5, "count": >=1, "source": строка}.
    Оценка вне шкалы и ноль отзывов — это не «плохой рейтинг», а сломанный
    разбор разметки: такую пару лучше не знать вовсе, чем поставить цифру на
    страницу клиента. Ключи оставляем только известные — по тому же правилу,
    по которому from_dict не пускает в профиль неизвестное поле.
    """
    if not isinstance(value, dict):
        return None
    # bool в питоне число: без этого отказа True дал бы «оценку 1,0» и «один
    # отзыв» из мусора. Тот же гард стоит в site_scrape._float — контракт на
    # обоих концах обязан совпадать побайтово.
    if isinstance(value.get("value"), bool) or isinstance(value.get("count"), bool):
        return None
    try:
        rating, count = float(value.get("value")), int(value.get("count"))
    except (TypeError, ValueError):
        return None
    if not 0 < rating <= 5 or count < 1:
        return None
    source = str(value.get("source") or "").strip()
    return dict({"value": rating, "count": count},
                **({"source": source} if source else {}))


def _photo_order(name: str) -> tuple:
    """photo-2, photo-3, …, photo-10 — по числу снимка, а не по алфавиту.

    Числовая ветка только для photo-N: номер там раздаёт стейджинг по убыванию
    размера, и он значит «насколько снимок хорош». У остальных имён цифра на
    конце ничего такого не значит, и они идут следом — в том числе ambient-N,
    кадры, дорисованные под нехватку пула. Снимок компании на странице стоит
    раньше дорисованного, пока он есть.
    """
    head, _, number = name.rpartition("-")
    if head == "photo" and number.isdigit():
        return (0, int(number), name)
    return (1, 0, name)


def _flag(source: Feature) -> Feature:
    """Признак «оно у нас есть» из самого значения: unknown остаётся unknown."""
    return known(bool(source.value)) if source.known else UNKNOWN


def _fallback(explicit: Feature, source: Feature) -> Feature:
    return explicit if explicit.known else _flag(source)


def _service_count(p: Profile) -> Feature:
    if p.service_count.known:
        return p.service_count
    return known(len(p.services.value)) if p.services.known else UNKNOWN


def _products_with_images(p: Profile) -> Feature:
    """Товаров, у которых есть картинка: товарная сетка живёт только ими."""
    if not p.products.known:
        return UNKNOWN
    return known(sum(1 for item in (p.products.value or [])
                     if (item or {}).get("image")))


def _has_logo(p: Profile) -> Feature:
    """Логотип — обычная картинка белого списка под именем logo (решение A)."""
    if not p.images.known:
        return UNKNOWN
    return known("logo" in (p.images.value or {}))


def _product_image_names(p: Profile) -> Feature:
    """Имена файлов, снятых как товар: «/img/photo-3.webp» -> «photo-3».

    Снимок товара попадает в стейджинг под общим именем photo-N, поэтому
    единственная связь товара с картинкой белого списка — имя файла.

    Занят кадр любого товара, а не только того, который выйдет на страницу.
    Пул беспредметный по определению: склад или блок питания во весь первый
    экран (hero_split_2) либо во всю ширину заявления (gallery_statement) — это
    брак, и от порогов товарных секций он не перестаёт им быть. Витрина может
    не набрать товаров на гейт, провалиться на пустых названиях или уступить
    роль списку без картинок — предметная съёмка от этого не становится
    атмосферным кадром.

    Товары неизвестны — заняты не бывают: без известных товаров товарной съёмки
    на странице нет.
    """
    if not p.products.known:
        return UNKNOWN
    names = {_image_stem((item.get("image") or {}).get("src"))
             for item in (p.products.value or []) if item and item.get("image")}
    return known(frozenset(names - {""}))


def _image_stem(src) -> str:
    return str(src or "").rsplit("/", 1)[-1].rsplit(".", 1)[0]


def _logo_is_dark(p: Profile) -> Feature:
    """Тёмный ли логотип лида. unknown — судить не по чему, и решает шапка.

    Старшинство свидетельств: средняя светлота самой картинки, потом её цвета,
    потом фирменный цвет — и то лишь снятый с логотипа: цвет из CSS старого
    сайта о самой картинке не говорит ничего.

    Светлота старше цветов, потому что цвета отбирают нейтральное
    (site_images.dominant_colors): чёрно-белый логотип — самый частый у малого
    бизнеса — цветов не даёт вовсе, и без светлоты он оставался бы неизвестным.
    """
    logo = _logo_record(p)
    value = logo.get("lightness")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return known(float(value) < DARK_LOGO_LIGHTNESS)
    tone = _logo_tone(logo, p.brand_colors)
    if tone is None:
        return UNKNOWN
    return known(lightness(srgb(tone)) < DARK_LOGO_LIGHTNESS)


def _logo_record(p: Profile) -> dict:
    """Запись логотипа из белого списка картинок. Пустая — логотипа нет."""
    return ((p.images.value or {}).get("logo") or {}) if p.images.known else {}


def _logo_tone(logo: dict, brand_colors: Feature) -> str | None:
    """Цвет, по которому судим о логотипе. None — такого цвета мы не знаем.

    Порядок кандидатов и есть старшинство: цвета картинки, потом фирменный цвет
    с неё же. Всё, что не читается как hex, пропускается — судить по мусору
    хуже, чем не судить вовсе.
    """
    brand = brand_colors.value if isinstance(brand_colors.value, dict) else {}
    candidates = list(logo.get("colors") or [])
    if brand.get("source") == "logo":
        candidates.append(brand.get("primary"))
    return next((value.strip() for value in candidates
                 if isinstance(value, str) and HEX.match(value.strip())), None)


def _has_rating(p: Profile) -> Feature:
    """Рейтинг показуем: оценка задана и отзыв хотя бы один."""
    if not p.rating.known:
        return UNKNOWN
    rating = p.rating.value or {}
    return known(rating.get("value") is not None
                 and (rating.get("count") or 0) >= 1)


_DERIVED: dict[str, Callable[[Profile], Feature]] = {
    "niche": lambda p: known(p.niche_key) if p.niche.known else UNKNOWN,
    "has_phone": lambda p: _flag(p.phone),
    "has_address": lambda p: _fallback(p.has_address, p.address),
    "has_hours": lambda p: _fallback(p.has_hours, p.hours),
    "has_logo": _has_logo,
    "logo_is_dark": _logo_is_dark,
    "has_brand_colors": lambda p: _flag(p.brand_colors),
    "service_count": _service_count,
    "product_count": lambda p: (known(len(p.products.value or []))
                                if p.products.known else UNKNOWN),
    "products_with_images": _products_with_images,
    "product_image_names": _product_image_names,
    "nonproduct_photo_count": lambda p: (known(len(p.free_photos()))
                                         if p.images.known else UNKNOWN),
    "has_rating": _has_rating,
    # Сколько показателей мы реально знаем — знаем всегда, поэтому known=True.
    "proof_stats_count": lambda p: known(len(p.proof_stats())),
}
