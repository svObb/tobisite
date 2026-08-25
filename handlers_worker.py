import html
import logging
import time
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError

import config
import email_verify
import gap_validation as gv
import keyboards as kb
import notify
import queue_service
from dedup import normalize_domain, normalize_phone
from models import (
    Contact, Lead, LeadEvent, Sale, Session, Worker, constraint_of, day_start,
    dup_message, gap_age_days, gap_repeated, gap_stale, log_event,
)

log = logging.getLogger(__name__)

router = Router()
# Оба роутера ниже — общие для работника и админа: у router в main.py стоит
# фильтр ~is_admin, у этих его нет. Админ заносит компании тем же мастером
# и правит их теми же хендлерами.
edit_router = Router()
add_router = Router()


def esc(v) -> str:
    return html.escape(str(v)) if v is not None else "—"


def is_url(s: str) -> bool:
    s = s.strip()
    return s.startswith(("http://", "https://")) and "." in s.split("://", 1)[1]


def is_email(s: str) -> bool:
    s = s.strip()
    return "@" in s and "." in s.rsplit("@", 1)[-1]


def contact_label(c: Contact) -> str:
    if c.ctype == "other" and c.ctype_other:
        return c.ctype_other
    return config.CONTACT_TYPE_LABELS.get(c.ctype, c.ctype)


def local(dt: datetime) -> str:
    return dt.astimezone(config.TZ).strftime("%d.%m.%Y %H:%M")


# Клавиатуры живут в чате вечно, а состояние формы — нет: после отправки,
# отмены или чистки старая кнопка приходит без данных, по которым её рисовали.
STALE = "Кнопка устарела, откройте меню заново."


async def safe_edit(message: Message, text, reply_markup=None):
    """edit_text, не падающий на повторном нажатии той же кнопки пагинации."""
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


MAX_ID_DIGITS = 19  # bigint: длиннее числа в базе всё равно нет


async def cb_id(cb: CallbackQuery) -> int | None:
    """Число из callback_data вида «pfx:123». None — данные не от нашей кнопки.

    Клиент вправе прислать в callback_query что угодно, а int() на этом падает:
    без проверки вместо внятного ответа пользователь видел «что-то пошло не так»,
    а в лог сыпался traceback.
    """
    part = (cb.data or "").split(":")
    key = part[1] if len(part) > 1 else ""
    # isascii вдобавок к isdecimal: без него проходят арабо-индийские цифры,
    # int() их принимает, и в запрос уходил бы id, которого мы не рисовали
    if not (key.isascii() and key.isdecimal()) or len(key) > MAX_ID_DIGITS:
        await cb.answer(STALE, show_alert=True)
        return None
    return int(key)


async def send_screenshot(target: Message, file_id: str | None) -> bool:
    """Скриншот в чат. False — file_id больше не открывается.

    file_id привязан к боту: после смены токена все снимки, сохранённые прежним
    ботом, отдают 400. Без этой обёртки карточка такой записи не открывалась
    вовсе — падение уходило в общий обработчик ошибок.
    """
    if not file_id:
        return True
    try:
        await target.answer_photo(file_id)
        return True
    except TelegramBadRequest as e:
        log.warning("screenshot unavailable: %s", e)
        return False


def pick(options, key: str):
    """Индекс из callback_data → элемент списка. None, если список изменился."""
    # isdecimal, а не isdigit: у isdigit истинны «²» и подобные, а int() на них падает
    if not key.isdecimal():
        return None
    i = int(key)
    return options[i] if i < len(options) else None


async def get_contacts(session, lead_id):
    rows = await session.scalars(
        select(Contact)
        .where(Contact.lead_id == lead_id, Contact.deleted_at.is_(None))
        .order_by(Contact.id)
    )
    return list(rows)


def fmt_lead(lead: Lead, contacts, *, author=None, edits=0, admin=False) -> str:
    lines = [
        f"<b>#{lead.id} {esc(lead.name)}</b>",
        f"Статус: {config.STATUS_LABELS.get(lead.status, lead.status)}",
        f"Сайт: {esc(lead.website_url) if lead.website_url else 'нет'}",
        f"Источник: {esc(lead.source_url)}",
        f"Страна/город: {esc(lead.country)}, {esc(lead.city)}",
        f"Язык: {esc(lead.language)}",
        f"Ниша: {esc(lead.niche)}",
        f"Рейтинг: {esc(lead.google_rating)}",
        f"Где нашли: {esc(lead.found_via)}",
        f"Заметка: {esc(lead.note)}",
        f"Наблюдение: {esc(gv.gap_line(lead.gap_type, lead.gap_value, lead.gap_note))}",
    ]
    if gap_stale(lead):
        lines.append(f"⚠️ наблюдению {gap_age_days(lead)} дней — переснимите")
    lines.append("Контакты:")
    for c in contacts:
        lines.append(f"  • {esc(contact_label(c))}: {esc(c.value)}")
    if not contacts:
        lines.append("  —")
    lines.append(f"Добавлено: {local(lead.created_at)}")
    if lead.possible_duplicate:
        lines.append("⚠️ Помечено как возможный дубликат")
    if admin and lead.has_ads:
        lines.append("💰 Уже платит за рекламу (Ads Transparency)")
    if admin:
        if author:
            lines.append(f"Работник: {esc(author.name)} (id {author.tg_id})")
            lines.append(f'<a href="tg://user?id={author.tg_id}">Открыть в Telegram</a>')
        lines.append(f"Редактировалось: {edits} раз")
        if lead.draft_url:
            lines.append(f"Черновик: {esc(lead.draft_url)}")
        if lead.admin_note:
            lines.append(f"Моя заметка: {esc(lead.admin_note)}")
    if lead.reject_reason:
        label = config.LEAD_REJECT_LABELS.get(lead.reject_reason,
                                              lead.reject_reason)
        lines.append(f"🚫 Причина отклонения: {esc(label)}")
    if lead.cancelled_at:
        lines.append(f"🚫 Отменено: {local(lead.cancelled_at)}")
    return "\n".join(lines)


def fmt_summary(d: dict) -> str:
    lines = [
        "<b>Проверьте данные</b>",
        f"Название: {esc(d['name'])}",
        f"Сайт: {esc(d.get('website_url')) if d.get('website_url') else 'нет'}",
        f"Источник: {esc(d['source_url'])}",
        f"Страна/город: {esc(d['country'])}, {esc(d['city'])}",
        f"Язык: {esc(d['language'])}",
        f"Ниша: {esc(d['niche'])}",
        f"Рейтинг: {esc(d.get('google_rating'))}",
        f"Заметка: {esc(d.get('note'))}",
        f"Скриншот: {'есть' if d.get('screenshot_file_id') else 'нет'}",
        f"Где нашли: {esc(d['found_via'])}",
        f"Наблюдение: {esc(gv.gap_line(d.get('gap_type'), d.get('gap_value'), d.get('gap_note')))}",
        "Контакты:",
    ]
    for c in d["contacts"]:
        name = c["ctype_other"] or config.CONTACT_TYPE_LABELS.get(c["ctype"], c["ctype"])
        lines.append(f"  • {esc(name)}: {esc(c['value'])}")
    # связку «расхождение телефонов ↔ телефон в карточке» раньше проверить негде:
    # контакты в форме идут после наблюдения. Поэтому мягко, предупреждением
    if d.get("gap_type") == "contact_mismatch" and not any(
        c["ctype"] == "phone" for c in d["contacts"]
    ):
        lines.append("⚠️ Наблюдение о расхождении телефонов, а телефона в контактах нет.")
    return "\n".join(lines)


# --- регистрация -------------------------------------------------------------

class Reg(StatesGroup):
    code = State()
    name = State()


# Счётчик попыток подбора кода живёт в памяти процесса, а не в FSM: любой /start
# чистит состояние и обнулил бы его. Сбрасывается при перезапуске бота, и это
# приемлемо — цель в том, чтобы перебор был бессмысленно медленным.
MAX_CODE_TRIES = 5
CODE_BLOCK_MIN = 15
# Ключ — tg_id любого, кто написал боту, поэтому у словарей обязан быть потолок:
# иначе поток чужих /start растит их в памяти процесса неограниченно.
MAX_TRACKED = 10_000
_code_tries: dict[int, int] = {}
_code_blocked: dict[int, datetime] = {}


def forget_stale() -> None:
    """Снимает истёкшие блокировки и подрезает словари до потолка."""
    now = datetime.now(config.TZ)
    for tg_id in [k for k, until in _code_blocked.items() if until <= now]:
        _code_blocked.pop(tg_id, None)
        _code_tries.pop(tg_id, None)
    # dict хранит порядок вставки, поэтому «первый» — самый давний
    for d in (_code_tries, _code_blocked):
        while len(d) > MAX_TRACKED:
            d.pop(next(iter(d)))


def code_block_left(tg_id: int) -> int:
    """Минут до конца блокировки подбора кода. 0 — не заблокирован."""
    until = _code_blocked.get(tg_id)
    if until is None:
        return 0
    left = (until - datetime.now(config.TZ)).total_seconds()
    if left <= 0:
        _code_blocked.pop(tg_id, None)
        _code_tries.pop(tg_id, None)
        return 0
    return int(left // 60) + 1


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, worker: Worker | None):
    await state.clear()
    if worker:
        await message.answer(
            f"С возвращением, {esc(worker.name)}!", reply_markup=kb.worker_menu()
        )
        return
    left = code_block_left(message.from_user.id)
    if left:
        await message.answer(f"Слишком много неверных попыток. Повторите через {left} мин.")
        return
    await state.set_state(Reg.code)
    await message.answer("Введите код доступа:")


@router.message(Reg.code)
async def reg_code(message: Message, state: FSMContext):
    tg_id = message.from_user.id
    left = code_block_left(tg_id)
    if left:
        await message.answer(f"Слишком много неверных попыток. Повторите через {left} мин.")
        return
    if message.text != config.ACCESS_CODE:
        forget_stale()
        tries = _code_tries.get(tg_id, 0) + 1
        _code_tries[tg_id] = tries
        log.warning("wrong access code from tg_id=%s (%s/%s)", tg_id, tries, MAX_CODE_TRIES)
        if tries >= MAX_CODE_TRIES:
            _code_blocked[tg_id] = datetime.now(config.TZ) + timedelta(minutes=CODE_BLOCK_MIN)
            await state.clear()
            await message.answer(
                f"Слишком много неверных попыток. Повторите через {CODE_BLOCK_MIN} мин."
            )
            return
        await message.answer("Неверный код. Попробуйте ещё раз:")
        return
    _code_tries.pop(tg_id, None)
    await state.set_state(Reg.name)
    await message.answer("Код принят. Как вас зовут?")


@router.message(Reg.name)
async def reg_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("Введите имя текстом:")
        return
    tg_id = message.from_user.id
    try:
        async with Session() as s, s.begin():
            # tg_id уникален: у удалённого работника строка в базе осталась,
            # и вставка упала бы на IntegrityError вместо внятного отказа.
            # Заодно это и есть смысл удаления — иначе человек просто вернулся
            # бы по общему коду.
            existing = await s.scalar(select(Worker).where(Worker.tg_id == tg_id))
            if existing is None:
                s.add(Worker(tg_id=tg_id, name=name))
    except IntegrityError:
        # два сообщения подряд успели пройти проверку до вставки друг друга
        async with Session() as s:
            existing = await s.scalar(select(Worker).where(Worker.tg_id == tg_id))
    await state.clear()
    if existing is not None:
        if existing.deleted_at or not existing.is_active:
            log.warning("blocked re-registration tg_id=%s", tg_id)
            await message.answer("Доступ закрыт администратором.")
        else:
            await message.answer(
                f"Вы уже зарегистрированы, {esc(existing.name)}.",
                reply_markup=kb.worker_menu(),
            )
        return
    log.info("worker registered tg_id=%s", tg_id)
    await message.answer(f"Готово, {esc(name)}!", reply_markup=kb.worker_menu())
    # админ должен знать о каждом, кто получил доступ по общему коду
    await notify.to_admins(message.bot,
                           f"👤 Новый работник: {esc(name)} (tg id {tg_id})")


# --- добавление компании -----------------------------------------------------

class Add(StatesGroup):
    name = State()
    website = State()
    # наблюдение стоит сразу за сайтом: в этот момент работник физически держит
    # сайт открытым, и вспоминать ему ничего не надо (Д12 §2)
    gap_type = State()
    gap_value = State()
    gap_note = State()
    source_url = State()
    country = State()
    country_other = State()
    city = State()
    language = State()
    language_other = State()
    niche = State()
    niche_other = State()
    c_type = State()
    c_other = State()
    c_value = State()
    c_more = State()
    rating = State()
    note = State()
    screenshot = State()
    found_via = State()
    found_via_other = State()
    confirm = State()
    dup = State()


COUNTRY_NAMES = [n for n, _ in config.COUNTRIES]


async def used_today(session, worker_id: int) -> int:
    # Отменённые СЧИТАЮТСЯ: отмена своей записи в окне CANCEL_WINDOW_MIN иначе
    # обнуляла бы квоту — добавил 15, отменил 15, добавил ещё 15. Лимит меряет
    # объём работы за день, а не число выживших записей. Удалённые админом
    # не считаются: это его решение, а не работника.
    return await session.scalar(
        select(func.count()).select_from(Lead).where(
            Lead.worker_id == worker_id,
            Lead.deleted_at.is_(None),
            Lead.created_at >= day_start(),
        )
    )


@add_router.message(F.text == kb.BTN_ADD)
async def add_start(message: Message, state: FSMContext, worker: Worker | None,
                    is_admin: bool):
    if not worker:
        await message.answer("Сначала /start")
        return
    # лимит нормирует работу наёмных людей, к владельцу базы он не относится
    if not is_admin:
        limit = (worker.daily_limit if worker.daily_limit is not None
                 else config.DEFAULT_DAILY_LIMIT)
        async with Session() as s:
            used = await used_today(s, worker.id)
        if used >= limit:
            await message.answer("Лимит на сегодня исчерпан.")
            return
    await state.set_state(Add.name)
    # set_data, а не update_data: иначе повторный вход в форму тащит за собой
    # поля прошлой попытки
    await state.set_data({"contacts": []})
    await message.answer("1/14. Название компании:", reply_markup=kb.cancel_kb())


@add_router.message(Add.name)
async def add_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("Название текстом:", reply_markup=kb.cancel_kb())
        return
    await state.update_data(name=name)
    await state.set_state(Add.website)
    await message.answer("2/14. Ссылка на сайт:", reply_markup=kb.website_kb())


async def _after_website(target: Message, state: FSMContext):
    d = await state.get_data()
    if d.get("resume_confirm"):
        # сюда вернулись из _commit_and_reply после дубля сайта: остальная
        # форма уже заполнена, гонять человека по шагам 3–14 заново незачем
        await _show_summary(target, state)
        return
    await _ask_gap_type(target, state, Add, "3/14. ")


async def _ask_source(target: Message, state: FSMContext):
    await state.set_state(Add.source_url)
    await target.answer(
        "5/14. Ссылка на источник (Google Maps, соцсеть, каталог):",
        reply_markup=kb.cancel_kb(),
    )


# --- наблюдение (Д12 §2) -----------------------------------------------------
#
# Один и тот же диалог живёт в двух местах: в форме Add сразу после сайта (в
# этот момент работник физически держит сайт открытым) и в «Переснять
# наблюдение» на карточке. Отсюда параметр states — группа состояний
# вызывающего сценария — и prefix с номером шага, которого у переснятия нет.

GAP_LABELS = [label for _, label in config.GAP_TYPES]


def gap_type_kb():
    # «Другое» здесь нет намеренно: свободное поле «опиши проблему» и есть тот
    # источник мусора, ради которого весь шаг затеян (Д12 §2)
    return kb.choices_kb(GAP_LABELS, "gp", other=False, per_row=3)


def gap_value_prompt(gap_type: str):
    options = gv.CHOICE_OPTIONS.get(gap_type)
    markup = (kb.choices_kb(options, "gv", other=False, per_row=3) if options
              else kb.cancel_kb())
    return gv.ask_value(gap_type), markup


async def _ask_gap_type(target: Message, state: FSMContext, states, prefix=""):
    # от этой отметки считаются и тайминг-чек (правило 6), и gap_seconds
    await state.update_data(gap_at=time.time())
    await state.set_state(states.gap_type)
    await target.answer(prefix + gv.ASK_TYPE, reply_markup=gap_type_kb())


async def _ask_gap_value(target: Message, state: FSMContext, states, gap_type,
                         prefix="", error=""):
    text, markup = gap_value_prompt(gap_type)
    await state.set_state(states.gap_value)
    head = f"{error}\n\n" if error else prefix
    await target.answer(head + text, reply_markup=markup)


async def _ask_gap_note(target: Message, state: FSMContext, states):
    await state.set_state(states.gap_note)
    await target.answer("Хочеш додати деталь? (необов'язково)",
                        reply_markup=kb.skip_kb("gn"))


async def _gap_type_chosen(target: Message, state: FSMContext, states, key,
                           prefix=""):
    pair = pick(config.GAP_TYPES, key)
    if pair is None:
        await target.answer(STALE)
        return
    gap_type = pair[0]
    d = await state.get_data()
    err = gv.type_error(gap_type, d.get("website_url"))
    if err:
        await target.answer(err, reply_markup=gap_type_kb())
        return
    started = d.get("gap_at")
    await state.update_data(
        gap_type=gap_type, gap_value=None, gap_note=None, gap_screenshot=None,
        gap_too_fast=bool(started) and gv.too_fast(time.time() - started),
    )
    await _ask_gap_value(target, state, states, gap_type, prefix)


async def _gap_value_given(target: Message, state: FSMContext, states, raw):
    d = await state.get_data()
    gap_type = d.get("gap_type")
    if gap_type is None:
        await target.answer(STALE)
        return
    if gap_type == gv.PHOTO_TYPE:
        await _ask_gap_value(target, state, states, gap_type,
                             error="Тут потрібен скриншот сторінки з телефону.")
        return
    value, err = gv.check_value(gap_type, raw)
    if err:
        await _ask_gap_value(target, state, states, gap_type, error=err)
        return
    await state.update_data(gap_value=value)
    await _after_gap_value(target, state, states, gap_type, value)


async def _after_gap_value(target: Message, state: FSMContext, states, gap_type,
                           value):
    await target.answer(f"✅ Записав: {esc(gv.gap_line(gap_type, value, None))}")
    await _ask_gap_note(target, state, states)


async def _finish_gap(target: Message, state: FSMContext, states, worker, is_admin):
    """Общий хвост: правило 4, gap_seconds — и дальше по своему сценарию."""
    d = await state.get_data()
    if not d.get("gap_type"):
        await target.answer(STALE)
        return
    async with Session() as s:
        repeated = await gap_repeated(
            s, worker.id, d.get("gap_type"), d.get("gap_value"), d.get("gap_note"),
            exclude_lead_id=d.get("lead_id"),
        )
    if repeated:
        await state.update_data(gap_note=None)
        await _ask_gap_value(target, state, states, d["gap_type"],
                             error=gv.COPYPASTE_ANSWER)
        return
    started = d.get("gap_at")
    if started:
        await state.update_data(gap_seconds=int(time.time() - started))
    if states is Add:
        await _ask_source(target, state)
        return
    await _save_regap(target, state, worker, is_admin)


@add_router.callback_query(Add.gap_type, F.data.startswith("gp:"))
async def add_gap_type(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await _gap_type_chosen(cb.message, state, Add, cb.data.split(":")[1], "4/14. ")


@add_router.callback_query(Add.gap_value, F.data.startswith("gv:"))
async def add_gap_choice(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    d = await state.get_data()
    value = pick(gv.CHOICE_OPTIONS.get(d.get("gap_type"), []), cb.data.split(":")[1])
    if value is None:
        await cb.message.answer(STALE)
        return
    await _gap_value_given(cb.message, state, Add, value)


@add_router.message(Add.gap_value, F.photo)
async def add_gap_photo(message: Message, state: FSMContext):
    await _gap_photo(message, state, Add)


@add_router.message(Add.gap_value)
async def add_gap_value(message: Message, state: FSMContext):
    await _gap_value_given(message, state, Add, message.text or "")


async def _gap_photo(message: Message, state: FSMContext, states):
    d = await state.get_data()
    gap_type = d.get("gap_type")
    if gap_type is None:
        await message.answer(STALE)
        return
    if gap_type != gv.PHOTO_TYPE:
        await _ask_gap_value(message, state, states, gap_type,
                             error="Для цього типу потрібен не скриншот, а відповідь.")
        return
    await state.update_data(gap_screenshot=message.photo[-1].file_id)
    await _after_gap_value(message, state, states, gap_type, None)


@add_router.callback_query(Add.gap_note, F.data == "gn:skip")
async def add_gap_note_skip(cb: CallbackQuery, state: FSMContext, worker: Worker,
                            is_admin: bool):
    await cb.answer()
    await _finish_gap(cb.message, state, Add, worker, is_admin)


@add_router.message(Add.gap_note)
async def add_gap_note(message: Message, state: FSMContext, worker: Worker,
                       is_admin: bool):
    note, err = gv.check_note(message.text or "")
    if err:
        await message.answer(err, reply_markup=kb.skip_kb("gn"))
        return
    await state.update_data(gap_note=note)
    await _finish_gap(message, state, Add, worker, is_admin)


@add_router.callback_query(Add.website, F.data == "ws:none")
async def add_website_none(cb: CallbackQuery, state: FSMContext):
    await state.update_data(website_url=None, domain_norm=None)
    await cb.answer()
    await _after_website(cb.message, state)


@add_router.message(Add.website)
async def add_website(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if not is_url(raw):
        await message.answer(
            "Ссылка должна начинаться с http:// или https:// и содержать точку. "
            "Повторите:",
            reply_markup=kb.website_kb(),
        )
        return
    dom = normalize_domain(raw)
    async with Session() as s:
        exists = await s.scalar(
            select(Lead.id).where(
                Lead.domain_norm == dom,
                Lead.cancelled_at.is_(None),
                Lead.deleted_at.is_(None),
            ).limit(1)
        )
    if exists:
        # форму не чистим: у человека, может, просто опечатка в адресе,
        # а «Сайта нет» и «Отмена» остаются доступными кнопками
        await message.answer(
            "❌ Такой сайт уже есть в базе. Введите другой или нажмите "
            "«Сайта нет»:",
            reply_markup=kb.website_kb(),
        )
        return
    await state.update_data(website_url=raw, domain_norm=dom)
    await _after_website(message, state)


@add_router.message(Add.source_url)
async def add_source(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if not is_url(raw):
        await message.answer(
            "Ссылка должна начинаться с http:// или https:// и содержать точку. "
            "Повторите:",
            reply_markup=kb.cancel_kb(),
        )
        return
    await state.update_data(source_url=raw)
    await state.set_state(Add.country)
    await message.answer(
        "6/14. Страна:", reply_markup=kb.choices_kb(COUNTRY_NAMES, "co")
    )


async def _ask_city(target: Message, state: FSMContext):
    await state.set_state(Add.city)
    await target.answer("7/14. Город:", reply_markup=kb.cancel_kb())


@add_router.callback_query(Add.country, F.data.startswith("co:"))
async def add_country(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    await cb.answer()
    if key == "oth":
        await state.set_state(Add.country_other)
        await cb.message.answer("Впишите страну:", reply_markup=kb.cancel_kb())
        return
    val = pick(COUNTRY_NAMES, key)
    if val is None:
        await cb.message.answer(STALE)
        return
    await state.update_data(country=val)
    await _ask_city(cb.message, state)


@add_router.message(Add.country_other)
async def add_country_other(message: Message, state: FSMContext):
    val = (message.text or "").strip()
    if not val:
        await message.answer("Впишите страну текстом:", reply_markup=kb.cancel_kb())
        return
    await state.update_data(country=val)
    await _ask_city(message, state)


@add_router.message(Add.city)
async def add_city(message: Message, state: FSMContext):
    val = (message.text or "").strip()
    if not val:
        await message.answer("Город текстом:", reply_markup=kb.cancel_kb())
        return
    await state.update_data(city=val)
    await state.set_state(Add.language)
    await message.answer(
        "8/14. Язык компании:", reply_markup=kb.choices_kb(config.LANGUAGES, "la")
    )


async def _ask_niche(target: Message, state: FSMContext):
    await state.set_state(Add.niche)
    await target.answer("9/14. Ниша:", reply_markup=kb.choices_kb(config.NICHES, "ni"))


@add_router.callback_query(Add.language, F.data.startswith("la:"))
async def add_language(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    await cb.answer()
    if key == "oth":
        await state.set_state(Add.language_other)
        await cb.message.answer("Впишите язык:", reply_markup=kb.cancel_kb())
        return
    val = pick(config.LANGUAGES, key)
    if val is None:
        await cb.message.answer(STALE)
        return
    await state.update_data(language=val)
    await _ask_niche(cb.message, state)


@add_router.message(Add.language_other)
async def add_language_other(message: Message, state: FSMContext):
    val = (message.text or "").strip()
    if not val:
        await message.answer("Впишите язык текстом:", reply_markup=kb.cancel_kb())
        return
    await state.update_data(language=val)
    await _ask_niche(message, state)


async def _ask_contact_type(target: Message, state: FSMContext):
    await state.set_state(Add.c_type)
    labels = [label for _, label in config.CONTACT_TYPES]
    await target.answer("10/14. Тип контакта:", reply_markup=kb.choices_kb(labels, "ct"))


@add_router.callback_query(Add.niche, F.data.startswith("ni:"))
async def add_niche(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    await cb.answer()
    if key == "oth":
        await state.set_state(Add.niche_other)
        await cb.message.answer("Впишите нишу:", reply_markup=kb.cancel_kb())
        return
    val = pick(config.NICHES, key)
    if val is None:
        await cb.message.answer(STALE)
        return
    await state.update_data(niche=val)
    await _ask_contact_type(cb.message, state)


@add_router.message(Add.niche_other)
async def add_niche_other(message: Message, state: FSMContext):
    val = (message.text or "").strip()
    if not val:
        await message.answer("Впишите нишу текстом:", reply_markup=kb.cancel_kb())
        return
    await state.update_data(niche=val)
    await _ask_contact_type(message, state)


async def _ask_contact_value(target: Message, state: FSMContext):
    await state.set_state(Add.c_value)
    await target.answer(
        "Введите значение (номер, email или ссылку на профиль):",
        reply_markup=kb.cancel_kb(),
    )


@add_router.callback_query(Add.c_type, F.data.startswith("ct:"))
async def add_contact_type(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    await cb.answer()
    if key == "oth":
        await state.set_state(Add.c_other)
        await cb.message.answer("Впишите название канала:", reply_markup=kb.cancel_kb())
        return
    pair = pick(config.CONTACT_TYPES, key)
    if pair is None:
        await cb.message.answer(STALE)
        return
    await state.update_data(cur_type=pair[0], cur_other=None)
    await _ask_contact_value(cb.message, state)


@add_router.message(Add.c_other)
async def add_contact_other(message: Message, state: FSMContext):
    val = (message.text or "").strip()
    if not val:
        await message.answer("Название канала текстом:", reply_markup=kb.cancel_kb())
        return
    await state.update_data(cur_type="other", cur_other=val)
    await _ask_contact_value(message, state)


async def phone_dup_exists(session, value_norm: str | None,
                           exclude_contact_id: int | None = None) -> bool:
    """Занят ли номер живым контактом живой записи.

    Условия обязаны повторять предикат уникального индекса
    uq_contacts_phone_norm_active: проверяем ровно то, на чём упадёт INSERT.
    """
    if not value_norm:
        return False
    conds = [
        Contact.ctype == "phone",
        Contact.value_norm == value_norm,
        Contact.deleted_at.is_(None),
        Contact.lead_cancelled_at.is_(None),
    ]
    if exclude_contact_id is not None:
        conds.append(Contact.id != exclude_contact_id)
    row = await session.scalar(select(Contact.id).where(*conds).limit(1))
    return row is not None


def contact_error(ctype: str, value: str, region: str | None = None) -> str | None:
    # region обязан быть тем же, с каким номер потом ляжет в value_norm, иначе
    # проверка и запись разойдутся и дубликат пройдёт мимо уникального индекса
    if ctype == "phone":
        if not normalize_phone(value, region):
            return ("Не разобрал номер. Введите в международном формате, "
                    "например +380501234567:")
    elif ctype == "email":
        if not is_email(value):
            return "Email должен содержать @ и домен. Повторите:"
    elif value.startswith("http") and not is_url(value):
        return "Ссылка должна начинаться с http:// или https:// и содержать точку:"
    return None


@add_router.message(Add.c_value)
async def add_contact_value(message: Message, state: FSMContext, is_admin: bool):
    val = (message.text or "").strip()
    d = await state.get_data()
    if not val:
        await message.answer("Введите значение текстом:", reply_markup=kb.cancel_kb())
        return
    ctype = d.get("cur_type")
    if ctype is None:
        await state.clear()
        await message.answer(STALE, reply_markup=kb.menu(is_admin))
        return
    region = config.COUNTRY_ISO.get(d.get("country"))
    err = contact_error(ctype, val, region)
    if err:
        await message.answer(err, reply_markup=kb.cancel_kb())
        return
    contacts = d.get("contacts", [])
    # Дубль телефона ловится здесь, на шаге ввода, а не на финальном INSERT:
    # раньше человек узнавал о нём после 12 шагов и терял всю форму.
    if ctype == "phone":
        norm = normalize_phone(val, region)
        in_form = any(
            c["ctype"] == "phone" and normalize_phone(c["value"], region) == norm
            for c in contacts
        )
        async with Session() as s:
            in_db = await phone_dup_exists(s, norm)
        if in_form or in_db:
            await message.answer(
                "❌ Такой телефон уже есть в базе. Введите другой номер:",
                reply_markup=kb.cancel_kb(),
            )
            return
    contacts.append({"ctype": ctype, "ctype_other": d.get("cur_other"), "value": val})
    await state.update_data(contacts=contacts)
    if len(contacts) >= config.MAX_CONTACTS:
        await _ask_rating(message, state)
        return
    await state.set_state(Add.c_more)
    await message.answer("Добавить ещё контакт?", reply_markup=kb.contact_more_kb())


async def _ask_rating(target: Message, state: FSMContext):
    d = await state.get_data()
    if d.get("resume_confirm"):
        # контакт перевведён после дубля на сохранении: шаги 9–12 уже заполнены
        await _show_summary(target, state)
        return
    await state.set_state(Add.rating)
    await target.answer(
        "11/14. Рейтинг Google и число отзывов:", reply_markup=kb.skip_kb("rt")
    )


@add_router.callback_query(Add.c_more, F.data == "cm:yes")
async def add_more_yes(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await _ask_contact_type(cb.message, state)


@add_router.callback_query(Add.c_more, F.data == "cm:next")
async def add_more_next(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await _ask_rating(cb.message, state)


async def _ask_note(target: Message, state: FSMContext):
    await state.set_state(Add.note)
    await target.answer("12/14. Заметка:", reply_markup=kb.skip_kb("nt"))


@add_router.callback_query(Add.rating, F.data == "rt:skip")
async def add_rating_skip(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await _ask_note(cb.message, state)


@add_router.message(Add.rating)
async def add_rating(message: Message, state: FSMContext):
    await state.update_data(google_rating=(message.text or "").strip() or None)
    await _ask_note(message, state)


async def _ask_screenshot(target: Message, state: FSMContext):
    await state.set_state(Add.screenshot)
    await target.answer("13/14. Скриншот сайта (фото):", reply_markup=kb.skip_kb("sc"))


@add_router.callback_query(Add.note, F.data == "nt:skip")
async def add_note_skip(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await _ask_screenshot(cb.message, state)


@add_router.message(Add.note)
async def add_note(message: Message, state: FSMContext):
    await state.update_data(note=(message.text or "").strip() or None)
    await _ask_screenshot(message, state)


async def _ask_found_via(target: Message, state: FSMContext):
    await state.set_state(Add.found_via)
    await target.answer(
        "14/14. Где нашли:", reply_markup=kb.choices_kb(config.FOUND_VIA, "fv")
    )


@add_router.callback_query(Add.screenshot, F.data == "sc:skip")
async def add_screenshot_skip(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await _ask_found_via(cb.message, state)


@add_router.message(Add.screenshot, F.photo)
async def add_screenshot(message: Message, state: FSMContext):
    await state.update_data(screenshot_file_id=message.photo[-1].file_id)
    await _ask_found_via(message, state)


@add_router.message(Add.screenshot)
async def add_screenshot_bad(message: Message, state: FSMContext):
    await message.answer("Пришлите фото или нажмите «Пропустить».",
                         reply_markup=kb.skip_kb("sc"))


async def _show_summary(target: Message, state: FSMContext):
    d = await state.get_data()
    await state.set_state(Add.confirm)
    await target.answer(fmt_summary(d), reply_markup=kb.confirm_kb())


@add_router.callback_query(Add.found_via, F.data.startswith("fv:"))
async def add_found_via(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    await cb.answer()
    if key == "oth":
        await state.set_state(Add.found_via_other)
        await cb.message.answer("Впишите источник:", reply_markup=kb.cancel_kb())
        return
    val = pick(config.FOUND_VIA, key)
    if val is None:
        await cb.message.answer(STALE)
        return
    await state.update_data(found_via=val)
    await _show_summary(cb.message, state)


@add_router.message(Add.found_via_other)
async def add_found_via_other(message: Message, state: FSMContext):
    val = (message.text or "").strip()
    if not val:
        await message.answer("Впишите источник текстом:", reply_markup=kb.cancel_kb())
        return
    await state.update_data(found_via=val)
    await _show_summary(message, state)


@add_router.callback_query(Add.confirm, F.data == "cf:redo")
async def add_redo(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(Add.name)
    await state.set_data({"contacts": []})
    await cb.message.answer("1/14. Название компании:", reply_markup=kb.cancel_kb())


class LimitReached(Exception):
    """Лимит исчерпан между открытием формы и её отправкой."""


async def save_lead(d: dict, worker: Worker, possible_dup: bool, is_admin: bool) -> int:
    region = config.COUNTRY_ISO.get(d["country"])
    async with Session() as s, s.begin():
        # форма из 12 шагов переживает и полночь, и снижение лимита админом,
        # поэтому проверка на входе в форму ничего не гарантирует
        if not is_admin:
            fresh = await s.get(Worker, worker.id)
            limit = (fresh.daily_limit if fresh and fresh.daily_limit is not None
                     else config.DEFAULT_DAILY_LIMIT)
            if await used_today(s, worker.id) >= limit:
                raise LimitReached
        lead = Lead(
            worker_id=worker.id, name=d["name"], website_url=d.get("website_url"),
            domain_norm=d.get("domain_norm"), source_url=d["source_url"],
            country=d["country"], city=d["city"], language=d["language"],
            niche=d["niche"], google_rating=d.get("google_rating"), note=d.get("note"),
            screenshot_file_id=d.get("screenshot_file_id"), found_via=d["found_via"],
            possible_duplicate=possible_dup,
            gap_type=d.get("gap_type"), gap_value=d.get("gap_value"),
            gap_note=d.get("gap_note"), gap_screenshot=d.get("gap_screenshot"),
            gap_captured_at=datetime.now(config.TZ) if d.get("gap_type") else None,
            gap_seconds=d.get("gap_seconds"),
        )
        s.add(lead)
        await s.flush()
        if d.get("gap_too_fast"):
            # правило 6: не блокируем, но след остаётся — по нему видно, кто
            # «смотрел сайт 20 секунд», не открывая его
            log_event(s, lead.id, "observation_too_fast", worker.tg_id)
        for c in d["contacts"]:
            s.add(Contact(
                lead_id=lead.id, ctype=c["ctype"], ctype_other=c["ctype_other"],
                value=c["value"],
                value_norm=normalize_phone(c["value"], region) if c["ctype"] == "phone" else None,
            ))
        return lead.id


async def _drop_dup_phones(d: dict) -> list[str]:
    """Убирает из данных формы телефоны, уже занятые в базе. Возвращает убранные.

    Зовётся после IntegrityError на сохранении: какой именно контакт упал,
    Postgres не говорит, поэтому проверяем каждый телефон формы тем же условием,
    что и уникальный индекс.
    """
    region = config.COUNTRY_ISO.get(d.get("country"))
    dropped, kept = [], []
    async with Session() as s:
        for c in d["contacts"]:
            if c["ctype"] == "phone":
                if await phone_dup_exists(s, normalize_phone(c["value"], region)):
                    dropped.append(c["value"])
                    continue
            kept.append(c)
    d["contacts"] = kept
    return dropped


async def _commit_and_reply(cb: CallbackQuery, state: FSMContext, worker: Worker,
                            possible_dup: bool, is_admin: bool):
    d = await state.get_data()
    try:
        lead_id = await save_lead(d, worker, possible_dup, is_admin)
    except LimitReached:
        await state.clear()
        await cb.message.answer(
            "Лимит на сегодня исчерпан, запись не сохранена.",
            reply_markup=kb.menu(is_admin),
        )
        return
    except IntegrityError as e:
        # Дубль, проскочивший ранние проверки (гонка или обход шага). Форму
        # не выбрасываем: 12 шагов работы человека дороже, чем пара веток кода.
        log.warning("dup on insert: %s", e.orig)
        name = constraint_of(e)
        if name == "uq_contacts_phone_norm_active":
            dropped = await _drop_dup_phones(d)
            if dropped:
                await state.update_data(contacts=d["contacts"])
                listed = "\n".join(f"  • {esc(v)}" for v in dropped)
                if d["contacts"]:
                    await cb.message.answer(
                        "❌ Телефон уже есть в базе, контакт убран из формы:\n"
                        f"{listed}\nОстальные данные целы — проверьте и "
                        "отправьте ещё раз."
                    )
                    await _show_summary(cb.message, state)
                    return
                await state.update_data(resume_confirm=True)
                await cb.message.answer(
                    "❌ Телефон уже есть в базе, контакт убран из формы:\n"
                    f"{listed}\nДобавьте другой контакт — остальные данные целы."
                )
                await _ask_contact_type(cb.message, state)
                return
        if name == "uq_leads_domain_norm_active":
            await state.update_data(resume_confirm=True)
            await state.set_state(Add.website)
            await cb.message.answer(
                "❌ Такой сайт уже есть в базе. Введите другой или нажмите "
                "«Сайта нет» — остальные данные целы.",
                reply_markup=kb.website_kb(),
            )
            return
        await state.clear()
        await cb.message.answer(dup_message(e), reply_markup=kb.menu(is_admin))
        return
    await state.clear()
    await cb.message.answer(
        f"✅ Компания #{lead_id} принята",
        reply_markup=kb.admin_saved_kb(lead_id) if is_admin else kb.saved_kb(lead_id),
    )
    # 6.13: админ узнаёт о компании сразу, а не при следующем открытии списка
    await notify.new_lead(cb.bot, lead_id, skip_tg_id=cb.from_user.id)


@add_router.callback_query(Add.confirm, F.data == "cf:send")
async def add_send(cb: CallbackQuery, state: FSMContext, worker: Worker, is_admin: bool):
    await cb.answer()
    d = await state.get_data()
    async with Session() as s:
        similar = await s.scalar(
            select(Lead.id).where(
                func.lower(func.btrim(Lead.name)) == d["name"].strip().lower(),
                func.lower(func.btrim(Lead.city)) == d["city"].strip().lower(),
                Lead.cancelled_at.is_(None),
                Lead.deleted_at.is_(None),
            ).limit(1)
        )
    if similar:
        await state.set_state(Add.dup)
        await cb.message.answer(
            "⚠️ Похоже, такая компания уже есть. Всё равно отправить?",
            reply_markup=kb.dup_kb(),
        )
        return
    await _commit_and_reply(cb, state, worker, False, is_admin)


@add_router.callback_query(Add.dup, F.data == "dup:yes")
async def add_send_dup(cb: CallbackQuery, state: FSMContext, worker: Worker,
                       is_admin: bool):
    await cb.answer()
    await _commit_and_reply(cb, state, worker, True, is_admin)


# --- мои компании / статистика / инструкция ----------------------------------

async def my_page(session, worker_id, offset):
    total = await session.scalar(
        select(func.count()).select_from(Lead).where(
            Lead.worker_id == worker_id,
            Lead.cancelled_at.is_(None),
            Lead.deleted_at.is_(None),
        )
    )
    rows = await session.scalars(
        select(Lead).where(
            Lead.worker_id == worker_id,
            Lead.cancelled_at.is_(None),
            Lead.deleted_at.is_(None),
        ).order_by(Lead.id.desc()).offset(offset).limit(config.PAGE_SIZE)
    )
    return list(rows), total


async def status_counts(session, conds) -> dict[str, int]:
    """Сколько лидов в каждом статусе — одним запросом на весь экран."""
    rows = await session.execute(
        select(Lead.status, func.count(Lead.id)).where(*conds)
        .group_by(Lead.status)
    )
    return dict(rows.all())


def outcome_line(counts: dict[str, int]) -> str:
    """6.15: «принято» — прошёл проверку админом, «продано» — состоялась сделка.

    Раньше и то и другое, вместе с отказом клиента, лежало в одном счётчике
    «Принято»: работник видел завышенную приёмку, а продажи не видел вовсе.
    Продано и отказ входят и в «принято» — приёмку они не отменяют.
    """
    accepted = sum(counts.get(k, 0) for k in config.ACCEPTED_STATUSES)
    return (f"Принято: {accepted} · продано: {counts.get('sold', 0)} · "
            f"отказ клиента: {counts.get('refused', 0)} · "
            f"отклонено: {counts.get('rejected', 0)}")


def list_text(leads, total, offset):
    if not leads:
        return "Пока пусто."
    head = f"Всего: {total}. Записи {offset + 1}–{offset + len(leads)}:"
    body = [
        f"#{l.id} {esc(l.name)} — {local(l.created_at)} — "
        f"{config.STATUS_LABELS.get(l.status, l.status)}"
        for l in leads
    ]
    return head + "\n" + "\n".join(body)


# Кнопки нижнего меню перехватывают ввод формы: add_router подключён последним,
# поэтому нажатие «Мои компании» посреди формы открывает раздел, а не становится
# названием города. Состояние при этом надо погасить, иначе следующее сообщение
# уйдёт в брошенную форму. set_state(None), а не clear(): в тех же данных у
# админа лежат фильтры списка.
@router.message(F.text == kb.BTN_MY)
async def my_list(message: Message, state: FSMContext, worker: Worker):
    await state.set_state(None)
    async with Session() as s:
        leads, total = await my_page(s, worker.id, 0)
    await message.answer(
        list_text(leads, total, 0),
        reply_markup=kb.leads_list_kb(leads, "mlp", "mcd", 0, total),
    )


@router.callback_query(F.data.startswith("mlp:"))
async def my_list_page(cb: CallbackQuery, worker: Worker):
    offset = await cb_id(cb)
    if offset is None:
        return
    async with Session() as s:
        leads, total = await my_page(s, worker.id, offset)
    await cb.answer()
    await safe_edit(
        cb.message,
        list_text(leads, total, offset),
        kb.leads_list_kb(leads, "mlp", "mcd", offset, total),
    )


def cancel_open(lead: Lead) -> bool:
    return (
        lead.cancelled_at is None
        and lead.status == "new"
        and lead.created_at + timedelta(minutes=config.CANCEL_WINDOW_MIN)
        > datetime.now(lead.created_at.tzinfo)
    )


@router.callback_query(F.data.startswith("mcd:"))
async def my_card(cb: CallbackQuery, worker: Worker):
    lead_id = await cb_id(cb)
    if lead_id is None:
        return
    async with Session() as s:
        lead = await s.get(Lead, lead_id)
        if not lead or lead.worker_id != worker.id or lead.deleted_at or lead.cancelled_at:
            await cb.answer("Запись недоступна", show_alert=True)
            return
        contacts = await get_contacts(s, lead_id)
    await cb.answer()
    can_edit = lead.status == "new"
    shown = await send_screenshot(cb.message, lead.screenshot_file_id)
    text = fmt_lead(lead, contacts)
    if not shown:
        text += '\n📷 Скриншот недоступен (сохранён прежним ботом).'
    await cb.message.answer(
        text, reply_markup=kb.my_card_kb(lead_id, can_edit, cancel_open(lead))
    )


@router.message(F.text == kb.BTN_MY_STATS)
async def my_stats(message: Message, state: FSMContext, worker: Worker):
    await state.set_state(None)
    base = [
        Lead.worker_id == worker.id,
        Lead.cancelled_at.is_(None),
        Lead.deleted_at.is_(None),
    ]
    async with Session() as s:
        total = await s.scalar(select(func.count()).select_from(Lead).where(*base))
        today = await s.scalar(
            select(func.count()).select_from(Lead)
            .where(*base, Lead.created_at >= day_start())
        )
        week = await s.scalar(
            select(func.count()).select_from(Lead)
            .where(*base, Lead.created_at >= day_start(6))
        )
        counts = await status_counts(s, base)
        # для остатка лимита — used_today, а не today: в квоте отменённые
        # считаются, в статистике «за сегодня» — нет
        used = await used_today(s, worker.id)
        # начисления по валютам: складывать доллары с евро нельзя, а продажа
        # в другой валюте однажды случится
        payouts = (await s.execute(
            select(Sale.currency,
                   func.coalesce(func.sum(case(
                       (Sale.paid_at.is_(None), Sale.amount_due), else_=0)), 0),
                   func.coalesce(func.sum(case(
                       (Sale.paid_at.isnot(None), Sale.amount_due), else_=0)), 0))
            .where(Sale.worker_id == worker.id, Sale.received_at.isnot(None))
            .group_by(Sale.currency).order_by(Sale.currency)
        )).all()
    limit = worker.daily_limit if worker.daily_limit is not None else config.DEFAULT_DAILY_LIMIT
    lines = [
        f"Всего добавлено: {total}", f"За сегодня: {today}",
        f"За 7 дней: {week}", outcome_line(counts),
        f"Осталось по лимиту сегодня: {max(0, limit - used)}",
        f"Комиссия: {worker.commission_pct}%",
    ]
    # только оплаченные клиентом: показывать «к выплате» до прихода денег —
    # обещание, которого мы не давали (7.15)
    for currency, due, paid in payouts:
        lines.append(f"Начислено {esc(currency)}: к выплате {due:.2f}, "
                     f"выплачено {paid:.2f}")
    await message.answer("\n".join(lines))


@router.message(F.text == kb.BTN_HELP)
async def help_text(message: Message, state: FSMContext):
    await state.set_state(None)
    await message.answer(config.INSTRUCTION_TEXT)


# --- отмена отправки ---------------------------------------------------------

@router.callback_query(F.data.startswith("lcx:"))
async def cancel_lead(cb: CallbackQuery, worker: Worker):
    lead_id = await cb_id(cb)
    if lead_id is None:
        return
    async with Session() as s, s.begin():
        lead = await s.get(Lead, lead_id, with_for_update=True)
        if not lead or lead.worker_id != worker.id or lead.deleted_at:
            await cb.answer("Запись недоступна", show_alert=True)
            return
        if lead.cancelled_at:
            await cb.answer("Уже отменено", show_alert=True)
            return
        if not cancel_open(lead):
            await cb.answer("Окно отмены истекло", show_alert=True)
            return
        # именно datetime, а не func.now(): при expire_on_commit=False в атрибуте
        # объекта осталась бы SQL-конструкция вместо даты
        now = datetime.now(config.TZ)
        lead.cancelled_at = now
        # tg_id, а не workers.id: в lead_events actor записан именно так,
        # и колонка на два разных пространства идентификаторов бесполезна
        lead.cancelled_by = cb.from_user.id
        await s.execute(
            update(Contact).where(Contact.lead_id == lead_id)
            .values(lead_cancelled_at=now)
        )
        # автостоп цепочки (решение 5 этапа): отменённому лиду письма не шлём
        await queue_service.cancel_drafts(s, lead_id, cb.from_user.id,
                                          "отмена отправки")
        log_event(s, lead_id, "cancel", cb.from_user.id)
    log.info("lead %s cancelled by tg_id=%s", lead_id, cb.from_user.id)
    await cb.answer()
    await cb.message.answer(f"Запись #{lead_id} отменена.", reply_markup=kb.worker_menu())


# --- редактирование (общий роутер: работник и админ) -------------------------

class Ed(StatesGroup):
    value = State()
    other = State()
    c_type = State()
    c_other = State()
    c_value = State()


CHOICE_FIELDS = {
    "country": (COUNTRY_NAMES, "Страна"),
    "language": (config.LANGUAGES, "Язык"),
    "niche": (config.NICHES, "Ниша"),
    "found_via": (config.FOUND_VIA, "Где нашли"),
}


async def load_editable(session, lead_id, worker, is_admin):
    lead = await session.get(Lead, lead_id)
    if not lead or lead.deleted_at:
        return None
    if is_admin:
        return lead
    if not worker or lead.worker_id != worker.id:
        return None
    if lead.cancelled_at or lead.status != "new":
        return None
    return lead


# --- переснять наблюдение ----------------------------------------------------
#
# Отдельная кнопка, а не поле в EDITABLE_FIELDS: наблюдение переснимается
# целиком (тип, артефакт, время съёмки), а не правится по кусочкам. Нужно это
# при TTL в 14 дней и после брака «наблюдение банальное» в очереди одобрения.

class Regap(StatesGroup):
    gap_type = State()
    gap_value = State()
    gap_note = State()


async def load_regap(session, lead_id, worker, is_admin):
    """Кому можно переснимать: админу — любой лид, работнику — свой неотменённый.

    Ограничения load_editable по статусу здесь не годятся: переснимают как раз
    старые лиды, которые давно вышли из «нового».
    """
    lead = await session.get(Lead, lead_id)
    if not lead or lead.deleted_at:
        return None
    if is_admin:
        return lead
    if not worker or lead.worker_id != worker.id or lead.cancelled_at:
        return None
    return lead


async def _save_regap(target: Message, state: FSMContext, worker, is_admin):
    d = await state.get_data()
    lead_id = d.get("lead_id")
    async with Session() as s, s.begin():
        lead = await load_regap(s, lead_id, worker, is_admin)
        if lead is None:
            await state.clear()
            await target.answer("Запись недоступна.", reply_markup=kb.menu(is_admin))
            return
        old = gv.gap_line(lead.gap_type, lead.gap_value, lead.gap_note)
        lead.gap_type = d["gap_type"]
        lead.gap_value = d.get("gap_value")
        lead.gap_note = d.get("gap_note")
        lead.gap_screenshot = d.get("gap_screenshot")
        lead.gap_captured_at = datetime.now(config.TZ)
        lead.gap_seconds = d.get("gap_seconds")
        # автопроверка (правило 7) считается по конкретному замеру, а замер новый
        lead.gap_auto_verified = None
        new = gv.gap_line(lead.gap_type, lead.gap_value, lead.gap_note)
        log_event(s, lead_id, "gap_recapture", worker.tg_id, "gap_type", old, new)
        if d.get("gap_too_fast"):
            log_event(s, lead_id, "observation_too_fast", worker.tg_id)
    await state.clear()
    await target.answer(f"✅ Наблюдение по #{lead_id} обновлено: {esc(new)}",
                        reply_markup=kb.menu(is_admin))


@edit_router.callback_query(F.data.startswith("rgp:"))
async def regap_start(cb: CallbackQuery, state: FSMContext, worker, is_admin: bool):
    lead_id = await cb_id(cb)
    if lead_id is None:
        return
    async with Session() as s:
        lead = await load_regap(s, lead_id, worker, is_admin)
    if not lead:
        await cb.answer("Запись недоступна", show_alert=True)
        return
    await state.clear()
    await state.set_data({"lead_id": lead_id, "website_url": lead.website_url})
    await cb.answer()
    await cb.message.answer(f"Переснимаем наблюдение по #{lead_id}.")
    await _ask_gap_type(cb.message, state, Regap)


@edit_router.callback_query(Regap.gap_type, F.data.startswith("gp:"))
async def regap_type(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await _gap_type_chosen(cb.message, state, Regap, cb.data.split(":")[1])


@edit_router.callback_query(Regap.gap_value, F.data.startswith("gv:"))
async def regap_choice(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    d = await state.get_data()
    value = pick(gv.CHOICE_OPTIONS.get(d.get("gap_type"), []), cb.data.split(":")[1])
    if value is None:
        await cb.message.answer(STALE)
        return
    await _gap_value_given(cb.message, state, Regap, value)


@edit_router.message(Regap.gap_value, F.photo)
async def regap_photo(message: Message, state: FSMContext):
    await _gap_photo(message, state, Regap)


@edit_router.message(Regap.gap_value)
async def regap_value(message: Message, state: FSMContext):
    await _gap_value_given(message, state, Regap, message.text or "")


@edit_router.callback_query(Regap.gap_note, F.data == "gn:skip")
async def regap_note_skip(cb: CallbackQuery, state: FSMContext, worker, is_admin: bool):
    await cb.answer()
    await _finish_gap(cb.message, state, Regap, worker, is_admin)


@edit_router.message(Regap.gap_note)
async def regap_note(message: Message, state: FSMContext, worker, is_admin: bool):
    note, err = gv.check_note(message.text or "")
    if err:
        await message.answer(err, reply_markup=kb.skip_kb("gn"))
        return
    await state.update_data(gap_note=note)
    await _finish_gap(message, state, Regap, worker, is_admin)


@edit_router.callback_query(F.data.startswith("led:"))
async def edit_menu(cb: CallbackQuery, state: FSMContext, worker, is_admin: bool):
    lead_id = await cb_id(cb)
    if lead_id is None:
        return
    async with Session() as s:
        lead = await load_editable(s, lead_id, worker, is_admin)
    if not lead:
        await cb.answer("Редактирование недоступно", show_alert=True)
        return
    await state.clear()
    await cb.answer()
    await cb.message.answer(
        f"Что изменить в #{lead_id}?", reply_markup=kb.edit_fields_kb(lead_id)
    )


async def apply_field(lead_id, field, value, actor_tg_id, worker, is_admin) -> str | None:
    # setattr пишет в произвольный атрибут модели, поэтому имя поля берём только
    # из белого списка: иначе ошибка в клавиатуре открыла бы запись в status
    if field not in config.FIELD_LABELS:
        log.warning("edit of non-editable field %r by tg_id=%s", field, actor_tg_id)
        return "Это поле нельзя редактировать."
    try:
        async with Session() as s, s.begin():
            # права проверялись при открытии меню; пока работник набирал текст,
            # админ мог сменить статус записи или удалить её
            lead = await load_editable(s, lead_id, worker, is_admin)
            if lead is None:
                return "Редактирование больше недоступно."
            old = getattr(lead, field)
            setattr(lead, field, value)
            if field == "website_url":
                lead.domain_norm = normalize_domain(value)
            if field == "country":
                # регион разбора номеров идёт от страны лида: без пересчёта
                # value_norm остаётся посчитанным по старой стране, и дедуп
                # телефонов этого лида ломается (массовая версия того же —
                # tools/renorm_phones.py)
                region = config.COUNTRY_ISO.get(value)
                phones = await s.scalars(
                    select(Contact).where(
                        Contact.lead_id == lead.id,
                        Contact.ctype == "phone",
                        Contact.deleted_at.is_(None),
                    )
                )
                for c in phones:
                    c.value_norm = normalize_phone(c.value, region)
            log_event(s, lead_id, "field_edit", actor_tg_id, field,
                      None if old is None else str(old), str(value))
    except IntegrityError as e:
        log.warning("dup on edit lead=%s field=%s: %s", lead_id, field, e.orig)
        return dup_message(e)
    return None


async def _finish_edit(target: Message, state: FSMContext, lead_id, field, value,
                       actor_tg_id, worker, is_admin):
    err = await apply_field(lead_id, field, value, actor_tg_id, worker, is_admin)
    await state.clear()
    if err:
        await target.answer(f"{err} Старое значение осталось.")
        return
    await target.answer(
        f"Готово: {config.FIELD_LABELS.get(field, field)} обновлено.",
        reply_markup=kb.edit_fields_kb(lead_id),
    )


@edit_router.callback_query(F.data.startswith("ef:"))
async def edit_field(cb: CallbackQuery, state: FSMContext, worker, is_admin: bool):
    parts = cb.data.split(":")
    if len(parts) != 3 or parts[2] not in config.FIELD_LABELS:
        await cb.answer("Это поле нельзя редактировать", show_alert=True)
        return
    field = parts[2]
    lead_id = await cb_id(cb)
    if lead_id is None:
        return
    async with Session() as s:
        lead = await load_editable(s, lead_id, worker, is_admin)
    if not lead:
        await cb.answer("Редактирование недоступно", show_alert=True)
        return
    await state.update_data(lead_id=lead_id, field=field)
    await cb.answer()
    if field in CHOICE_FIELDS:
        options, label = CHOICE_FIELDS[field]
        await state.set_state(Ed.other)
        await state.update_data(choice=True)
        await cb.message.answer(f"{label}:", reply_markup=kb.choices_kb(options, "ev"))
        return
    await state.set_state(Ed.value)
    await state.update_data(choice=False)
    prompt = {
        "screenshot_file_id": "Пришлите новое фото:",
        "website_url": "Новая ссылка на сайт:",
        "source_url": "Новая ссылка на источник:",
    }.get(field, f"Новое значение ({config.FIELD_LABELS.get(field, field)}):")
    await cb.message.answer(prompt, reply_markup=kb.cancel_kb())


@edit_router.callback_query(Ed.other, F.data.startswith("ev:"))
async def edit_choice(cb: CallbackQuery, state: FSMContext, worker, is_admin: bool):
    key = cb.data.split(":")[1]
    d = await state.get_data()
    field, lead_id = d.get("field"), d.get("lead_id")
    if field not in CHOICE_FIELDS or lead_id is None:
        await cb.answer(STALE, show_alert=True)
        return
    await cb.answer()
    if key == "oth":
        await state.update_data(choice=False)
        await cb.message.answer("Впишите значение:", reply_markup=kb.cancel_kb())
        return
    val = pick(CHOICE_FIELDS[field][0], key)
    if val is None:
        await cb.message.answer(STALE)
        return
    await _finish_edit(
        cb.message, state, lead_id, field, val, cb.from_user.id, worker, is_admin
    )


@edit_router.message(Ed.other)
async def edit_choice_other(message: Message, state: FSMContext, worker, is_admin: bool):
    d = await state.get_data()
    val = (message.text or "").strip()
    if not val:
        await message.answer("Впишите значение текстом:", reply_markup=kb.cancel_kb())
        return
    field, lead_id = d.get("field"), d.get("lead_id")
    if field is None or lead_id is None:
        await state.clear()
        await message.answer(STALE)
        return
    await _finish_edit(
        message, state, lead_id, field, val, message.from_user.id, worker, is_admin
    )


@edit_router.message(Ed.value, F.photo)
async def edit_photo(message: Message, state: FSMContext, worker, is_admin: bool):
    d = await state.get_data()
    lead_id = d.get("lead_id")
    if d.get("field") != "screenshot_file_id" or lead_id is None:
        return
    await _finish_edit(
        message, state, lead_id, "screenshot_file_id",
        message.photo[-1].file_id, message.from_user.id, worker, is_admin,
    )


@edit_router.message(Ed.value)
async def edit_value(message: Message, state: FSMContext, worker, is_admin: bool):
    d = await state.get_data()
    field = d.get("field")
    lead_id = d.get("lead_id")
    val = (message.text or "").strip()
    if field is None or lead_id is None:
        await state.clear()
        await message.answer(STALE)
        return
    if field == "screenshot_file_id":
        await message.answer("Пришлите фото:", reply_markup=kb.cancel_kb())
        return
    if not val:
        await message.answer("Введите значение текстом:", reply_markup=kb.cancel_kb())
        return
    if field in ("website_url", "source_url") and not is_url(val):
        await message.answer(
            "Ссылка должна начинаться с http:// или https:// и содержать точку:",
            reply_markup=kb.cancel_kb(),
        )
        return
    await _finish_edit(
        message, state, lead_id, field, val, message.from_user.id, worker, is_admin
    )


# --- редактирование контактов ------------------------------------------------

@edit_router.callback_query(F.data.startswith("ecm:"))
async def edit_contacts_menu(cb: CallbackQuery, state: FSMContext, worker, is_admin: bool):
    lead_id = await cb_id(cb)
    if lead_id is None:
        return
    async with Session() as s:
        lead = await load_editable(s, lead_id, worker, is_admin)
        if not lead:
            await cb.answer("Редактирование недоступно", show_alert=True)
            return
        contacts = await get_contacts(s, lead_id)
    await state.clear()
    await cb.answer()
    await cb.message.answer(
        "Контакты:", reply_markup=kb.contacts_menu_kb(lead_id, contacts)
    )


@edit_router.callback_query(F.data.startswith("eca:"))
async def contact_add(cb: CallbackQuery, state: FSMContext, worker, is_admin: bool):
    lead_id = await cb_id(cb)
    if lead_id is None:
        return
    async with Session() as s:
        lead = await load_editable(s, lead_id, worker, is_admin)
        if not lead:
            await cb.answer("Редактирование недоступно", show_alert=True)
            return
        contacts = await get_contacts(s, lead_id)
    if len(contacts) >= config.MAX_CONTACTS:
        await cb.answer("Максимум 10 контактов", show_alert=True)
        return
    await state.set_state(Ed.c_type)
    await state.update_data(lead_id=lead_id, contact_id=None)
    await cb.answer()
    labels = [label for _, label in config.CONTACT_TYPES]
    await cb.message.answer("Тип контакта:", reply_markup=kb.choices_kb(labels, "ect"))


@edit_router.callback_query(Ed.c_type, F.data.startswith("ect:"))
async def contact_type(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    await cb.answer()
    if key == "oth":
        await state.set_state(Ed.c_other)
        await cb.message.answer("Впишите название канала:", reply_markup=kb.cancel_kb())
        return
    pair = pick(config.CONTACT_TYPES, key)
    if pair is None:
        await cb.message.answer(STALE)
        return
    await state.update_data(ctype=pair[0], ctype_other=None)
    await state.set_state(Ed.c_value)
    await cb.message.answer("Значение:", reply_markup=kb.cancel_kb())


@edit_router.message(Ed.c_other)
async def contact_type_other(message: Message, state: FSMContext):
    val = (message.text or "").strip()
    if not val:
        await message.answer("Название канала текстом:", reply_markup=kb.cancel_kb())
        return
    await state.update_data(ctype="other", ctype_other=val)
    await state.set_state(Ed.c_value)
    await message.answer("Значение:", reply_markup=kb.cancel_kb())


@edit_router.callback_query(F.data.startswith("ece:"))
async def contact_edit(cb: CallbackQuery, state: FSMContext, worker, is_admin: bool):
    contact_id = await cb_id(cb)
    if contact_id is None:
        return
    async with Session() as s:
        contact = await s.get(Contact, contact_id)
        if not contact or contact.deleted_at:
            await cb.answer("Контакт недоступен", show_alert=True)
            return
        lead = await load_editable(s, contact.lead_id, worker, is_admin)
    if not lead:
        await cb.answer("Редактирование недоступно", show_alert=True)
        return
    await state.set_state(Ed.c_value)
    await state.update_data(
        lead_id=contact.lead_id, contact_id=contact_id,
        ctype=contact.ctype, ctype_other=contact.ctype_other,
    )
    await cb.answer()
    await cb.message.answer("Новое значение:", reply_markup=kb.cancel_kb())


async def save_contact_value(lead_id: int, contact_id: int | None, ctype: str,
                             ctype_other: str | None, value: str,
                             norm: str | None, actor_tg_id: int) -> bool:
    """Пишет значение контакта. False — контакта уже нет, писать некуда.

    IntegrityError уходит наверх: сообщение о дубле собирает хендлер.
    """
    async with Session() as s, s.begin():
        if contact_id:
            contact = await s.get(Contact, contact_id)
            if contact is None or contact.deleted_at:
                return False
            old = contact.value
            contact.value = value
            contact.value_norm = norm
            if value != old:
                # вердикт проверки относился к прежнему адресу (9.29)
                email_verify.forget(contact)
            log_event(s, lead_id, "contact_edit", actor_tg_id,
                      contact_label(contact), old, value)
        else:
            contact = Contact(lead_id=lead_id, ctype=ctype,
                              ctype_other=ctype_other,
                              value=value, value_norm=norm)
            s.add(contact)
            log_event(s, lead_id, "contact_add", actor_tg_id,
                      contact_label(contact), None, value)
    return True


@edit_router.message(Ed.c_value)
async def contact_value(message: Message, state: FSMContext, worker, is_admin: bool):
    d = await state.get_data()
    val = (message.text or "").strip()
    if not val:
        await message.answer("Введите значение текстом:", reply_markup=kb.cancel_kb())
        return
    ctype, lead_id = d.get("ctype"), d.get("lead_id")
    if ctype is None or lead_id is None:
        await state.clear()
        await message.answer(STALE)
        return
    async with Session() as s:
        lead = await load_editable(s, lead_id, worker, is_admin)
    if lead is None:
        await state.clear()
        await message.answer("Редактирование больше недоступно.")
        return
    # регион нужен до проверки: contact_error и value_norm обязаны считать одинаково
    region = config.COUNTRY_ISO.get(lead.country)
    err = contact_error(ctype, val, region)
    if err:
        await message.answer(err, reply_markup=kb.cancel_kb())
        return
    if ctype == "phone":
        async with Session() as s:
            dup = await phone_dup_exists(
                s, normalize_phone(val, region),
                exclude_contact_id=d.get("contact_id"),
            )
        if dup:
            await message.answer(
                "❌ Такой телефон уже есть в базе. Введите другой номер:",
                reply_markup=kb.cancel_kb(),
            )
            return
    try:
        alive = await save_contact_value(
            lead_id, d.get("contact_id"), ctype, d.get("ctype_other"), val,
            normalize_phone(val, region) if ctype == "phone" else None,
            message.from_user.id,
        )
    except IntegrityError as e:
        log.warning("dup on contact lead=%s: %s", lead_id, e.orig)
        await state.clear()
        await message.answer(f"{dup_message(e)} Старое значение осталось.")
        return
    if not alive:
        await state.clear()
        await message.answer("Контакт больше недоступен.")
        return
    await state.clear()
    async with Session() as s:
        contacts = await get_contacts(s, lead_id)
    await message.answer("Готово.", reply_markup=kb.contacts_menu_kb(lead_id, contacts))


@edit_router.callback_query(F.data.startswith("ecd:"))
async def contact_delete(cb: CallbackQuery, worker, is_admin: bool):
    contact_id = await cb_id(cb)
    if contact_id is None:
        return
    async with Session() as s, s.begin():
        contact = await s.get(Contact, contact_id)
        if not contact or contact.deleted_at:
            await cb.answer("Контакт недоступен", show_alert=True)
            return
        lead = await load_editable(s, contact.lead_id, worker, is_admin)
        if not lead:
            await cb.answer("Редактирование недоступно", show_alert=True)
            return
        alive = await get_contacts(s, contact.lead_id)
        if len(alive) <= 1:
            await cb.answer("Должен остаться хотя бы один контакт", show_alert=True)
            return
        contact.deleted_at = datetime.now(config.TZ)
        log_event(s, contact.lead_id, "contact_delete", cb.from_user.id,
                  contact_label(contact), contact.value, None)
        lead_id = contact.lead_id
    async with Session() as s:
        contacts = await get_contacts(s, lead_id)
    await cb.answer("Удалено")
    await cb.message.edit_reply_markup(
        reply_markup=kb.contacts_menu_kb(lead_id, contacts)
    )


# Кнопки формы добавления отфильтрованы по состоянию. Когда форма уже отправлена,
# отменена или вычищена, состояния нет, ни один хендлер не срабатывает, и в чате
# навсегда виснут часики.
# Хендлер объявлен последним в add_router, а сам add_router подключается
# последним в main.py — поэтому перехватывает только то, что не разобрали выше.
FORM_PREFIXES = (
    "ws:", "co:", "la:", "ni:", "ct:", "cm:", "rt:", "nt:", "sc:", "fv:", "cf:", "dup:",
)


@add_router.callback_query(F.data.startswith(FORM_PREFIXES))
async def stale_form_button(cb: CallbackQuery):
    await cb.answer(STALE, show_alert=True)


async def edits_count(session, lead_id) -> int:
    return await session.scalar(
        select(func.count()).select_from(LeadEvent).where(
            LeadEvent.lead_id == lead_id,
            LeadEvent.event.in_(
                ["field_edit", "contact_add", "contact_edit", "contact_delete"]
            ),
        )
    )
