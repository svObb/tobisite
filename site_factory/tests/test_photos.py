"""Курсор пула: кто из беспредметных кадров кому достался.

Контракты здесь синтетические — правила раздачи кадров одни на всю библиотеку,
и проверять их надо в отрыве от того, какие секции написаны сегодня.
"""
from site_factory.engine import gates, photos, score, slots
from site_factory.engine.profile import Profile

from .conftest import BRAND_SHOP

RECIPE = {"id": "synthetic", "free_defaults": {"uk": {"_common": {}}}}

# Пять кадров разной ширины: по ним видно и номерной порядок, и «самый широкий».
POOL = {
    "photo-2": {"src": "/img/photo-2.webp", "width": 1200, "height": 900},
    "photo-3": {"src": "/img/photo-3.webp", "width": 640, "height": 480},
    "photo-4": {"src": "/img/photo-4.webp", "width": 2400, "height": 1350},
    "photo-5": {"src": "/img/photo-5.webp", "width": 800, "height": 600},
    "photo-6": {"src": "/img/photo-6.webp", "width": 1600, "height": 1200},
}

ONE = {"id": "one", "role": "gallery", "image_pool": "free_photos",
       "image_slots": 1}
WIDEST = {"id": "widest", "role": "gallery", "image_pool": "free_photos",
          "image_slots": 1, "pool_pick": "widest", "pool_min_width": 900}
STRETCHY = {"id": "stretchy", "role": "gallery", "image_pool": "free_photos",
            "image_slots": 5, "pool_min": 3}
PAIR = {"id": "pair", "role": "hero", "image_pool": "free_photos",
        "image_slots": 2, "pool_min_width": 700,
        "requires": {"nonproduct_photo_count": ">=2"}}
POLITE = {"id": "polite", "role": "gallery", "image_pool": "free_photos",
          "image_slots": 4, "pool_min": 2, "pool_leave": 1}
# Порог в requires ниже, чем в prefers: гейт пропускает вариант на остатке, а
# скоринг обязан считать тот же остаток, а не весь пул.
PICKY = {"id": "picky", "role": "gallery", "image_pool": "free_photos",
         "image_slots": 1, "requires": {"nonproduct_photo_count": ">=1"},
         "prefers": {"nonproduct_photo_count": ">=4"}}


def shop(**kw) -> Profile:
    """brand_shop, у которого пул — POOL и ничего кроме."""
    return Profile.from_dict(dict(BRAND_SHOP, images=dict(POOL), **kw))


def test_the_pool_hands_out_frames_in_number_order():
    profile = shop()
    assert photos.picked(ONE, profile) == ["photo-2"]
    assert photos.picked(STRETCHY, profile) == \
        ["photo-2", "photo-3", "photo-4", "photo-5", "photo-6"]


def test_a_taken_frame_leaves_the_pool():
    profile = shop()
    assert photos.remaining(profile, {"photo-2", "photo-4"}) == \
        ["photo-3", "photo-5", "photo-6"]
    assert photos.picked(ONE, profile, {"photo-2"}) == ["photo-3"]


def test_widest_takes_the_broadest_frame_of_the_remainder():
    profile = shop()
    assert photos.picked(WIDEST, profile) == ["photo-4"]
    # самый широкий уже занят — берётся следующий по ширине, а не по номеру
    assert photos.picked(WIDEST, profile, {"photo-4"}) == ["photo-6"]


def test_a_narrow_frame_is_not_offered_at_all():
    """pool_min_width — отсев, а не предпочтение: узкий кадр не годится вовсе."""
    profile = shop()
    assert photos.available(WIDEST, profile) == ["photo-2", "photo-4", "photo-6"]
    assert photos.available(PAIR, profile) == \
        ["photo-2", "photo-4", "photo-5", "photo-6"]


def test_the_gate_counts_the_remainder_not_the_whole_pool():
    """Три кадра, из которых два разобрали, — это один кадр, а не три."""
    profile = shop()
    assert gates.check(STRETCHY, profile).ok
    assert gates.check(STRETCHY, profile, {"photo-2", "photo-3"}).ok
    verdict = gates.check(STRETCHY, profile, {"photo-2", "photo-3", "photo-4"})
    assert not verdict.ok
    assert {reason.kind for reason in verdict.reasons} == {gates.MISSING_IMAGE}


def test_the_remainder_answers_the_requires_of_the_variant():
    """nonproduct_photo_count у варианта с пулом — остаток, а не белый список."""
    profile = shop()
    assert gates.check(PAIR, profile, {"photo-2", "photo-3"}).ok
    verdict = gates.check(PAIR, profile, {"photo-2", "photo-3", "photo-4",
                                          "photo-5"})
    assert not verdict.ok
    assert {reason.field for reason in verdict.reasons} == \
        {"nonproduct_photo_count", "images"}


def test_a_stretchy_variant_lives_below_its_ceiling():
    """image_slots — потолок, pool_min — порог: коллаж рисует и три кадра."""
    profile = shop()
    taken = {"photo-3", "photo-5"}
    assert gates.check(STRETCHY, profile, taken).ok
    filled = slots.build(STRETCHY, profile, RECIPE, taken)
    assert list(filled.images) == ["photo-2", "photo-4", "photo-6"]


def test_the_slots_hand_out_the_same_frames_the_gate_counted():
    profile = shop()
    filled = slots.build(WIDEST, profile, RECIPE, {"photo-4"})
    assert list(filled.images) == ["photo-6"]
    assert filled.images["photo-6"] == POOL["photo-6"]


def test_a_polite_variant_leaves_a_frame_to_the_sections_below():
    """pool_leave: потолок четыре, но последний кадр остаётся тем, кто ниже."""
    profile = shop()
    assert photos.picked(POLITE, profile) == \
        ["photo-2", "photo-3", "photo-4", "photo-5"]
    assert photos.picked(POLITE, profile, {"photo-2"}) == \
        ["photo-3", "photo-4", "photo-5"]


def test_politeness_never_costs_the_variant_its_own_floor():
    """Порог сильнее вежливости: на двух кадрах отдавать нечего."""
    profile = shop()
    taken = {"photo-2", "photo-3", "photo-4"}
    assert photos.remaining(profile, taken) == ["photo-5", "photo-6"]
    assert photos.picked(POLITE, profile, taken) == ["photo-5", "photo-6"]


def test_frames_of_one_width_keep_their_number_order():
    """widest переставляет только по ширине: у равных остаётся номерной порядок."""
    twins = Profile.from_dict(dict(
        BRAND_SHOP,
        images={f"photo-{number}": {"src": f"/img/photo-{number}.webp",
                                    "width": 1600, "height": 1200}
                for number in (2, 3, 4)}))
    assert photos.picked(dict(WIDEST, image_slots=3), twins) == \
        ["photo-2", "photo-3", "photo-4"]


def test_the_scoring_reads_the_same_remainder_the_gate_read():
    """Один признак — одно значение: и гейт, и prefers смотрят на остаток пула."""
    profile = shop()
    recipe = {"id": "synthetic", "niche_affinity": {}}

    assert gates.check(PICKY, profile).ok
    assert score.score(PICKY, profile, recipe).data_fit == 1.0

    taken = {"photo-2", "photo-3"}
    assert gates.check(PICKY, profile, taken).ok
    assert score.score(PICKY, profile, recipe, (), taken).data_fit == 0.0


def test_the_strictest_floor_of_the_library_wins_over_the_loose_ones():
    """Кадр, годный не всем секциям, не годится: побеждает самый строгий порог.

    По этому числу отказывает выкладке ambient_stage — потому оно и считается
    по контрактам, а не пишется в боте второй раз.
    """
    assert photos.strictest_min_width({"one": ONE, "pair": PAIR}) == 700
    assert photos.strictest_min_width({"one": ONE, "pair": PAIR,
                                       "widest": WIDEST}) == 900
    assert photos.strictest_min_width({}) == 0


def test_a_named_picture_never_goes_through_the_pool():
    """logo/hero_bg/portrait/map заняты своей ролью — курсор их не касается."""
    section = {"images": {"hero_bg": POOL["photo-4"]},
               "contract": {"id": "hero_bg_photo", "image_names": ["hero_bg"]}}
    assert photos.claimed(section) == set()
    pooled = {"images": {"photo-2": POOL["photo-2"]},
              "contract": {"id": "one", "image_pool": "free_photos"}}
    assert photos.claimed(pooled) == {"photo-2"}
