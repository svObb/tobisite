"""Таблица первых строк и тем писем (Д12 §3): детерминизм и подстановка."""
from types import SimpleNamespace

import config
import phrases

SPAM_WORDS = ["free", "бесплатно", "безкоштовно", "гарантія", "guarantee",
              "urgent", "терміново", "знижка", "discount", "!!!", "$"]


GAP_VALUES = {"contact_mismatch": "+380501112233, +380671114455"}


def lead(**kw):
    base = dict(id=1, name="Клініка Здоров'я", language="Украинский",
                city="Ужгород", niche="Стоматология",
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
        gap_type="contact_mismatch", gap_value=GAP_VALUES["contact_mismatch"]
    ))
    assert "+380501112233" in line and "+380671114455" in line


def test_no_phrases_without_gap_or_known_language():
    assert phrases.first_line(lead(gap_type=None)) == ""
    assert phrases.first_line(lead(language="Словацкий")) == ""


def test_empty_gap_value_kills_the_phrase_instead_of_a_hole():
    # «вантажилась  секунд» с дырой на месте числа уходило в письмо
    assert phrases.first_line(lead(gap_value=None)) == ""
    assert phrases.first_line(lead(gap_value="  ")) == ""
    # у пары контактов дыра возможна и в одной половине
    assert phrases.first_line(lead(gap_type="contact_mismatch",
                                   gap_value="+380501112233")) == ""
    assert phrases.first_line(lead(gap_type="contact_mismatch",
                                   gap_value="+380501112233, ")) == ""
    assert phrases.lang_of(lead(language="Словацкий")) is None


def test_every_phrase_fills_completely():
    # незакрытый {v} уехал бы прямо в письмо конкретному юрлицу
    for gap_type in phrases.FIRST_LINES:
        for language in ("Украинский", "Английский"):
            for lead_id in range(3):
                line = phrases.first_line(lead(
                    id=lead_id, gap_type=gap_type, language=language,
                    gap_value=GAP_VALUES.get(gap_type, "8"),
                ))
                assert line and "{" not in line, (gap_type, language, lead_id)


# --- зачин (Фаза A.1) ---------------------------------------------------------

def test_niche_forms_cover_the_card():
    assert set(phrases.NICHE_FORMS) == set(config.NICHES)
    for niche, forms in phrases.NICHE_FORMS.items():
        assert set(forms) == set(phrases.LANGS), niche


def test_both_openers_glue_with_every_gap_type():
    uk = ("Шукав стоматолога в Ужгороді", "Вибирав стоматолога в Ужгороді")
    en = ("I was looking for a dentist in Ужгород",
          "While looking for a dentist in Ужгород, I")
    seen = set()
    for gap_type in set(phrases.FIRST_LINES) - {"no_site"}:
        for language, openers in (("Украинский", uk), ("Английский", en)):
            for lead_id in range(6):
                line = phrases.first_line(lead(
                    id=lead_id, gap_type=gap_type, language=language,
                    gap_value=GAP_VALUES.get(gap_type, "8"),
                ))
                opener = next((o for o in openers if line.startswith(o)), "")
                assert opener, line
                # у второго английского зачина запятая своя, дублировать нечем
                glue = " " if opener.endswith(", I") else ", "
                tail = line[len(opener):]
                assert tail.startswith(glue), line
                assert not tail[len(glue):].startswith(","), line
                seen.add(opener)
    assert len(seen) == 4


def test_glued_opener_only_meets_verb_tails():
    """«While looking …, I» продолжается глаголом; остальным — первый зачин."""
    glued = "While looking for a dentist in Ужгород, I "
    for gap_type, options in phrases.FIRST_LINES.items():
        if gap_type == "no_site":
            continue
        verbal = phrases.EN_VERB_TAILS.get(gap_type, ())
        for lead_id in range(6):
            line = phrases.first_line(lead(
                id=lead_id, gap_type=gap_type, language="Английский",
                gap_value=GAP_VALUES.get(gap_type, "8"),
            ))
            if not line.startswith(glued):
                continue
            tail = line[len(glued):]
            index = next(i for i, t in enumerate(options["en"])
                         if tail.startswith(t.split("{")[0]))
            assert index in verbal, (gap_type, tail)


def test_verb_tails_start_with_a_verb():
    verbs = ("opened", "checked", "had", "filled", "sent", "couldn't")
    for gap_type, indexes in phrases.EN_VERB_TAILS.items():
        for i in indexes:
            tail = phrases.FIRST_LINES[gap_type]["en"][i]
            assert tail.startswith(verbs), (gap_type, i, tail)
    assert sum(len(v) for v in phrases.EN_VERB_TAILS.values()) == 9


def test_no_site_lines_carry_their_own_opener():
    for lead_id in range(3):
        uk = phrases.first_line(lead(id=lead_id, gap_type="no_site",
                                     gap_value="сторінку у Facebook"))
        assert uk.startswith("Шукав стоматолога в Ужгороді")
        assert "сторінку у Facebook" in uk

        en = phrases.first_line(lead(id=lead_id, gap_type="no_site",
                                     language="Английский",
                                     gap_value="a Facebook page"))
        assert "a dentist in Ужгород" in en and "a Facebook page" in en
        assert "—" not in en  # длинное тире в английском письме запрещено


def test_niche_outside_the_table_leaves_the_tail_alone():
    line = phrases.first_line(lead(id=0, niche="Пекарня"))
    assert line.startswith("Відкрив з телефону")
    no_site = phrases.first_line(lead(id=0, niche="Пекарня", gap_type="no_site",
                                      gap_value="сторінку у Facebook"))
    assert no_site.startswith("Шукав ваш сайт")


def test_locative_matches_spellings_and_falls_back():
    assert phrases.uk_locative("Київ") == "у Києві"
    assert phrases.uk_locative("Киев") == "у Києві"
    assert phrases.uk_locative("ужгород") == "в Ужгороді"
    assert phrases.uk_locative("Кривой Рог") == "у Кривому Розі"
    assert phrases.uk_locative(" Мукачево ") == "у Мукачеві"
    # города вне таблицы падеж не получают: неверный хуже нейтрального
    assert phrases.uk_locative("Пряшів") == "у місті Пряшів"


def test_every_locative_carries_its_preposition():
    for form in phrases.CITY_LOCATIVE.values():
        assert form.split()[0] in ("у", "в"), form


def test_city_outside_the_table_in_the_opener():
    assert phrases.first_line(lead(id=0, city="Пряшів")).startswith(
        "Шукав стоматолога у місті Пряшів, ")
    assert phrases.first_line(
        lead(id=0, city="Пряшів", language="Английский")
    ).startswith("I was looking for a dentist in Пряшів, ")


def test_subject_templates_are_short_and_clean():
    # 3–6 слов считаются по шаблону: {name} — один факт, укоротить его нечем
    for options in phrases.SUBJECTS.values():
        for template in options:
            assert 3 <= len(template.split()) <= 6, template
            low = template.lower()
            assert not any(word in low for word in SPAM_WORDS), template


def test_every_uk_subject_carries_the_company_name():
    # в украинском приветствии имени нет: без названия в теме письму не
    # набрать четырёх якорей карточки
    for template in phrases.SUBJECTS["uk"]:
        assert "{name}" in template, template


def test_subject_is_filled_and_deterministic():
    for language in ("Украинский", "Английский"):
        for lead_id in range(3):
            text = phrases.subject(lead(id=lead_id, language=language))
            assert text and "{" not in text
            assert text == phrases.subject(lead(id=lead_id, language=language))
    assert phrases.subject(lead(language="Словацкий")) == ""
