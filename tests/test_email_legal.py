"""Юридический низ письма: подпись, пометка рекламы, отписка (9.8–9.9, 9.30).

Проверяется и обратное: страна вне списка юрисдикций пометки не получает, а
пустая переменная окружения не превращается в выдуманную строку.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import config
import email_gen
import email_legal
import email_lint
import queue_service as qs
from models import LeadEvent, MessageDraft, Session
from test_email_gen import UK_DRAFT, UK_JSON

ADMIN = 1


def lead_of(country="Украина"):
    return SimpleNamespace(id=1, country=country, name="Клініка", city="Київ")


def filled(monkeypatch, **changes):
    """Окружение как на бою после 5.43: подпись, адрес и тег отписки заданы."""
    values = {"SIGNATURE_NAME": "Микола Тобі", "SIGNATURE_COMPANY": "tobisite",
              "POSTAL_ADDRESS": "вулиця Соборна 12, Київ",
              "UNSUBSCRIBE_TAG": "{{unsubscribe_link}}"} | changes
    for name, value in values.items():
        monkeypatch.setattr(config, name, value)


# --- 9.8: кто пишет и откуда --------------------------------------------------

def test_footer_carries_sender_and_address(monkeypatch):
    filled(monkeypatch)
    footer = email_legal.footer(lead_of(), "uk")
    assert footer.startswith("Микола Тобі, tobisite")
    assert "вулиця Соборна 12, Київ" in footer


def test_empty_address_gives_no_line_at_all(monkeypatch):
    filled(monkeypatch, POSTAL_ADDRESS="")
    footer = email_legal.footer(lead_of(), "uk")
    # выдумывать адрес вместо заглушки нельзя: строки просто нет
    assert "вулиця" not in footer
    assert footer.startswith("Микола Тобі, tobisite")


def test_missing_names_every_gap(monkeypatch):
    filled(monkeypatch, POSTAL_ADDRESS="", UNSUBSCRIBE_TAG="")
    gaps = email_legal.missing(lead_of(), "uk")
    assert any("POSTAL_ADDRESS" in g for g in gaps)
    assert any("UNSUBSCRIBE_TAG" in g for g in gaps)


def test_filled_environment_has_no_gaps(monkeypatch):
    filled(monkeypatch)
    assert email_legal.missing(lead_of(), "uk") == []


async def test_linter_warns_about_the_missing_address(monkeypatch, model,
                                                      gap_lead):
    filled(monkeypatch, POSTAL_ADDRESS="")
    model(UK_JSON)
    lead = await gap_lead()

    result = await email_gen.build_email(lead, UK_DRAFT)

    # письмо собирается и в очередь идёт — отправлять его нельзя, и это warn
    assert result.ok, result.reason
    assert any("POSTAL_ADDRESS" in w for w in result.lint.warns)
    assert result.lint.fails == []


# --- 9.9: пометка рекламного характера ---------------------------------------

def test_ad_disclosure_for_the_jurisdictions_that_require_it(monkeypatch):
    filled(monkeypatch)
    assert email_legal.ad_line(lead_of(), "uk") == "Це рекламний лист."
    # страна карточки и язык письма независимы: у лида из США письмо английское
    monkeypatch.setattr(config, "COUNTRY_ISO", {"США": "US"})
    assert email_legal.ad_line(lead_of("США"), "en") \
        == "This email is an advertisement."


def test_no_disclosure_where_it_is_not_required(monkeypatch):
    filled(monkeypatch)
    # Словакия есть в COUNTRIES тестов, но её закон такой строки не требует
    assert email_legal.ad_line(lead_of("Словакия"), "uk") == ""
    assert "рекламний" not in email_legal.footer(lead_of("Словакия"), "uk")


# --- 9.30: отписка ------------------------------------------------------------

def test_opt_out_offers_the_stop_word(monkeypatch):
    filled(monkeypatch)
    for lang in ("uk", "en"):
        line = email_legal.OPT_OUT[lang]
        assert "STOP" in line
        # вопрос в письме ровно один, и это CTA
        assert "?" not in line


def test_unsubscribe_link_stays_out_of_the_first_letter(monkeypatch):
    filled(monkeypatch)
    first = email_legal.footer(lead_of(), "uk")
    later = email_legal.footer(lead_of(), "uk", with_link=True)
    assert "{{unsubscribe_link}}" not in first
    assert "Відписатись: {{unsubscribe_link}}" in later


def test_no_tag_no_line(monkeypatch):
    filled(monkeypatch, UNSUBSCRIBE_TAG="")
    assert "Відписатись" not in email_legal.footer(lead_of(), "uk",
                                                   with_link=True)


async def test_second_letter_carries_the_unsubscribe_line(monkeypatch,
                                                          gap_lead):
    filled(monkeypatch)
    lead = await gap_lead()
    second = email_gen.build_email_2(lead, "klinika.tobisitepreview.com")
    assert "Відписатись: {{unsubscribe_link}}" in second.body


# --- линтер не ломается о константы -------------------------------------------

def test_stop_word_in_the_signature_is_not_a_shout():
    """STOP заглавными — команда отписки, а не крик в прозе письма."""
    slots = {"greeting": "Доброго дня!", "first_line": "Відкрив з телефону.",
             "bridge": "Люди йдуть назад у пошук.",
             "offer": "Я зібрав чернетку вашої головної.",
             "cta": "Скинути подивитись? Відповідайте «так».",
             "signature": "Микола Тобі, tobisite\nЦе рекламний лист.\n"
                          + email_legal.OPT_OUT["uk"]}
    body = "\n\n".join(slots.values())
    result = email_lint.lint(body, lang="uk", slots=slots)
    assert not any("заглавными" in f for f in result.fails)


async def _claimed_card(lead) -> tuple[int, int]:
    """Карточка лида в руках ADMIN: (draft_id, version_id)."""
    queued = await qs.enqueue(lead.id, actor_tg_id=ADMIN, draft_summary=UK_DRAFT)
    assert queued.ok, queued.reason
    # карточка берётся напрямую: claim_next выдал бы старейшую в общей базе
    async with Session() as s, s.begin():
        draft = await s.get(MessageDraft, queued.draft_id)
        draft.status, draft.claimed_by = "claimed", ADMIN
        draft.claimed_at = datetime.now(config.TZ)
        draft.expires_at = draft.claimed_at + timedelta(minutes=qs.LEASE_MINUTES)
        return draft.id, draft.shown_version_id


async def _events_of(lead) -> list[str]:
    async with Session() as s:
        return list(await s.scalars(
            select(LeadEvent.event).where(LeadEvent.lead_id == lead.id)
        ))


async def test_approval_records_the_legal_gap(monkeypatch, model, gap_lead):
    """Одобрение не блокируется, но факт «отправлять нельзя» не теряется."""
    filled(monkeypatch, POSTAL_ADDRESS="")
    model(UK_JSON)
    lead = await gap_lead(status="verified")
    draft_id, version_id = await _claimed_card(lead)

    decision = await qs.approve(draft_id, version_id, ADMIN)

    assert decision.ok
    assert any("POSTAL_ADDRESS" in g for g in decision.legal_fails)
    assert "letter_legal_gap" in await _events_of(lead)


async def test_gap_and_approval_commit_together(monkeypatch, model, gap_lead):
    """Гэп считается в транзакции одобрения: иначе падение теряет сигнал."""
    filled(monkeypatch, POSTAL_ADDRESS="")
    model(UK_JSON)
    lead = await gap_lead(status="verified")
    draft_id, version_id = await _claimed_card(lead)

    def boom(lead, lang):
        raise RuntimeError("проверка гэпа упала")

    monkeypatch.setattr(qs.email_legal, "missing", boom)
    with pytest.raises(RuntimeError):
        await qs.approve(draft_id, version_id, ADMIN)

    # одобрение откатилось вместе с гэпом: карточка всё ещё у дежурного
    async with Session() as s:
        draft = await s.get(MessageDraft, draft_id)
    assert draft.status == "claimed"
    assert "letter_approved" not in await _events_of(lead)
