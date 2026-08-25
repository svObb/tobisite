"""Классификатор ответов (11.21), стоп-лист по негативу (11.24) и запрет
на очередь (11.6).

Разбор — чистая функция без базы и сети, и проверяется он примерами, похожими
на то, что люди действительно пишут в ответ. Обвязка ходит в настоящую базу:
её смысл — в том, что закрытая компания больше никуда не проходит.
"""
import itertools

import pytest
from sqlalchemy import select

import email_gen
import queue_service as qs
import replies
from models import (
    Contact, LeadEvent, MessageDraft, Session, Suppression, SuppressionEvent,
    suppression_hit, suppression_keys,
)
from replies import (
    AUTO_REPLY, BOUNCE, INTERESTED, NOT_INTERESTED, OTHER, QUESTION, STOP,
)
from test_email_gen import UK_DRAFT, UK_JSON

# домен и адрес у каждого лида свои: закрытый в одном тесте ключ иначе
# закрыл бы компанию следующего
_seq = itertools.count(1)

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


# --- негативный ответ закрывает компанию (11.24) ------------------------------

async def _lead(gap_lead):
    """Проверенный лид с почтой — то, что вообще попадает в очередь писем."""
    n = next(_seq)
    lead = await gap_lead(status="verified", domain_norm=f"reply-{n}.example")
    async with Session() as s, s.begin():
        s.add(Contact(lead_id=lead.id, ctype="email",
                      value=f"office@reply-{n}.example"))
    return lead


async def _closed(lead) -> bool:
    async with Session() as s:
        return await suppression_hit(s, lead)


async def _journal(lead_id) -> list[SuppressionEvent]:
    async with Session() as s:
        return list(await s.scalars(
            select(SuppressionEvent).where(SuppressionEvent.lead_id == lead_id)
        ))


async def _events(lead_id) -> list[str]:
    async with Session() as s:
        return list(await s.scalars(
            select(LeadEvent.event).where(LeadEvent.lead_id == lead_id)
        ))


async def test_a_refusal_closes_the_whole_company(gap_lead):
    lead = await _lead(gap_lead)

    done = await replies.apply(lead.id, "Дякую, ні.", source="pytest")

    assert done.verdict.category == NOT_INTERESTED and done.suppressed
    # компания, домен и хеш адреса — все три пространства сразу
    assert done.added == 3 and await _closed(lead)
    assert [e.event for e in await _journal(lead.id)] == ["unsubscribe"]
    assert "reply_negative" in await _events(lead.id)


async def test_a_request_to_stop_closes_it_too(gap_lead):
    lead = await _lead(gap_lead)

    done = await replies.apply(lead.id, "Please unsubscribe me.",
                               source="pytest")

    assert done.verdict.category == STOP and await _closed(lead)


async def test_the_same_answer_twice_adds_nothing(gap_lead):
    lead = await _lead(gap_lead)
    await replies.apply(lead.id, "Not interested.", source="pytest")

    again = await replies.apply(lead.id, "Not interested.", source="pytest")

    assert again.suppressed and again.added == 0
    async with Session() as s:
        pairs = await suppression_keys(s, lead)
        rows = list(await s.scalars(
            select(Suppression.value_norm)
            .where(Suppression.value_norm.in_([v for _, v in pairs]))
        ))
    assert len(rows) == 3
    # в стоп-листе по строке на значение, а в журнале — по обращению
    assert len(await _journal(lead.id)) == 2


@pytest.mark.parametrize("text,category", [
    ("Скільки коштує?", INTERESTED),
    ("Out of office until Monday.", AUTO_REPLY),
    ("Undeliverable: mailbox is full", BOUNCE),
    ("Добрий день.", OTHER),
])
async def test_a_non_negative_answer_closes_nothing(gap_lead, text, category):
    lead = await _lead(gap_lead)

    done = await replies.apply(lead.id, text, source="pytest")

    assert done.verdict.category == category
    assert not done.suppressed and not await _closed(lead)


async def test_a_negative_answer_takes_the_card_off_the_queue(model, gap_lead):
    model(UK_JSON)
    lead = await _lead(gap_lead)
    queued = await qs.enqueue(lead.id, actor_tg_id=1, draft_summary=UK_DRAFT)
    assert queued.ok, queued.reason

    done = await replies.apply(lead.id, "Не пишіть більше.", source="pytest")

    async with Session() as s:
        card = await s.get(MessageDraft, queued.draft_id)
    assert done.cancelled == 1 and card.status == "cancelled"


# --- закрытая компания не проходит в очередь (11.6) ---------------------------

@pytest.mark.parametrize("touch", [1, 2, 3])
async def test_a_closed_company_never_enters_the_queue(model, gap_lead, touch):
    fake = model(UK_JSON)
    lead = await _lead(gap_lead)
    await replies.apply(lead.id, "Дякую, ні.", source="pytest")

    result = await qs.enqueue(lead.id, actor_tg_id=1, draft_summary=UK_DRAFT,
                              touch_number=touch)

    assert not result.ok and "стоп-лист" in result.reason
    # ни карточки, ни вызова модели: отказ стоит до всякой генерации
    assert result.draft_id is None and fake.messages.calls == []
    async with Session() as s:
        cards = list(await s.scalars(
            select(MessageDraft).where(MessageDraft.lead_id == lead.id)
        ))
    assert cards == []


@pytest.mark.parametrize("kind", ["company", "domain", "email_hash"])
async def test_any_one_of_the_three_keys_is_enough(model, gap_lead, kind):
    model(UK_JSON)
    lead = await _lead(gap_lead)
    async with Session() as s, s.begin():
        pairs = dict(await suppression_keys(s, lead))
        s.add(Suppression(kind=kind, value_norm=pairs[kind], reason="pytest",
                          source="pytest"))

    result = await qs.enqueue(lead.id, actor_tg_id=1, draft_summary=UK_DRAFT)

    assert not result.ok and "стоп-лист" in result.reason


async def test_the_letter_builder_refuses_as_well(model, gap_lead):
    model(UK_JSON)
    lead = await _lead(gap_lead)
    await replies.apply(lead.id, "Не пишіть більше.", source="pytest")

    built = await email_gen.build_email(lead, UK_DRAFT)

    assert built.needs_manual and "стоп-лист" in built.reason
