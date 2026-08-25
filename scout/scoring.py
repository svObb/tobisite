"""Детерминированный скоринг 0–100 (15.13, 15.15). Никакого ИИ.

Смысл шкалы: насколько компания похожа на нашего клиента. Чем хуже её сайт
(или его нет) и чем легче с ней связаться — тем выше балл. Правила — из
рубрики скилла qdif-lead-qualify (закрывает старый пункт 14.24).

Три исхода (15.15): candidate (>= CANDIDATE_MIN) — сразу на модерацию,
review (>= REVIEW_MIN) — спорная, её судьбу решает ИИ-гейт (scout.gate),
reject — в базу не попадает, только строчка в дайджесте.
"""
from dataclasses import dataclass, field
from datetime import datetime

from scout.types import RawBiz

CANDIDATE_MIN = 70
# Спорных ждём 10–20% пачки: их и только их видит платный гейт. Доля уезжает
# в дайджест каждого прогона — по ней калибруются оба порога (15.14).
REVIEW_MIN = 40

STALE_YEARS = 2  # копирайт отстал на столько лет — сайт признаём заброшенным


@dataclass(frozen=True)
class Split:
    """Пачка, разложенная по трём исходам."""
    candidates: list = field(default_factory=list)
    gray: list = field(default_factory=list)
    rejected: list = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.candidates) + len(self.gray) + len(self.rejected)

    @property
    def gray_share(self) -> float:
        return len(self.gray) / self.total if self.total else 0.0


def split(cards: list[RawBiz]) -> Split:
    groups = {"candidate": [], "review": [], "reject": []}
    for card in cards:
        groups[card.verdict].append(card)  # KeyError = карточку не оценили
    return Split(groups["candidate"], groups["review"], groups["reject"])


def score(raw: RawBiz, probe: dict | None) -> RawBiz:
    """Заполняет score/verdict/reasons карточки и возвращает её же."""
    pts = 0
    reasons = []

    if not raw.website:
        pts += 60
        reasons.append("сайта нет")
    elif probe is None or not probe.get("reachable"):
        pts += 55
        reasons.append("сайт не открывается")
    else:
        if not probe.get("https"):
            pts += 25
            reasons.append("без HTTPS")
        if not probe.get("viewport"):
            pts += 25
            reasons.append("нет мобильной версии")
        year = probe.get("copyright_year")
        if year and year <= datetime.now().year - STALE_YEARS:
            pts += 15
            reasons.append(f"копирайт {year}")
        builders = probe.get("builders") or []
        if builders:
            pts += 15
            reasons.append("конструктор: " + ", ".join(builders))
        if (probe.get("size_bytes") or 0) > 3_000_000:
            pts += 10
            reasons.append("страница тяжелее 3 МБ")

    if raw.phone:
        pts += 20
        reasons.append("есть телефон")
    if raw.has_ads:
        # уже платит за клики — идеальный покупатель missed-call и голоса
        pts += 10
        reasons.append("платит за рекламу")

    if not raw.phone and not raw.website:
        # связаться не через что и смотреть нечего — лид мёртвый при любых баллах
        pts = min(pts, 10)
        reasons.append("нет ни телефона, ни сайта")

    raw.score = max(0, min(100, pts))
    raw.reasons = reasons
    if raw.score >= CANDIDATE_MIN:
        raw.verdict = "candidate"
    elif raw.score >= REVIEW_MIN:
        raw.verdict = "review"
    else:
        raw.verdict = "reject"
    return raw
