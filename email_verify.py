"""Проверка адреса получателя до сборки письма (9.29, цель — баунсы <3%).

Два вопроса, и оба дешёвые: разбирается ли адрес по синтаксису и принимает ли
его домен почту вообще. Второе решается одним DNS-запросом: есть MX — принимает;
MX нет, но есть A/AAAA — принимает тоже (RFC 5321 §5.1, неявный MX).

Дальше мы не идём намеренно. SMTP-хендшейк с чужим сервером — это стук в дверь
с нашего адреса ради проверки, и он портит репутацию домена не хуже баунса;
платные сервисы верификации не подключаем вовсе. Всё, что здесь есть, — синтаксис
и DNS.

Результат живёт на контакте (verify_status/verified_at/verify_note) и держится
VERIFY_TTL_DAYS: домены умирают, и «проверено полгода назад» ничего не значит.
Мёртвый адрес письма не получит — карточка уйдёт в ручную ветку, как при любом
другом сомнении. «Не смогли проверить» тоже не пропускаем: цель пункта —
не отправить в никуда, а не отчитаться о проверке.
"""
import logging
import re
from datetime import datetime, timedelta

import dns.asyncresolver
import dns.exception
import dns.resolver
from sqlalchemy import select

import config
from models import Contact, VERIFY_TTL_DAYS

log = logging.getLogger(__name__)

# Синтаксис: одна @, непустая локальная часть без пробелов и кавычек, домен из
# меток с точкой и TLD хотя бы в две буквы. Полный RFC 5322 здесь не нужен и
# вреден — он пропускает адреса, которые не примет ни один почтовый сервер.
SYNTAX_RE = re.compile(
    r"^[^\s@,;<>\"'\\]{1,64}@(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z]{2,}$",
    re.IGNORECASE,
)

# DNS не отвечает мгновенно и не должен держать хендлер: пять секунд на весь
# запрос вместе с повторами.
DNS_TIMEOUT = 5.0

# Домен переспрашивать на каждом лиде незачем: MX меняются раз в годы, а лидов
# с одного домена бывает несколько. Кэш живёт в процессе и умирает с ним.
DOMAIN_CACHE_MINUTES = 60
_domain_cache: dict[str, tuple[bool | None, datetime]] = {}


async def check(value: str) -> tuple[str, str]:
    """(статус, причина) для одного адреса. Статусы — models.VERIFY_STATUSES."""
    value = (value or "").strip()
    if not SYNTAX_RE.match(value):
        return "invalid", "адрес не разбирается как email"
    domain = value.rpartition("@")[2].lower()
    accepts = await domain_accepts_mail(domain)
    if accepts is True:
        return "valid", ""
    if accepts is False:
        return "invalid", f"домен {domain} почту не принимает"
    return "unknown", f"DNS не ответил про {domain}"


async def domain_accepts_mail(domain: str) -> bool | None:
    """True/False/None — принимает, не принимает, не удалось выяснить."""
    cached = _domain_cache.get(domain)
    if cached is not None and cached[1] > _now():
        return cached[0]
    answer = await _lookup(domain)
    _domain_cache[domain] = (answer,
                             _now() + timedelta(minutes=DOMAIN_CACHE_MINUTES))
    return answer


async def verify_lead(session, lead) -> tuple[bool, str]:
    """(можно писать, причина отказа) по всем адресам лида.

    Адреса нет вовсе — писать некуда, но и проверять нечего: такие лиды ловит
    метрика 7.19, а не эта функция. Адрес есть и он мёртвый — письмо вернётся
    баунсом, и собирать его нельзя.
    """
    contacts = list(await session.scalars(
        select(Contact).where(
            Contact.lead_id == lead.id, Contact.ctype == "email",
            Contact.deleted_at.is_(None),
        ).order_by(Contact.id)
    ))
    if not contacts:
        return True, ""
    ok, reasons = False, []
    for contact in contacts:
        status, note = await _refresh(contact)
        if status == "valid":
            ok = True
            break  # одного живого адреса достаточно, остальные не трогаем
        reasons.append(note or status)
    # статусы проверки пишутся в любом исходе: следующий лид с того же домена
    # и повторная сборка письма второй раз в DNS не пойдут
    await session.commit()
    if ok:
        return True, ""
    return False, "почта не проходит проверку: " + reasons[0]


def stale(contact) -> bool:
    """Проверять заново: не проверяли, не смогли или проверка постарела."""
    if contact.verify_status in (None, "unknown") or contact.verified_at is None:
        return True
    age = datetime.now(contact.verified_at.tzinfo) - contact.verified_at
    return age > timedelta(days=VERIFY_TTL_DAYS)


# --- внутреннее ---------------------------------------------------------------

async def _refresh(contact) -> tuple[str, str]:
    """Статус контакта, при необходимости — свежей проверкой. Пишет в строку."""
    if not stale(contact):
        return contact.verify_status, contact.verify_note or ""
    status, note = await check(contact.value)
    contact.verify_status = status
    contact.verify_note = note or None
    contact.verified_at = _now()
    log.info("почта контакта %s: %s%s", contact.id, status,
             f" ({note})" if note else "")
    return status, note


async def _lookup(domain: str) -> bool | None:
    resolver = dns.asyncresolver.Resolver()
    resolver.lifetime = DNS_TIMEOUT
    try:
        answer = await resolver.resolve(domain, "MX")
        return bool(len(answer))
    except dns.resolver.NoAnswer:
        pass  # домен есть, MX нет — сервером почты считается сам домен
    except (dns.resolver.NXDOMAIN, dns.resolver.NoNameservers):
        return False
    except (dns.exception.DNSException, OSError) as e:
        log.warning("MX для %s не выяснить: %s", domain, e)
        return None
    try:
        return bool(len(await resolver.resolve(domain, "A")))
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN,
            dns.resolver.NoNameservers):
        return False
    except (dns.exception.DNSException, OSError) as e:
        log.warning("A для %s не выяснить: %s", domain, e)
        return None


def _now() -> datetime:
    return datetime.now(config.TZ)
