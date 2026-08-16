import html
import logging
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

import config
import keyboards as kb
from dedup import normalize_domain, normalize_phone
from models import (
    Contact, Lead, LeadEvent, Session, Worker, day_start, dup_message, log_event,
)

log = logging.getLogger(__name__)

router = Router()
edit_router = Router()


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
        "Контакты:",
    ]
    for c in contacts:
        lines.append(f"  • {esc(contact_label(c))}: {esc(c.value)}")
    if not contacts:
        lines.append("  —")
    lines.append(f"Добавлено: {local(lead.created_at)}")
    if lead.possible_duplicate:
        lines.append("⚠️ Помечено как возможный дубликат")
    if admin:
        if author:
            lines.append(f"Работник: {esc(author.name)} (id {author.tg_id})")
            lines.append(f'<a href="tg://user?id={author.tg_id}">Открыть в Telegram</a>')
        lines.append(f"Редактировалось: {edits} раз")
        if lead.draft_url:
            lines.append(f"Черновик: {esc(lead.draft_url)}")
        if lead.admin_note:
            lines.append(f"Моя заметка: {esc(lead.admin_note)}")
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
        "Контакты:",
    ]
    for c in d["contacts"]:
        name = c["ctype_other"] or config.CONTACT_TYPE_LABELS.get(c["ctype"], c["ctype"])
        lines.append(f"  • {esc(name)}: {esc(c['value'])}")
    return "\n".join(lines)


# --- регистрация -------------------------------------------------------------

class Reg(StatesGroup):
    code = State()
    name = State()


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, worker: Worker | None):
    await state.clear()
    if worker:
        await message.answer(
            f"С возвращением, {esc(worker.name)}!", reply_markup=kb.worker_menu()
        )
        return
    await state.set_state(Reg.code)
    await message.answer("Введите код доступа:")


@router.message(Reg.code)
async def reg_code(message: Message, state: FSMContext):
    if message.text != config.ACCESS_CODE:
        await message.answer("Неверный код. Попробуйте ещё раз:")
        return
    await state.set_state(Reg.name)
    await message.answer("Код принят. Как вас зовут?")


@router.message(Reg.name)
async def reg_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("Введите имя текстом:")
        return
    async with Session() as s, s.begin():
        s.add(Worker(tg_id=message.from_user.id, name=name))
    await state.clear()
    log.info("worker registered tg_id=%s", message.from_user.id)
    await message.answer(f"Готово, {esc(name)}!", reply_markup=kb.worker_menu())


# --- добавление компании -----------------------------------------------------

class Add(StatesGroup):
    name = State()
    website = State()
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
    return await session.scalar(
        select(func.count()).select_from(Lead).where(
            Lead.worker_id == worker_id,
            Lead.cancelled_at.is_(None),
            Lead.deleted_at.is_(None),
            Lead.created_at >= day_start(),
        )
    )


@router.message(F.text == kb.BTN_ADD)
async def add_start(message: Message, state: FSMContext, worker: Worker | None):
    if not worker:
        await message.answer("Сначала /start")
        return
    limit = worker.daily_limit if worker.daily_limit is not None else config.DEFAULT_DAILY_LIMIT
    async with Session() as s:
        used = await used_today(s, worker.id)
    if used >= limit:
        await message.answer("Лимит на сегодня исчерпан.")
        return
    await state.set_state(Add.name)
    await state.update_data(contacts=[])
    await message.answer("1/12. Название компании:", reply_markup=kb.cancel_kb())


@router.message(Add.name)
async def add_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("Название текстом:", reply_markup=kb.cancel_kb())
        return
    await state.update_data(name=name)
    await state.set_state(Add.website)
    await message.answer("2/12. Ссылка на сайт:", reply_markup=kb.website_kb())


async def _after_website(target: Message, state: FSMContext):
    await state.set_state(Add.source_url)
    await target.answer(
        "3/12. Ссылка на источник (Google Maps, соцсеть, каталог):",
        reply_markup=kb.cancel_kb(),
    )


@router.callback_query(Add.website, F.data == "ws:none")
async def add_website_none(cb: CallbackQuery, state: FSMContext):
    await state.update_data(website_url=None, domain_norm=None)
    await cb.answer()
    await _after_website(cb.message, state)


@router.message(Add.website)
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
        await state.clear()
        await message.answer(
            "❌ Эта компания уже есть в базе. Добавление отменено.",
            reply_markup=kb.worker_menu(),
        )
        return
    await state.update_data(website_url=raw, domain_norm=dom)
    await _after_website(message, state)


@router.message(Add.source_url)
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
        "4/12. Страна:", reply_markup=kb.choices_kb(COUNTRY_NAMES, "co")
    )


async def _ask_city(target: Message, state: FSMContext):
    await state.set_state(Add.city)
    await target.answer("5/12. Город:", reply_markup=kb.cancel_kb())


@router.callback_query(Add.country, F.data.startswith("co:"))
async def add_country(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    await cb.answer()
    if key == "oth":
        await state.set_state(Add.country_other)
        await cb.message.answer("Впишите страну:", reply_markup=kb.cancel_kb())
        return
    await state.update_data(country=COUNTRY_NAMES[int(key)])
    await _ask_city(cb.message, state)


@router.message(Add.country_other)
async def add_country_other(message: Message, state: FSMContext):
    val = (message.text or "").strip()
    if not val:
        await message.answer("Впишите страну текстом:", reply_markup=kb.cancel_kb())
        return
    await state.update_data(country=val)
    await _ask_city(message, state)


@router.message(Add.city)
async def add_city(message: Message, state: FSMContext):
    val = (message.text or "").strip()
    if not val:
        await message.answer("Город текстом:", reply_markup=kb.cancel_kb())
        return
    await state.update_data(city=val)
    await state.set_state(Add.language)
    await message.answer(
        "6/12. Язык компании:", reply_markup=kb.choices_kb(config.LANGUAGES, "la")
    )


async def _ask_niche(target: Message, state: FSMContext):
    await state.set_state(Add.niche)
    await target.answer("7/12. Ниша:", reply_markup=kb.choices_kb(config.NICHES, "ni"))


@router.callback_query(Add.language, F.data.startswith("la:"))
async def add_language(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    await cb.answer()
    if key == "oth":
        await state.set_state(Add.language_other)
        await cb.message.answer("Впишите язык:", reply_markup=kb.cancel_kb())
        return
    await state.update_data(language=config.LANGUAGES[int(key)])
    await _ask_niche(cb.message, state)


@router.message(Add.language_other)
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
    await target.answer("8/12. Тип контакта:", reply_markup=kb.choices_kb(labels, "ct"))


@router.callback_query(Add.niche, F.data.startswith("ni:"))
async def add_niche(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    await cb.answer()
    if key == "oth":
        await state.set_state(Add.niche_other)
        await cb.message.answer("Впишите нишу:", reply_markup=kb.cancel_kb())
        return
    await state.update_data(niche=config.NICHES[int(key)])
    await _ask_contact_type(cb.message, state)


@router.message(Add.niche_other)
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


@router.callback_query(Add.c_type, F.data.startswith("ct:"))
async def add_contact_type(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    await cb.answer()
    if key == "oth":
        await state.set_state(Add.c_other)
        await cb.message.answer("Впишите название канала:", reply_markup=kb.cancel_kb())
        return
    ctype = config.CONTACT_TYPES[int(key)][0]
    await state.update_data(cur_type=ctype, cur_other=None)
    await _ask_contact_value(cb.message, state)


@router.message(Add.c_other)
async def add_contact_other(message: Message, state: FSMContext):
    val = (message.text or "").strip()
    if not val:
        await message.answer("Название канала текстом:", reply_markup=kb.cancel_kb())
        return
    await state.update_data(cur_type="other", cur_other=val)
    await _ask_contact_value(message, state)


def contact_error(ctype: str, value: str) -> str | None:
    if ctype == "phone":
        if not normalize_phone(value):
            return "Не похоже на номер. Введите телефон, лучше в формате +380…:"
    elif ctype == "email":
        if not is_email(value):
            return "Email должен содержать @ и домен. Повторите:"
    elif value.startswith("http") and not is_url(value):
        return "Ссылка должна начинаться с http:// или https:// и содержать точку:"
    return None


@router.message(Add.c_value)
async def add_contact_value(message: Message, state: FSMContext):
    val = (message.text or "").strip()
    d = await state.get_data()
    if not val:
        await message.answer("Введите значение текстом:", reply_markup=kb.cancel_kb())
        return
    err = contact_error(d["cur_type"], val)
    if err:
        await message.answer(err, reply_markup=kb.cancel_kb())
        return
    contacts = d["contacts"]
    contacts.append({"ctype": d["cur_type"], "ctype_other": d.get("cur_other"), "value": val})
    await state.update_data(contacts=contacts)
    if len(contacts) >= config.MAX_CONTACTS:
        await _ask_rating(message, state)
        return
    await state.set_state(Add.c_more)
    await message.answer("Добавить ещё контакт?", reply_markup=kb.contact_more_kb())


async def _ask_rating(target: Message, state: FSMContext):
    await state.set_state(Add.rating)
    await target.answer(
        "9/12. Рейтинг Google и число отзывов:", reply_markup=kb.skip_kb("rt")
    )


@router.callback_query(Add.c_more, F.data == "cm:yes")
async def add_more_yes(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await _ask_contact_type(cb.message, state)


@router.callback_query(Add.c_more, F.data == "cm:next")
async def add_more_next(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await _ask_rating(cb.message, state)


async def _ask_note(target: Message, state: FSMContext):
    await state.set_state(Add.note)
    await target.answer("10/12. Заметка:", reply_markup=kb.skip_kb("nt"))


@router.callback_query(Add.rating, F.data == "rt:skip")
async def add_rating_skip(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await _ask_note(cb.message, state)


@router.message(Add.rating)
async def add_rating(message: Message, state: FSMContext):
    await state.update_data(google_rating=(message.text or "").strip() or None)
    await _ask_note(message, state)


async def _ask_screenshot(target: Message, state: FSMContext):
    await state.set_state(Add.screenshot)
    await target.answer("11/12. Скриншот сайта (фото):", reply_markup=kb.skip_kb("sc"))


@router.callback_query(Add.note, F.data == "nt:skip")
async def add_note_skip(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await _ask_screenshot(cb.message, state)


@router.message(Add.note)
async def add_note(message: Message, state: FSMContext):
    await state.update_data(note=(message.text or "").strip() or None)
    await _ask_screenshot(message, state)


async def _ask_found_via(target: Message, state: FSMContext):
    await state.set_state(Add.found_via)
    await target.answer(
        "12/12. Где нашли:", reply_markup=kb.choices_kb(config.FOUND_VIA, "fv")
    )


@router.callback_query(Add.screenshot, F.data == "sc:skip")
async def add_screenshot_skip(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await _ask_found_via(cb.message, state)


@router.message(Add.screenshot, F.photo)
async def add_screenshot(message: Message, state: FSMContext):
    await state.update_data(screenshot_file_id=message.photo[-1].file_id)
    await _ask_found_via(message, state)


@router.message(Add.screenshot)
async def add_screenshot_bad(message: Message, state: FSMContext):
    await message.answer("Пришлите фото или нажмите «Пропустить».",
                         reply_markup=kb.skip_kb("sc"))


async def _show_summary(target: Message, state: FSMContext):
    d = await state.get_data()
    await state.set_state(Add.confirm)
    await target.answer(fmt_summary(d), reply_markup=kb.confirm_kb())


@router.callback_query(Add.found_via, F.data.startswith("fv:"))
async def add_found_via(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    await cb.answer()
    if key == "oth":
        await state.set_state(Add.found_via_other)
        await cb.message.answer("Впишите источник:", reply_markup=kb.cancel_kb())
        return
    await state.update_data(found_via=config.FOUND_VIA[int(key)])
    await _show_summary(cb.message, state)


@router.message(Add.found_via_other)
async def add_found_via_other(message: Message, state: FSMContext):
    val = (message.text or "").strip()
    if not val:
        await message.answer("Впишите источник текстом:", reply_markup=kb.cancel_kb())
        return
    await state.update_data(found_via=val)
    await _show_summary(message, state)


@router.callback_query(Add.confirm, F.data == "cf:redo")
async def add_redo(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(Add.name)
    await state.set_data({"contacts": []})
    await cb.message.answer("1/12. Название компании:", reply_markup=kb.cancel_kb())


async def save_lead(d: dict, worker: Worker, possible_dup: bool) -> int:
    region = config.COUNTRY_ISO.get(d["country"])
    async with Session() as s, s.begin():
        lead = Lead(
            worker_id=worker.id, name=d["name"], website_url=d.get("website_url"),
            domain_norm=d.get("domain_norm"), source_url=d["source_url"],
            country=d["country"], city=d["city"], language=d["language"],
            niche=d["niche"], google_rating=d.get("google_rating"), note=d.get("note"),
            screenshot_file_id=d.get("screenshot_file_id"), found_via=d["found_via"],
            possible_duplicate=possible_dup,
        )
        s.add(lead)
        await s.flush()
        for c in d["contacts"]:
            s.add(Contact(
                lead_id=lead.id, ctype=c["ctype"], ctype_other=c["ctype_other"],
                value=c["value"],
                value_norm=normalize_phone(c["value"], region) if c["ctype"] == "phone" else None,
            ))
        return lead.id


async def _commit_and_reply(cb: CallbackQuery, state: FSMContext, worker: Worker,
                            possible_dup: bool):
    d = await state.get_data()
    try:
        lead_id = await save_lead(d, worker, possible_dup)
    except IntegrityError as e:
        log.warning("dup on insert: %s", e.orig)
        await state.clear()
        await cb.message.answer(dup_message(e), reply_markup=kb.worker_menu())
        return
    await state.clear()
    await cb.message.answer(
        f"✅ Компания #{lead_id} принята", reply_markup=kb.saved_kb(lead_id)
    )


@router.callback_query(Add.confirm, F.data == "cf:send")
async def add_send(cb: CallbackQuery, state: FSMContext, worker: Worker):
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
    await _commit_and_reply(cb, state, worker, False)


@router.callback_query(Add.dup, F.data == "dup:yes")
async def add_send_dup(cb: CallbackQuery, state: FSMContext, worker: Worker):
    await cb.answer()
    await _commit_and_reply(cb, state, worker, True)


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


@router.message(F.text == kb.BTN_MY)
async def my_list(message: Message, worker: Worker):
    async with Session() as s:
        leads, total = await my_page(s, worker.id, 0)
    await message.answer(
        list_text(leads, total, 0),
        reply_markup=kb.leads_list_kb(leads, "mlp", "mcd", 0, total),
    )


@router.callback_query(F.data.startswith("mlp:"))
async def my_list_page(cb: CallbackQuery, worker: Worker):
    offset = int(cb.data.split(":")[1])
    async with Session() as s:
        leads, total = await my_page(s, worker.id, offset)
    await cb.answer()
    await cb.message.edit_text(
        list_text(leads, total, offset),
        reply_markup=kb.leads_list_kb(leads, "mlp", "mcd", offset, total),
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
    lead_id = int(cb.data.split(":")[1])
    async with Session() as s:
        lead = await s.get(Lead, lead_id)
        if not lead or lead.worker_id != worker.id or lead.deleted_at or lead.cancelled_at:
            await cb.answer("Запись недоступна", show_alert=True)
            return
        contacts = await get_contacts(s, lead_id)
    await cb.answer()
    can_edit = lead.status == "new"
    if lead.screenshot_file_id:
        await cb.message.answer_photo(lead.screenshot_file_id)
    await cb.message.answer(
        fmt_lead(lead, contacts),
        reply_markup=kb.my_card_kb(lead_id, can_edit, cancel_open(lead)),
    )


@router.message(F.text == kb.BTN_MY_STATS)
async def my_stats(message: Message, worker: Worker):
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
        accepted = await s.scalar(
            select(func.count()).select_from(Lead)
            .where(*base, Lead.status.in_(config.ACCEPTED_STATUSES))
        )
    limit = worker.daily_limit if worker.daily_limit is not None else config.DEFAULT_DAILY_LIMIT
    await message.answer(
        f"Всего добавлено: {total}\nЗа сегодня: {today}\nЗа 7 дней: {week}\n"
        f"Принято: {accepted}\nОсталось по лимиту сегодня: {max(0, limit - today)}"
    )


@router.message(F.text == kb.BTN_HELP)
async def help_text(message: Message):
    await message.answer(config.INSTRUCTION_TEXT)


# --- отмена отправки ---------------------------------------------------------

@router.callback_query(F.data.startswith("lcx:"))
async def cancel_lead(cb: CallbackQuery, worker: Worker):
    lead_id = int(cb.data.split(":")[1])
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
        now = func.now()
        lead.cancelled_at = now
        lead.cancelled_by = worker.id
        await s.execute(
            update(Contact).where(Contact.lead_id == lead_id)
            .values(lead_cancelled_at=now)
        )
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


@edit_router.callback_query(F.data.startswith("led:"))
async def edit_menu(cb: CallbackQuery, state: FSMContext, worker, is_admin: bool):
    lead_id = int(cb.data.split(":")[1])
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


async def apply_field(lead_id, field, value, actor_tg_id) -> str | None:
    try:
        async with Session() as s, s.begin():
            lead = await s.get(Lead, lead_id)
            old = getattr(lead, field)
            setattr(lead, field, value)
            if field == "website_url":
                lead.domain_norm = normalize_domain(value)
            log_event(s, lead_id, "field_edit", actor_tg_id, field, str(old), str(value))
    except IntegrityError as e:
        log.warning("dup on edit lead=%s field=%s: %s", lead_id, field, e.orig)
        return dup_message(e)
    return None


async def _finish_edit(target: Message, state: FSMContext, lead_id, field, value,
                       actor_tg_id):
    err = await apply_field(lead_id, field, value, actor_tg_id)
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
    _, lead_id, field = cb.data.split(":")
    lead_id = int(lead_id)
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
async def edit_choice(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    d = await state.get_data()
    options = CHOICE_FIELDS[d["field"]][0]
    await cb.answer()
    if key == "oth":
        await state.update_data(choice=False)
        await cb.message.answer("Впишите значение:", reply_markup=kb.cancel_kb())
        return
    await _finish_edit(
        cb.message, state, d["lead_id"], d["field"], options[int(key)], cb.from_user.id
    )


@edit_router.message(Ed.other)
async def edit_choice_other(message: Message, state: FSMContext):
    d = await state.get_data()
    val = (message.text or "").strip()
    if not val:
        await message.answer("Впишите значение текстом:", reply_markup=kb.cancel_kb())
        return
    await _finish_edit(message, state, d["lead_id"], d["field"], val, message.from_user.id)


@edit_router.message(Ed.value, F.photo)
async def edit_photo(message: Message, state: FSMContext):
    d = await state.get_data()
    if d["field"] != "screenshot_file_id":
        return
    await _finish_edit(
        message, state, d["lead_id"], d["field"],
        message.photo[-1].file_id, message.from_user.id,
    )


@edit_router.message(Ed.value)
async def edit_value(message: Message, state: FSMContext):
    d = await state.get_data()
    field = d["field"]
    val = (message.text or "").strip()
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
    await _finish_edit(message, state, d["lead_id"], field, val, message.from_user.id)


# --- редактирование контактов ------------------------------------------------

@edit_router.callback_query(F.data.startswith("ecm:"))
async def edit_contacts_menu(cb: CallbackQuery, state: FSMContext, worker, is_admin: bool):
    lead_id = int(cb.data.split(":")[1])
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
    lead_id = int(cb.data.split(":")[1])
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
    await state.update_data(ctype=config.CONTACT_TYPES[int(key)][0], ctype_other=None)
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
    contact_id = int(cb.data.split(":")[1])
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


@edit_router.message(Ed.c_value)
async def contact_value(message: Message, state: FSMContext):
    d = await state.get_data()
    val = (message.text or "").strip()
    if not val:
        await message.answer("Введите значение текстом:", reply_markup=kb.cancel_kb())
        return
    err = contact_error(d["ctype"], val)
    if err:
        await message.answer(err, reply_markup=kb.cancel_kb())
        return
    lead_id = d["lead_id"]
    try:
        async with Session() as s, s.begin():
            lead = await s.get(Lead, lead_id)
            region = config.COUNTRY_ISO.get(lead.country)
            norm = normalize_phone(val, region) if d["ctype"] == "phone" else None
            if d.get("contact_id"):
                contact = await s.get(Contact, d["contact_id"])
                old = contact.value
                contact.value = val
                contact.value_norm = norm
                log_event(s, lead_id, "contact_edit", message.from_user.id,
                          contact_label(contact), old, val)
            else:
                contact = Contact(
                    lead_id=lead_id, ctype=d["ctype"], ctype_other=d.get("ctype_other"),
                    value=val, value_norm=norm,
                )
                s.add(contact)
                log_event(s, lead_id, "contact_add", message.from_user.id,
                          contact_label(contact), None, val)
    except IntegrityError as e:
        log.warning("dup on contact lead=%s: %s", lead_id, e.orig)
        await state.clear()
        await message.answer(f"{dup_message(e)} Старое значение осталось.")
        return
    await state.clear()
    async with Session() as s:
        contacts = await get_contacts(s, lead_id)
    await message.answer("Готово.", reply_markup=kb.contacts_menu_kb(lead_id, contacts))


@edit_router.callback_query(F.data.startswith("ecd:"))
async def contact_delete(cb: CallbackQuery, worker, is_admin: bool):
    contact_id = int(cb.data.split(":")[1])
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
        contact.deleted_at = func.now()
        log_event(s, contact.lead_id, "contact_delete", cb.from_user.id,
                  contact_label(contact), contact.value, None)
        lead_id = contact.lead_id
    async with Session() as s:
        contacts = await get_contacts(s, lead_id)
    await cb.answer("Удалено")
    await cb.message.edit_reply_markup(
        reply_markup=kb.contacts_menu_kb(lead_id, contacts)
    )


async def edits_count(session, lead_id) -> int:
    return await session.scalar(
        select(func.count()).select_from(LeadEvent).where(
            LeadEvent.lead_id == lead_id,
            LeadEvent.event.in_(
                ["field_edit", "contact_add", "contact_edit", "contact_delete"]
            ),
        )
    )
