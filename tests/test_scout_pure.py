"""Чистая часть скаута: запрос/разбор Overpass, разбор HTML, скоринг,
разбор аргументов /scout. Ни одного сетевого вызова и ни одной записи в базу.
"""
from datetime import datetime
from decimal import Decimal

from handlers_admin import _parse_scout_args
from scout.gate import GateResult
from scout.ingest import IngestStats
from scout.overpass import MAX_RESULTS, build_query, parse_elements
from scout.runner import _digest
from scout.scoring import score, split
from scout.site_probe import analyze_html, parse_psi
from scout.types import RawBiz


# --- overpass.build_query ----------------------------------------------------

def test_build_query_area_tags_and_limit():
    q = build_query([("amenity", "cafe"), ("amenity", "restaurant")], "Košice")
    assert 'area["name"="Košice"]->.a;' in q
    assert 'nwr["amenity"="cafe"](area.a);' in q
    assert 'nwr["amenity"="restaurant"](area.a);' in q
    assert f"out tags center {MAX_RESULTS};" in q


def test_build_query_strips_quotes_from_city():
    q = build_query([("amenity", "dentist")], ' "Ужгород" ')
    assert 'area["name"="Ужгород"]->.a;' in q


# --- overpass.parse_elements -------------------------------------------------

_PAYLOAD = {"elements": [
    {"type": "node", "id": 11, "tags": {
        "name": " Zubna ambulancia ", "phone": "+421 55 111 22 33",
        "website": "https://zub.example", "addr:street": "Hlavná",
        "addr:housenumber": "12", "addr:city": "Košice",
    }},
    # контактные поля только в fallback-ключах
    {"type": "way", "id": 22, "tags": {
        "name": "Dental X", "contact:phone": "+421555554433",
        "contact:website": "dental-x.example",
    }},
    # без имени — карточка бесполезна, пропускается
    {"type": "node", "id": 33, "tags": {"amenity": "dentist"}},
]}


def test_parse_elements_fields_and_fallbacks():
    cards = parse_elements(_PAYLOAD, city="Prešov")
    assert [c.name for c in cards] == ["Zubna ambulancia", "Dental X"]
    first, second = cards
    assert first.phone == "+421 55 111 22 33"
    assert first.website == "https://zub.example"
    assert first.address == "Hlavná 12"
    assert first.city == "Košice"          # addr:city сильнее города прогона
    assert first.source_url == "https://www.openstreetmap.org/node/11"
    assert second.phone == "+421555554433"  # contact:phone
    assert second.website == "dental-x.example"
    assert second.city == "Prešov"          # города в тегах нет — берём прогонный


# --- site_probe.analyze_html -------------------------------------------------

def test_analyze_html_viewport_and_size():
    html = '<html><meta name="viewport" content="width=device-width"></html>'
    r = analyze_html(html)
    assert r["viewport"] is True
    assert r["size_bytes"] == len(html.encode())
    assert analyze_html("<html><body>hi</body></html>")["viewport"] is False


def test_analyze_html_copyright_takes_max_year():
    r = analyze_html("<footer>Copyright 2018–2021, © 2016 Firma</footer>")
    assert r["copyright_year"] == 2021
    assert analyze_html("<p>без года</p>")["copyright_year"] is None


def test_analyze_html_builder_fingerprints():
    html = ('<script src="https://STATIC.PARASTORAGE.COM/x.js"></script>'
            '<link href="/wp-content/style.css">')
    assert analyze_html(html)["builders"] == ["wix", "wordpress"]


# --- scoring.score -----------------------------------------------------------

def _probe(**kw) -> dict:
    good = {"reachable": True, "https": True, "status": 200, "error": None,
            "viewport": True, "copyright_year": None, "builders": [],
            "size_bytes": 50_000}
    return good | kw


def test_score_no_website_with_phone_is_candidate():
    c = score(RawBiz(name="a", phone="+380501112233"), None)
    assert c.score == 80 and c.verdict == "candidate"
    assert "сайта нет" in c.reasons


def test_score_dead_site_with_phone_is_candidate():
    c = score(RawBiz(name="a", phone="+380501112233",
                     website="dead.example"),
              _probe(reachable=False))
    assert c.score == 75 and c.verdict == "candidate"


def test_score_good_site_is_reject():
    c = score(RawBiz(name="a", phone="+380501112233",
                     website="ok.example"), _probe())
    assert c.score == 20 and c.verdict == "reject"


def test_score_weak_site_sums_defects():
    stale = datetime.now().year - 2
    c = score(RawBiz(name="a", phone="+380501112233", website="w.example"),
              _probe(https=False, viewport=False, copyright_year=stale,
                     builders=["wix"], size_bytes=4_000_000))
    # 25 + 25 + 15 + 15 + 10 + телефон 20 = 110 → потолок 100
    assert c.score == 100 and c.verdict == "candidate"
    assert f"копирайт {stale}" in c.reasons


def test_score_unreachable_with_ads_is_review():
    c = score(RawBiz(name="a", website="w.example", has_ads=True),
              _probe(reachable=False))
    assert c.score == 65 and c.verdict == "review"
    assert "платит за рекламу" in c.reasons


def test_score_no_contact_at_all_is_capped():
    c = score(RawBiz(name="a"), None)
    assert c.score == 10 and c.verdict == "reject"
    assert "нет ни телефона, ни сайта" in c.reasons


# --- /scout: разбор аргументов ----------------------------------------------
# conftest фиксирует COUNTRIES=Украина|UA,Словакия|SK

def test_parse_scout_args_one_word_niche():
    assert _parse_scout_args("Словакия Стоматология Košice") == \
        ("Словакия", "Стоматология", "Košice")


def test_parse_scout_args_two_word_niche_and_spaced_city():
    assert _parse_scout_args("Украина Салон красоты Кривой Рог") == \
        ("Украина", "Салон красоты", "Кривой Рог")


def test_parse_scout_args_rejects_garbage():
    assert _parse_scout_args("") is None
    assert _parse_scout_args("Франция Стоматология Париж") is None  # не наш рынок
    assert _parse_scout_args("Словакия Барбершоп Киев") is None     # нет такой ниши
    assert _parse_scout_args("Словакия Стоматология") is None       # города нет


# --- site_probe.parse_psi (15.12) --------------------------------------------

def _psi_payload(score):
    return {"lighthouseResult": {"categories": {"performance": {"score": score}}}}


def test_parse_psi_score():
    assert parse_psi(_psi_payload(0.23)) == 23
    assert parse_psi(_psi_payload(1)) == 100
    assert parse_psi(_psi_payload(0)) == 0


def test_parse_psi_bad_payloads():
    # Lighthouse вернул null, ошибку или вовсе не тот JSON — это None, не падение
    assert parse_psi(_psi_payload(None)) is None
    assert parse_psi({"error": {"message": "Lighthouse returned error"}}) is None
    assert parse_psi({}) is None
    assert parse_psi({"lighthouseResult": None}) is None


# --- дайджест: спорные и гейт (15.15, 15.18) ---------------------------------

def _parts(candidates=1, gray=1, rejected=2):
    def cards(n, verdict):
        return [RawBiz(name=f"карточка {verdict} {i}", verdict=verdict)
                for i in range(n)]
    return split(cards(candidates, "candidate") + cards(gray, "review")
                 + cards(rejected, "reject"))


def _text(parts, verdict):
    text, _ = _digest("Скаут", parts, verdict, IngestStats(), Decimal("0.02"))
    return text


def test_digest_shows_the_gray_share_and_gate_verdict():
    text = _text(_parts(), GateResult(True, kept=1, dropped=0))
    assert "Найдено карточек: 4" in text
    assert "Отсеяно скорингом: 2" in text
    assert "Спорных: 1 (25%) — гейт оставил 1, отсеял 0" in text


def test_digest_says_when_the_gate_did_not_work():
    text = _text(_parts(), GateResult(False, unseen=1,
                                      reason="не задан ANTHROPIC_API_KEY"))
    assert "гейт не работал: не задан ANTHROPIC_API_KEY" in text


def test_digest_without_gray_cards_says_nothing_about_the_gate():
    assert "Спорных" not in _text(_parts(gray=0), GateResult(True))


def test_digest_shows_the_numbers_of_a_half_failed_gate():
    # решения удавшихся чанков уже применены: без цифр отсев выглядел бы потерей
    text = _text(_parts(gray=6), GateResult(False, kept=2, dropped=2, unseen=2,
                                            reason="чанк 2: модель недоступна"))
    assert "гейт не работал: чанк 2: модель недоступна" in text
    assert "успел оставить 2, отсеять 2, не видел 2" in text
