"""Очередь одобрения (Д12 §6): лиз, идемпотентность, петля брака, автостоп.

Сервис-функции зовутся напрямую: раннер aiogram здесь ничего не проверял бы,
кроме себя самого. Клиент модели подменяет фикстура model из conftest — сети
в тестах нет, письма наружу не уходят ни при каком исходе.
"""
import ast
import asyncio
import pathlib
import re
from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

import config
import handlers_review as hr
import keyboards as kb
import queue_service as qs
from conftest import TEST_TG_BASE
from models import (
    Lead, LeadEvent, MessageDraft, MessageVersion, Session, Worker,
)
from test_email_gen import UK_DRAFT, UK_JSON

ROOT = pathlib.Path(__file__).resolve().parent.parent
REVIEWER, OTHER = 700_001, 700_002


@pytest.fixture(autouse=True)
def clean_queue():
    """Очередь общая на всю базу: карточка соседнего теста досталась бы этому."""
    asyncio.run(_wipe())
    yield
    asyncio.run(_wipe())


async def _wipe():
    async with Session() as s, s.begin():
        leads = select(Lead.id).where(Lead.worker_id.in_(
            select(Worker.id).where(Worker.tg_id >= TEST_TG_BASE)
        ))
        drafts = list(await s.scalars(
            select(MessageDraft.id).where(MessageDraft.lead_id.in_(leads))
        ))
        if drafts:
            await s.execute(delete(MessageVersion)
                            .where(MessageVersion.draft_id.in_(drafts)))
            await s.execute(delete(MessageDraft)
                            .where(MessageDraft.id.in_(drafts)))


async def _queued(gap_lead, **kw) -> tuple[Lead, int, int]:
    """Лид с карточкой в очереди: (лид, draft_id, version_id)."""
    lead = await gap_lead(status="verified", **kw)
    result = await qs.enqueue(lead.id, actor_tg_id=REVIEWER,
                              draft_summary=UK_DRAFT)
    assert result.ok, result.reason
    async with Session() as s:
        draft = await s.get(MessageDraft, result.draft_id)
    return lead, draft.id, draft.shown_version_id


async def _expire_lease(draft_id: int):
    async with Session() as s, s.begin():
        await s.execute(
            update(MessageDraft).where(MessageDraft.id == draft_id)
            .values(expires_at=datetime.now(config.TZ) - timedelta(minutes=1))
        )


async def _draft(draft_id: int) -> MessageDraft:
    async with Session() as s:
        return await s.get(MessageDraft, draft_id)


async def _events(lead_id: int) -> list[LeadEvent]:
    async with Session() as s:
        return list(await s.scalars(
            select(LeadEvent).where(LeadEvent.lead_id == lead_id)
        ))


# --- постановка в очередь -----------------------------------------------------

async def test_valid_letter_lands_in_the_queue(model, gap_lead):
    model(UK_JSON)
    lead, draft_id, version_id = await _queued(gap_lead)

    draft = await _draft(draft_id)
    assert draft.status == "queued" and draft.lang == "uk"
    assert draft.touch_number == 1 and draft.channel == "email"
    assert draft.claimed_by is None and draft.available_at is None
    async with Session() as s:
        version = await s.get(MessageVersion, version_id)
    assert version.author == "model" and version.model == "claude-sonnet-5"
    assert version.subject and version.body
    assert version.slots_json["bridge"] in version.body
    assert version.prompt_version == "p1"
    assert any(e.event == "letter_queued" for e in await _events(lead.id))


async def test_lint_failure_twice_goes_manual(model, gap_lead):
    # тот же кейс, что в фазе B: одна перегенерация и ручная ветка
    long_offer = '{"bridge": "Коротко.", "offer": "%s."}' % " ".join(["слово"] * 30)
    fake = model(long_offer, long_offer)
    lead = await gap_lead(status="verified")

    result = await qs.enqueue(lead.id, actor_tg_id=REVIEWER,
                              draft_summary=UK_DRAFT)

    assert not result.ok and result.manual
    assert result.reason.startswith("линтер:")
    assert len(fake.messages.calls) == 2
    async with Session() as s:
        draft = await s.get(MessageDraft, result.draft_id)
        versions = list(await s.scalars(
            select(MessageVersion).where(MessageVersion.draft_id == draft.id)
        ))
    assert draft.status == "needs_manual" and draft.shown_version_id is None
    assert versions == []
    assert any(e.event == "letter_manual" for e in await _events(lead.id))


async def test_unverified_lead_is_not_generated(model, gap_lead):
    fake = model(UK_JSON)
    lead = await gap_lead()
    result = await qs.enqueue(lead.id, actor_tg_id=REVIEWER,
                              draft_summary=UK_DRAFT)
    assert not result.ok and "проверенному" in result.reason
    # платный вызов до проверки статуса не делается
    assert fake.messages.calls == []


async def test_second_enqueue_of_the_same_touch_is_refused(model, gap_lead):
    model(UK_JSON, UK_JSON)
    lead, _, _ = await _queued(gap_lead)
    again = await qs.enqueue(lead.id, actor_tg_id=REVIEWER,
                             draft_summary=UK_DRAFT)
    assert not again.ok and "уже есть" in again.reason


async def test_unique_lead_touch_is_a_database_rule(make_lead):
    async with Session() as s, s.begin():
        lead = await make_lead(s)
        s.add(MessageDraft(lead_id=lead.id, touch_number=1, lang="uk"))
    with pytest.raises(IntegrityError):
        async with Session() as s, s.begin():
            s.add(MessageDraft(lead_id=lead.id, touch_number=1, lang="uk"))


# --- клейм и лиз --------------------------------------------------------------

async def test_claim_race_gives_the_card_to_one(model, gap_lead):
    model(UK_JSON)
    _, draft_id, _ = await _queued(gap_lead)

    first = await qs.claim_next(REVIEWER)
    second = await qs.claim_next(OTHER)

    assert first is not None and first.draft.id == draft_id
    assert second is None
    draft = await _draft(draft_id)
    assert draft.status == "claimed" and draft.claimed_by == REVIEWER


async def test_simultaneous_claims_do_not_split_the_card(model, gap_lead):
    """Не «выбрал, потом обновил», а один UPDATE: карточка достаётся одному."""
    model(UK_JSON)
    _, draft_id, _ = await _queued(gap_lead)

    cards = await asyncio.gather(qs.claim_next(REVIEWER), qs.claim_next(OTHER))

    got = [c for c in cards if c is not None]
    assert len(got) == 1 and got[0].draft.id == draft_id
    assert (await _draft(draft_id)).claimed_by in (REVIEWER, OTHER)


async def test_held_card_comes_back_instead_of_a_second_one(model, gap_lead):
    model(UK_JSON, UK_JSON)
    _, first_id, _ = await _queued(gap_lead)
    await _queued(gap_lead)
    await qs.claim_next(REVIEWER)

    held = await qs.current_card(REVIEWER)

    assert held is not None and held.draft.id == first_id
    assert await qs.current_card(OTHER) is None


async def test_expired_lease_returns_to_the_queue(model, gap_lead):
    model(UK_JSON)
    _, draft_id, _ = await _queued(gap_lead)
    await qs.claim_next(REVIEWER)

    assert await qs.claim_next(OTHER) is None
    await _expire_lease(draft_id)
    card = await qs.claim_next(OTHER)

    assert card is not None and card.draft.id == draft_id
    assert card.draft.claimed_by == OTHER
    assert card.draft.expired_leases == 1


async def test_third_expired_lease_escalates(model, gap_lead):
    model(UK_JSON)
    _, draft_id, _ = await _queued(gap_lead)
    cards = []
    for _ in range(qs.MAX_EXPIRED_LEASES + 1):
        cards.append(await qs.claim_next(REVIEWER))
        await _expire_lease(draft_id)

    assert [c.escalate for c in cards] == [False, False, False, True]
    assert cards[-1].draft.expired_leases == qs.MAX_EXPIRED_LEASES


async def test_decision_clears_the_lease_counter(model, gap_lead):
    model(UK_JSON)
    _, draft_id, version_id = await _queued(gap_lead)
    await qs.claim_next(REVIEWER)
    await _expire_lease(draft_id)
    card = await qs.claim_next(REVIEWER)

    await qs.postpone(draft_id, card.version.id, REVIEWER)

    draft = await _draft(draft_id)
    assert draft.expired_leases == 0 and draft.status == "queued"
    assert draft.available_at > datetime.now(config.TZ) + timedelta(hours=1)
    # отложенная карточка на глаза больше не попадается
    assert await qs.claim_next(REVIEWER) is None


async def test_stop_returns_the_card_to_the_queue(model, gap_lead):
    model(UK_JSON)
    _, draft_id, _ = await _queued(gap_lead)
    await qs.claim_next(REVIEWER)

    await qs.release(draft_id, REVIEWER)

    assert (await _draft(draft_id)).status == "queued"
    assert (await qs.claim_next(OTHER)).draft.id == draft_id


# --- решения ------------------------------------------------------------------

async def test_approve_is_the_end_of_the_line(model, gap_lead):
    model(UK_JSON)
    lead, draft_id, version_id = await _queued(gap_lead)
    await qs.claim_next(REVIEWER)

    decision = await qs.approve(draft_id, version_id, REVIEWER)

    assert decision.ok and decision.lead_id == lead.id
    draft = await _draft(draft_id)
    assert draft.status == "approved" and draft.claimed_by is None
    assert any(e.event == "letter_approved" for e in await _events(lead.id))


async def test_old_version_button_does_not_fire(model, gap_lead):
    model(UK_JSON)
    _, draft_id, version_id = await _queued(gap_lead)
    await qs.claim_next(REVIEWER)
    await qs.edit_slot(draft_id, version_id, REVIEWER, "subject", "Нова тема")

    stale = await qs.approve(draft_id, version_id, REVIEWER)

    assert not stale.ok and stale.stale
    assert (await _draft(draft_id)).status == "claimed"


async def test_someone_elses_card_cannot_be_decided(model, gap_lead):
    model(UK_JSON)
    _, draft_id, version_id = await _queued(gap_lead)
    await qs.claim_next(REVIEWER)
    decision = await qs.approve(draft_id, version_id, OTHER)
    assert not decision.ok and decision.stale


async def test_fast_decision_is_marked_not_blocked(model, gap_lead):
    model(UK_JSON)
    lead, draft_id, version_id = await _queued(gap_lead)
    await qs.claim_next(REVIEWER)

    decision = await qs.approve(draft_id, version_id, REVIEWER)

    assert decision.ok and decision.too_fast
    assert any(e.event == "too_fast" for e in await _events(lead.id))


async def test_unhurried_decision_is_clean(model, gap_lead):
    model(UK_JSON)
    lead, draft_id, version_id = await _queued(gap_lead)
    await qs.claim_next(REVIEWER)
    async with Session() as s, s.begin():
        await s.execute(
            update(MessageDraft).where(MessageDraft.id == draft_id)
            .values(claimed_at=datetime.now(config.TZ) - timedelta(minutes=1))
        )

    decision = await qs.approve(draft_id, version_id, REVIEWER)

    assert decision.ok and not decision.too_fast
    assert not any(e.event == "too_fast" for e in await _events(lead.id))


def test_min_read_ms_follows_the_reading_speed():
    # 91 слово ≈ 11 секунд (Д12 §6.2)
    assert 11_000 <= qs.min_read_ms(91) <= 11_600
    assert qs.min_read_ms(0) == 0


# --- брак ---------------------------------------------------------------------

async def test_reject_with_loop_back_reaches_the_finder(model, gap_lead):
    model(UK_JSON)
    lead, draft_id, version_id = await _queued(gap_lead)
    await qs.claim_next(REVIEWER)
    async with Session() as s:
        author = await s.get(Worker, lead.worker_id)

    decision = await qs.reject(draft_id, version_id, REVIEWER,
                               "observation_generic")

    assert decision.ok and decision.notify_tg_id == author.tg_id
    assert (await _draft(draft_id)).status == "rejected"
    events = await _events(lead.id)
    assert any(e.event == "letter_rejected" and e.field == "observation_generic"
               for e in events)


async def test_reject_without_the_loop_notifies_nobody(model, gap_lead):
    model(UK_JSON)
    lead, draft_id, version_id = await _queued(gap_lead)
    await qs.claim_next(REVIEWER)

    decision = await qs.reject(draft_id, version_id, REVIEWER, "too_long")

    assert decision.ok and decision.notify_tg_id is None
    assert any(e.event == "letter_rejected" and e.field == "too_long"
               for e in await _events(lead.id))


async def test_unknown_reject_reason_is_refused(model, gap_lead):
    model(UK_JSON)
    _, draft_id, version_id = await _queued(gap_lead)
    await qs.claim_next(REVIEWER)
    decision = await qs.reject(draft_id, version_id, REVIEWER, "не нравится")
    assert not decision.ok
    assert (await _draft(draft_id)).status == "claimed"


def test_eight_reject_reasons_from_the_source():
    assert [key for key, _ in qs.REJECT_REASONS] == [
        "fact_wrong", "observation_generic", "smells_ai", "too_long",
        "wrong_language", "link_broken", "lead_bad", "other",
    ]
    assert set(qs.LOOP_BACK_REASONS) == set(qs.REJECT_FEEDBACK)


# --- правка по слотам ---------------------------------------------------------

async def test_edit_creates_a_human_version(model, gap_lead):
    model(UK_JSON)
    lead, draft_id, version_id = await _queued(gap_lead)
    await qs.claim_next(REVIEWER)
    new_line = "Відкрив ваш сайт з телефону, головна вантажилась 8 секунд."

    decision = await qs.edit_slot(draft_id, version_id, REVIEWER,
                                  "first_line", new_line)

    assert decision.ok and decision.version_id != version_id
    async with Session() as s:
        version = await s.get(MessageVersion, decision.version_id)
        draft = await s.get(MessageDraft, draft_id)
    assert version.author == "human" and version.edited_slots == ["first_line"]
    assert 0 < version.diff_ratio <= 1
    assert new_line in version.body
    assert version.slots_json["bridge"] in version.body
    assert draft.shown_version_id == version.id and draft.status == "claimed"
    assert any(e.event == "letter_edited" and e.field == "first_line"
               for e in await _events(lead.id))


async def test_edited_text_goes_through_the_linter_again(model, gap_lead):
    model(UK_JSON)
    _, draft_id, version_id = await _queued(gap_lead)
    await qs.claim_next(REVIEWER)

    decision = await qs.edit_slot(draft_id, version_id, REVIEWER, "bridge",
                                  "Чи довго ви це терпите?")

    assert decision.ok
    # правку человека линтер не отменяет, но молчать о ней не имеет права
    assert any("вопросительных" in f for f in decision.lint_fails)


async def test_whole_letter_edit_narrows_the_slots(model, gap_lead):
    model(UK_JSON)
    _, draft_id, version_id = await _queued(gap_lead)
    await qs.claim_next(REVIEWER)
    text = "Доброго дня!\n\nКоротко про сайт. Скинути чернетку?"

    decision = await qs.edit_slot(draft_id, version_id, REVIEWER, "body", text)

    async with Session() as s:
        version = await s.get(MessageVersion, decision.version_id)
    assert version.body == text
    assert [k for k, _ in qs.editable_slots(version)] == list(qs.WHOLE_SLOTS)
    assert qs.slot_text(version, "body") == text


async def test_subject_edit_keeps_the_body(model, gap_lead):
    model(UK_JSON)
    _, draft_id, version_id = await _queued(gap_lead)
    await qs.claim_next(REVIEWER)
    async with Session() as s:
        before = (await s.get(MessageVersion, version_id)).body

    decision = await qs.edit_slot(draft_id, version_id, REVIEWER, "subject",
                                  "Кілька слів про сайт")

    async with Session() as s:
        version = await s.get(MessageVersion, decision.version_id)
    assert version.subject == "Кілька слів про сайт" and version.body == before


async def test_edit_of_an_old_version_is_stale(model, gap_lead):
    model(UK_JSON)
    _, draft_id, version_id = await _queued(gap_lead)
    await qs.claim_next(REVIEWER)
    await qs.edit_slot(draft_id, version_id, REVIEWER, "subject", "Раз")
    again = await qs.edit_slot(draft_id, version_id, REVIEWER, "subject", "Два")
    assert not again.ok and again.stale


# --- автостоп -----------------------------------------------------------------

async def test_stop_status_cancels_the_queue(model, gap_lead):
    model(UK_JSON)
    lead, draft_id, _ = await _queued(gap_lead)

    async with Session() as s, s.begin():
        cancelled = await qs.cancel_drafts(s, lead.id, REVIEWER, "статус sold")

    assert cancelled == 1
    draft = await _draft(draft_id)
    assert draft.status == "cancelled" and draft.claimed_by is None
    assert await qs.claim_next(REVIEWER) is None
    assert any(e.event == "letter_cancelled" for e in await _events(lead.id))


async def test_claimed_card_is_cancelled_too(model, gap_lead):
    model(UK_JSON)
    lead, draft_id, version_id = await _queued(gap_lead)
    await qs.claim_next(REVIEWER)

    async with Session() as s, s.begin():
        assert await qs.cancel_drafts(s, lead.id, REVIEWER) == 1

    assert (await _draft(draft_id)).status == "cancelled"
    # кнопка из уже показанной карточки после автостопа не срабатывает
    assert (await qs.approve(draft_id, version_id, REVIEWER)).stale


async def test_approved_card_survives_the_hook(model, gap_lead):
    model(UK_JSON)
    lead, draft_id, version_id = await _queued(gap_lead)
    await qs.claim_next(REVIEWER)
    await qs.approve(draft_id, version_id, REVIEWER)

    async with Session() as s, s.begin():
        assert await qs.cancel_drafts(s, lead.id, REVIEWER) == 0

    assert (await _draft(draft_id)).status == "approved"


def test_stop_statuses_are_a_list_not_a_condition():
    # replied_interested раздела 7 добавился сюда строкой, как и задумывалось
    assert set(qs.STOP_LEAD_STATUSES) == {"replied", "replied_interested",
                                          "sold", "refused", "rejected"}
    assert all(s in config.STATUS_LABELS for s in qs.STOP_LEAD_STATUSES)


# --- счётчик и СТОП-точка -----------------------------------------------------

async def test_queue_size_counts_what_is_left(model, gap_lead):
    model(UK_JSON, UK_JSON)
    assert await qs.queue_size() == 0
    await _queued(gap_lead)
    assert await qs.queue_size() == 1
    _, draft_id, version_id = await _queued(gap_lead)
    assert await qs.queue_size() == 2

    card = await qs.claim_next(REVIEWER)
    assert (card.position, card.total) == (1, 2)
    await qs.approve(card.draft.id, card.version.id, REVIEWER)
    assert await qs.queue_size() == 1
    # решённая карточка уходит в левую часть счётчика, а не пропадает
    assert (await qs.claim_next(REVIEWER)).position == 2


# --- карточка и кнопки (Д12 §6.2) --------------------------------------------

async def test_card_keeps_the_layout_of_the_source(model, gap_lead):
    model(UK_JSON)
    lead, draft_id, _ = await _queued(gap_lead, gap_note="кнопка не працює")
    card = await qs.claim_next(REVIEWER)

    text = hr.card_text(card)
    head, second = text.split("\n")[:2]

    # счётчик — первым, что видит глаз: очередь без конца учит штамповать
    assert head.startswith(f"📨 <b>1/1</b>  ·  #{lead.id} ")
    assert second.endswith("· uk · касание 1")
    # наблюдение — отдельным блоком, с именем работника и временем съёмки
    async with Session() as s:
        author = await s.get(Worker, lead.worker_id)
    assert f"👤 {author.name}, " in text
    assert "кнопка не працює" in text
    assert text.count(hr.DIVIDER) == 2
    assert "Тема: " in text and text.rstrip().endswith("слов")


def test_approve_sits_alone_in_the_top_row():
    rows = kb.review_card_kb(12, 34).inline_keyboard
    assert [len(r) for r in rows] == [1, 2, 3]
    assert rows[0][0].text == "✅ Одобрить"
    # версия едет в каждой кнопке: после правки старая перестаёт срабатывать
    for row in rows:
        for button in row:
            assert button.callback_data.endswith("12:34")


def test_edit_prompt_survives_the_round_trip():
    label = qs.SLOT_LABELS["first_line"]
    prompt = f"{hr.EDIT_MARK}{label} · #12 v34 m56"
    parsed = hr.EDIT_RE.match(prompt)
    assert parsed.groups() == (label, "12", "34", "56")
    assert hr.SLOT_BY_LABEL[parsed.group(1)] == "first_line"


def test_word_forms_read_like_russian():
    assert [hr._words_word(n) for n in (91, 92, 95, 111, 1)] == [
        "слово", "слова", "слов", "слов", "слово"]


# --- СТОП-точка этапа ---------------------------------------------------------

MAIL_SEND = re.compile(
    r"smtp|sendmail|starttls|send_mail|send_email|send_letter|sendgrid|"
    r"mailgun|postmark|sparkpost|mandrill|mailchimp|sendinblue|brevo|"
    r"instantly|resend|amazonses",
    re.IGNORECASE,
)
SKIP_PARTS = {"tests", "__pycache__", "node_modules", "venv", ".venv",
              ".wrangler"}


def _sources():
    """Весь код репозитория: отправку добавят там, где её никто не ждёт."""
    return (p for p in ROOT.rglob("*.py") if not SKIP_PARTS & set(p.parts))


def _docstrings(tree) -> set[int]:
    """Узлы докстрингов: в них объясняют, почему отправки нет (email_verify)."""
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, holders) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(
                    getattr(first.value, "value", None), str):
                found.add(id(first.value))
    return found


def _mail_words(path) -> list[str]:
    """Имена и строки модуля, похожие на отправку почты. Комментарии не в счёт."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = _docstrings(tree)
    words = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            words += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            words.append(node.module or "")
        elif isinstance(node, ast.Attribute):
            words.append(node.attr)
        elif isinstance(node, ast.Name):
            words.append(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            words.append(node.name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docs:
                words.append(node.value)
    return [w for w in words if MAIL_SEND.search(w)]


def test_no_code_path_sends_a_letter():
    """СТОП-точка этапа: конвейер кончается на approved.

    До подключения Instantly письмо живёт только в базе. Сторож смотрит на код
    всего репозитория, а не на три файла конвейера.
    """
    scanned = {str(p.relative_to(ROOT)) for p in _sources()}
    # обход сломается молча: без этой строчки пустой список тоже «зелёный»
    assert {"queue_service.py", "email_gen.py", "handlers_review.py"} <= scanned
    found = {str(p.relative_to(ROOT)): words
             for p in _sources() if (words := _mail_words(p))}
    assert found == {}, found
