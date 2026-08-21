"""Профиль лида из leads + contacts + leads.enrichment (13-шаблоны §3 шаг 1).

Признаки: photo_count, service_count, has_prices, has_hours, has_booking_url,
review_count, google_rating, has_address, text_volume, old_site_state,
brand_colors, ниша, язык, страна.

Главное правило: у каждого поля отдельный флаг known, и unknown != false.
Иначе система однажды соберёт сайт без телефона потому, что телефон не
спрашивали, а не потому, что его нет.

Отсюда правило конструктора: **ключ есть в словаре — признак известен, ключа
нет — неизвестен**. `{"has_prices": False}` значит «спросили, прайса нет»;
отсутствие ключа значит «не спрашивали», и гейт такой вариант не пропустит.

Кроме сырых полей профиль отдаёт производные признаки (`feature`), которыми
оперируют контракты секций: has_phone, has_address, service_count из списка
услуг, proof_stats_count, нормализованная ниша. Производные считаются в одном
месте, чтобы гейт и скоринг не разошлись в трактовке.

from_lead (сборка из БД) появится вместе с этапом 6 — здесь только from_dict
для фикстур: пакет не импортирует ни моделей бота, ни драйвера базы.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Callable


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

# Ниша лида приходит словом на языке работника; рецепты и контракты знают
# только ключи. Всё, чего нет в таблице, обслуживает generic (§ рамка этапа 5).
NICHE_ALIASES = {
    "юрист": "lawyer",
    "юристы": "lawyer",
    "адвокат": "lawyer",
    "адвокати": "lawyer",
    "юридические услуги": "lawyer",
    "юридичні послуги": "lawyer",
    "lawyer": "lawyer",
    "law firm": "lawyer",
    "attorney": "lawyer",
    "стоматология": "dental",
    "стоматологія": "dental",
    "dentist": "dental",
    "dental": "dental",
}


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
    has_address: Feature = UNKNOWN
    text_volume: Feature = UNKNOWN
    old_site_state: Feature = UNKNOWN
    brand_colors: Feature = UNKNOWN

    # Белый список готовых картинок: имя из image_names контракта ->
    # {src, width, height}. Чего здесь нет — того на странице не будет.
    images: Feature = UNKNOWN

    @classmethod
    def from_dict(cls, data: dict) -> "Profile":
        raw = dict(data)
        domain_norm = raw.pop("domain_norm")
        allowed = {f.name for f in dataclasses.fields(cls)} - {"domain_norm"}
        unexpected = set(raw) - allowed
        if unexpected:
            raise ValueError(f"неизвестные поля профиля: {sorted(unexpected)}")
        return cls(domain_norm=domain_norm,
                   **{name: known(value) for name, value in raw.items()})

    @property
    def niche_key(self) -> str | None:
        """Ниша, приведённая к ключу рецепта. None, если ниша неизвестна."""
        if not self.niche.known or not self.niche.value:
            return None
        word = str(self.niche.value).strip().lower()
        return NICHE_ALIASES.get(word, word)

    def proof_stats(self) -> list[dict]:
        """Показатели для proof-секции. Только цифры из профиля Google."""
        stats = []
        if self.google_rating.known and self.google_rating.value is not None:
            stats.append({"key": "rating", "value": self.google_rating.value})
        if self.review_count.known and self.review_count.value is not None:
            stats.append({"key": "reviews", "value": self.review_count.value})
        return stats

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


def _flag(source: Feature) -> Feature:
    """Признак «оно у нас есть» из самого значения: unknown остаётся unknown."""
    return known(bool(source.value)) if source.known else UNKNOWN


def _fallback(explicit: Feature, source: Feature) -> Feature:
    return explicit if explicit.known else _flag(source)


def _service_count(p: Profile) -> Feature:
    if p.service_count.known:
        return p.service_count
    return known(len(p.services.value)) if p.services.known else UNKNOWN


_DERIVED: dict[str, Callable[[Profile], Feature]] = {
    "niche": lambda p: known(p.niche_key) if p.niche.known else UNKNOWN,
    "has_phone": lambda p: _flag(p.phone),
    "has_address": lambda p: _fallback(p.has_address, p.address),
    "has_hours": lambda p: _fallback(p.has_hours, p.hours),
    "service_count": _service_count,
    # Сколько показателей мы реально знаем — знаем всегда, поэтому known=True.
    "proof_stats_count": lambda p: known(len(p.proof_stats())),
}
