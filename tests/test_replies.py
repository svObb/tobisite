"""Классификатор ответов (11.21): правила, два языка, приоритет категорий.

Ни базы, ни сети: разбор — чистая функция, и проверяется он примерами,
похожими на то, что люди действительно пишут в ответ.
"""
import pytest

import replies
from replies import (
    AUTO_REPLY, BOUNCE, INTERESTED, NOT_INTERESTED, OTHER, QUESTION, STOP,
)

EN = [
    ("Thanks, but we're not interested.", NOT_INTERESTED),
    ("No thanks.", NOT_INTERESTED),
    ("We already have a website, but thank you.", NOT_INTERESTED),
    ("Please unsubscribe me from this list.", STOP),
    ("Remove me and do not contact this address again.", STOP),
    ("STOP", STOP),
    ("How much would that cost?", INTERESTED),
    ("Sounds good, call me on Tuesday.", INTERESTED),
    ("Tell me more about the draft.", INTERESTED),
    ("I am out of office until 3 September.", AUTO_REPLY),
    ("Who gave you my address?", QUESTION),
    ("Received.", OTHER),
]

UA = [
    ("Дякую, ні.", NOT_INTERESTED),
    ("Нам це не цікаво.", NOT_INTERESTED),
    ("У нас вже є сайт.", NOT_INTERESTED),
    ("Не пишіть мені більше.", STOP),
    ("Приберіть мене зі списку.", STOP),
    ("Скільки коштує така сторінка?", INTERESTED),
    ("Цікаво, зателефонуйте після обіду.", INTERESTED),
    ("Зараз не в офісі, повернуся 5 вересня.", AUTO_REPLY),
    ("А ви хто?", QUESTION),
    ("Добрий день.", OTHER),
]


@pytest.mark.parametrize("text,category", EN)
def test_english_replies(text, category):
    assert replies.classify(text).category == category


@pytest.mark.parametrize("text,category", UA)
def test_ukrainian_replies(text, category):
    assert replies.classify(text).category == category


# --- приоритет категорий ------------------------------------------------------

def test_a_request_to_stop_beats_a_polite_refusal():
    verdict = replies.classify("Not interested, please unsubscribe me.")
    assert verdict.category == STOP and verdict.negative


def test_an_auto_reply_is_not_a_decision():
    verdict = replies.classify(
        "Automatic reply: I'm on vacation. Not interested in meetings until May."
    )
    assert verdict.category == AUTO_REPLY and not verdict.negative


def test_a_bounce_is_not_a_persons_answer():
    verdict = replies.classify(
        "Your message could not be delivered to office@example.com",
        subject="Undeliverable: draft of your page",
        from_addr="MAILER-DAEMON@mx.example.com",
    )
    assert verdict.category == BOUNCE and not verdict.negative


def test_a_refusal_beats_a_question_mark():
    assert replies.classify("Не цікавить. Хто вам дав мою пошту?").category \
        == NOT_INTERESTED


def test_subject_alone_is_enough():
    assert replies.classify("", subject="Out of office").category == AUTO_REPLY


# --- цитата прошлого письма ---------------------------------------------------

def test_the_quoted_letter_does_not_decide():
    text = ("Please call me tomorrow.\n\n"
            "On Mon, 24 Aug 2026, Stan wrote:\n"
            "> not interested in anything, unsubscribe\n")
    assert replies.classify(text).category == INTERESTED


def test_a_header_block_cuts_the_quote():
    text = "Дякую, ні.\n\nВід: Stan\nТема: чернетка\nЦікаво, скільки коштує?"
    assert replies.classify(text).category == NOT_INTERESTED


# --- мелочи разбора -----------------------------------------------------------

def test_matched_phrase_is_reported():
    assert replies.classify("we're not interested").matched == "not interested"


def test_apostrophes_and_case_do_not_matter():
    assert replies.classify("WE’RE NOT INTERESTED").category == NOT_INTERESTED


def test_empty_reply_is_not_a_verdict():
    assert replies.classify("").category == OTHER
    assert replies.classify(None).category == OTHER


def test_every_category_has_a_label():
    assert set(replies.LABELS) == set(replies.CATEGORIES)
