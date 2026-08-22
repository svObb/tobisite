"""Семь правил приёмки наблюдения (Д12 §2) — на чистых функциях, без aiogram."""
import config
import gap_validation as gv


def test_every_type_has_question_and_phrase_slot():
    # разошедшиеся кнопки и вопросы дали бы пустой экран на одном из типов
    assert set(gv.QUESTIONS) == set(config.GAP_TYPE_LABELS)
    for gap_type in config.GAP_TYPE_LABELS:
        assert gv.QUESTIONS[gap_type]
        # у кнопочных типов примеров нет: ответ — одна из трёх кнопок
        examples = gv.EXAMPLES.get(gap_type, [])
        assert bool(examples) == (gap_type not in gv.CHOICE_OPTIONS)
        if examples:
            assert len(examples) == 3
            assert sum(e.startswith("✅") for e in examples) == 2


# --- правило 1: длина -------------------------------------------------------

def test_short_and_long_text_rejected():
    assert gv.check_value("no_prices", "нема цін")[1]
    assert gv.check_value("no_prices", "ц" * (gv.MAX_LEN + 1))[1]


def test_good_text_passes_and_squeezes_spaces():
    value, err = gv.check_value("no_prices", "  шукав ціну   на імплантацію  ")
    assert err is None
    assert value == "шукав ціну на імплантацію"


# --- правило 2: стоп-лист банальностей --------------------------------------

def test_banality_gets_scripted_answer():
    assert gv.check_value("no_prices", "тут все погано зроблено")[1] == gv.BANAL_ANSWER


def test_banality_with_a_date_is_an_observation():
    value, err = gv.check_value("no_prices", "старий сайт, останній пост 2019")
    assert err is None and value


# --- правило 3: связки полей ------------------------------------------------

def test_no_site_forbidden_when_site_is_filled():
    assert gv.type_error("no_site", "https://example.com")
    assert gv.type_error("no_site", None) is None


def test_site_types_need_a_site():
    assert gv.type_error("slow", None)
    assert gv.type_error("slow", "https://example.com") is None
    # no_booking видно и без сайта — в списке NEEDS_SITE его нет
    assert gv.type_error("no_booking", None) is None


def test_unknown_type_rejected():
    assert gv.type_error("site_is_bad", None)


# --- правило 4: анти-копипаста ----------------------------------------------

def test_same_free_text_gives_same_hash():
    a = gv.copypaste_hash("no_prices", "Шукав ціни на  імплантацію", None)
    b = gv.copypaste_hash("no_prices", "шукав ціни на імплантацію", None)
    assert a == b


def test_button_and_number_values_are_not_compared():
    # у no_booking всего три значения, у slow — число: совпадение с прошлой
    # карточкой там означает похожие сайты, а не халтуру
    assert gv.copypaste_hash("no_booking", "тільки телефон", None) is None
    assert gv.copypaste_hash("slow", "8", None) is None
    assert gv.copypaste_hash("slow", "8", "мобільний інтернет 4G") is not None


# --- правило 5: продающие фразы ---------------------------------------------

def test_selling_phrases_rejected():
    assert gv.check_value("no_prices", "пропоную зробити новий сайт")[1] == \
        gv.SELLING_ANSWER
    assert gv.check_note("ми можемо зібрати кращий сайт")[1] == gv.SELLING_ANSWER


# --- правило 6: тайминг -----------------------------------------------------

def test_too_fast_threshold():
    assert gv.too_fast(5)
    assert not gv.too_fast(gv.MIN_OBSERVE_SECONDS)
    assert not gv.too_fast(None)


# --- артефакты по типам -----------------------------------------------------

def test_slow_takes_seconds_in_range():
    assert gv.check_value("slow", "8") == ("8", None)
    assert gv.check_value("slow", "1")[1]
    assert gv.check_value("slow", "61")[1]
    assert gv.check_value("slow", "вісім")[1]


def test_choice_types_accept_only_their_options():
    assert gv.check_value("no_booking", "тільки телефон")[0] == "тільки телефон"
    assert gv.check_value("no_booking", "тільки Viber")[1]
    assert gv.check_value("form_broken", "перезавантажилась")[0]


def test_stale_needs_quote_or_date():
    assert gv.check_value("stale", "на головній графік роботи торішній")[1]
    assert gv.check_value("stale", 'на головній «Графік на 2023 рік»')[1] is None
    assert gv.check_value("stale", "останній запис у новинах 12.03.2022")[1] is None


def test_contact_mismatch_needs_two_values():
    assert gv.check_value("contact_mismatch", "+380501112233, +380671114455")[1] is None
    assert gv.check_value("contact_mismatch", "телефони не збігаються ніде")[1]
    assert gv.check_value("contact_mismatch", "+380501112233, +380671114455, ще")[1]


def test_gap_line_reads_as_a_sentence():
    line = gv.gap_line("slow", "8", "з мобільного інтернету")
    assert line == "Вантажився довго — 8 — з мобільного інтернету"
    assert gv.gap_line(None, None, None) == "не снято"
