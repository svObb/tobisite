import asyncio
import csv
import logging
import os
import tempfile

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

import config
import keyboards as kb
from handlers_worker import (
    edits_count, esc, fmt_lead, get_contacts, is_url, list_text, local,
)
from models import (
    Contact, Lead, LeadEvent, Session, Worker, day_start, log_event,
)

log = logging.getLogger(__name__)
router = Router()

DATE_PRESETS = [("Сегодня", 0), ("7 дней", 6), ("30 дней", 29)]
ACTIVE = (Lead.cancelled_at.is_(None), Lead.deleted_at.is_(None))


class Adm(StatesGroup):
    search = State()
    note = State()
    draft = State()
    limit = State()
    broadcast = State()


@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Админ-панель", reply_markup=kb.admin_menu())


# --- статистика --------------------------------------------------------------

@router.message(F.text == kb.BTN_A_STATS)
async def stats(message: Message):
    async with Session() as s:
        total = await s.scalar(select(func.count()).select_from(Lead).where(*ACTIVE))
        today = await s.scalar(
            select(func.count()).select_from(Lead)
            .where(*ACTIVE, Lead.created_at >= day_start())
        )
        week = await s.scalar(
            select(func.count()).select_from(Lead)
            .where(*ACTIVE, Lead.created_at >= day_start(6))
        )
        rows = await s.execute(
            select(Worker.name, func.count(Lead.id))
            .join(Lead, Lead.worker_id == Worker.id)
            .where(*ACTIVE)
            .group_by(Worker.name)
            .order_by(func.count(Lead.id).desc())
        )
    lines = [f"Всего: {total}", f"За сегодня: {today}", f"За 7 дней: {week}", "", "По работникам:"]
    lines += [f"  {esc(name)}: {cnt}" for name, cnt in rows] or ["  —"]
    await message.answer("\n".join(lines))


# --- фильтры и список --------------------------------------------------------

def flt_conditions(flt: dict):
    conds = list(ACTIVE)
    if flt.get("worker_id"):
        conds.append(Lead.worker_id == flt["worker_id"])
    if flt.get("country"):
        conds.append(Lead.country == flt["country"])
    if flt.get("niche"):
        conds.append(Lead.niche == flt["niche"])
    if flt.get("status"):
        conds.append(Lead.status == flt["status"])
    if flt.get("days") is not None:
        conds.append(Lead.created_at >= day_start(flt["days"]))
    return conds


def flt_text(flt: dict) -> str:
    return (
        "<b>Фильтры</b>\n"
        f"Работник: {esc(flt.get('worker_name') or 'любой')}\n"
        f"Страна: {esc(flt.get('country') or 'любая')}\n"
        f"Ниша: {esc(flt.get('niche') or 'любая')}\n"
        f"Статус: {esc(config.STATUS_LABELS.get(flt.get('status'), 'любой'))}\n"
        f"Дата: {esc(flt.get('days_label') or 'любая')}"
    )


async def get_flt(state: FSMContext) -> dict:
    return (await state.get_data()).get("flt", {})


@router.message(F.text == kb.BTN_A_ALL)
async def all_leads(message: Message, state: FSMContext):
    flt = await get_flt(state)
    await message.answer(flt_text(flt), reply_markup=kb.filters_kb())


@router.callback_query(F.data.startswith("afk:"))
async def filter_menu(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    await cb.answer()
    flt = await get_flt(state)
    if key == "reset":
        await state.update_data(flt={})
        await cb.message.edit_text(flt_text({}), reply_markup=kb.filters_kb())
        return
    if key == "back":
        await cb.message.edit_text(flt_text(flt), reply_markup=kb.filters_kb())
        return
    if key == "worker":
        async with Session() as s:
            workers = list(await s.scalars(
                select(Worker).where(Worker.deleted_at.is_(None)).order_by(Worker.name)
            ))
        await state.update_data(opts=[w.id for w in workers],
                                opt_names=[w.name for w in workers])
        await cb.message.edit_text(
            "Работник:", reply_markup=kb.options_kb([w.name for w in workers], "afw")
        )
        return
    if key in ("country", "niche"):
        col = Lead.country if key == "country" else Lead.niche
        async with Session() as s:
            values = [v for v in await s.scalars(
                select(col).where(*ACTIVE).distinct().order_by(col)
            )]
        await state.update_data(opts=values)
        pfx = "afc" if key == "country" else "afn"
        await cb.message.edit_text(
            "Страна:" if key == "country" else "Ниша:",
            reply_markup=kb.options_kb(values, pfx),
        )
        return
    if key == "status":
        await cb.message.edit_text(
            "Статус:",
            reply_markup=kb.options_kb([lbl for _, lbl in config.STATUSES], "afs"),
        )
        return
    if key == "date":
        await cb.message.edit_text(
            "Дата:", reply_markup=kb.options_kb([lbl for lbl, _ in DATE_PRESETS], "afd")
        )


async def _set_filter(cb: CallbackQuery, state: FSMContext, **values):
    flt = await get_flt(state)
    flt.update(values)
    await state.update_data(flt=flt)
    await cb.answer()
    await cb.message.edit_text(flt_text(flt), reply_markup=kb.filters_kb())


@router.callback_query(F.data.startswith("afw:"))
async def filter_worker(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    if key == "any":
        await _set_filter(cb, state, worker_id=None, worker_name=None)
        return
    d = await state.get_data()
    i = int(key)
    await _set_filter(cb, state, worker_id=d["opts"][i], worker_name=d["opt_names"][i])


@router.callback_query(F.data.startswith("afc:"))
async def filter_country(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    if key == "any":
        await _set_filter(cb, state, country=None)
        return
    d = await state.get_data()
    await _set_filter(cb, state, country=d["opts"][int(key)])


@router.callback_query(F.data.startswith("afn:"))
async def filter_niche(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    if key == "any":
        await _set_filter(cb, state, niche=None)
        return
    d = await state.get_data()
    await _set_filter(cb, state, niche=d["opts"][int(key)])


@router.callback_query(F.data.startswith("afs:"))
async def filter_status(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    if key == "any":
        await _set_filter(cb, state, status=None)
        return
    await _set_filter(cb, state, status=config.STATUSES[int(key)][0])


@router.callback_query(F.data.startswith("afd:"))
async def filter_date(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    if key == "any":
        await _set_filter(cb, state, days=None, days_label=None)
        return
    label, days = DATE_PRESETS[int(key)]
    await _set_filter(cb, state, days=days, days_label=label)


async def page(session, conds, offset):
    total = await session.scalar(select(func.count()).select_from(Lead).where(*conds))
    rows = await session.scalars(
        select(Lead).where(*conds).order_by(Lead.id.desc())
        .offset(offset).limit(config.PAGE_SIZE)
    )
    return list(rows), total


@router.callback_query(F.data.startswith("alp:"))
async def all_page(cb: CallbackQuery, state: FSMContext):
    offset = int(cb.data.split(":")[1])
    conds = flt_conditions(await get_flt(state))
    async with Session() as s:
        leads, total = await page(s, conds, offset)
    await cb.answer()
    await cb.message.edit_text(
        list_text(leads, total, offset),
        reply_markup=kb.leads_list_kb(leads, "alp", "acd", offset, total),
    )


# --- карточка ----------------------------------------------------------------

async def show_card(target: Message, lead_id: int):
    async with Session() as s:
        lead = await s.get(Lead, lead_id)
        if not lead:
            await target.answer("Запись не найдена.")
            return
        contacts = await get_contacts(s, lead_id)
        author = await s.get(Worker, lead.worker_id)
        edits = await edits_count(s, lead_id)
    if lead.screenshot_file_id:
        await target.answer_photo(lead.screenshot_file_id)
    markup = (
        kb.cancelled_card_kb(lead_id) if lead.cancelled_at else kb.admin_card_kb(lead_id)
    )
    await target.answer(
        fmt_lead(lead, contacts, author=author, edits=edits, admin=True),
        reply_markup=markup,
    )


@router.callback_query(F.data.startswith("acd:"))
async def admin_card(cb: CallbackQuery):
    await cb.answer()
    await show_card(cb.message, int(cb.data.split(":")[1]))


@router.callback_query(F.data.startswith("sts:"))
async def status_menu(cb: CallbackQuery):
    lead_id = int(cb.data.split(":")[1])
    await cb.answer()
    await cb.message.answer("Новый статус:", reply_markup=kb.statuses_kb(lead_id))


@router.callback_query(F.data.startswith("stv:"))
async def status_set(cb: CallbackQuery):
    _, lead_id, new = cb.data.split(":")
    lead_id = int(lead_id)
    async with Session() as s, s.begin():
        lead = await s.get(Lead, lead_id)
        if not lead:
            await cb.answer("Запись не найдена", show_alert=True)
            return
        old = lead.status
        lead.status = new
        log_event(s, lead_id, "status_change", cb.from_user.id, "status", old, new)
    log.info("lead %s status %s -> %s", lead_id, old, new)
    await cb.answer("Статус изменён")
    await cb.message.answer(
        f"#{lead_id}: {config.STATUS_LABELS.get(old, old)} → "
        f"{config.STATUS_LABELS.get(new, new)}"
    )


@router.callback_query(F.data.startswith("hst:"))
async def history(cb: CallbackQuery):
    lead_id = int(cb.data.split(":")[1])
    async with Session() as s:
        events = list(await s.scalars(
            select(LeadEvent).where(LeadEvent.lead_id == lead_id)
            .order_by(LeadEvent.id.desc()).limit(30)
        ))
    await cb.answer()
    if not events:
        await cb.message.answer("Изменений не было.")
        return
    lines = [f"<b>История #{lead_id}</b>"]
    for e in events:
        part = f"{local(e.created_at)} — {esc(e.event)}"
        if e.field:
            part += f" ({esc(e.field)})"
        if e.old_value or e.new_value:
            part += f": {esc(e.old_value)} → {esc(e.new_value)}"
        lines.append(part + f" — tg {e.actor_tg_id}")
    await cb.message.answer("\n".join(lines))


@router.callback_query(F.data.startswith("drf:"))
async def draft_ask(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.draft)
    await state.update_data(lead_id=int(cb.data.split(":")[1]))
    await cb.answer()
    await cb.message.answer("Ссылка на черновик:", reply_markup=kb.cancel_kb())


@router.message(Adm.draft)
async def draft_save(message: Message, state: FSMContext):
    val = (message.text or "").strip()
    if not is_url(val):
        await message.answer("Нужна ссылка http(s)://…", reply_markup=kb.cancel_kb())
        return
    d = await state.get_data()
    async with Session() as s, s.begin():
        lead = await s.get(Lead, d["lead_id"])
        lead.draft_url = val
    await state.clear()
    await message.answer("Ссылка сохранена.")


@router.callback_query(F.data.startswith("anz:"))
async def note_ask(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.note)
    await state.update_data(lead_id=int(cb.data.split(":")[1]))
    await cb.answer()
    await cb.message.answer("Текст заметки:", reply_markup=kb.cancel_kb())


@router.message(Adm.note)
async def note_save(message: Message, state: FSMContext):
    d = await state.get_data()
    async with Session() as s, s.begin():
        lead = await s.get(Lead, d["lead_id"])
        lead.admin_note = (message.text or "").strip() or None
    await state.clear()
    await message.answer("Заметка сохранена.")


@router.callback_query(F.data.startswith("del:"))
async def delete_lead(cb: CallbackQuery):
    lead_id = int(cb.data.split(":")[1])
    async with Session() as s, s.begin():
        lead = await s.get(Lead, lead_id)
        if not lead or lead.deleted_at:
            await cb.answer("Запись недоступна", show_alert=True)
            return
        now = func.now()
        lead.deleted_at = now
        await s.execute(
            update(Contact).where(Contact.lead_id == lead_id).values(deleted_at=now)
        )
        log_event(s, lead_id, "delete", cb.from_user.id)
    log.info("lead %s soft-deleted", lead_id)
    await cb.answer("Удалено")
    await cb.message.answer(f"Запись #{lead_id} удалена (мягко).")


# --- поиск -------------------------------------------------------------------

@router.message(F.text == kb.BTN_A_SEARCH)
async def search_ask(message: Message, state: FSMContext):
    await state.set_state(Adm.search)
    await message.answer("Название компании:", reply_markup=kb.cancel_kb())


@router.message(Adm.search)
async def search_run(message: Message, state: FSMContext):
    q = (message.text or "").strip()
    if not q:
        await message.answer("Введите текст:", reply_markup=kb.cancel_kb())
        return
    await state.clear()
    await state.update_data(query=q)
    await _search_page(message, q, 0)


async def _search_page(target: Message, q: str, offset: int):
    conds = [*ACTIVE, Lead.name.ilike(f"%{q}%")]
    async with Session() as s:
        leads, total = await page(s, conds, offset)
    await target.answer(
        list_text(leads, total, offset),
        reply_markup=kb.leads_list_kb(leads, "spg", "acd", offset, total),
    )


@router.callback_query(F.data.startswith("spg:"))
async def search_page(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await cb.answer()
    await _search_page(cb.message, d.get("query", ""), int(cb.data.split(":")[1]))


# --- отменённые --------------------------------------------------------------

@router.message(F.text == kb.BTN_A_CANCELLED)
async def cancelled_list(message: Message):
    await _cancelled_page(message, 0, edit=False)


async def _cancelled_page(target: Message, offset: int, edit: bool):
    conds = [Lead.cancelled_at.is_not(None), Lead.deleted_at.is_(None)]
    async with Session() as s:
        leads, total = await page(s, conds, offset)
    markup = kb.leads_list_kb(leads, "cxp", "acd", offset, total)
    text = list_text(leads, total, offset)
    await (target.edit_text(text, reply_markup=markup) if edit
           else target.answer(text, reply_markup=markup))


@router.callback_query(F.data.startswith("cxp:"))
async def cancelled_page(cb: CallbackQuery):
    await cb.answer()
    await _cancelled_page(cb.message, int(cb.data.split(":")[1]), edit=True)


@router.callback_query(F.data.startswith("rst:"))
async def restore_lead(cb: CallbackQuery):
    lead_id = int(cb.data.split(":")[1])
    try:
        async with Session() as s, s.begin():
            lead = await s.get(Lead, lead_id)
            if not lead or not lead.cancelled_at:
                await cb.answer("Запись не отменена", show_alert=True)
                return
            lead.cancelled_at = None
            lead.cancelled_by = None
            await s.execute(
                update(Contact).where(Contact.lead_id == lead_id)
                .values(lead_cancelled_at=None)
            )
            log_event(s, lead_id, "restore", cb.from_user.id)
    except IntegrityError as e:
        log.warning("restore conflict lead=%s: %s", lead_id, e.orig)
        await cb.answer()
        await cb.message.answer(
            "Нельзя восстановить: в базе уже есть активный дубликат."
        )
        return
    log.info("lead %s restored", lead_id)
    await cb.answer("Восстановлено")
    await cb.message.answer(f"Запись #{lead_id} восстановлена.")


# --- работники ---------------------------------------------------------------

@router.message(F.text == kb.BTN_A_WORKERS)
async def workers_list(message: Message):
    await _workers_page(message, 0, edit=False)


async def _workers_page(target: Message, offset: int, edit: bool):
    async with Session() as s:
        total = await s.scalar(
            select(func.count()).select_from(Worker).where(Worker.deleted_at.is_(None))
        )
        workers = list(await s.scalars(
            select(Worker).where(Worker.deleted_at.is_(None))
            .order_by(Worker.id).offset(offset).limit(config.PAGE_SIZE)
        ))
        counts = dict(await s.execute(
            select(Lead.worker_id, func.count(Lead.id)).where(*ACTIVE)
            .group_by(Lead.worker_id)
        ))
    lines = [f"Работников: {total}"] + [
        f"{esc(w.name)} — {counts.get(w.id, 0)}"
        f"{'' if w.is_active else ' (отключён)'}"
        for w in workers
    ]
    markup = kb.workers_kb(workers, offset, total)
    text = "\n".join(lines) if workers else "Пока никого."
    await (target.edit_text(text, reply_markup=markup) if edit
           else target.answer(text, reply_markup=markup))


@router.callback_query(F.data.startswith("wlp:"))
async def workers_page(cb: CallbackQuery):
    await cb.answer()
    await _workers_page(cb.message, int(cb.data.split(":")[1]), edit=True)


@router.callback_query(F.data.startswith("wcd:"))
async def worker_card(cb: CallbackQuery):
    wid = int(cb.data.split(":")[1])
    async with Session() as s:
        worker = await s.get(Worker, wid)
        if not worker:
            await cb.answer("Не найден", show_alert=True)
            return
        total = await s.scalar(
            select(func.count()).select_from(Lead)
            .where(*ACTIVE, Lead.worker_id == wid)
        )
        today = await s.scalar(
            select(func.count()).select_from(Lead)
            .where(*ACTIVE, Lead.worker_id == wid, Lead.created_at >= day_start())
        )
    limit = worker.daily_limit if worker.daily_limit is not None else config.DEFAULT_DAILY_LIMIT
    await cb.answer()
    await cb.message.answer(
        f"<b>{esc(worker.name)}</b>\ntg id: {worker.tg_id}\n"
        f"Добавлено: {total}, сегодня: {today}\n"
        f"Лимит: {limit}{'' if worker.daily_limit is not None else ' (по умолчанию)'}\n"
        f"Статус: {'активен' if worker.is_active else 'отключён'}\n"
        f'<a href="tg://user?id={worker.tg_id}">Открыть в Telegram</a>',
        reply_markup=kb.worker_card_kb(worker),
    )


@router.callback_query(F.data.startswith("wlm:"))
async def worker_limit_ask(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.limit)
    await state.update_data(worker_id=int(cb.data.split(":")[1]))
    await cb.answer()
    await cb.message.answer(
        "Новый дневной лимит (число, или 0 — вернуть значение по умолчанию):",
        reply_markup=kb.cancel_kb(),
    )


@router.message(Adm.limit)
async def worker_limit_save(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Нужно число:", reply_markup=kb.cancel_kb())
        return
    value = int(raw)
    d = await state.get_data()
    async with Session() as s, s.begin():
        worker = await s.get(Worker, d["worker_id"])
        worker.daily_limit = value or None
    await state.clear()
    await message.answer(
        f"Лимит: {value}" if value else "Лимит сброшен на значение по умолчанию."
    )


@router.callback_query(F.data.startswith("wof:"))
async def worker_toggle(cb: CallbackQuery):
    wid = int(cb.data.split(":")[1])
    async with Session() as s, s.begin():
        worker = await s.get(Worker, wid)
        worker.is_active = not worker.is_active
        active = worker.is_active
    await cb.answer("Готово")
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer("Работник включён." if active else "Работник отключён.")


# --- рассылка ----------------------------------------------------------------

@router.message(F.text == kb.BTN_A_BROADCAST)
async def broadcast_ask(message: Message, state: FSMContext):
    await state.set_state(Adm.broadcast)
    await message.answer("Текст сообщения всем работникам:", reply_markup=kb.cancel_kb())


@router.message(Adm.broadcast)
async def broadcast_send(message: Message, state: FSMContext):
    text = message.text or ""
    await state.clear()
    async with Session() as s:
        targets = list(await s.scalars(
            select(Worker.tg_id).where(Worker.is_active.is_(True),
                                       Worker.deleted_at.is_(None))
        ))
    sent = 0
    for tg_id in targets:
        try:
            await message.bot.send_message(tg_id, text)
            sent += 1
        except Exception as e:
            log.warning("broadcast failed for %s: %s", tg_id, e)
        await asyncio.sleep(0.05)
    await message.answer(f"Отправлено: {sent} из {len(targets)}")


# --- CSV ---------------------------------------------------------------------

CSV_HEADER = [
    "id", "дата", "работник", "название", "сайт", "источник", "страна", "город",
    "язык", "ниша", "рейтинг", "статус", "возможный_дубликат", "где_нашли",
    "заметка", "контакты",
]


@router.message(F.text == kb.BTN_A_CSV)
async def export_csv(message: Message):
    fd, path = tempfile.mkstemp(suffix=".csv", prefix="qdif_")
    os.close(fd)
    rows_written = 0
    try:
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerow(CSV_HEADER)
            last_id = 0
            while True:
                async with Session() as s:
                    leads = list(await s.scalars(
                        select(Lead).where(*ACTIVE, Lead.id > last_id)
                        .order_by(Lead.id).limit(500)
                    ))
                    if not leads:
                        break
                    ids = [l.id for l in leads]
                    names = dict(await s.execute(
                        select(Worker.id, Worker.name)
                        .where(Worker.id.in_([l.worker_id for l in leads]))
                    ))
                    contacts = list(await s.scalars(
                        select(Contact).where(
                            Contact.lead_id.in_(ids), Contact.deleted_at.is_(None)
                        ).order_by(Contact.id)
                    ))
                by_lead = {}
                for c in contacts:
                    label = c.ctype_other if c.ctype == "other" and c.ctype_other else \
                        config.CONTACT_TYPE_LABELS.get(c.ctype, c.ctype)
                    by_lead.setdefault(c.lead_id, []).append(f"{label}: {c.value}")
                for l in leads:
                    writer.writerow([
                        l.id, local(l.created_at), names.get(l.worker_id, ""), l.name,
                        l.website_url or "", l.source_url, l.country, l.city,
                        l.language, l.niche, l.google_rating or "",
                        config.STATUS_LABELS.get(l.status, l.status),
                        "да" if l.possible_duplicate else "",
                        l.found_via, l.note or "",
                        " | ".join(by_lead.get(l.id, [])),
                    ])
                    rows_written += 1
                last_id = ids[-1]
        await message.answer_document(
            FSInputFile(path, filename="companies.csv"),
            caption=f"Записей: {rows_written}",
        )
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
