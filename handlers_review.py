"""Экран очереди одобрения писем (Д12 §6).

Роутер без ролевого фильтра: очередь разбирают обе роли, как общие edit_router
и add_router. Доступ проверяется внутри — по активному работнику или админу.

Вся механика (лиз, версии, решения) живёт в queue_service; здесь только
карточка, кнопки и петля обратной связи работнику. Отправки писем нет: кнопка
подписана «Одобрить», и дальше approved конвейер v1 не идёт.

Идемпотентность держится на version_id в callback_data каждой кнопки: после
правки старые кнопки несут прежнюю версию и честно отвечают «карточка
устарела». В FSM не лежит ничего — даже адрес правки едет в тексте ForceReply.
"""
import logging
import re

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, ForceReply, Message

import email_lint
import gap_validation as gv
import keyboards as kb
import notify
import queue_service as qs
from handlers_worker import esc, fmt_lead, get_contacts, local, safe_edit
from models import Session

log = logging.getLogger(__name__)

router = Router()

STALE = "Карточка устарела — обновил."
EMPTY = "📭 Очередь пуста. Все письма разобраны."
DIVIDER = "─────────────────────"

# Адрес правки едет в тексте ForceReply, а не в FSM: состояние формы теряется
# при деплое, а сообщение в чате — нет (Д12 §6.5).
EDIT_MARK = "✏️ Правка · "
EDIT_RE = re.compile(r"^✏️ Правка · (.+?) · #(\d+) v(\d+) m(\d+)$")
SLOT_BY_LABEL = {label: key for key, label in qs.SLOTS}


def allowed(worker, is_admin: bool) -> bool:
    return bool(is_admin or (worker and worker.is_active and not worker.deleted_at))


# --- вход в очередь -----------------------------------------------------------

@router.message(Command("queue"))
async def open_queue(message: Message, worker, is_admin: bool):
    if not allowed(worker, is_admin):
        await message.answer("Очередь доступна работникам и админу.")
        return
    # уже взятая карточка возвращается той же: иначе второй /queue занимал бы
    # ещё одну и держал обе до конца лиза
    card = (await qs.current_card(message.from_user.id)
            or await qs.claim_next(message.from_user.id))
    if card is None:
        await message.answer(EMPTY)
        return
    await _escalate(message, card)
    await message.answer(card_text(card),
                         reply_markup=kb.review_card_kb(card.draft.id,
                                                        card.version.id))


# --- решения ------------------------------------------------------------------

@router.callback_query(F.data.startswith("qok:"))
async def approve(cb: CallbackQuery, worker, is_admin: bool):
    ids = _ids(cb)
    if ids is None or not allowed(worker, is_admin):
        await cb.answer(STALE, show_alert=True)
        return
    draft_id, version_id = ids
    decision = await qs.approve(draft_id, version_id, cb.from_user.id)
    if not decision.ok:
        # у отказа бывает причина, не связанная с устаревшей кнопкой: при
        # экстренном стопе (1.26) дежурному важно прочитать именно её
        await _stale(cb, draft_id, decision.reason)
        return
    # 9.8: подпись без физического адреса — не письмо для отправки. Отдельным
    # сообщением об этом не пишем: пока переменная не заполнена, оно повторялось
    # бы на каждой карточке. Подробности — в истории лида и в логе.
    await cb.answer("Одобрено · отправлять пока нельзя"
                    if decision.legal_fails else "Одобрено")
    await _next_card(cb)


@router.callback_query(F.data.startswith("qpp:"))
async def postpone(cb: CallbackQuery, worker, is_admin: bool):
    ids = _ids(cb)
    if ids is None or not allowed(worker, is_admin):
        await cb.answer(STALE, show_alert=True)
        return
    decision = await qs.postpone(ids[0], ids[1], cb.from_user.id)
    if not decision.ok:
        await _stale(cb, ids[0])
        return
    await cb.answer(f"Вернётся через {qs.POSTPONE_HOURS} ч")
    await _next_card(cb)


@router.callback_query(F.data.startswith("qst:"))
async def stop(cb: CallbackQuery, worker, is_admin: bool):
    ids = _ids(cb)
    if ids is None or not allowed(worker, is_admin):
        await cb.answer(STALE, show_alert=True)
        return
    await qs.release(ids[0], cb.from_user.id)
    await cb.answer("Вышли из очереди")
    await safe_edit(cb.message, "Очередь закрыта. Вернуться — /queue")


@router.callback_query(F.data.startswith("qld:"))
async def show_lead(cb: CallbackQuery, worker, is_admin: bool):
    """«🔎 Лид» — карточка лида отдельным сообщением: очередь остаётся на месте."""
    ids = _ids(cb)
    if ids is None or not allowed(worker, is_admin):
        await cb.answer(STALE, show_alert=True)
        return
    async with Session() as s:
        card = await qs.load_card(s, ids[0])
        contacts = await get_contacts(s, card.lead.id) if card else []
    if card is None:
        await cb.answer(STALE, show_alert=True)
        return
    await cb.answer()
    await cb.message.answer(fmt_lead(card.lead, contacts))


# --- брак ---------------------------------------------------------------------

@router.callback_query(F.data.startswith("qrj:"))
async def reject_menu(cb: CallbackQuery, worker, is_admin: bool):
    ids = _ids(cb)
    if ids is None or not allowed(worker, is_admin):
        await cb.answer(STALE, show_alert=True)
        return
    await cb.answer()
    await safe_edit(cb.message, "Что не так с письмом?",
                    kb.review_reasons_kb(ids[0], ids[1], qs.REJECT_REASONS))


@router.callback_query(F.data.startswith("qrs:"))
async def reject_reason(cb: CallbackQuery, worker, is_admin: bool):
    ids = _ids(cb)
    parts = (cb.data or "").split(":")
    if ids is None or len(parts) != 4 or not allowed(worker, is_admin):
        await cb.answer(STALE, show_alert=True)
        return
    draft_id, version_id = ids
    async with Session() as s:
        card = await qs.load_card(s, draft_id)
    decision = await qs.reject(draft_id, version_id, cb.from_user.id, parts[3])
    if not decision.ok:
        await _stale(cb, draft_id)
        return
    if decision.notify_tg_id and card is not None:
        await _feedback(cb, card, parts[3], decision.notify_tg_id)
    await cb.answer("Брак записан")
    await _next_card(cb)


async def _feedback(cb: CallbackQuery, card, reason: str, tg_id: int):
    """Петля вверх по течению: работник узнаёт, что именно не так (Д12 §6.4)."""
    text = (f"🚫 #{card.lead.id} {esc(card.lead.name)}: письмо отклонено — "
            f"{esc(qs.REJECT_FEEDBACK[reason])}.\n"
            f"Что конкретно ты увидел на сайте?")
    try:
        await cb.bot.send_message(tg_id, text,
                                  reply_markup=kb.regap_kb(card.lead.id))
    except Exception as e:
        log.warning("петля обратной связи не дошла до tg=%s: %s", tg_id, e)


# --- правка по слотам ---------------------------------------------------------

@router.callback_query(F.data.startswith("qed:"))
async def edit_menu(cb: CallbackQuery, worker, is_admin: bool):
    ids = _ids(cb)
    if ids is None or not allowed(worker, is_admin):
        await cb.answer(STALE, show_alert=True)
        return
    async with Session() as s:
        card = await qs.load_card(s, ids[0])
    if card is None or card.version.id != ids[1]:
        await _stale(cb, ids[0])
        return
    await cb.answer()
    await safe_edit(cb.message, "Что поправить?",
                    kb.review_slots_kb(ids[0], ids[1],
                                       qs.editable_slots(card.version)))


@router.callback_query(F.data.startswith("qbk:"))
async def edit_back(cb: CallbackQuery, worker, is_admin: bool):
    ids = _ids(cb)
    if ids is None or not allowed(worker, is_admin):
        await cb.answer(STALE, show_alert=True)
        return
    await cb.answer()
    await _redraw(cb, ids[0])


@router.callback_query(F.data.startswith("qsl:"))
async def slot_pick(cb: CallbackQuery, worker, is_admin: bool):
    """Два сообщения: голый текст слота и ForceReply (Д12 §6.3).

    Голый — чтобы long-press → «Копировать» брал ровно его, без обвязки.
    """
    ids = _ids(cb)
    parts = (cb.data or "").split(":")
    if ids is None or len(parts) != 4 or not allowed(worker, is_admin):
        await cb.answer(STALE, show_alert=True)
        return
    slot = parts[3]
    async with Session() as s:
        card = await qs.load_card(s, ids[0])
    if card is None or card.version.id != ids[1] or slot not in qs.SLOT_LABELS:
        await _stale(cb, ids[0])
        return
    await cb.answer()
    current = qs.slot_text(card.version, slot)
    await cb.message.answer(current or "(пусто)", parse_mode=None)
    await cb.message.answer(
        f"{EDIT_MARK}{qs.SLOT_LABELS[slot]} · #{ids[0]} v{ids[1]} "
        f"m{cb.message.message_id}",
        reply_markup=ForceReply(input_field_placeholder="Новый текст"),
    )


@router.message(F.reply_to_message, F.reply_to_message.text.func(
    lambda t: bool(t) and t.startswith(EDIT_MARK)), F.text)
async def slot_reply(message: Message, worker, is_admin: bool):
    parsed = EDIT_RE.match(message.reply_to_message.text or "")
    if parsed is None or not allowed(worker, is_admin):
        await message.answer(STALE)
        return
    label, draft_id, version_id, card_msg = parsed.groups()
    slot = SLOT_BY_LABEL.get(label)
    if slot is None:
        await message.answer(STALE)
        return
    decision = await qs.edit_slot(int(draft_id), int(version_id),
                                  message.from_user.id, slot, message.text)
    if not decision.ok:
        await message.answer(decision.reason or STALE)
        return
    note = "✅ Правка сохранена."
    if decision.lint_fails:
        # линтер отработал заново, но правку человека он не отменяет
        note += "\n⚠️ линтер: " + esc("; ".join(decision.lint_fails))
    await message.answer(note)
    await _redraw_message(message, int(draft_id), int(card_msg))


# --- отрисовка ----------------------------------------------------------------

def card_text(card: qs.Card) -> str:
    """Карточка очереди — формат Д12 §6.2.

    Счётчик первым символом: очередь без видимого конца ощущается бесконечной,
    а это прямой путь к штамповке. Наблюдение человека отбито отдельным блоком
    с именем и временем — сверяют в первую очередь именно его.
    """
    lead, version = card.lead, card.version
    head = [
        f"📨 <b>{card.position}/{card.total}</b>  ·  #{lead.id} "
        f"{esc(lead.name)}",
        f"{esc(lead.city)} · {esc(lead.niche)} · {esc(card.draft.lang)} · "
        f"касание {card.draft.touch_number}",
    ]
    if card.email:
        head.append(f"📧 {esc(card.email)}")
    link = lead.draft_url or lead.website_url
    if link:
        head.append(f"🔗 {esc(link)}")

    seen = local(lead.gap_captured_at) if lead.gap_captured_at else "—"
    who = esc(card.author.name) if card.author else "—"
    observation = esc(gv.gap_line(lead.gap_type, lead.gap_value, lead.gap_note))
    words = email_lint.word_count(version.body or "")
    stamp = [f"v{card.version_no}"]
    if version.author == "human":
        stamp.append("правка человека")
    else:
        stamp.append(esc(_short_model(version.model)))
        if version.prompt_version:
            stamp.append(f"промпт {esc(version.prompt_version)}")
    stamp.append(f"{words} {_words_word(words)}")

    return "\n".join([
        *head,
        "",
        f"👤 {who}, {seen}:",
        f"«{observation}»",
        "",
        DIVIDER,
        f"Тема: {esc(version.subject)}",
        "",
        esc(version.body),
        DIVIDER,
        " · ".join(stamp),
    ])


def _short_model(model: str | None) -> str:
    return (model or "—").removeprefix("claude-")


def _words_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "слово"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "слова"
    return "слов"


async def _next_card(cb: CallbackQuery):
    """Следующая карточка — в том же сообщении: палец не двигается (Д12 §6.2)."""
    card = await qs.claim_next(cb.from_user.id)
    if card is None:
        await safe_edit(cb.message, EMPTY)
        return
    await _escalate(cb.message, card)
    await safe_edit(cb.message, card_text(card),
                    kb.review_card_kb(card.draft.id, card.version.id))


async def _stale(cb: CallbackQuery, draft_id: int, reason: str = ""):
    await cb.answer(reason or STALE, show_alert=True)
    await _redraw(cb, draft_id)


async def _redraw(cb: CallbackQuery, draft_id: int):
    async with Session() as s:
        card = await qs.load_card(s, draft_id)
    if card is None or card.draft.claimed_by != cb.from_user.id:
        await _next_card(cb)
        return
    await safe_edit(cb.message, card_text(card),
                    kb.review_card_kb(card.draft.id, card.version.id))


async def _redraw_message(message: Message, draft_id: int, card_msg_id: int):
    """Перерисовать карточку после правки — в том же сообщении, что и была."""
    async with Session() as s:
        card = await qs.load_card(s, draft_id)
    if card is None:
        return
    text = card_text(card)
    markup = kb.review_card_kb(card.draft.id, card.version.id)
    try:
        await message.bot.edit_message_text(
            text, chat_id=message.chat.id, message_id=card_msg_id,
            reply_markup=markup,
        )
    except TelegramBadRequest as e:
        # исходное сообщение могли удалить — карточка не должна пропасть
        log.info("карточка %s перерисована новым сообщением: %s", draft_id, e)
        await message.answer(text, reply_markup=markup)


async def _escalate(target: Message, card: qs.Card):
    if not card.escalate:
        return
    text = (f"⚠️ Карточка #{card.lead.id} {esc(card.lead.name)} "
            f"{qs.MAX_EXPIRED_LEASES} раза подряд осталась без решения — "
            f"посмотрите, что с ней не так.")
    await notify.to_admins(target.bot, text)


def _ids(cb: CallbackQuery) -> tuple[int, int] | None:
    """(draft_id, version_id) из callback_data. None — данные не от нашей кнопки."""
    parts = (cb.data or "").split(":")
    if len(parts) < 3:
        return None
    draft, version = parts[1], parts[2]
    if not all(p.isascii() and p.isdecimal() and len(p) <= 19
               for p in (draft, version)):
        return None
    return int(draft), int(version)
