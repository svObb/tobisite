"""Детерминизм и след решения: тот же лид -> тот же сайт (§3)."""
import hashlib
import json

from site_factory.engine.profile import Profile
from site_factory.engine.render import (load_tokens, preset_for, recipe_id_for,
                                        render, seed_for, track_for)

from .conftest import GENERIC_LIGHT, LAWYER_POOR, LAWYER_RICH

# Домены-однодневки, на которых видно, что пресет меняется вместе с доменом.
DOMAINS = ["alfa.example", "beta.example", "gamma.example", "delta.example",
           "epsilon.example", "zeta.example", "eta.example", "theta.example"]


def test_two_renders_are_byte_identical(buildable_profile):
    first, first_trace = render(buildable_profile)
    second, second_trace = render(buildable_profile)
    assert first == second
    assert json.dumps(first_trace, ensure_ascii=False, sort_keys=True) == \
        json.dumps(second_trace, ensure_ascii=False, sort_keys=True)


def test_seed_comes_from_domain(lawyer_rich):
    _, trace = render(lawyer_rich)
    digest = hashlib.sha256(lawyer_rich.domain_norm.encode()).hexdigest()
    assert trace["seed"] == seed_for(lawyer_rich.domain_norm)
    assert trace["seed"] == int(digest[:8], 16)


def test_domain_changes_preset_and_page():
    """Домен решает пресет и ничьи в скоринге.

    На одних и тех же данных варианты секций совпадают (их выбирает score, а не
    домен), поэтому страниц ровно столько, сколько выпало разных пресетов —
    восемь доменов покрывают все четыре.
    """
    tokens = load_tokens()
    pages, presets, seeds = set(), set(), set()
    for domain in DOMAINS:
        html, trace = render(Profile.from_dict(dict(LAWYER_RICH,
                                                    domain_norm=domain)))
        pages.add(html)
        presets.add(trace["preset"])
        seeds.add(trace["seed"])
    assert len(presets) == len(tokens["presets"])
    assert len(pages) == len(presets)
    assert len(seeds) == len(DOMAINS)


def test_preset_index_is_sha256_not_builtin_hash():
    """Встроенный hash() солится на запуск — тот же лид получил бы другой дизайн."""
    tokens = load_tokens()
    for domain in DOMAINS:
        digest = int(hashlib.sha256(domain.encode()).hexdigest(), 16)
        expected = tokens["presets"][digest % len(tokens["presets"])]
        assert preset_for(domain, tokens)["id"] == expected["id"]


def test_track_and_recipe(lawyer_rich, lawyer_light, generic_light, lawyer_poor):
    assert track_for(lawyer_rich) == "rich"
    assert recipe_id_for(lawyer_rich) == "lawyer_rich"
    assert track_for(lawyer_light) == "light"
    assert recipe_id_for(lawyer_light) == "lawyer_light"
    assert recipe_id_for(generic_light) == "generic_light"
    # photo_count не спрашивали — это не «нет фото», но трек всё равно light.
    assert not lawyer_poor.photo_count.known
    assert track_for(lawyer_poor) == "light"


def test_trace_keeps_rejected_candidates_and_versions(lawyer_rich):
    _, trace = render(lawyer_rich)
    hero = next(role for role in trace["roles"] if role["role"] == "hero")
    losers = [c["variant"] for c in hero["candidates"] if c["variant"] != hero["chosen"]]
    assert losers, "в следе нет отвергнутых альтернатив со score"
    assert hero["chosen"] in [c["variant"] for c in hero["candidates"]]
    assert trace["versions"] == {"engine": 1, "library": load_tokens()["version"],
                                 "recipe": 1}
    assert trace["profile"]["photo_count"] == {"value": 6, "known": True}
    assert trace["profile"]["brand_colors"] == {"value": None, "known": False}


def test_trace_is_json_serialisable(any_profile):
    _, trace = render(any_profile)
    assert json.loads(json.dumps(trace, ensure_ascii=False))["seed"] == trace["seed"]


def test_needs_enrichment_has_no_html(lawyer_poor):
    html, trace = render(lawyer_poor)
    assert html is None
    assert trace["needs_enrichment"]
    assert trace["sections"] == []


def test_language_switches_texts(generic_light):
    html, _ = render(generic_light)
    assert "Skip to content" in html
    assert "Перейти" not in html


def test_recent_variants_lower_the_score(lawyer_rich):
    _, plain = render(lawyer_rich)
    _, penalised = render(lawyer_rich, recent_variants=("hero_photo_left",))
    hero_before = _score_of(plain, "hero", "hero_photo_left")
    hero_after = _score_of(penalised, "hero", "hero_photo_left")
    assert hero_after["novelty"] == 0.0
    assert round(hero_before["total"] - hero_after["total"], 6) == 2.0


def test_unknown_profile_field_stays_unknown():
    profile = Profile.from_dict(dict(LAWYER_POOR))
    assert profile.feature("has_phone") == profile.feature("has_address")
    assert not profile.feature("has_phone").known
    # спрошено и пусто — это уже известно и это false
    empty = Profile.from_dict(dict(GENERIC_LIGHT))
    assert empty.feature("service_count").known
    assert empty.feature("service_count").value == 0


def _score_of(trace, role, variant):
    record = next(r for r in trace["roles"] if r["role"] == role)
    return next(c for c in record["candidates"] if c["variant"] == variant)
