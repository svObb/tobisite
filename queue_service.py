"""Очередь одобрения писем: постановка, лиз, решения (Д12 §6, 9.19–9.24).

Вся механика очереди живёт здесь и ничего не знает про aiogram: хендлеры
только рисуют карточку и передают идентификаторы. Поэтому гонка за карточкой
проверяется настоящими параллельными запросами, а не через раннер бота.

Карточку выдаёт один атомарный UPDATE с условием «queued либо лиз истёк»:
двое одну и ту же взять не могут даже теоретически, отдельного индекса клейма
для этого не нужно (Д12 §6.5).

Отправки здесь нет и быть не может: конвейер v1 заканчивается статусом
approved. Ни одна функция модуля не отправляет письмо наружу.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from difflib import SequenceMatcher

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

import config
import draft_service
import email_gen
import email_legal
import email_lint
import outbound
import phrases
from models import (
    Contact, Lead, MessageDraft, MessageVersion, Session, Worker, day_start,
    log_event, suppression_hit,
)

log = logging.getLogger(__name__)

# Лиз 10 минут: столько карточка держится за одним человеком, дальше её
# забирает следующий. «Отложить» отодвигает карточку на 2 часа (Д12 §6.5).
LEASE_MINUTES = 10
POSTPONE_HOURS = 2
# Три истёкших лиза подряд по одной карточке — она чем-то мешает, и об этом
# должен узнать админ, а не следующий дежурный.
MAX_EXPIRED_LEASES = 3
# 238 слов в минуту — метаанализ 190 исследований; половина этого времени и
# есть порог, ниже которого решение помечается too_fast (Д12 §6.2). Порог не
# блокирует кнопку: задержка учит ждать и жать.
WORDS_PER_MINUTE = 238
READ_SHARE = 0.5

ACTIVE_STATUSES = ("queued", "claimed")
# Что снимает закрытие компании: одобренное письмо тоже, хотя в очереди его уже
# нет. approved — статус, с которого письмо однажды уедет, и оставить его лиду,
# попросившему не писать, значит оставить письмо в стопке на отправку.
CANCELLABLE_STATUSES = ACTIVE_STATUSES + ("approved",)
SUPPRESSED = "лид в стоп-листе: писать нельзя"
# Статусы лида, на которых цепочка касаний останавливается (решение 5 этапа).
# Именно список: replied_interested добавился сюда строкой, а не правкой
# условия в трёх хендлерах.
STOP_LEAD_STATUSES = ("replied", "replied_interested", "sold", "refused",
                      "rejected")

# Причины брака (Д12 §6.4). Первые две чинят качество вверх по течению:
# работнику, нашедшему лид, уходит просьба переснять наблюдение.
REJECT_REASONS = [
    ("fact_wrong", "Факт неверный"),
    ("observation_generic", "Наблюдение шаблонное"),
    ("smells_ai", "Пахнет ИИ"),
    ("too_long", "Слишком длинно"),
    ("wrong_language", "Не тот язык"),
    ("link_broken", "Ссылка битая"),
    ("lead_bad", "Лид плохой"),
    ("other", "Другое"),
]
REJECT_LABELS = dict(REJECT_REASONS)
LOOP_BACK_REASONS = ("fact_wrong", "observation_generic")
# Что именно написать работнику: без конкретики петля превращается в упрёк.
REJECT_FEEDBACK = {
    "fact_wrong": "факт в письме не сходится с сайтом",
    "observation_generic": "наблюдение шаблонное",
}

# Правка идёт по одному слоту (Д12 §6.3): переписывать 600 символов с телефона
# никто не станет, и правки просто перестанут делать.
SLOTS = [
    ("subject", "📝 Тема"),
    ("first_line", "👤 Первая строка"),
    ("bridge", "🔗 Переход"),
    ("offer", "🎯 Просьба"),
    ("body", "✏️ Всё письмо целиком"),
]
SLOT_LABELS = dict(SLOTS)
# После ручной перезаписи письма целиком разбор по слотам не восстановить:
# остаются тема и текст целиком.
WHOLE_SLOTS = ("subject", "body")


@dataclass(frozen=True)
class Queued:
    """Итог постановки в очередь. reason непустой — карточки в очереди нет."""
    ok: bool
    draft_id: int | None = None
    manual: bool = False
    reason: str = ""


@dataclass
class Card:
    """Всё, что нужно нарисовать одну карточку очереди."""
    draft: MessageDraft
    version: MessageVersion
    lead: Lead
    author: Worker | None
    email: str = ""
    version_no: int = 1
    position: int = 1
    total: int = 1
    escalate: bool = False


@dataclass(frozen=True)
class Decision:
    """Итог решения по карточке. stale — человек нажал устаревшую кнопку."""
    ok: bool
    stale: bool = False
    reason: str = ""
    lead_id: int | None = None
    version_id: int | None = None
    notify_tg_id: int | None = None
    too_fast: bool = False
    lint_fails: list[str] = field(default_factory=list)
    # чего письму не хватает по закону (9.8–9.9): одобрению не мешает,
    # отправке помешает — см. approve()
    legal_fails: list[str] = field(default_factory=list)


def min_read_ms(words: int) -> int:
    """Ниже этого времени решение по карточке помечается too_fast."""
    return int(words / WORDS_PER_MINUTE * 60_000 * READ_SHARE)


def slot_text(version: MessageVersion, slot: str) -> str:
    if slot == "subject":
        return version.subject or ""
    if slot == "body":
        return version.body or ""
    return (version.slots_json or {}).get(slot, "")


def editable_slots(version: MessageVersion) -> list[tuple[str, str]]:
    slots = version.slots_json or {}
    if "body" in slots or not slots:
        return [(k, SLOT_LABELS[k]) for k in WHOLE_SLOTS]
    return list(SLOTS)


# --- постановка в очередь -----------------------------------------------------

async def enqueue(lead_id: int, *, actor_tg_id: int, draft_summary: str = "",
                  touch_number: int = 1) -> Queued:
    """Собрать письмо и положить карточку в очередь.

    Линтер и одна перегенерация при его fail живут внутри email_gen; сюда
    возвращается либо готовое письмо, либо честная причина, по которой его
    придётся писать руками (Д12 §5).

    Описание черновика берётся из собранного черновика лида; ручной ввод
    остаётся фолбэком на лидов, у которых черновика ещё нет.
    """
    async with Session() as s:
        lead = await s.get(Lead, lead_id)
        if lead is None or lead.deleted_at or lead.cancelled_at:
            return Queued(False, reason="лид недоступен")
        # 1.26 и 11.6: экстренный стоп и стоп-лист закрывают вход в очередь
        # целиком. Стоп-лист проверяется и внутри сборки письма 1, но касания
        # 2 и 3 её не проходят вовсе — а запрет писать не про то, какое по
        # счёту письмо
        refusal = await _send_refusal(s, lead)
        if refusal:
            return Queued(False, reason=refusal)
        if lead.status != "verified":
            return Queued(False, reason="письмо собирается только по "
                                        "проверенному лиду")
        if touch_number > config.MAX_TOUCHES_PER_LEAD:
            return Queued(False, reason=f"больше {config.MAX_TOUCHES_PER_LEAD} "
                                        f"касаний одному лиду не пишем")
        busy = await s.scalar(select(MessageDraft.status).where(
            MessageDraft.lead_id == lead_id,
            MessageDraft.touch_number == touch_number,
        ))
        if busy in ("queued", "claimed", "approved"):
            return Queued(False, reason="письмо этого касания уже есть")

    summary = (draft_summary or "").strip() or await draft_service.summary_for(
        lead, phrases.lang_of(lead)
    )
    if not summary:
        return Queued(False, reason="нечего назвать в письме: соберите "
                                    "черновик или опишите его руками")

    result = await email_gen.build_email(lead, summary)
    try:
        return await _store(lead, result, touch_number, actor_tg_id)
    except IntegrityError:
        # UNIQUE(lead_id, touch_number): второй «Собрать письмо» успел первым
        log.info("draft lead=%s touch=%s уже создан параллельно",
                 lead_id, touch_number)
        return Queued(False, reason="письмо этого касания уже есть")


async def _store(lead, result, touch_number, actor_tg_id) -> Queued:
    async with Session() as s, s.begin():
        # между проверками входа и этой транзакцией лежит сетевой вызов модели —
        # секунды, за которые успевает прийти негативный ответ или экстренный
        # стоп. Без повторной проверки карточка встала бы в очередь уже после
        # запрета, и снимать её было бы некому: закрытие компании прошло раньше
        fresh = await s.get(Lead, lead.id)
        refusal = "лид недоступен" if fresh is None else await _send_refusal(
            s, fresh)
        if refusal:
            return Queued(False, reason=refusal)
        draft = await s.scalar(select(MessageDraft).where(
            MessageDraft.lead_id == lead.id,
            MessageDraft.touch_number == touch_number,
        ).with_for_update())
        if draft is None:
            draft = MessageDraft(lead_id=lead.id, touch_number=touch_number,
                                 channel="email", lang=result.lang or "")
            s.add(draft)
        elif draft.status in ("queued", "claimed", "approved"):
            return Queued(False, reason="письмо этого касания уже есть")
        draft.lang = result.lang or draft.lang
        draft.status = "queued" if result.ok else "needs_manual"
        draft.claimed_by = draft.claimed_at = draft.expires_at = None
        draft.available_at = None
        draft.expired_leases = 0
        draft.shown_version_id = None
        await s.flush()
        if not result.ok:
            log_event(s, lead.id, "letter_manual", actor_tg_id,
                      new=result.reason[:200])
            return Queued(False, draft_id=draft.id, manual=True,
                          reason=result.reason)
        version = MessageVersion(
            draft_id=draft.id, author="model", subject=result.subject,
            body=result.body, slots_json=dict(result.slots),
            prompt_version=result.prompt_version, model=result.model,
        )
        s.add(version)
        await s.flush()
        draft.shown_version_id = version.id
        log_event(s, lead.id, "letter_queued", actor_tg_id, new=str(draft.id))
        return Queued(True, draft_id=draft.id)


# --- клейм и лиз --------------------------------------------------------------

# Один UPDATE вместо «выбрать, потом обновить»: между этими двумя запросами
# карточку успевал бы забрать сосед. Условие статуса повторено в WHERE снаружи
# подзапроса намеренно — Postgres перечитывает строку после чужого коммита, и
# именно эта проверка отбивает второго претендента.
CLAIM_SQL = text("""
UPDATE message_drafts SET
    status = 'claimed',
    claimed_by = :tg,
    claimed_at = now(),
    expires_at = now() + (:lease * interval '1 minute'),
    expired_leases = CASE WHEN status = 'claimed'
                          THEN expired_leases + 1 ELSE expired_leases END,
    updated_at = now()
WHERE id = (
    SELECT id FROM message_drafts
    WHERE (status = 'queued' OR (status = 'claimed' AND expires_at < now()))
      AND (available_at IS NULL OR available_at <= now())
    ORDER BY id
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
  AND (status = 'queued' OR (status = 'claimed' AND expires_at < now()))
RETURNING id, expired_leases
""")


async def claim_next(worker_tg_id: int) -> Card | None:
    """Взять следующую карточку под лиз. None — очередь пуста."""
    async with Session() as s, s.begin():
        row = (await s.execute(CLAIM_SQL, {"tg": worker_tg_id,
                                           "lease": LEASE_MINUTES})).first()
        if row is None:
            return None
        draft_id, expired = row
    async with Session() as s:
        card = await load_card(s, draft_id)
    if card is None:
        return None
    card.escalate = expired == MAX_EXPIRED_LEASES
    return card


async def current_card(worker_tg_id: int) -> Card | None:
    """Карточка, которую человек уже держит: второй /queue не забирает вторую."""
    async with Session() as s:
        draft_id = await s.scalar(
            select(MessageDraft.id).where(
                MessageDraft.status == "claimed",
                MessageDraft.claimed_by == worker_tg_id,
                MessageDraft.expires_at > func.now(),
            ).order_by(MessageDraft.id).limit(1)
        )
        if draft_id is None:
            return None
        return await load_card(s, draft_id)


async def load_card(session, draft_id: int) -> Card | None:
    draft = await session.get(MessageDraft, draft_id)
    if draft is None or draft.shown_version_id is None:
        return None
    version = await session.get(MessageVersion, draft.shown_version_id)
    lead = await session.get(Lead, draft.lead_id)
    if version is None or lead is None:
        return None
    author = await session.get(Worker, lead.worker_id)
    version_no = await session.scalar(
        select(func.count()).select_from(MessageVersion).where(
            MessageVersion.draft_id == draft.id,
            MessageVersion.id <= version.id,
        )
    )
    done, left = await _counters(session)
    return Card(
        draft=draft, version=version, lead=lead, author=author,
        email=await _email_of(session, lead.id),
        version_no=version_no or 1, position=done + 1, total=done + max(left, 1),
    )


async def release(draft_id: int, worker_tg_id: int) -> Decision:
    """«Стоп»: карточка возвращается в очередь, лиз снимается сразу."""
    async with Session() as s, s.begin():
        draft = await _claimed(s, draft_id, worker_tg_id)
        if draft is None:
            return Decision(False, stale=True, reason="карточка устарела")
        _unclaim(draft, "queued")
        return Decision(True, lead_id=draft.lead_id)


async def queue_size() -> int:
    async with Session() as s:
        _, left = await _counters(s)
    return left


# --- решения ------------------------------------------------------------------

async def approve(draft_id: int, version_id: int, worker_tg_id: int) -> Decision:
    """Конец конвейера v1: письмо одобрено, но никуда не уходит (СТОП-точка).

    Одобрено не значит «можно отправлять»: юридический низ письма собирается
    из переменных окружения, и пока в них нет физического адреса, письмо не
    проходит по CAN-SPAM (9.8). Кнопку это не блокирует — отправки в конвейере
    всё равно нет, — но факт попадает в историю лида и в ответ дежурному.

    А вот экстренный стоп (1.26) и стоп-лист (11.6) блокируют: «одобрено» — это
    ровно тот статус, с которого письмо однажды уедет, и набирать такие письма
    во время стопа значит готовить залп на момент его снятия. Проверка идёт
    внутри транзакции решения, под тем же замком карточки: дежурный жмёт кнопку
    через минуты после того, как её увидел, и запрет мог прийти как раз в них.
    """
    return await _decide(draft_id, version_id, worker_tg_id, "approved",
                         "letter_approved", legal_gap=True, sendable=True)


async def reject(draft_id: int, version_id: int, worker_tg_id: int,
                 reason: str) -> Decision:
    if reason not in REJECT_LABELS:
        return Decision(False, reason="неизвестная причина брака")
    decision = await _decide(draft_id, version_id, worker_tg_id, "rejected",
                             "letter_rejected", field=reason)
    if not decision.ok or reason not in LOOP_BACK_REASONS:
        return decision
    # петля вверх по течению: без неё тот же работник завтра принесёт такой же
    # мусор, а письмо мы просто удалим (Д12 §6.4)
    async with Session() as s:
        lead = await s.get(Lead, decision.lead_id)
        author = await s.get(Worker, lead.worker_id) if lead else None
    if author is None or author.deleted_at or not author.is_active:
        return decision
    return Decision(True, lead_id=decision.lead_id,
                    version_id=decision.version_id,
                    too_fast=decision.too_fast, notify_tg_id=author.tg_id)


async def postpone(draft_id: int, version_id: int,
                   worker_tg_id: int) -> Decision:
    """«Отложить»: карточка вернётся в очередь через POSTPONE_HOURS часов."""
    async with Session() as s, s.begin():
        draft = await _claimed(s, draft_id, worker_tg_id)
        if draft is None or draft.shown_version_id != version_id:
            return Decision(False, stale=True, reason="карточка устарела")
        _unclaim(draft, "queued")
        draft.available_at = _now() + timedelta(hours=POSTPONE_HOURS)
        log_event(s, draft.lead_id, "letter_postponed", worker_tg_id)
        return Decision(True, lead_id=draft.lead_id)


async def cancel_drafts(session, lead_id: int, actor_tg_id: int,
                        note: str = "") -> int:
    """Автостоп цепочки (решение 5): снять с очереди всё живое по лиду.

    Зовут её всегда об одном — по этой компании писем больше не будет: стоп-лист,
    удаление, отмена, продажа, отказ. Поэтому снимается и одобренное письмо: в
    очереди его уже нет, но именно оно однажды уедет.

    Принимает чужую сессию: отмена обязана попасть в ту же транзакцию, что и
    смена статуса лида, иначе между ними карточку успеют одобрить.
    """
    result = await session.execute(
        update(MessageDraft)
        .where(MessageDraft.lead_id == lead_id,
               MessageDraft.status.in_(CANCELLABLE_STATUSES))
        .values(status="cancelled", claimed_by=None, claimed_at=None,
                expires_at=None)
    )
    if result.rowcount:
        log_event(session, lead_id, "letter_cancelled", actor_tg_id,
                  new=note or None)
        log.info("lead %s: снято с очереди %s карточек", lead_id, result.rowcount)
    return result.rowcount


# --- правка по слотам ---------------------------------------------------------

async def edit_slot(draft_id: int, version_id: int, worker_tg_id: int,
                    slot: str, value: str) -> Decision:
    """Новая версия текста от человека: перелинтовка и новый version_id.

    Старые кнопки после этого не срабатывают — они несут прежний version_id,
    а карточка показывает уже другой текст (Д12 §6.5).
    """
    value = (value or "").strip()
    if slot not in SLOT_LABELS:
        return Decision(False, reason="неизвестный слот")
    if not value:
        return Decision(False, reason="пустой текст")
    async with Session() as s, s.begin():
        draft = await _claimed(s, draft_id, worker_tg_id)
        if draft is None or draft.shown_version_id != version_id:
            return Decision(False, stale=True, reason="карточка устарела")
        old = await s.get(MessageVersion, version_id)
        lead = await s.get(Lead, draft.lead_id)
        if old is None or lead is None:
            return Decision(False, stale=True, reason="карточка устарела")
        before = slot_text(old, slot)
        slots = dict(old.slots_json or {})
        subject = old.subject or ""
        if slot == "subject":
            subject = value
        else:
            slots[slot] = value
        body = _body_of(slots)
        lint = email_lint.lint(body, lang=draft.lang, slots=_lint_slots(slots),
                               anchors=email_gen.anchors_of(lead),
                               subject=subject,
                               legal=email_legal.missing(lead, draft.lang))
        version = MessageVersion(
            draft_id=draft.id, author="human", subject=subject, body=body,
            slots_json=slots, edited_slots=[slot],
            diff_ratio=_diff_ratio(before, value),
            prompt_version=old.prompt_version, model=old.model,
        )
        s.add(version)
        await s.flush()
        draft.shown_version_id = version.id
        draft.expires_at = _now() + timedelta(minutes=LEASE_MINUTES)
        draft.expired_leases = 0
        log_event(s, draft.lead_id, "letter_edited", worker_tg_id, field=slot)
        return Decision(True, lead_id=draft.lead_id, version_id=version.id,
                        lint_fails=list(lint.fails))


# --- внутреннее ---------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(config.TZ)


def _body_of(slots: dict) -> str:
    """Тело версии: ручная перезапись целиком, если она была, иначе слои."""
    return slots.get("body") or email_gen.compose_body(slots)


def _lint_slots(slots: dict) -> dict:
    """Слоты для линтера. После перезаписи письма целиком разбора больше нет:
    текст идёт одним куском прозы, и стоп-лист продаж к нему не применяется —
    это уже слова человека, а не модели (Д12 §5)."""
    if "body" not in slots:
        return slots
    return {"greeting": slots.get("greeting", ""), "first_line": slots["body"]}


def _diff_ratio(before: str, after: str) -> Decimal:
    """Доля изменённого: 0 — не тронули, 1 — переписали заново.

    В банк примеров раздела 7 Д12 идут пары от 0,3 — то есть переписанные
    заметно, а не поправленные на запятую.
    """
    ratio = 1 - SequenceMatcher(None, before or "", after or "").ratio()
    return Decimal(f"{ratio:.3f}")


async def _decide(draft_id, version_id, worker_tg_id, status, event,
                  field=None, legal_gap=False, sendable=False) -> Decision:
    async with Session() as s, s.begin():
        draft = await _claimed(s, draft_id, worker_tg_id)
        if draft is None or draft.shown_version_id != version_id:
            return Decision(False, stale=True, reason="карточка устарела")
        if sendable:
            lead = await s.get(Lead, draft.lead_id)
            refusal = ("лид недоступен" if lead is None
                       else await _send_refusal(s, lead))
            if refusal:
                return Decision(False, reason=refusal)
        version = await s.get(MessageVersion, version_id)
        too_fast = _too_fast(draft, version.body if version else "")
        _unclaim(draft, status)
        log_event(s, draft.lead_id, event, worker_tg_id, field=field)
        if too_fast:
            # не блокируем: задержка учит ждать и жать, а доля too_fast — это
            # метрика штамповки, по ней отстраняют от очереди (Д12 §6.7)
            log_event(s, draft.lead_id, "too_fast", worker_tg_id, field=field)
        gaps = []
        if legal_gap:
            # в той же транзакции, что и одобрение: разойдись они, падение
            # между коммитами оставило бы «одобрено» без отметки о гэпе
            lead = await s.get(Lead, draft.lead_id)
            gaps = email_legal.missing(lead, draft.lang) if lead else []
            if gaps:
                log.warning("письмо лида %s одобрено, но отправлять его "
                            "нельзя: %s", draft.lead_id, "; ".join(gaps))
                log_event(s, draft.lead_id, "letter_legal_gap", worker_tg_id,
                          new="; ".join(gaps)[:200])
        return Decision(True, lead_id=draft.lead_id, version_id=version_id,
                        too_fast=too_fast, legal_fails=gaps)


async def _send_refusal(session, lead) -> str:
    """Почему этому лиду сейчас нельзя готовить письмо. Пусто — можно.

    Одна проверка на оба конца конвейера: и на входе в очередь, и на одобрении.
    Разъехавшись, они дали бы карточке пройти хотя бы одним путём.
    """
    if await outbound.stopped(session):
        return outbound.REASON
    if await suppression_hit(session, lead):
        return SUPPRESSED
    return ""


async def _claimed(session, draft_id: int, worker_tg_id: int):
    """Карточка, которую этот человек действительно держит. None — чужая."""
    draft = await session.get(MessageDraft, draft_id, with_for_update=True)
    if draft is None or draft.status != "claimed":
        return None
    if draft.claimed_by != worker_tg_id:
        return None
    return draft


def _unclaim(draft, status: str):
    draft.status = status
    draft.claimed_by = None
    draft.claimed_at = None
    draft.expires_at = None
    draft.expired_leases = 0


def _too_fast(draft, body: str) -> bool:
    if not draft.claimed_at or not body:
        return False
    elapsed = (_now() - draft.claimed_at).total_seconds() * 1000
    return elapsed < min_read_ms(email_lint.word_count(body))


async def _counters(session) -> tuple[int, int]:
    """(решено сегодня, осталось в очереди) — счётчик «3/47» карточки."""
    done = await session.scalar(
        select(func.count()).select_from(MessageDraft).where(
            MessageDraft.status.in_(("approved", "rejected")),
            MessageDraft.updated_at >= day_start(),
        )
    )
    left = await session.scalar(
        select(func.count()).select_from(MessageDraft).where(
            MessageDraft.status.in_(ACTIVE_STATUSES),
            (MessageDraft.available_at.is_(None))
            | (MessageDraft.available_at <= func.now()),
        )
    )
    return done or 0, left or 0


async def _email_of(session, lead_id: int) -> str:
    value = await session.scalar(
        select(Contact.value).where(
            Contact.lead_id == lead_id, Contact.ctype == "email",
            Contact.deleted_at.is_(None),
        ).order_by(Contact.id).limit(1)
    )
    return value or ""
