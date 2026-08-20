"""Каталог услуг и рекомендатор (16.A/16.B): схема, фильтры, приоритеты.

Валидатор схемы отрабатывает на импорте services — свалившийся импорт и есть
красный schema-check. База не нужна: recommend читает четыре поля лида,
хватает SimpleNamespace.
"""
from types import SimpleNamespace

import services


def _lead(**kw):
    kw.setdefault("country", "Украина")
    kw.setdefault("niche", "Стоматология")
    kw.setdefault("has_ads", False)
    kw.setdefault("website_url", None)
    return SimpleNamespace(**kw)


def _ids(recs):
    return [r["svc"]["id"] for r in recs]


def test_catalog_is_complete():
    assert len(services.SERVICES) >= 16
    # правило 16.3 опирается на все три статуса — в каталоге должны жить все
    present = {s["status"] for s in services.SERVICES}
    assert present == {"ready", "pilot", "idea"}


def test_idea_never_recommended():
    ideas = {s["id"] for s in services.SERVICES if s["status"] == "idea"}
    assert ideas
    recs = services.recommend(
        _lead(has_ads=True, website_url="https://x.example"), limit=99
    )
    assert not ideas & set(_ids(recs))


def test_has_ads_pushes_missed_call_to_top():
    recs = services.recommend(_lead(has_ads=True))
    assert _ids(recs)[0] == "missed-call-textback"
    assert "уже платит за рекламу" in recs[0]["why"]


def test_no_website_prefers_gbp():
    recs = services.recommend(_lead())
    assert _ids(recs)[0] == "gbp-optimization"
    assert "сайта нет" in recs[0]["why"]


def test_website_services_need_a_website():
    with_site = _ids(services.recommend(
        _lead(website_url="https://x.example"), limit=99
    ))
    without = _ids(services.recommend(_lead(), limit=99))
    assert "site-audit" in with_site and "site-audit" not in without
    assert "extra-pages" in with_site and "extra-pages" not in without


def test_country_filter():
    ua = _ids(services.recommend(_lead(), limit=99))
    sk = _ids(services.recommend(_lead(country="Словакия"), limit=99))
    assert "tg-viber-bot" in ua and "tg-viber-bot" not in sk


def test_niche_filter_and_bonus():
    dent = services.recommend(_lead(), limit=99)
    build = _ids(services.recommend(_lead(niche="Строительство"), limit=99))
    assert "calcom-booking" not in build  # запись — не про стройку
    booking = next(r for r in dent if r["svc"]["id"] == "calcom-booking")
    assert "под нишу «Стоматология»" in booking["why"]


def test_top3_is_top3():
    assert len(services.recommend(_lead(has_ads=True))) == 3
