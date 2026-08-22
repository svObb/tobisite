"""Таблица первых строк и тем писем (Д12 §3): детерминизм и подстановка."""
from types import SimpleNamespace

import config
import phrases

SPAM_WORDS = ["free", "бесплатно", "безкоштовно", "гарантія", "guarantee",
              "urgent", "терміново", "знижка", "discount", "!!!", "$"]


def lead(**kw):
    base = dict(id=1, name="Клініка Здоров'я", language="Украинский",
                gap_type="slow", gap_value="8")
    return SimpleNamespace(**(base | kw))


def test_table_covers_every_type_and_language():
    assert set(phrases.FIRST_LINES) == set(config.GAP_TYPE_LABELS)
    for gap_type, langs in phrases.FIRST_LINES.items():
        assert set(langs) == set(phrases.LANGS), gap_type
        for lang, options in langs.items():
            assert len(options) == 3, (gap_type, lang)


def test_first_line_substitutes_value():
    assert "8 секунд" in phrases.first_line(lead())
    assert "8 seconds" in phrases.first_line(lead(language="Английский"))


def test_first_line_is_deterministic_and_varies_by_lead():
    a = phrases.first_line(lead(id=1))
    assert a == phrases.first_line(lead(id=1))
    variants = {phrases.first_line(lead(id=i)) for i in range(3)}
    assert len(variants) == 3


def test_contact_mismatch_splits_the_pair():
    line = phrases.first_line(lead(
        gap_type="contact_mismatch", gap_value="+380501112233, +380671114455"
    ))
    assert "+380501112233" in line and "+380671114455" in line


def test_no_phrases_without_gap_or_known_language():
    assert phrases.first_line(lead(gap_type=None)) == ""
    assert phrases.first_line(lead(language="Словацкий")) == ""
    assert phrases.lang_of(lead(language="Словацкий")) is None


def test_every_phrase_fills_completely():
    # незакрытый {v} уехал бы прямо в письмо конкретному юрлицу
    values = {"contact_mismatch": "+380501112233, +380671114455"}
    for gap_type in phrases.FIRST_LINES:
        for language in ("Украинский", "Английский"):
            for lead_id in range(3):
                line = phrases.first_line(lead(
                    id=lead_id, gap_type=gap_type, language=language,
                    gap_value=values.get(gap_type, "8"),
                ))
                assert line and "{" not in line, (gap_type, language, lead_id)


def test_subject_templates_are_short_and_clean():
    # 3–6 слов считаются по шаблону: {name} — один факт, укоротить его нечем
    for options in phrases.SUBJECTS.values():
        for template in options:
            assert 3 <= len(template.split()) <= 6, template
            low = template.lower()
            assert not any(word in low for word in SPAM_WORDS), template


def test_subject_is_filled_and_deterministic():
    for language in ("Украинский", "Английский"):
        for lead_id in range(3):
            text = phrases.subject(lead(id=lead_id, language=language))
            assert text and "{" not in text
            assert text == phrases.subject(lead(id=lead_id, language=language))
    assert phrases.subject(lead(language="Словацкий")) == ""
