"""Проверка адреса получателя (9.29): синтаксис, MX домена, ручная ветка.

Сети нет: резолвер подменяется целиком, а автоматическая фикстура _no_dns из
conftest не пускает в интернет ни один тест прогона. Настоящая функция модуля
взята до подмены — именно её проверяет тест кэша.
"""
from datetime import datetime, timedelta

import dns.exception
import dns.resolver
import pytest
from sqlalchemy import select

import config
import email_gen
import email_verify
from handlers_worker import save_contact_value
from models import VERIFY_TTL_DAYS, Contact, Session
from test_email_gen import UK_DRAFT, UK_JSON

REAL_DOMAIN_ACCEPTS = email_verify.domain_accepts_mail
ACTOR = 1


class FakeResolver:
    """Ответы по типу записи: список — есть, исключение — поднимается."""

    def __init__(self, answers):
        self.answers = answers
        self.lifetime = None
        self.asked = []

    async def resolve(self, name, rdtype):
        self.asked.append((name, rdtype))
        answer = self.answers.get(rdtype, dns.resolver.NoAnswer())
        if isinstance(answer, Exception):
            raise answer
        return answer


def resolver(monkeypatch, **answers) -> FakeResolver:
    fake = FakeResolver(answers)
    monkeypatch.setattr(email_verify.dns.asyncresolver, "Resolver",
                        lambda *a, **kw: fake)
    email_verify._domain_cache.clear()
    return fake


async def _email_contact(lead, value="office@example.com"):
    async with Session() as s, s.begin():
        s.add(Contact(lead_id=lead.id, ctype="email", value=value))


async def _contacts_of(lead) -> list[Contact]:
    async with Session() as s:
        return list(await s.scalars(
            select(Contact).where(Contact.lead_id == lead.id)
            .order_by(Contact.id)
        ))


# --- синтаксис ----------------------------------------------------------------

@pytest.mark.parametrize("value", [
    "office@example.com", "a.b-c+tag@sub.example.co.uk", "INFO@Example.COM",
])
async def test_valid_syntax_passes(value):
    status, _ = await email_verify.check(value)
    assert status == "valid"


@pytest.mark.parametrize("value", [
    "", "нет-собаки.com", "office@example", "office@.com", "office@example.c",
    "of fice@example.com", "<office@example.com>",
    "office@example.com, second@example.com",
])
async def test_broken_syntax_is_invalid(value):
    status, note = await email_verify.check(value)
    assert status == "invalid" and "не разбирается" in note


# --- домен --------------------------------------------------------------------

async def test_domain_with_mx_accepts_mail(monkeypatch):
    fake = resolver(monkeypatch, MX=["10 mx.example.com."])
    assert await email_verify._lookup("example.com") is True
    assert fake.asked == [("example.com", "MX")]


async def test_domain_without_mx_falls_back_to_the_address_record(monkeypatch):
    # RFC 5321 §5.1: нет MX — почтовым сервером считается сам домен
    fake = resolver(monkeypatch, MX=dns.resolver.NoAnswer(), A=["1.2.3.4"])
    assert await email_verify._lookup("example.com") is True
    assert [rdtype for _, rdtype in fake.asked] == ["MX", "A"]


async def test_unknown_domain_does_not_accept_mail(monkeypatch):
    resolver(monkeypatch, MX=dns.resolver.NXDOMAIN())
    assert await email_verify._lookup("nope.example") is False


async def test_domain_only_on_ipv6_accepts_mail(monkeypatch):
    fake = resolver(monkeypatch, MX=dns.resolver.NoAnswer(),
                    A=dns.resolver.NoAnswer(), AAAA=["2001:db8::25"])
    assert await email_verify._lookup("example.com") is True
    assert [rdtype for _, rdtype in fake.asked] == ["MX", "A", "AAAA"]


async def test_domain_without_mx_and_without_address_records(monkeypatch):
    resolver(monkeypatch, MX=dns.resolver.NoAnswer(),
             A=dns.resolver.NoAnswer(), AAAA=dns.resolver.NoAnswer())
    assert await email_verify._lookup("nope.example") is False


async def test_silent_dns_gives_unknown_not_a_verdict(monkeypatch):
    resolver(monkeypatch, MX=dns.exception.Timeout())
    assert await email_verify._lookup("example.com") is None


async def test_domain_is_asked_once(monkeypatch):
    fake = resolver(monkeypatch, MX=["10 mx.example.com."])
    monkeypatch.setattr(email_verify, "domain_accepts_mail",
                        REAL_DOMAIN_ACCEPTS)

    assert await email_verify.domain_accepts_mail("example.com") is True
    assert await email_verify.domain_accepts_mail("example.com") is True

    assert len(fake.asked) == 1


# --- контакты и письмо --------------------------------------------------------

async def test_verdict_is_written_to_the_contact(gap_lead):
    lead = await gap_lead()
    await _email_contact(lead)

    async with Session() as s:
        ok, reason = await email_verify.verify_lead(s, lead)

    assert ok and reason == ""
    contact, = await _contacts_of(lead)
    assert contact.verify_status == "valid" and contact.verified_at


async def test_dead_address_stops_the_letter(monkeypatch, model, gap_lead):
    fake = model(UK_JSON)
    lead = await gap_lead()
    await _email_contact(lead, "office@nope.example")

    async def _dead(domain):
        return False

    monkeypatch.setattr(email_verify, "domain_accepts_mail", _dead)
    result = await email_gen.build_email(lead, UK_DRAFT)

    assert result.needs_manual and "почта не проходит проверку" in result.reason
    # за письмо в никуда не платим: модель не зовётся вообще
    assert fake.messages.calls == []
    contact, = await _contacts_of(lead)
    assert contact.verify_status == "invalid"


async def test_silent_dns_also_stops_the_letter(monkeypatch, model, gap_lead):
    fake = model(UK_JSON)
    lead = await gap_lead()
    await _email_contact(lead, "office@quiet.example")

    async def _silent(domain):
        return None

    monkeypatch.setattr(email_verify, "domain_accepts_mail", _silent)
    result = await email_gen.build_email(lead, UK_DRAFT)

    # «не проверили» — не «в порядке»: цель пункта не отправить в никуда
    assert result.needs_manual and "DNS не ответил" in result.reason
    assert fake.messages.calls == []


async def test_broken_address_stops_the_letter(model, gap_lead):
    fake = model(UK_JSON)
    lead = await gap_lead()
    await _email_contact(lead, "office(at)example.com")

    result = await email_gen.build_email(lead, UK_DRAFT)

    assert result.needs_manual and "не разбирается" in result.reason
    assert fake.messages.calls == []


async def test_lead_without_email_still_gets_a_letter(model, gap_lead):
    model(UK_JSON)
    lead = await gap_lead()
    result = await email_gen.build_email(lead, UK_DRAFT)
    # проверять нечего: лида без адреса ловит метрика 7.19, а не эта проверка
    assert result.ok, result.reason


async def test_one_live_address_is_enough(model, gap_lead):
    model(UK_JSON)
    lead = await gap_lead()
    await _email_contact(lead, "office(at)example.com")
    await _email_contact(lead, "office@example.com")

    result = await email_gen.build_email(lead, UK_DRAFT)
    assert result.ok, result.reason


# --- срок годности проверки ---------------------------------------------------

def test_fresh_verification_is_not_repeated():
    contact = Contact(verify_status="valid",
                      verified_at=datetime.now(config.TZ))
    assert not email_verify.stale(contact)


def test_old_verification_is_repeated():
    old = datetime.now(config.TZ) - timedelta(days=VERIFY_TTL_DAYS + 1)
    assert email_verify.stale(Contact(verify_status="valid", verified_at=old))


def test_unknown_is_always_repeated():
    contact = Contact(verify_status="unknown",
                      verified_at=datetime.now(config.TZ))
    assert email_verify.stale(contact)


async def test_edited_address_loses_the_old_verdict(monkeypatch, model,
                                                    gap_lead):
    """Адрес поправили — прежнее «valid» относилось не к нему."""
    model(UK_JSON)
    lead = await gap_lead()
    await _email_contact(lead)
    async with Session() as s:
        await email_verify.verify_lead(s, lead)
    contact, = await _contacts_of(lead)
    assert contact.verify_status == "valid"

    await save_contact_value(lead.id, contact.id, "email", None,
                             "office@nope.example", None, ACTOR)

    contact, = await _contacts_of(lead)
    assert contact.verify_status is None and contact.verified_at is None

    async def _dead(domain):
        return False

    monkeypatch.setattr(email_verify, "domain_accepts_mail", _dead)
    result = await email_gen.build_email(lead, UK_DRAFT)
    # без сброса письмо ушло бы на непроверенный адрес по старому вердикту
    assert result.needs_manual and "почта не проходит проверку" in result.reason


async def test_untouched_value_keeps_the_verdict(gap_lead):
    lead = await gap_lead()
    await _email_contact(lead)
    async with Session() as s:
        await email_verify.verify_lead(s, lead)
    contact, = await _contacts_of(lead)

    await save_contact_value(lead.id, contact.id, "email", None,
                             contact.value, None, ACTOR)

    contact, = await _contacts_of(lead)
    assert contact.verify_status == "valid" and contact.verified_at


async def test_second_letter_does_not_ask_dns_again(monkeypatch, model,
                                                    gap_lead):
    model(UK_JSON, UK_JSON)
    lead = await gap_lead()
    await _email_contact(lead)
    calls = []

    async def _counting(domain):
        calls.append(domain)
        return True

    monkeypatch.setattr(email_verify, "domain_accepts_mail", _counting)
    await email_gen.build_email(lead, UK_DRAFT)
    await email_gen.build_email(lead, UK_DRAFT)

    assert len(calls) == 1  # вердикт лежит на контакте, срок ещё не вышел
