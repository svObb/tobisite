"""Ниша -> ключ -> пул пресетов (tokens/niches.yaml)."""
import pytest

from site_factory.engine import niches
from site_factory.engine.color import PIVOT_LUMINANCE, luminance, srgb
from site_factory.engine.profile import Profile
from site_factory.engine.render import load_tokens, preset_for

from .conftest import BRAND_SHOP

DOMAINS = ["alfa.example", "beta.example", "gamma.example", "delta.example",
           "epsilon.example", "zeta.example", "eta.example", "theta.example"]


def preset_ids():
    return tuple(preset["id"] for preset in load_tokens()["presets"])


def papers():
    return {preset["id"]: preset["palette"]["paper"]
            for preset in load_tokens()["presets"]}


def test_every_pool_id_exists_in_presets():
    ids = set(preset_ids())
    for niche, pool in niches.pools(preset_ids()).items():
        assert set(pool) <= ids, niche
        assert len(set(pool)) == len(pool), f"{niche}: пресет в пуле дважды"


def test_pool_of_a_missing_preset_is_an_error():
    """Опечатка в пуле обязана падать при загрузке, а не на публикации лида."""
    with pytest.raises(ValueError, match="niches.yaml"):
        niches.pools(("corporate-trust",))


def test_aliases_normalise_the_word_of_the_worker():
    assert niches.key_for("Юрист") == "lawyer"
    assert niches.key_for("  адвокат ") == "lawyer"
    assert niches.key_for("Кафе/ресторан") == "cafe"
    assert niches.key_for("Автосервіс") == "auto_service"
    assert niches.key_for("Стоматологія") == "dental"


def test_word_outside_the_table_stays_itself():
    assert niches.key_for("Bakery") == "bakery"
    assert niches.key_for("") is None
    assert niches.key_for(None) is None


def test_unknown_niche_gets_every_preset():
    ids = preset_ids()
    assert niches.pool_for("bakery", ids) == ids
    assert niches.pool_for(None, ids) == ids


def test_every_bot_niche_has_a_pool():
    """Семь ниш бота (config.NICHES) — те, под которые собираются черновики."""
    table = niches.pools(preset_ids())
    for niche in ("Стоматология", "Автосервис", "Кафе/ресторан", "Юрист",
                  "Салон красоты", "Гостиница", "Строительство"):
        assert niches.key_for(niche) in table, niche


def test_logo_pushes_dark_presets_out_of_the_pool():
    """Логотип нарисован под белый фон — на чёрной бумаге он пропадает."""
    paper = papers()
    without = dict(BRAND_SHOP)
    without.pop("brand_colors")
    without["images"] = {"portrait": BRAND_SHOP["images"]["portrait"]}

    dark_seen = False
    for domain in DOMAINS:
        plain = Profile.from_dict(dict(without, domain_norm=domain))
        dark_seen |= _dark(paper[preset_for(plain)["id"]])
        branded = Profile.from_dict(dict(BRAND_SHOP, domain_norm=domain))
        assert not _dark(paper[preset_for(branded)["id"]]), domain
    assert dark_seen, "без логотипа тёмный пресет обязан хоть раз выпасть"


def test_brand_colors_alone_are_enough_to_filter():
    paper = papers()
    for domain in DOMAINS:
        data = dict(BRAND_SHOP, domain_norm=domain,
                    images={"portrait": BRAND_SHOP["images"]["portrait"]})
        assert not _dark(paper[preset_for(Profile.from_dict(data))["id"]])


def test_pool_survives_a_niche_of_only_dark_presets(tmp_path):
    """Пустого выбора не бывает: фильтровать нечего — пул остаётся как был."""
    (tmp_path / "tokens").mkdir()
    (tmp_path / "tokens" / "niches.yaml").write_text(
        "aliases: {}\npools:\n  darkroom: [deep-premium]\n", encoding="utf-8")
    profile = Profile.from_dict(dict(BRAND_SHOP, niche="darkroom"))
    assert preset_for(profile, load_tokens(), tmp_path)["id"] == "deep-premium"


def _dark(paper: str) -> bool:
    return luminance(srgb(paper)) < PIVOT_LUMINANCE
