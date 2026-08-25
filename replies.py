"""Разбор ответа на письмо: категория по правилам (11.21) и стоп-лист по
негативу (11.24).

Словари фраз uk/en плюс порядок приоритетов — и всё. Чистая функция: её можно
проверить сотней примеров, а не «на глаз», она не стоит ни цента за вызов и не
ходит в сеть. Модели здесь делать нечего: «не пишіть більше» — это не задача
на понимание текста, это словарь.

Боевыми данными классификатор пока никто не зовёт: почтового ящика у конвейера
ещё нет, и входящие в него не приходят. Это заготовка под разбор ответов — и та
её часть, которая обязана быть готовой заранее: просьбу не писать нельзя
разбирать через неделю после того, как она пришла.

Порядок проверок и есть приоритет категорий:

    отбивка сервера → просьба не писать → автоответ → отказ → интерес → вопрос

Отбивку почтового сервера нельзя принять за ответ человека. Просьбу не писать
нельзя утопить в вежливом «дякую, ні» — она сильнее любого отказа. Автоответ
«я у відпустці» не значит ни отказа, ни интереса, и решать по нему нечего.
"""
import logging
import re
from dataclasses import dataclass

import config
import queue_service
from models import Lead, Session, log_event, suppress_lead

log = logging.getLogger(__name__)

BOUNCE = "bounce"
STOP = "stop"
AUTO_REPLY = "auto_reply"
NOT_INTERESTED = "not_interested"
INTERESTED = "interested"
QUESTION = "question"
OTHER = "other"

CATEGORIES = (BOUNCE, STOP, AUTO_REPLY, NOT_INTERESTED, INTERESTED, QUESTION,
              OTHER)
# После этих категорий компания закрывается в стоп-листе (11.24): она либо
# попросила не писать, либо сказала «нет». Отбивки здесь нет намеренно — один
# недоставленный лист это ещё не отказ юрлица, и разбирать баунсы будет тот,
# кто их получает.
NEGATIVE = (STOP, NOT_INTERESTED)

LABELS = {
    BOUNCE: "отбивка сервера", STOP: "просьба не писать",
    AUTO_REPLY: "автоответ", NOT_INTERESTED: "отказ", INTERESTED: "интерес",
    QUESTION: "вопрос", OTHER: "непонятно",
}

# Цитата прошлого письма живёт в конце ответа и содержит наши же слова: не
# отрезав её, мы разберём собственное письмо вместо чужого ответа.
QUOTE_PREFIXES = ("from:", "sent:", "to:", "subject:", "від:", "від кого:",
                  "от:", "кому:", "тема:", "-----")
QUOTE_SUFFIXES = ("wrote:", "написав:", "написала:", "написал:", "пише:")

# Отправители, которым отвечает не человек, а почтовый сервер.
BOUNCE_SENDERS = ("mailer daemon", "postmaster")
BOUNCE_PHRASES = (
    "delivery status notification", "undeliverable", "delivery has failed",
    "delivery failed", "mail delivery failed", "could not be delivered",
    "was not delivered", "address not found", "recipient address rejected",
    "user unknown", "no such user", "mailbox unavailable", "mailbox is full",
    "message blocked", "mailer daemon",
    "не вдалося доставити", "лист не доставлено", "адреса не існує",
    "повідомлення не доставлено",
)
STOP_PHRASES = (
    "unsubscribe", "remove me", "take me off", "opt out", "do not contact",
    "don't contact", "do not email", "don't email", "stop contacting",
    "stop writing", "stop messaging", "leave me alone", "never contact",
    "не пишіть", "більше не пишіть", "припиніть писати", "не турбуйте",
    "видаліть мене", "приберіть мене", "відпишіть", "відписатися",
    "відписка", "не надсилайте більше",
)
AUTO_PHRASES = (
    "out of office", "auto reply", "automatic reply", "autoreply",
    "i am currently away", "i'm currently away", "away from my desk",
    "on vacation", "on annual leave", "on parental leave", "will be back on",
    "автовідповідь", "автоматична відповідь", "у відпустці",
    "перебуваю у відпустці", "зараз не в офісі", "повернуся",
)
NOT_INTERESTED_PHRASES = (
    "not interested", "no thanks", "no thank you", "we're all set",
    "we are all set", "not at this time", "not right now", "no need",
    "we already have", "we have a website", "no budget", "maybe later",
    "not looking", "pass on this",
    "не цікаво", "не цікавить", "нам не потрібно", "не потрібно",
    "нема потреби", "немає потреби", "у нас вже є", "дякую ні",
    "поки не актуально", "не актуально",
)
INTERESTED_PHRASES = (
    "how much", "what does it cost", "what's the price", "what is the price",
    "your price", "price list", "send me", "send more", "tell me more",
    "i'm interested", "we're interested", "i am interested", "interested in",
    "sounds good", "let's talk", "let's do it", "call me", "give me a call",
    "schedule a call", "when can we", "yes please", "go ahead", "we'd like",
    "цікаво", "цікавить", "скільки коштує", "яка ціна", "скільки це",
    "розкажіть більше", "зателефонуйте", "передзвоніть", "давайте",
    "готові обговорити", "хочемо", "надішліть", "коли можемо",
)
# Ответ в одно слово: «Stop.» это просьба не писать, «Ні.» — отказ, и оба
# читаются только целиком. Внутри длинного текста то же слово ничего не значит.
SOLO_STOP = ("stop", "стоп", "unsubscribe", "відписка")
SOLO_NO = ("no", "ні", "нет")

_NOISE = re.compile(r"[^\w']+", re.UNICODE)


@dataclass(frozen=True)
class Verdict:
    """Категория ответа и фраза, по которой её выбрали.

    matched — не украшение: без неё разбор непроверяем, и спорный случай не
    разобрать иначе как перечитыванием всего письма.
    """
    category: str = OTHER
    matched: str = ""

    @property
    def negative(self) -> bool:
        return self.category in NEGATIVE


def normalize(text: str) -> str:
    """Текст, по которому ищутся фразы: без регистра, знаков и переносов."""
    text = (text or "").replace("’", "'").replace("ʼ", "'")
    return _NOISE.sub(" ", text.lower()).strip()


def strip_quote(text: str) -> str:
    """Ответ без процитированного письма: цитата идёт последней, режем по ней."""
    kept = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        low = line.lower()
        if line.startswith(">") or low.startswith(QUOTE_PREFIXES) \
                or low.endswith(QUOTE_SUFFIXES):
            break
        kept.append(raw)
    return "\n".join(kept)


def classify(text: str, *, subject: str = "", from_addr: str = "") -> Verdict:
    """Категория ответа. Порядок проверок = приоритет, см. докстринг модуля."""
    body = strip_quote(text)
    haystack = f" {normalize(f'{subject} {body}')} "
    sender = f" {normalize(from_addr)} "

    hit = _first(sender, BOUNCE_SENDERS) or _first(haystack, BOUNCE_PHRASES)
    if hit:
        return Verdict(BOUNCE, hit)
    solo = haystack.strip()
    if solo in SOLO_STOP:
        return Verdict(STOP, solo)
    for phrases, category in ((STOP_PHRASES, STOP),
                              (AUTO_PHRASES, AUTO_REPLY),
                              (NOT_INTERESTED_PHRASES, NOT_INTERESTED),
                              (INTERESTED_PHRASES, INTERESTED)):
        hit = _first(haystack, phrases)
        if hit:
            return Verdict(category, hit)
    if solo in SOLO_NO:
        return Verdict(NOT_INTERESTED, solo)
    if "?" in body:
        return Verdict(QUESTION, "?")
    return Verdict(OTHER)


def _first(haystack: str, phrases) -> str:
    """Первая совпавшая фраза списка. Пусто — не совпала ни одна."""
    for phrase in phrases:
        if f" {phrase} " in haystack:
            return phrase
    return ""


# --- негативный ответ закрывает компанию (11.24) ------------------------------

@dataclass(frozen=True)
class Applied:
    """Что сделал разбор: вердикт, закрытые значения, снятые карточки."""
    verdict: Verdict
    suppressed: bool = False
    added: int = 0
    cancelled: int = 0


async def apply(lead_id: int, text: str, *, subject: str = "",
                from_addr: str = "", source: str = "reply",
                actor_tg_id: int | None = None) -> Applied:
    """Разобрать ответ и закрыть компанию, если ответ негативный (11.24).

    Стоп-лист и снятие карточек с очереди — одной транзакцией: разойдись они,
    письмо успели бы одобрить между двумя коммитами.

    Идемпотентно: закрытие идёт по тем же трём пространствам значений, что и
    проверка, и повторный разбор того же ответа ничего не добавляет. Журнал
    отписок при этом пополняется всегда — доказывать приходится факт и дату
    обращения, а не устройство нашего стоп-листа (models.SuppressionEvent).

    Отказ пишется в журнал событием unsubscribe: своего вида у него нет, а
    придумывать пятый вид ради оттенка «сказали нет» вместо «просили не
    писать» незачем — оттенок остаётся в заметке и в событии лида.
    """
    verdict = classify(text, subject=subject, from_addr=from_addr)
    if not verdict.negative:
        return Applied(verdict)
    actor = actor_tg_id if actor_tg_id is not None else config.ADMIN_TG_ID
    note = f"{LABELS[verdict.category]}: {verdict.matched}"
    async with Session() as s, s.begin():
        lead = await s.get(Lead, lead_id)
        if lead is None:
            log.warning("ответ по лиду %s разобран, но лида нет", lead_id)
            return Applied(verdict)
        added = await suppress_lead(s, lead, event="unsubscribe", source=source,
                                    note=note, actor_tg_id=actor_tg_id)
        cancelled = await queue_service.cancel_drafts(s, lead.id, actor,
                                                      note=note)
        log_event(s, lead.id, "reply_negative", actor,
                  field=verdict.category, new=verdict.matched)
    log.info("лид %s: ответ «%s» — компания закрыта, снято карточек %s",
             lead_id, verdict.category, cancelled)
    return Applied(verdict, suppressed=True, added=added, cancelled=cancelled)
