"""Линтер письма (Д12 §5): по кейсу на каждое правило fail + эталоны en и uk.

Эталонные письма собираются настоящим email_gen с подменённым клиентом модели:
проверять линтер на тексте, написанном под линтер, бессмысленно — сойтись
должны именно те письма, которые конвейер выдаёт на самом деле.
"""
import email_fewshot
import email_gen
import email_lint
from test_email_gen import EN_DRAFT, EN_JSON, UK_DRAFT, UK_JSON

# Проходное украинское письмо — основа для кейсов: каждый тест портит ровно
# одну вещь и смотрит, что упало именно она.
GOOD_SLOTS = {
    "greeting": "Доброго дня, Олена.",
    "first_line": "Відкрив з телефону — головна вантажилась 8 секунд.",
    "bridge": "На такому екрані людині простіше повернутись у пошук, "
              "ніж дочекатись.",
    "offer": "Я зібрав чернетку вашої головної на ваших реальних даних, "
             "вона відкривається за секунду.",
    "cta": "Скинути подивитись? Відповідайте «так».",
    "signature": "Микола Тобі, tobisite\nвулиця Соборна 12, Київ\n"
                 "Не цікаво — просто відповідайте «ні», більше не напишу.",
}
ANCHORS = ["Олена", "Ужгород", "стоматологія", "Клініка Здоров'я", "8"]
# Правила, которые смотрят внутрь сгенерированных слотов: только они проверяют
# то, что действительно написала модель.
SLOT_RULES = ("стоп-слово", "вежливость", "не просто", "«Как»", "ёлочки",
              "три однородных", "длинное тире", "сокращения", "условное")


def body_of(slots: dict) -> str:
    return "\n\n".join((
        slots["greeting"],
        " ".join((slots["first_line"], slots["bridge"], slots["offer"])),
        slots["cta"],
        slots["signature"],
    ))


def check(lang="uk", **changes):
    slots = GOOD_SLOTS | changes
    return email_lint.lint(body_of(slots), lang=lang, slots=slots,
                           anchors=ANCHORS)


def check_en(**changes):
    slots = _english_slots(**changes)
    return email_lint.lint(body_of(slots), lang="en", slots=slots,
                           anchors=ANCHORS)


def test_reference_letters_pass():
    assert check().fails == []
    assert check_en().fails == []


def test_too_few_words_fails():
    result = check(signature="tobisite", offer="Я зібрав чернетку.")
    assert not result.ok
    assert any("слов в письме" in f for f in result.fails)


def test_too_many_words_fails():
    result = check(offer="Я зібрав чернетку. " + "слова тут " * 40)
    assert any("слов в письме" in f for f in result.fails)


def test_words_near_the_edge_only_warn():
    # 52 слова: вне 60–90, но внутри 50–110 — человеку видно, кнопка жива
    result = check()
    assert result.ok
    assert any("норма 60–90" in w for w in result.warns)


def test_long_sentence_fails():
    result = check(offer="Я " + "дуже " * 25 + "зібрав чернетку.")
    assert any("самое длинное предложение" in f for f in result.fails)


def test_average_sentence_length_only_warns():
    result = check(bridge="Люди йдуть.", offer="Я зібрав чернетку сайту.")
    assert any("средняя длина предложения" in w for w in result.warns)
    assert not any("средняя длина" in f for f in result.fails)


def test_flat_rhythm_only_warns():
    result = check()
    assert any("разброс длин" in w for w in result.warns)
    assert not any("разброс длин" in f for f in result.fails)


def test_exclamation_fails():
    assert any("восклицательный" in f for f in check(cta="Скинути? Так!").fails)


def test_emoji_fails():
    result = check(cta="Скинути подивитись? Відповідайте 👍")
    assert any("эмодзи" in f for f in result.fails)


def test_caps_word_fails():
    result = check(offer="Я зібрав ЧЕРНЕТКУ вашої головної на ваших даних.")
    assert any("заглавными" in f for f in result.fails)


def test_stop_word_in_generated_slot_fails():
    result = check(offer="Я зібрав чернетку вашої головної, це безкоштовно.")
    assert any("стоп-слово" in f for f in result.fails)


def test_stop_word_outside_generated_slots_passes():
    # «гарантія» в названии компании валидное письмо не роняет (Д12 §5)
    assert check(greeting="Доброго дня, Гарантія Плюс.").fails == []


def test_two_questions_fail():
    result = check(bridge="Чи знаєте ви, скільки людей закриває таку сторінку?")
    assert any("вопросительных" in f for f in result.fails)


def test_no_question_fails():
    result = check(cta="Скинути подивитись. Відповідайте «так».")
    assert any("вопросительных" in f for f in result.fails)


def test_conditional_in_offer_fails():
    result = check(offer="Я міг би зібрати чернетку вашої головної сторінки.")
    assert any("условное наклонение" in f for f in result.fails)


def test_link_fails():
    result = check(offer="Я зібрав чернетку, вона тут: klinika.tobisite.com.")
    assert any("ссылка" in f for f in result.fails)


def test_politeness_marker_fails():
    result = check_en(bridge="I hope this email finds you well, and it loads.")
    assert any("вежливость" in f for f in result.fails)


def test_not_just_construction_fails():
    result = check(bridge="Це не просто сторінка, а перше враження про вас.")
    assert any("не просто" in f for f in result.fails)


def test_opener_kak_fails():
    result = check(bridge="Як люди поводяться на такій сторінці, видно одразу.")
    assert any("«Как»" in f for f in result.fails)


def test_guillemets_around_generated_words_fail():
    result = check(offer="Я зібрав «чернетку» вашої головної на ваших даних.")
    assert any("ёлочки" in f for f in result.fails)


def test_three_homogeneous_members_fail():
    result = check(offer="Чернетка швидка, зручна і зрозуміла з телефону.")
    assert any("три однородных" in f for f in result.fails)


def test_em_dash_in_english_fails():
    result = check_en(bridge="Most people go back — before a page loads.")
    assert any("длинное тире" in f for f in result.fails)


def test_english_without_contractions_fails():
    result = check_en(
        offer="I have built a draft of your homepage on your real data.")
    assert any("сокращения" in f for f in result.fails)


def test_few_anchors_only_warn():
    result = check()
    assert result.ok
    assert any("якорей из карточки" in w for w in result.warns)


def test_fewshot_bank_passes_the_slot_rules():
    # банк — единственный образец тона: пример, который сам нарушает правила,
    # учит модель ровно тому, что мы потом отбиваем
    for lang, pairs in email_fewshot.FEWSHOT.items():
        for pair in pairs:
            base = GOOD_SLOTS if lang == "uk" else _english_slots()
            slots = base | pair["output"]
            result = email_lint.lint(body_of(slots), lang=lang, slots=slots,
                                     anchors=ANCHORS)
            broken = [f for f in result.fails
                      if any(rule in f for rule in SLOT_RULES)]
            assert broken == [], (lang, pair["input"]["gap"], broken)


# --- эталоны, собранные конвейером -------------------------------------------

async def test_generated_uk_letter_passes(model, gap_lead):
    model(UK_JSON)
    lead = await gap_lead()
    result = await email_gen.build_email(lead, UK_DRAFT)
    assert result.ok and result.lint.fails == []


async def test_generated_en_letter_passes(model, gap_lead):
    model(EN_JSON)
    lead = await gap_lead(language="Английский")
    result = await email_gen.build_email(lead, EN_DRAFT)
    assert result.ok and result.lint.fails == []
    assert result.body.startswith("Hi Олена,")


def _english_slots(**changes):
    slots = {
        "greeting": "Hi Olena,",
        "first_line": "Opened it on my phone, the homepage took 8 seconds "
                      "to load.",
        "bridge": "Most people go back to the search results before a page "
                  "like that loads.",
        "offer": "I've built a draft of your homepage on your real data, "
                 "and it loads in under a second.",
        "cta": "Want me to send it over? Just reply yes.",
        "signature": "Mykola Tobi, tobisite\n12 Soborna Street, Kyiv\n"
                     "If this is not relevant, just reply no and I won't "
                     "write again.",
    }
    return slots | changes
